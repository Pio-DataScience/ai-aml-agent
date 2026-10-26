"""Offline behavioral coverage for the local DWH SQL orchestration."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from services.aml_builder.web.domain.dwh_scenario.contracts import ScenarioSqlRequest
from services.aml_builder.web.services.dwh_scenario.generator import (
    LocalScenarioSqlGenerator,
)
from services.aml_builder.web.services.dwh_scenario.retrieval import (
    build_readiness_checks,
)
from services.aml_builder.web.services.settings import Settings


SQL = "SELECT CUS_NUM FROM BI_DWH.TX"


def make_config(**updates: object) -> Settings:
    """Build isolated local-generator settings without environment access."""
    values: dict[str, object] = {
        "ORACLE_DSN": "unused",
        "ORACLE_USER": "unused",
        "ORACLE_PASSWORD": "unused",
        "_env_file": None,
        "AML_INST_CODE": "TEST",
        "DWH_SQL_ALLOWED_SCHEMAS": ("BI_DWH",),
        "DWH_SQL_ALLOWED_OBJECTS": ("BI_DWH.TX",),
        "DWH_SQL_MAX_CRITIQUE_ATTEMPTS": 2,
        "DWH_SQL_DEADLINE_SECONDS": 10,
    }
    values.update(updates)
    return Settings(**values)


def make_request() -> ScenarioSqlRequest:
    """Return the smallest valid Service A handoff contract."""
    return ScenarioSqlRequest(
        intent={
            "scenario_name": "Test scenario",
            "scenario_type": "TRANSACTION",
            "detection_logic": "Find relevant transactions",
            "ready_for_handoff": True,
        }
    )


class ScriptedRetriever:
    """Return fixed retrieval records or fail deterministically."""

    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.calls = 0

    def search(
        self, _query: str, _institution: str, _limit: int
    ) -> list[dict[str, str]]:
        self.calls += 1
        if self.failure:
            raise self.failure
        return []


class ScriptedModel:
    """Serve deterministic generator and critic responses."""

    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.inputs: list[object] = []

    def invoke(self, messages: object) -> object:
        self.inputs.append(messages)
        if not self.responses:
            raise RuntimeError("No scripted model response")
        return self.responses.pop(0)


class ScriptedRepository:
    """Record candidate SQL and return bounded execution evidence."""

    def __init__(self, *, success: bool = True, row_count: int = 1) -> None:
        self.success = success
        self.row_count = row_count
        self.sql: list[str] = []

    def execute_candidate(self, sql: str, **_kwargs: object) -> SimpleNamespace:
        self.sql.append(sql)
        return SimpleNamespace(
            sql=sql,
            success=self.success,
            columns=("CUS_NUM",),
            row_count=self.row_count,
            truncated=False,
            error=None if self.success else "candidate execution failed",
        )


def models(generator_responses: list[object], critic_responses: list[object]):
    """Create a role-aware fake model factory."""
    generator = ScriptedModel(generator_responses)
    critic = ScriptedModel(critic_responses)

    def factory(*, role: str) -> ScriptedModel:
        return generator if role == "generator" else critic

    return factory, generator, critic


def make_generator(
    factory,
    repository: ScriptedRepository | None = None,
    *,
    config: Settings | None = None,
    experience: ScriptedRetriever | None = None,
) -> LocalScenarioSqlGenerator:
    """Wire deterministic adapters into the injected implementation."""
    return LocalScenarioSqlGenerator(
        llm_factory=factory,
        repository=repository or ScriptedRepository(),
        experience_retriever=experience or ScriptedRetriever(),
        table_retriever=ScriptedRetriever(),
        column_retriever=ScriptedRetriever(),
        config=config or make_config(),
    )


@pytest.mark.asyncio
async def test_success_returns_only_executed_critique_approved_sql() -> None:
    """Successful SQL is passed unchanged to execution and returned after approval."""
    factory, _, _ = models([SQL], ['{"approved": true, "feedback": "ok"}'])
    repository = ScriptedRepository()
    result = await make_generator(factory, repository).generate(make_request())
    assert result.approved is True
    assert result.sql == SQL
    assert repository.sql == [SQL]
    assert result.execution.row_count == 1
    assert result.metrics["critique_attempts"] == 1


@pytest.mark.asyncio
async def test_rejected_critique_exhausts_with_no_approved_sql() -> None:
    """Repeated critique rejection ends explicitly and withholds candidate SQL."""
    factory, generator, _ = models(
        [SQL, SQL],
        ['{"approved": false, "feedback": "revise"}'] * 2,
    )
    result = await make_generator(factory).generate(make_request())
    assert result.approved is False
    assert result.sql is None
    assert result.error == "Critique attempts exhausted"
    assert "REPAIR FEEDBACK: revise" in generator.inputs[1][1]["content"]


@pytest.mark.asyncio
async def test_unsafe_sql_is_never_executed_or_returned() -> None:
    """AST policy rejection prevents repository execution even if critic approves."""
    factory, _, _ = models(
        ["DELETE FROM BI_DWH.TX"], ['{"approved": true, "feedback": "safe"}']
    )
    repository = ScriptedRepository()
    result = await make_generator(factory, repository).generate(make_request())
    assert result.approved is False
    assert result.sql is None
    assert repository.sql == []


@pytest.mark.asyncio
async def test_zero_rows_are_a_successful_execution() -> None:
    """An empty candidate result is valid evidence and may be critique-approved."""
    factory, _, critic = models([SQL], ['{"approved": true, "feedback": "ok"}'])
    repository = ScriptedRepository(row_count=0)
    result = await make_generator(factory, repository).generate(make_request())
    assert result.approved is True
    assert result.sql == SQL
    assert result.execution.row_count == 0
    critique_payload = critic.inputs[0][1]["content"]
    assert '"row_count": 0' in critique_payload
    assert '"rows"' not in critique_payload


@pytest.mark.asyncio
async def test_retrieval_failure_is_explicit_and_fail_closed() -> None:
    """A retriever error produces a failure result before any provider invocation."""
    factory, generator_model, _ = models([SQL], ['{"approved": true}'])
    result = await make_generator(
        factory, experience=ScriptedRetriever(ConnectionError())
    ).generate(make_request())
    assert result.approved is False
    assert result.sql is None
    assert result.error == "Generation failed: ConnectionError"
    assert generator_model.inputs == []


@pytest.mark.asyncio
async def test_provider_failure_is_explicit_and_fail_closed() -> None:
    """Role-provider construction failure cannot leak an unapproved candidate."""

    def factory(*, role: str):
        raise ValueError(f"missing {role} config")

    result = await make_generator(factory).generate(make_request())
    assert result.approved is False
    assert result.sql is None
    assert result.error == "Generation failed: ValueError"


@pytest.mark.asyncio
async def test_context_budget_exhaustion_is_explicit() -> None:
    """Oversized retrieved context stops before any LLM call."""
    factory, generator_model, _ = models([SQL], ['{"approved": true}'])
    result = await make_generator(
        factory,
        experience=ScriptedRetriever(),
        config=make_config(DWH_SQL_CONTEXT_BUDGET=1),
    ).generate(make_request())
    assert result.approved is False
    assert result.sql is None
    assert result.error == "Context budget exhausted"
    assert generator_model.inputs == []


@pytest.mark.asyncio
async def test_tool_call_budget_exhaustion_is_explicit() -> None:
    """A hard tool-call cap prevents SQL execution when no budget remains."""
    factory, generator_model, _ = models([SQL], ['{"approved": true}'])
    repository = ScriptedRepository()
    result = await make_generator(
        factory,
        repository,
        config=make_config(DWH_SQL_MAX_TOOL_CALLS=4),
    ).generate(make_request())
    assert result.approved is False
    assert result.sql is None
    assert result.error == "Tool-call budget exhausted"
    assert len(generator_model.inputs) == 1
    assert repository.sql == []


@pytest.mark.asyncio
async def test_oracle_statement_budget_exhaustion_is_explicit() -> None:
    """The local pipeline reports the repository budget instead of retrying forever."""
    factory, _, _ = models([SQL], ['{"approved": true}'])
    repository = ScriptedRepository(success=False)

    def budget_exhausted(_sql: str, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            sql=SQL,
            success=False,
            columns=(),
            row_count=0,
            truncated=False,
            error="DWH Oracle statement budget exceeded",
        )

    repository.execute_candidate = budget_exhausted
    result = await make_generator(factory, repository).generate(make_request())
    assert result.approved is False
    assert result.sql is None
    assert result.error == "Oracle statement budget exhausted"


def test_legacy_http_readiness_does_not_initialize_optional_retrievers() -> None:
    """B4 readiness can be built while Chroma and Neo4j are not installed/configured."""
    config = make_config(DWH_SQL_CHROMA_PERSIST_DIR=None, DWH_SQL_NEO4J_ENABLED=False)
    checks, graph = build_readiness_checks(config, oracle_check={"status": "ok"})
    assert checks["oracle"] == {"status": "ok"}
    assert checks["chroma"]["experience_templates"]()["status"] == "empty"
    assert checks["chroma"]["schema_tables"]()["status"] == "down"
    assert graph.readiness() == {"status": "disabled"}
