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
) -> list[BaseMessage]:
    """Add current-state guidance and stub superseded tool results.

    The input list is copied, so the graph's checkpoint messages remain unchanged.
    """
    projected: list[BaseMessage] = []
    for message in messages:
        if isinstance(message, ToolMessage):
            revision_id = _result_revision_id(message.content)
            if revision_id in superseded:
                projected.append(
                    ToolMessage(
                        content=f"[superseded by revision {superseded[revision_id]}]",
                        tool_call_id=message.tool_call_id,
                        name=message.name,
                        additional_kwargs=message.additional_kwargs,
                        response_metadata=message.response_metadata,
                    )
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


def _result_revision_id(content: Any) -> str | None:
    """Decode an optional revision ID from a tool-result envelope."""
    try:
        value = json.loads(content) if isinstance(content, str) else content
    except (json.JSONDecodeError, TypeError):
        return None
    return value.get("revision_id") if isinstance(value, dict) else None
