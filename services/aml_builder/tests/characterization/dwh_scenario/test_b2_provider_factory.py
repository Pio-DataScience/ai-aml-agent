"""Offline provider construction contracts for B2."""

from __future__ import annotations

import sys
import types

import pytest

from services.aml_builder.web.services import llm_client


@pytest.mark.parametrize(
    ("provider", "expected_url", "key_name"),
    [
        ("openai", None, "DWH_SQL_OPENAI_API_KEY"),
        ("lmstudio", "http://127.0.0.1:1234/v1", None),
        ("cohere", None, "DWH_SQL_COHERE_API_KEY"),
        ("sambanova", "https://api.sambanova.ai/v1", "DWH_SQL_SAMBANOVA_API_KEY"),
        ("openrouter", "https://openrouter.ai/api/v1", "DWH_SQL_OPENROUTER_API_KEY"),
    ],
)
def test_role_provider_matrix_uses_role_model_and_deadline_settings(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    expected_url: str | None,
    key_name: str | None,
) -> None:
    """Supported roles create clients with role model, bounded retry and tracing."""
    captured: dict[str, object] = {}
    trace_callbacks = [object()]
    monkeypatch.setattr(llm_client, "_tracing_callbacks", lambda: trace_callbacks)
    monkeypatch.setattr(
        llm_client, "ChatOpenAI", lambda **kwargs: captured.update(kwargs) or object()
    )
    config = llm_client.settings
    values: dict[str, object] = {
        "DWH_SQL_GENERATOR_PROVIDER": provider,
        "DWH_SQL_GENERATOR_MODEL": "role-model",
        "DWH_SQL_BASE_URL": None,
        "DWH_SQL_RETRY_BUDGET": 2,
        "DWH_SQL_MAX_ATTEMPTS": 3,
        "DWH_SQL_OUTPUT_TOKEN_LIMIT": 222,
        "DWH_SQL_READ_TIMEOUT_SECONDS": 40.0,
        "DWH_SQL_CONNECT_TIMEOUT_SECONDS": 5.0,
        "LANGCHAIN_TRACING_V2": True,
    }
    if key_name:
        values[key_name] = "test-key"
    for name, value in values.items():
        monkeypatch.setattr(config, name, value)
    if provider == "cohere":
        module = types.SimpleNamespace(
            ChatCohere=lambda **kwargs: captured.update(kwargs) or object()
        )
        monkeypatch.setitem(sys.modules, "langchain_cohere", module)
    llm_client.build_llm(role="generator")
    assert captured["model"] == "role-model"
    assert captured["max_retries"] == 2
    assert captured["max_tokens"] == 222
    assert captured["callbacks"] is trace_callbacks
    if expected_url:
        assert captured["base_url"] == expected_url


def test_missing_provider_key_fails_without_leaking_secret(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Missing role credentials produce actionable errors and no key logging."""
    monkeypatch.setattr(llm_client.settings, "DWH_SQL_GENERATOR_PROVIDER", "openrouter")
    monkeypatch.setattr(llm_client.settings, "DWH_SQL_GENERATOR_MODEL", "model")
    monkeypatch.setattr(llm_client.settings, "DWH_SQL_OPENROUTER_API_KEY", None)
    with pytest.raises(ValueError, match="API key is required"):
        llm_client.build_llm(role="generator")
    assert "test-key" not in caplog.text
