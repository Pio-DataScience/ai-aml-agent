"""P0 settings and SSE contract regressions."""

import os
import asyncio
import sqlite3
from pathlib import Path

import pytest
from langsmith import utils as langsmith_utils
from pydantic import ValidationError

from services.aml_builder.web.services.schemas import SSEEvent
from services.aml_builder.web.services import llm_client
from services.aml_builder.web.api import main as aml_main
from services.aml_builder.web.services import session_store
from services.aml_builder.web.services.settings import (
    Settings,
    configure_langsmith_environment,
)


def _required_settings(**overrides: object) -> Settings:
    """Construct settings without reading the repository's environment file."""
    values = {
        "ORACLE_DSN": "unused",
        "ORACLE_USER": "unused",
        "ORACLE_PASSWORD": "unused",
        "SERVICE_AUTH_TOKEN": "test-service-token",
        "_env_file": None,
    }
    values.update(overrides)
    return Settings(**values)


def test_state_database_defaults_follow_state_dir(tmp_path: Path) -> None:
    """Default sqlite paths are rooted under STATE_DIR and remain overridable."""
    settings = _required_settings(STATE_DIR=tmp_path)

    assert settings.CHECKPOINT_DB_PATH == tmp_path / "checkpoints.sqlite"
    assert settings.APP_STATE_DB_PATH == tmp_path / "app_state.sqlite"
    assert _required_settings(
        STATE_DIR=tmp_path,
        CHECKPOINT_DB_PATH=tmp_path / "custom.sqlite",
    ).CHECKPOINT_DB_PATH == tmp_path / "custom.sqlite"
    assert settings.AGENT_RECURSION_LIMIT == 25
    assert settings.MAX_AGENT_ITERATIONS == 25
    assert settings.MAX_VALIDATION_RETRIES == 3
    assert settings.LLM_MAX_TOKENS is None
    assert settings.CORS_ALLOWED_ORIGINS == "http://localhost:3000"


def test_service_startup_fails_clearly_without_service_token() -> None:
    """Service A refuses startup when the BFF shared token is not configured."""
    with pytest.raises(RuntimeError, match="SERVICE_AUTH_TOKEN is required"):
        aml_main.ensure_service_auth_token(None)


def test_unknown_environment_keys_do_not_break_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Extra .env keys are ignored and LangSmith settings are loaded safely."""
    for key in (
        "ORACLE_DSN",
        "ORACLE_USER",
        "ORACLE_PASSWORD",
        "LANGCHAIN_TRACING_V2",
        "LANGCHAIN_API_KEY",
        "LANGCHAIN_PROJECT",
    ):
        monkeypatch.delenv(key, raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text(
        "ORACLE_DSN=unused\nORACLE_USER=unused\nORACLE_PASSWORD=unused\n"
        "LEGACY_UNRELATED_KEY=ignored\nLANGCHAIN_TRACING_V2=true\n"
        "LANGCHAIN_API_KEY=redacted-test-key\n"
        "LANGCHAIN_PROJECT=dotenv-canary\n",
        encoding="utf-8",
    )

    settings = Settings(_env_file=env_path)

    assert settings.ORACLE_DSN == "unused"
    assert settings.LANGCHAIN_TRACING_V2 is True
    assert settings.LANGCHAIN_API_KEY == "redacted-test-key"
    assert settings.LANGCHAIN_PROJECT == "dotenv-canary"
    configure_langsmith_environment(settings)
    assert os.environ["LANGCHAIN_TRACING_V2"] == "true"
    assert os.environ["LANGSMITH_TRACING"] == "true"
    assert langsmith_utils.get_env_var("TRACING_V2") == "true"


@pytest.mark.parametrize("event_name", ["final_answer", "escalation_report"])
def test_removed_sse_event_names_are_rejected(event_name: str) -> None:
    """Service A no longer declares events that it never emits."""
    with pytest.raises(ValidationError):
        SSEEvent(type=event_name)


def test_existing_sse_event_names_remain_accepted() -> None:
    """P0 preserves the currently emitted SSE event contract."""
    for event_name in (
        "tool_call",
        "thinking",
        "content",
        "scenario_result",
        "plan_artifact",
        "scenario_metadata_catalog",
        "error",
        "done",
    ):
        assert SSEEvent(type=event_name).type == event_name


def test_langsmith_settings_are_exported_without_overwriting_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BaseSettings values reach LangChain's process environment without logs."""
    for key in (
        "LANGCHAIN_TRACING_V2",
        "LANGCHAIN_API_KEY",
        "LANGCHAIN_PROJECT",
        "LANGSMITH_TRACING",
        "LANGSMITH_API_KEY",
        "LANGSMITH_PROJECT",
    ):
        monkeypatch.delenv(key, raising=False)
    config = _required_settings(
        LANGCHAIN_TRACING_V2=True,
        LANGCHAIN_API_KEY="redacted-test-key",
        LANGCHAIN_PROJECT="offline-test-project",
    )

    configure_langsmith_environment(config)

    assert os.environ["LANGCHAIN_TRACING_V2"] == "true"
    assert os.environ["LANGCHAIN_PROJECT"] == "offline-test-project"
    assert os.environ["LANGCHAIN_API_KEY"] == "redacted-test-key"
    assert os.environ["LANGSMITH_TRACING"] == "true"
    assert os.environ["LANGSMITH_PROJECT"] == "offline-test-project"
    assert os.environ["LANGSMITH_API_KEY"] == "redacted-test-key"
    assert langsmith_utils.get_env_var("TRACING_V2") == "true"
    monkeypatch.setenv("LANGCHAIN_PROJECT", "process-project")
    configure_langsmith_environment(config)
    assert os.environ["LANGCHAIN_PROJECT"] == "process-project"


