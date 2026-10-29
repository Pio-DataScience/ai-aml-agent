"""HTTP SSE adapter implementing the DWH scenario generator contract."""

from __future__ import annotations

import json
import hashlib
import logging
import uuid
from typing import Any

import httpx

from services.aml_builder.web.domain.dwh_scenario.contracts import (
    ScenarioSqlRequest,
    ScenarioSqlResult,
)
from services.aml_builder.web.domain.dwh_scenario.validation import (
    validate_generated_sql,
)
from services.aml_builder.web.domain.filter_resolution import build_service_b_intent
from services.aml_builder.web.services.config_loader import load_catalogs
from services.aml_builder.web.services.observability import build_service_b_headers
from services.aml_builder.web.services.settings import settings
from services.aml_builder.web.services.sql_extraction import extract_sql

logger = logging.getLogger(__name__)


def _sql_text_from_sse_event(event: dict[str, Any], *, generation_only: bool) -> str | None:
    """Extract only approved SQL or an explicitly unapproved guarded candidate."""
    event_type = event.get("type")
    if event_type == "final_answer":
        return str(event.get("text") or "")
    if (
        generation_only
        and event_type == "generation_only_candidate"
        and event.get("approved") is False
    ):
        return str(event.get("text") or "")
    return None


def build_service_b_generation_content(
    request: ScenarioSqlRequest, filter_catalog: Any
) -> str:
    """Serialize the exact AML generation user content used by the HTTP adapter."""
    service_intent = build_service_b_intent(
        request.intent.model_dump(mode="json", exclude_none=False), filter_catalog
    )
    service_intent["resolved_filters"] = [
        item.model_dump(mode="json", exclude_unset=True)
        for item in request.confirmed_filters
    ]
    return json.dumps(
        {"request_type": "AML_SCENARIO_GENERATION", "intent": service_intent},
        indent=2,
        ensure_ascii=False,
    )


