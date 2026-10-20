"""Map typed tool results to the established UI-visible SSE events."""

from typing import Any, List

from services.aml_builder.web.api.codec import decode_tool_result
from services.aml_builder.web.services.schemas import SSEEvent


def map_tool_result_to_sse(tool_name: str, content: Any) -> List[SSEEvent]:
    """Map a tool message to zero or more established SSE events.

    Args:
        tool_name: LangGraph tool name associated with the message.
        content: Tool output as JSON text or a decoded object.

    Returns:
        SSE events in the order expected by the existing client.
    """
    try:
        payload = decode_tool_result(content, tool_name).payload
    except (ValueError, TypeError):
        return []

    if tool_name == "generate_scenario_execution_plan":
        plan = payload.get("plan_artifact")
        return [SSEEvent(type="plan_artifact", text=plan)] if plan else []
    if tool_name == "analyze_intent_and_discover_explanation_codes":
        events: list[SSEEvent] = []
        card = payload.get("parameter_card")
        if isinstance(card, dict):
            events.append(SSEEvent(type="parameter_card", data=card))
        elif payload.get("card_auto_confirmed"):
            events.append(SSEEvent(type="parameter_card", data={"auto_confirmed": True}))
        conflict = payload.get("conflict_report")
        if isinstance(conflict, dict):
            events.append(SSEEvent(type="conflict_report", data=conflict))
        return events
    if tool_name == "prepare_scenario_metadata_for_persistence":
        return (
            [SSEEvent(type="scenario_metadata_catalog", data=payload)]
            if "fields" in payload
            else []
        )
    if tool_name in (
        "execute_oracle_dwh_shadow_test",
        "persist_and_validate_scenario_in_dwh",
    ):
        if (
            "raw_sql" in payload
            or payload.get("write_success")
            or "shadow_test_metrics" in payload
        ):
            return [SSEEvent(type="scenario_result", data=payload)]
    return []


def stage_changed_event(stage: str, revision_id: str | None) -> SSEEvent:
    """Build the SSE event emitted after a workflow tool round."""
    return SSEEvent(
        type="stage_changed",
        data={"stage": stage, "revision_id": revision_id},
    )