@pytest.mark.parametrize(
    ("model_name", "expected_parameter"),
    [("gpt-4o", "max_tokens"), ("gpt-5.6-luna", "max_completion_tokens")],
)
def test_llm_token_setting_preserves_provider_default_when_unset(
    monkeypatch: pytest.MonkeyPatch,
    model_name: str,
    expected_parameter: str,
) -> None:
    """Optional token overrides use the compatible ChatOpenAI parameter."""
    captured: dict[str, object] = {}

    def fake_chat_openai(**kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(llm_client, "ChatOpenAI", fake_chat_openai)
    monkeypatch.setattr(llm_client.settings, "LLM_PROVIDER", "openai")
    monkeypatch.setattr(llm_client.settings, "OPENAI_API_KEY", "redacted-test-key")
    monkeypatch.setattr(llm_client.settings, "LLM_MODEL", model_name)
    monkeypatch.setattr(llm_client.settings, "LLM_MAX_TOKENS", None)

    llm_client.build_llm()

    assert "max_tokens" not in captured
    assert "max_completion_tokens" not in captured
    monkeypatch.setattr(llm_client.settings, "LLM_MAX_TOKENS", 123)
    llm_client.build_llm()
    assert captured[expected_parameter] == 123


def test_fresh_state_directory_initializes_catalogs_and_checkpoint_store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Fresh-state startup components work without contacting external systems."""
    config = _required_settings(
        STATE_DIR=tmp_path / "v2",
        CATALOG_DIR=Path("config/aml_catalogs"),
    )
    monkeypatch.setattr(session_store, "settings", config)
    from services.aml_builder.web.services.config_loader import load_catalogs

    assert load_catalogs(config.CATALOG_DIR).policy.default_role == {
        "dev": "manager",
        "jwt": "viewer",
    }
    session_store.init_session_db()

    assert config.CHECKPOINT_DB_PATH is not None
    assert config.CHECKPOINT_DB_PATH.exists()
    assert config.APP_STATE_DB_PATH == config.STATE_DIR / "app_state.sqlite"
    with sqlite3.connect(config.CHECKPOINT_DB_PATH) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "chat_sessions" in tables


def test_application_lifespan_boots_with_fresh_state_and_mocked_adapters(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The full FastAPI startup path boots without contacting external systems."""
    for key in (
        "LANGCHAIN_TRACING_V2",
        "LANGCHAIN_API_KEY",
        "LANGCHAIN_PROJECT",
        "LANGSMITH_TRACING",
        "LANGSMITH_API_KEY",
        "LANGSMITH_PROJECT",
    ):
        monkeypatch.delenv(key, raising=False)
    config = _required_settings(
        STATE_DIR=tmp_path / "lifespan-state",
        CATALOG_DIR=Path("config/aml_catalogs"),
    )
    monkeypatch.setattr(aml_main, "settings", config)
    monkeypatch.setattr(session_store, "settings", config)
    monkeypatch.setattr(aml_main, "setup_logging", lambda: None)
    monkeypatch.setattr(aml_main, "init_pool", lambda: None)
    monkeypatch.setattr(aml_main, "init_shadow_pool", lambda: None)
    monkeypatch.setattr(aml_main, "close_pool", lambda: None)
    monkeypatch.setattr(aml_main, "close_shadow_pool", lambda: None)

    async def no_graph() -> object:
        """Stub graph warm-up to avoid LLM or Oracle integrations."""
        return object()

    async def no_close() -> None:
        """Stub checkpointer shutdown for this isolated lifecycle check."""

    monkeypatch.setattr(aml_main, "get_tool_driven_graph", no_graph)
    monkeypatch.setattr(aml_main, "close_checkpointer", no_close)

    async def run_lifespan() -> None:
        async with aml_main.lifespan(aml_main.app):
            assert config.CHECKPOINT_DB_PATH is not None
            assert config.CHECKPOINT_DB_PATH.exists()
            assert config.APP_STATE_DB_PATH == config.STATE_DIR / "app_state.sqlite"

    asyncio.run(run_lifespan())
