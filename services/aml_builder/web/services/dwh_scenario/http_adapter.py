"""HTTP SSE adapter implementing the DWH scenario generator contract."""

from __future__ import annotations

import json
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
        catalog = self._filter_catalog
        if catalog is None:
            catalog = load_catalogs(settings.CATALOG_DIR).filters
        service_intent = build_service_b_intent(
            request.intent.model_dump(mode="json", exclude_none=False), catalog
        )
        service_intent["resolved_filters"] = [
            item.model_dump(mode="json", exclude_unset=True)
            for item in request.confirmed_filters
        ]
        payload_content = json.dumps(
            {"request_type": "AML_SCENARIO_GENERATION", "intent": service_intent},
            indent=2,
            ensure_ascii=False,
        )
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
        request_id = str(request.version_context.request_id or "")
        correlation_id = str(request.version_context.correlation_id or "")
        collected_text: list[str] = []
        final_answer_text: str | None = None
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
                            break
                        try:
                            event = json.loads(raw)
                            event_type = event.get("type", "")
                            if event_type == "final_answer":
                                final_answer_text = event.get("text", "")
                            elif event_type == "content":
                                collected_text.append(event.get("text", ""))
                        except json.JSONDecodeError:
                            continue
        except httpx.HTTPError as exc:
            logger.error("[TOOL: SQL_BRIDGE] HTTP error calling PioTech AI: %s", exc)
            return ScenarioSqlResult(error=f"PioTech AI HTTP error: {exc}")

        sql = extract_sql(final_answer_text or "") if final_answer_text else ""
        if not sql:
            sql = extract_sql("".join(collected_text).strip())
        if not sql:
            return ScenarioSqlResult(
                error="PioTech AI did not return a valid SQL query. Try refining the scenario intent."
            )
        return ScenarioSqlResult(
            sql=sql,
            approved=bool(final_answer_text and extract_sql(final_answer_text)),
            validation_findings=[
                {"message": finding} for finding in validate_generated_sql(request, sql)
            ],
            provider="http",
            trace_id=chat_id,
        )
