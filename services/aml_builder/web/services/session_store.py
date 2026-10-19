"""Chat sidebar metadata stored beside workflow state in app_state.sqlite."""

from __future__ import annotations

import logging
from typing import Any

from services.aml_builder.web.services.settings import settings
from services.aml_builder.web.workflow.repo import WorkflowRepo

logger = logging.getLogger(__name__)


def _repo() -> WorkflowRepo:
    """Build a session repository using the configured app-state database."""
    return WorkflowRepo(settings.APP_STATE_DB_PATH)


def init_session_db() -> None:
    """Initialize the composite-key sidebar table in app_state.sqlite."""
    _repo().initialize()


def upsert_chat_session(
    chat_id: str, user_id: str, project_id: str, first_message_content: str
) -> None:
    """Insert or touch one chat identified by project, chat, and owner."""
    title = first_message_content[:50] if first_message_content else "New Chat"
    _repo().upsert_chat_session(project_id, chat_id, user_id, title)


def list_chat_sessions(
    user_id: str, project_id: str | None = None
) -> list[dict[str, Any]]:
    """List non-deleted chat rows for one owner."""
    return _repo().list_chat_sessions(user_id, project_id)


def soft_delete_chat_session(chat_id: str, user_id: str, project_id: str) -> bool:
    """Soft-delete a composite-key sidebar row."""
    result = _repo().update_chat_session(project_id, chat_id, user_id, is_deleted=True)
    return bool(result)


def rename_chat_session(
    chat_id: str, user_id: str, project_id: str, title: str
) -> bool:
    """Rename a composite-key sidebar row."""
    result = _repo().update_chat_session(project_id, chat_id, user_id, title=title)
    return bool(result)


def toggle_chat_pin(chat_id: str, user_id: str, project_id: str) -> bool | None:
    """Toggle one chat's pin state, returning None when absent."""
    return _repo().toggle_chat_pin(project_id, chat_id, user_id)
