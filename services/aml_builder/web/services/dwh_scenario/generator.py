"""Local DWH SQL generator orchestration and bounded request state."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Callable

from services.aml_builder.web.domain.dwh_scenario.contracts import (
    ExecutionEvidence,
    ScenarioSqlRequest,
    ScenarioSqlResult,
)
from services.aml_builder.web.domain.dwh_scenario.sql_policy import (
    SqlPolicyError,
    validate_oracle_read,
)
from services.aml_builder.web.domain.dwh_scenario.validation import (
    validate_generated_sql,
)
from services.aml_builder.web.services.dwh_scenario.critique import critique_candidate
from services.aml_builder.web.services.dwh_scenario.context_assembly import (
    assemble_context,
)
from services.aml_builder.web.services.dwh_scenario.prompt_loader import load_prompt
from services.aml_builder.web.adapters.dwh_oracle_repo import OracleRequestBudget
from services.aml_builder.web.services.settings import Settings, settings
from services.aml_builder.web.services.dwh_scenario.tools.search_experience import (
    search_experience,
)
from services.aml_builder.web.services.dwh_scenario.tools.search_schema import (
    search_schema,
)
from services.aml_builder.web.services.dwh_scenario.tools.search_columns import (
    search_columns,
)
from services.aml_builder.web.services.dwh_scenario.tools.execute_sql import execute_sql


@dataclass
class RequestCounters:
    """Counters and fixed deadline for one generation request."""

    tool_calls: int = 0
    llm_calls: int = 0
    critique_attempts: int = 0
    oracle_statements: int = 0
    tokens: int = 0


class LocalScenarioSqlGenerator:
    """Generate, validate, execute and critique SQL through injected adapters."""

    def __init__(
        self,
        *,
        llm_factory: Callable[..., Any],
        repository: Any,
        experience_retriever: Any,
        table_retriever: Any,
        column_retriever: Any,
        reranker: Any = None,
        config: Settings = settings,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Store injected dependencies without creating clients or compiling graphs."""
        self._llm_factory = llm_factory
        self._repository = repository
        self._experience = experience_retriever
        self._tables = table_retriever
        self._columns = column_retriever
        self._reranker = reranker
        self._config = config
        self._clock = clock

    async def generate(self, request: ScenarioSqlRequest) -> ScenarioSqlResult:
        """Run bounded generation; only executed, policy-safe, critiqued SQL approves.

        Args:
            request: Validated AML intent and confirmed filter values.

        Returns:
            Typed result with explicit failure/evidence state and counters.
        """
        started = self._clock()
        deadline = started + self._config.DWH_SQL_DEADLINE_SECONDS
        counters = RequestCounters()
        try:
            institution_id = str(self._config.AML_INST_CODE or "")
            if not institution_id:
                return ScenarioSqlResult(error="Missing institution scope")
            context: dict[str, Any] = {}
            for name, action, retriever, limit in (
                (
                    "experience",
                    search_experience,
                    self._experience,
                    self._config.DWH_SQL_EXPERIENCE_K,
                ),
                (
                    "tables",
                    search_schema,
                    self._tables,
                    self._config.DWH_SQL_SCHEMA_TABLES_K,
                ),
                (
                    "columns",
                    search_columns,
                    self._columns,
                    self._config.DWH_SQL_SCHEMA_COLUMNS_K,
                ),
            ):
                self._reserve(counters, deadline)
                context[name] = action(
                    retriever, request.intent.detection_logic, institution_id, limit
                )
                counters.tool_calls += 1
            if self._reranker is not None:
                for name in ("tables", "columns"):
                    self._reserve(counters, deadline)
                    context[name] = self._reranker.rerank(
                        context[name], len(context[name])
                    )
                    counters.tool_calls += 1
            try:
                encoded = assemble_context(
                    request,
                    experience=context["experience"],
                    tables=context["tables"],
                    columns=context["columns"],
                    budget=self._config.DWH_SQL_CONTEXT_BUDGET,
                )
            except ValueError as exc:
                if str(exc) == "Context budget exhausted":
                    return self._failure(str(exc), counters)
                raise

            generator = self._llm_factory(role="generator")
            critic = self._llm_factory(role="critic")
            repair = ""
            last_sql: str | None = None
            findings: list[str] = []
            execution = ExecutionEvidence(error="Candidate was not executed")
            oracle_budget = OracleRequestBudget(
                max_statements=self._config.DWH_SQL_MAX_ORACLE_STATEMENTS,
                deadline_seconds=max(0.001, deadline - self._clock()),
            )
            for _ in range(self._config.DWH_SQL_MAX_CRITIQUE_ATTEMPTS):
                self._reserve(counters, deadline)
                counters.llm_calls += 1
                counters.tool_calls += 1
                response = await asyncio.wait_for(
                    asyncio.to_thread(
                        generator.invoke,
                        [
                            {
                                "role": "system",
                                "content": load_prompt("generator_v1.md"),
                            },
                            {
                                "role": "user",
                                "content": encoded + "\nREPAIR FEEDBACK: " + repair,
                            },
                        ],
                    ),
                    timeout=max(0.001, deadline - self._clock()),
                )
                counters.tokens += _usage_tokens(response)
                last_sql = _response_text(response).strip()
                try:
                    validate_oracle_read(
                        last_sql,
                        allowed_schemas=tuple(self._config.DWH_SQL_ALLOWED_SCHEMAS),
                        allowed_objects=tuple(self._config.DWH_SQL_ALLOWED_OBJECTS),
                        required_projection="CUS_NUM",
                        confirmed_filters=[
                            item.model_dump(mode="json")
                            for item in request.confirmed_filters
                        ],
                    )
                    findings = validate_generated_sql(request, last_sql)
                except SqlPolicyError as exc:
                    findings = [str(exc)]
                self._reserve(counters, deadline)
                evidence = (
                    execute_sql(
                        self._repository,
                        last_sql,
                        budget=oracle_budget,
                        required_projection="CUS_NUM",
                        confirmed_filters=[
                            item.model_dump(mode="json")
                            for item in request.confirmed_filters
                        ],
                    )
                    if not findings
                    else None
                )
                if evidence is not None:
                    counters.oracle_statements += 1
                    counters.tool_calls += 1
                    if (
                        evidence.error
                        and "statement budget exceeded" in evidence.error.casefold()
                    ):
                        return self._failure(
                            "Oracle statement budget exhausted", counters
                        )
                if evidence is None:
                    execution = ExecutionEvidence(
                        error="Deterministic SQL validation failed"
                    )
                else:
                    execution = ExecutionEvidence(
                        columns=list(evidence.columns),
                        row_count=evidence.row_count,
                        truncated=evidence.truncated,
                        error=evidence.error if not evidence.success else None,
                    )
                self._reserve(counters, deadline)
                counters.critique_attempts += 1
                counters.llm_calls += 1
                counters.tool_calls += 1
                approved, feedback, critic_tokens = await asyncio.wait_for(
                    asyncio.to_thread(
                        critique_candidate, critic, last_sql, execution, findings
                    ),
                    timeout=max(0.001, deadline - self._clock()),
                )
                counters.tokens += critic_tokens
                if findings:
                    approved = False
                    repair = "; ".join(findings)
                elif evidence is None or not evidence.success:
                    approved = False
                    repair = execution.error or "Candidate execution failed"
                elif not approved:
                    repair = feedback or "Critic rejected candidate"
                if (
                    approved
                    and evidence is not None
                    and evidence.success
                    and not findings
                ):
                    return ScenarioSqlResult(
                        sql=evidence.sql,
                        approved=True,
                        execution=execution,
                        provider=self._config.DWH_SQL_GENERATOR_PROVIDER
                        or self._config.LLM_PROVIDER,
                        model=self._config.DWH_SQL_GENERATOR_MODEL
                        or self._config.LLM_MODEL,
                        validation_findings=[],
                        metrics=_metrics(counters, self._clock() - started),
                    )
            return ScenarioSqlResult(
                error="Critique attempts exhausted",
                validation_findings=findings,
                execution=execution,
                sql=None,
                metrics=_metrics(counters, self._clock() - started),
            )
        except asyncio.TimeoutError:
            return self._failure("Request wall-time budget exhausted", counters)
        except RuntimeError as exc:
            if str(exc) in {
                "Tool-call budget exhausted",
                "DWH Oracle statement budget exceeded",
            }:
                return self._failure(
                    str(exc).replace(
                        "DWH Oracle statement budget exceeded",
                        "Oracle statement budget exhausted",
                    ),
                    counters,
                )
            return self._failure(f"Generation failed: {type(exc).__name__}", counters)
        except TimeoutError:
            return self._failure("Request wall-time budget exhausted", counters)
        except Exception as exc:
            return self._failure(f"Generation failed: {type(exc).__name__}", counters)

    def _reserve(self, counters: RequestCounters, deadline: float) -> None:
        """Check wall-time and tool-call budgets before each external action."""
        if self._clock() >= deadline:
            raise TimeoutError("Request wall-time budget exhausted")
        if counters.tool_calls >= self._config.DWH_SQL_MAX_TOOL_CALLS:
            raise RuntimeError("Tool-call budget exhausted")

    @staticmethod
    def _failure(message: str, counters: RequestCounters) -> ScenarioSqlResult:
        """Build fail-closed result without exposing SQL, prompts or row values."""
        return ScenarioSqlResult(
            approved=False,
            sql=None,
            error=message,
            validation_findings=[{"code": "GENERATION_FAILED", "message": message}],
            provider=None,
            model=None,
            metrics=_metrics(counters, 0),
        )


def _response_text(response: Any) -> str:
    """Extract SQL text from common chat-model response shapes."""
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(item.get("text", "")) if isinstance(item, dict) else str(item)
            for item in content
        )
    return str(content)


def _metrics(
    counters: RequestCounters, elapsed_seconds: float
) -> dict[str, int | float]:
    """Serialize safe request counters without prompt or customer data."""
    return {
        "llm_calls": counters.llm_calls,
        "tool_calls": counters.tool_calls,
        "critique_attempts": counters.critique_attempts,
        "oracle_statements": counters.oracle_statements,
        "tokens": counters.tokens,
        "elapsed_seconds": round(elapsed_seconds, 3),
    }


def _usage_tokens(response: Any) -> int:
    """Read provider-reported tokens when present without guessing usage."""
    usage = getattr(response, "usage_metadata", {}) or {}
    if isinstance(usage, dict):
        return int(usage.get("total_tokens", 0))
    return 0
