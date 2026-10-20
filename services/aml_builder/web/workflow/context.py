"""Transient model-message projection for current revision context."""

from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import BaseMessage, SystemMessage, ToolMessage


def build_model_messages(
    messages: list[BaseMessage],
    *,
    current_revision: dict[str, Any] | None,
    superseded: dict[str, int],
    allowed_actions: list[str] | None = None,
    enforcement: str = "enforce",
    pending_request_id: str | None = None,
    context_policy: dict[str, Any] | None = None,
) -> list[BaseMessage]:
    """Add current-state guidance and stub superseded tool results.

    The input list is copied, so the graph's checkpoint messages remain unchanged.
    """
    policy = context_policy or {}
    trimming = bool(policy.get("trimming", False))
    max_chars = int(policy.get("max_tool_payload_chars", 4000))
    keep_latest_turns = max(0, int(policy.get("keep_latest_turns", 3)))
    latest_tool_indexes: set[int] = set()
    seen_tools: set[str] = set()
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if not isinstance(message, ToolMessage):
            continue
        tool_name = str(message.name or "")
        if tool_name and tool_name not in seen_tools:
            latest_tool_indexes.add(index)
            seen_tools.add(tool_name)

    human_indexes = [
        index for index, message in enumerate(messages) if getattr(message, "type", None) == "human"
    ]
    recent_start = 0
    if keep_latest_turns == 0:
        recent_start = len(messages)
    elif len(human_indexes) > keep_latest_turns:
        recent_start = human_indexes[-keep_latest_turns]

    current_revision_id = (
        str(current_revision.get("revision_id"))
        if current_revision and current_revision.get("revision_id")
        else None
    )
    projected: list[BaseMessage] = []
    for index, message in enumerate(messages):
        if isinstance(message, ToolMessage):
            result = _decode_result(message.content)
            revision_id = result.get("revision_id") if result else None
            payload = result.get("payload", {}) if result else {}
            pending_card = payload.get("parameter_card") if isinstance(payload, dict) else None
            protects_pending = bool(
                pending_request_id
                and _contains_value(pending_card, pending_request_id)
            )
            protects_current = bool(current_revision_id and revision_id == current_revision_id)
            protects_latest = index in latest_tool_indexes
            protects_recent = index >= recent_start
            protected = (
                protects_pending
                or protects_current
                or protects_latest
                or protects_recent
            )
            if revision_id in superseded and not protected:
                content = _tool_summary(
                    message,
                    result,
                    prefix=f"[superseded by revision {superseded[revision_id]}]",
                )
                projected.append(_copy_tool_message(message, content))
                continue
            if (
                trimming
                and len(str(message.content)) > max_chars
                and not protected
            ):
                projected.append(
                    _copy_tool_message(message, _tool_summary(message, result))
                )
                continue
        projected.append(message)
    if current_revision is not None:
        summary = {
            "revision_id": current_revision.get("revision_id"),
            "rev_no": current_revision.get("rev_no"),
            "stage": current_revision.get("stage"),
        }
        guidance = (
            "Current workflow state: "
            + json.dumps(summary, ensure_ascii=False)
            + ". Allowed actions: "
            + ", ".join(allowed_actions or [])
            + ". Older tool results may be outdated."
        )
        if enforcement == "observe":
            guidance += (
                " Workflow stage checks are advisory in observe mode; "
                "permission denials and deployed-chat locks still block."
            )
        projected.insert(0, SystemMessage(content=guidance))
    return projected


def _decode_result(content: Any) -> dict[str, Any] | None:
    """Decode a tool-result envelope or legacy JSON object."""
    try:
        value = json.loads(content) if isinstance(content, str) else content
    except (json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _contains_value(value: Any, expected: str) -> bool:
    """Check nested JSON data for a pending-card request ID."""
    if isinstance(value, dict):
        return any(
            (key == "request_id" and str(item) == expected)
            or _contains_value(item, expected)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_value(item, expected) for item in value)
    return False


def _tool_summary(
    message: ToolMessage,
    result: dict[str, Any] | None,
    *,
    prefix: str = "[older tool result]",
) -> str:
    """Build a one-line tool/status/identity summary without payload values."""
    envelope = result or {}
    payload = envelope.get("payload", {})
    payload = payload if isinstance(payload, dict) else {}
    fields: list[str] = [f"tool={message.name or 'unknown'}"]
    fields.append(f"status={envelope.get('status', 'unknown')}")
    for key in (
        "revision_id",
        "request_id",
        "scenario_id",
        "scenario_key",
        "parent_scenario_id",
        "params_hash",
        "plan_hash",
        "sql_hash",
        "metadata_hash",
        "signature_hash",
    ):
        value = envelope.get(key, payload.get(key))
        if value is not None and isinstance(value, (str, int)):
            fields.append(f"{key}={value}")
    return f"{prefix} " + " ".join(fields)


def _copy_tool_message(message: ToolMessage, content: str) -> ToolMessage:
    """Copy a tool message while retaining LangGraph call linkage metadata."""
    return ToolMessage(
        content=content,
        tool_call_id=message.tool_call_id,
        name=message.name,
        additional_kwargs=message.additional_kwargs,
        response_metadata=message.response_metadata,
    )
