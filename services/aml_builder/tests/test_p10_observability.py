"""Offline coverage for P10 metrics and request correlation."""

from __future__ import annotations

import json
import logging
import os
from io import StringIO
from types import SimpleNamespace

from services.aml_builder.web.services.observability import (
    build_correlation_id,
    build_service_b_headers,
    emit_tool_metric,
    metrics_logger,
    resolve_request_id,
)
from services.aml_builder.web.services.settings import configure_langsmith_environment


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


def test_service_b_correlation_is_carried_in_http_headers() -> None:
    """Request correlation is transported as headers, separate from JSON payloads."""
    assert build_service_b_headers("req-1", "thread-1:req-1") == {
        "Accept": "text/event-stream",
        "Content-Type": "application/json",
        "X-Request-ID": "req-1",
        "X-Correlation-ID": "thread-1:req-1",
    }


def test_langsmith_values_reach_environment_without_printing_keys(
    monkeypatch, caplog
) -> None:
    """Tracing settings propagate to supported environment aliases without logging secrets."""
    keys = (
        "LANGCHAIN_TRACING_V2",
        "LANGCHAIN_API_KEY",
        "LANGCHAIN_PROJECT",
        "LANGSMITH_TRACING",
        "LANGSMITH_API_KEY",
        "LANGSMITH_PROJECT",
    )
    for key in keys:
        monkeypatch.delenv(key, raising=False)
    secret = "test-only-langsmith-secret"
    configure_langsmith_environment(
        SimpleNamespace(
            LANGCHAIN_TRACING_V2=True,
            LANGCHAIN_API_KEY=secret,
            LANGCHAIN_PROJECT="p10-test-project",
        )
    )
    assert os.environ["LANGCHAIN_TRACING_V2"] == "true"
    assert os.environ["LANGSMITH_TRACING"] == "true"
    assert os.environ["LANGCHAIN_API_KEY"] == secret
    assert os.environ["LANGSMITH_API_KEY"] == secret
    assert os.environ["LANGCHAIN_PROJECT"] == "p10-test-project"
    assert os.environ["LANGSMITH_PROJECT"] == "p10-test-project"
    assert secret not in caplog.text
