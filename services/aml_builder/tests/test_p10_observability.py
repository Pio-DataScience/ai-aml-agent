"""Offline coverage for P10 metrics and request correlation."""

from __future__ import annotations

import json
import logging
from io import StringIO

from services.aml_builder.web.services.observability import (
    build_correlation_id,
    emit_tool_metric,
    metrics_logger,
    resolve_request_id,
)


def test_metrics_are_json_lines_with_only_safe_fields() -> None:
    """Metrics include identifiers and counts without logging business payloads."""
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    previous_level = metrics_logger.level
    metrics_logger.setLevel(logging.INFO)
    metrics_logger.addHandler(handler)
    try:
        emit_tool_metric(
            correlation_id="thread-a:req-1",
            thread_id="thread-a",
            revision_id="rev-1",
            tool="fake_tool",
            duration_ms=12.5,
            status="error",
            error_code="FAKE_ERROR",
            token_usage={"input_tokens": 12, "output_tokens": 3},
        )
    finally:
        metrics_logger.removeHandler(handler)
        metrics_logger.setLevel(previous_level)
    line = stream.getvalue().strip()
    payload = json.loads(line)
    assert payload == {
        "correlation_id": "thread-a:req-1",
        "thread_id": "thread-a",
        "revision_id": "rev-1",
        "tool": "fake_tool",
        "duration_ms": 12.5,
        "status": "error",
        "error_code": "FAKE_ERROR",
        "token_usage": {"input_tokens": 12, "output_tokens": 3},
    }
    assert "SELECT" not in line
    assert "prompt" not in line.lower()


def test_metric_omits_unavailable_token_usage() -> None:
    """Unknown model usage does not produce a misleading token count."""
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    previous_level = metrics_logger.level
    metrics_logger.setLevel(logging.INFO)
    metrics_logger.addHandler(handler)
    try:
        emit_tool_metric(
            correlation_id="thread-a:req-2",
            thread_id="thread-a",
            revision_id=None,
            tool="fake_tool",
            duration_ms=1.0,
            status="ok",
            error_code=None,
        )
    finally:
        metrics_logger.removeHandler(handler)
        metrics_logger.setLevel(previous_level)
    payload = json.loads(stream.getvalue().strip())
    assert "token_usage" not in payload


def test_request_id_is_safe_and_correlation_contains_thread_and_request() -> None:
    """The response ID can be returned and correlated without unsafe header bytes."""
    request_id = resolve_request_id("ui-request-7\r\nInjected: bad")
    assert request_id == "ui-request-7Injected:bad"
    assert build_correlation_id("project_chat_user", request_id) == (
        "project_chat_user:ui-request-7Injected:bad"
    )
    assert resolve_request_id(None)
