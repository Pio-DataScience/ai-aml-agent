"""Bound and assemble model context from A's typed scenario request."""

from __future__ import annotations

import json
from typing import Any

from services.aml_builder.web.domain.dwh_scenario.contracts import ScenarioSqlRequest


def assemble_context(
    request: ScenarioSqlRequest,
    *,
    experience: list[dict[str, Any]],
    tables: list[dict[str, Any]],
    columns: list[dict[str, Any]],
    budget: int,
) -> str:
    """Serialize intent and retrieved metadata under a conservative context cap."""
    payload = {
        "intent": request.intent.model_dump(mode="json"),
        "confirmed_filters": [
            item.model_dump(mode="json") for item in request.confirmed_filters
        ],
        "experience": experience,
        "tables": tables,
        "columns": columns,
    }
    encoded = json.dumps(payload, default=str, ensure_ascii=True)
    if len(encoded) > budget * 4:
        raise ValueError("Context budget exhausted")
    return encoded
