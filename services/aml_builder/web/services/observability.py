"""Safe JSON-lines metrics and request-correlation helpers."""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any


metrics_logger = logging.getLogger("aml.metrics")
_REQUEST_ID_PATTERN = re.compile(r"[^A-Za-z0-9._:-]+")
_TOKEN_FIELDS = ("input_tokens", "output_tokens", "total_tokens")


def resolve_request_id(value: str | None) -> str:
    """Return a safe request ID, generating one when the header is absent or invalid.

    Args:
        value: Client-provided ``X-Request-ID`` header.

    Returns:
        A non-empty request ID with no control characters.
    """
    cleaned = _REQUEST_ID_PATTERN.sub("", value or "")[:128]
    return cleaned or uuid.uuid4().hex


def build_correlation_id(thread_id: str, request_id: str) -> str:
    """Join workflow thread and request identity for one request trace.

    Args:
        thread_id: LangGraph workflow thread identifier.
        request_id: Request identifier returned to the caller.

    Returns:
        A stable correlation ID containing both identifiers.
    """
    return f"{thread_id}:{request_id}"


def build_service_b_headers(request_id: str, correlation_id: str) -> dict[str, str]:
    """Build the non-payload correlation headers sent to Service B.

    Args:
        request_id: Request identifier returned by Service A.
        correlation_id: Thread and request correlation identifier.

    Returns:
        HTTP headers for the Service B SSE request.
    """
    return {
        "Accept": "text/event-stream",
        "Content-Type": "application/json",
        "X-Request-ID": request_id,
        "X-Correlation-ID": correlation_id,
    }


def emit_tool_metric(
    *,
    correlation_id: str,
    thread_id: str,
    revision_id: str | None,
    tool: str,
    duration_ms: float,
    status: str,
    error_code: str | None,
    token_usage: dict[str, Any] | None = None,
) -> None:
    """Emit one allow-listed JSON-lines timing record without business payloads.

    Args:
        correlation_id: Request/thread correlation identifier.
        thread_id: Workflow thread identifier.
        revision_id: Current or result revision identifier, when available.
        tool: Tool name or the measured stage name.
        duration_ms: Non-negative elapsed time in milliseconds.
        status: Stable result status such as ``ok``, ``error`` or ``blocked``.
        error_code: Stable machine-readable error code, when available.
        token_usage: Provider token counts, when the model reported them.
    """
    payload: dict[str, Any] = {
        "correlation_id": correlation_id,
        "thread_id": thread_id,
        "revision_id": revision_id,
        "tool": tool,
        "duration_ms": round(max(duration_ms, 0.0), 3),
        "status": status,
        "error_code": error_code,
    }
    if token_usage:
        safe_usage = {
            key: int(token_usage[key])
            for key in _TOKEN_FIELDS
            if key in token_usage and isinstance(token_usage[key], (int, float))
        }
        if safe_usage:
            payload["token_usage"] = safe_usage
    metrics_logger.info(json.dumps(payload, separators=(",", ":"), sort_keys=True))
