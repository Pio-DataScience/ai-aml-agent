"""P3 composite sidebar key and fresh-state behavior tests."""

import sqlite3
from pathlib import Path

import pytest

from services.aml_builder.web.services import session_store
from services.aml_builder.web.services.settings import Settings


def _settings(state_dir: Path) -> Settings:
    """Construct isolated settings for sidebar persistence tests."""
    return Settings(
        ORACLE_DSN="unused",
        ORACLE_USER="unused",
        ORACLE_PASSWORD="unused",
        STATE_DIR=state_dir,
        _env_file=None,
    )


def test_composite_sidebar_key_and_no_legacy_row_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same chat IDs remain isolated by owner and legacy checkpoint rows stay put."""
    config = _settings(tmp_path / "v2")
    monkeypatch.setattr(session_store, "settings", config)
    config.CHECKPOINT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(config.CHECKPOINT_DB_PATH) as connection:
        connection.execute(
            "CREATE TABLE chat_sessions(chat_id TEXT PRIMARY KEY, user_id TEXT, project_id TEXT, title TEXT, updated_at TEXT, is_pinned INTEGER, is_deleted INTEGER)"
        )
        connection.execute(
            "INSERT INTO chat_sessions VALUES('legacy','u1','p1','legacy','now',0,0)"
        )

    session_store.init_session_db()
    session_store.upsert_chat_session("same", "u1", "p1", "first")
    session_store.upsert_chat_session("same", "u2", "p1", "second")

    assert [row["title"] for row in session_store.list_chat_sessions("u1", "p1")] == [
        "first"
    ]
    assert [row["title"] for row in session_store.list_chat_sessions("u2", "p1")] == [
        "second"
    ]
    assert session_store.list_chat_sessions("u1", "p1")[0]["chat_id"] != "legacy"