class HttpScenarioSqlGenerator:
    """Call the existing Service B endpoint using its established SSE envelope."""

    def __init__(self, filter_catalog: Any | None = None) -> None:
        """Create the adapter with an optional already-loaded filter catalog.

        Args:
            filter_catalog: Catalog used to enrich filters; supplying it lets the
                active tool reuse its pre-request catalog snapshot.
        """
        self._filter_catalog = filter_catalog

    async def generate(self, request: ScenarioSqlRequest) -> ScenarioSqlResult:
        """Generate SQL through Service B without changing its wire payload.

        Args:
            request: Validated canonical AML intent and resolved filter context.

        Returns:
            Typed generator result containing extracted SQL or the existing adapter error.
        """
        return await self._generate(request)

    async def generate_only_for_approved_evaluation(
        self,
        request: ScenarioSqlRequest,
        *,
        scope_sha256: str,
        schema_records: list[dict[str, Any]],
        case_id: str,
        service_b_payload_sha256: str,
    ) -> ScenarioSqlResult:
        """Call Service B's explicit tool-free development generation boundary.

        Args:
            request: Typed synthetic AML request.
            scope_sha256: Reviewed B9 scope digest expected by Service B settings.
            schema_records: Exact pinned dictionary metadata included in the review scope.

        Returns:
            Candidate result that is always marked unapproved.

        Raises:
            ValueError: If the supplied scope or pinned metadata is malformed.
        """
        if (
            len(scope_sha256) != 64
            or not schema_records
            or not case_id.startswith("b9-")
            or len(service_b_payload_sha256) != 64
        ):
            raise ValueError("Generation-only scope, case, payload, and pinned schema are required")
        return await self._generate(
            request,
            generation_only_scope_sha256=scope_sha256,
            generation_only_schema_records=schema_records,
            generation_only_case_id=case_id,
            generation_only_payload_sha256=service_b_payload_sha256,
        )

    async def _generate(
        self,
        request: ScenarioSqlRequest,
        *,
        generation_only_scope_sha256: str | None = None,
        generation_only_schema_records: list[dict[str, Any]] | None = None,
        generation_only_case_id: str | None = None,
        generation_only_payload_sha256: str | None = None,
    ) -> ScenarioSqlResult:
        """Share existing HTTP serialization for normal and guarded requests."""
        catalog = self._filter_catalog
        if catalog is None:
            catalog = load_catalogs(settings.CATALOG_DIR).filters
        payload_content = build_service_b_generation_content(request, catalog)
        chat_id = f"aml_tool_{uuid.uuid4().hex[:8]}"
        payload = {
            "messages": [{"role": "user", "content": payload_content}],
            "metadata": {
                "user_id": settings.PIOTECH_AI_USER_ID,
                "project_id": settings.PIOTECH_AI_PROJECT_ID,
                "chat_id": chat_id,
            },
            "reasoning_mode": "instant",
        }
        if generation_only_scope_sha256 is not None:
            schema_hash = hashlib.sha256(
                json.dumps(
                    generation_only_schema_records,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            payload["metadata"].update({
                "generation_only": True,
                "generation_only_scope_sha256": generation_only_scope_sha256,
                "generation_only_schema_sha256": schema_hash,
                "generation_only_schema_records": generation_only_schema_records,
                "generation_only_case_id": generation_only_case_id,
                "generation_only_payload_sha256": generation_only_payload_sha256,
            })
        request_id = str(request.version_context.request_id or "")
        correlation_id = str(request.version_context.correlation_id or "")
        collected_text: list[str] = []
        final_answer_text: str | None = None
        generation_only_normalizations: list[str] = []
        generation_only_metadata_error: str | None = None
        generation_only_sse_events: list[dict[str, Any]] = []
        generation_only_provider_validation_findings: list[dict[str, Any]] = []
        try:
            async with httpx.AsyncClient(
                timeout=settings.PIOTECH_AI_TIMEOUT_SECONDS
            ) as client:
                async with client.stream(
                    "POST",
                    settings.PIOTECH_AI_URL,
                    json=payload,
                    headers=build_service_b_headers(request_id, correlation_id),
                ) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        raw = line[5:].strip()
                        if raw == "[DONE]":
                            if generation_only_scope_sha256 is not None:
                                generation_only_sse_events.append({"type": "transport_done"})
                            break
                        try:
                            event = json.loads(raw)
                            if (
                                generation_only_scope_sha256 is not None
                                and isinstance(event, dict)
                            ):
                                generation_only_sse_events.append(event)
                                findings = event.get("validation_findings", [])
                                if event.get("type") == "generation_only_candidate":
                                    if not isinstance(findings, list) or not all(
                                        isinstance(item, dict)
                                        and isinstance(item.get("message"), str)
                                        for item in findings
                                    ):
                                        generation_only_metadata_error = (
                                            "Generation-only validation disclosure is malformed"
                                        )
                                    else:
                                        generation_only_provider_validation_findings.extend(
                                            findings
                                        )
                            event_type = event.get("type", "")
                            candidate_text = _sql_text_from_sse_event(
                                event,
                                generation_only=generation_only_scope_sha256 is not None,
                            )
                            if candidate_text is not None:
                                final_answer_text = candidate_text
                                if event_type == "generation_only_candidate":
                                    normalizations = event.get("normalizations", [])
                                    if (
                                        not isinstance(normalizations, list)
                                        or any(not isinstance(item, str) for item in normalizations)
                                    ):
                                        generation_only_metadata_error = (
                                            "Generation-only normalization disclosure is malformed"
                                        )
                                    else:
                                        generation_only_normalizations.extend(normalizations)
                            elif event_type == "content":
                                collected_text.append(event.get("text", ""))
                        except json.JSONDecodeError:
                            continue
        except httpx.HTTPError as exc:
            logger.error("[TOOL: SQL_BRIDGE] HTTP error calling PioTech AI: %s", exc)
            return ScenarioSqlResult(
                error=f"PioTech AI HTTP error: {exc}",
                generation_only_sse_events=generation_only_sse_events,
                generation_only_provider_validation_findings=(
                    generation_only_provider_validation_findings
                ),
            )

        if generation_only_metadata_error:
            return ScenarioSqlResult(
                error=generation_only_metadata_error,
                generation_only_sse_events=generation_only_sse_events,
                generation_only_provider_validation_findings=(
                    generation_only_provider_validation_findings
                ),
            )

        sql = extract_sql(final_answer_text or "") if final_answer_text else ""
        if not sql:
            sql = extract_sql("".join(collected_text).strip())
        if not sql:
            return ScenarioSqlResult(
                error="PioTech AI did not return a valid SQL query. Try refining the scenario intent.",
                generation_only_sse_events=generation_only_sse_events,
                generation_only_provider_validation_findings=(
                    generation_only_provider_validation_findings
                ),
            )
        return ScenarioSqlResult(
            sql=sql,
            generation_only_candidate_sql=(
                sql if generation_only_scope_sha256 is not None else None
            ),
            approved=(
                False
                if generation_only_scope_sha256 is not None
                else bool(final_answer_text and extract_sql(final_answer_text))
            ),
            validation_findings=[
                {"message": finding} for finding in validate_generated_sql(request, sql)
            ],
            generation_only_normalizations=generation_only_normalizations,
            generation_only_sse_events=generation_only_sse_events,
            generation_only_provider_validation_findings=(
                generation_only_provider_validation_findings
            ),
            provider="http",
            trace_id=chat_id,
            error=(
                "Generation-only candidate is not execution or deployment approved"
                if generation_only_scope_sha256 is not None
                else None
            ),
        )
