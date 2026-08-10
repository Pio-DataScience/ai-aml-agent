"""
Chat session sidebar metadata store.

Owns the small SQLite `chat_sessions` table (title, pin, soft-delete) that
backs the frontend's chat list sidebar. This is separate from LangGraph's
own checkpointer table, which lives in the same SQLite file but stores
conversation state, not sidebar metadata.
"""

import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from services.aml_builder.web.services.settings import settings

logger = logging.getLogger(__name__)


def init_session_db() -> None:
    """Create the chat_sessions table in the SQLite checkpointer database if missing."""
    checkpoint_path = settings.CHECKPOINT_DB_PATH
    Path(checkpoint_path).parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(checkpoint_path)
    try:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS chat_sessions (
                chat_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                title TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                is_pinned INTEGER DEFAULT 0,
                is_deleted INTEGER DEFAULT 0
            );
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_id ON chat_sessions(user_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_project ON chat_sessions(user_id, project_id);")
        conn.commit()
        logger.info("[DB] SQLite chat_sessions table verified/initialized.")
    except Exception as e:
        logger.error("[DB] Error initializing chat_sessions table: %s", e)
    finally:
        conn.close()


def upsert_chat_session(chat_id: str, user_id: str, project_id: str, first_message_content: str) -> None:
    """Insert or update a chat session in the sidebar metadata table."""
    checkpoint_path = settings.CHECKPOINT_DB_PATH
    conn = sqlite3.connect(checkpoint_path)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT title, is_deleted FROM chat_sessions WHERE chat_id = ?", (chat_id,))
        row = cursor.fetchone()

        now = datetime.utcnow().isoformat()

        if row:
            cursor.execute("""
                UPDATE chat_sessions
                SET updated_at = ?, is_deleted = 0
                WHERE chat_id = ?
            """, (now, chat_id))
        else:
            title = first_message_content[:50]
            if not title:
                title = "New Chat"
            cursor.execute("""
                INSERT INTO chat_sessions (chat_id, user_id, project_id, title, updated_at, is_pinned, is_deleted)
                VALUES (?, ?, ?, ?, ?, 0, 0)
            """, (chat_id, user_id, project_id, title, now))
        conn.commit()
    except Exception as e:
        logger.error("[DB] Error upserting chat session %s: %s", chat_id, e)
    finally:
        conn.close()


def list_chat_sessions(user_id: str, project_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """List all non-deleted chat sessions for a user, optionally filtered by project."""
    checkpoint_path = settings.CHECKPOINT_DB_PATH
    if not Path(checkpoint_path).exists():
        return []

    conn = sqlite3.connect(checkpoint_path)
    try:
        cursor = conn.cursor()
        if project_id and project_id not in ("0", "None"):
            cursor.execute("""
                SELECT chat_id, project_id, title, updated_at, is_pinned, is_deleted
                FROM chat_sessions
                WHERE user_id = ? AND project_id = ? AND is_deleted = 0
                ORDER BY is_pinned DESC, updated_at DESC
            """, (user_id, project_id))
        else:
            cursor.execute("""
                SELECT chat_id, project_id, title, updated_at, is_pinned, is_deleted
                FROM chat_sessions
                WHERE user_id = ? AND is_deleted = 0
                ORDER BY is_pinned DESC, updated_at DESC
            """, (user_id,))

        rows = cursor.fetchall()
        return [
            {
                "chat_id": row[0],
                "project_id": row[1],
                "title": row[2],
                "updated_at": row[3],
                "is_pinned": bool(row[4]),
                "is_deleted": bool(row[5]),
            }
            for row in rows
        ]
    finally:
        conn.close()


def soft_delete_chat_session(chat_id: str, user_id: str, project_id: str) -> bool:
    """Soft delete a chat session by setting is_deleted = 1.

    Returns:
        bool: True if the session existed and was deleted, False if not found.
    """
    checkpoint_path = settings.CHECKPOINT_DB_PATH
    if not Path(checkpoint_path).exists():
        return False

    conn = sqlite3.connect(checkpoint_path)
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT chat_id FROM chat_sessions WHERE chat_id = ? AND user_id = ? AND project_id = ?",
            (chat_id, user_id, project_id),
        )
        if not cursor.fetchone():
            return False

        cursor.execute(
            "UPDATE chat_sessions SET is_deleted = 1 WHERE chat_id = ? AND user_id = ? AND project_id = ?",
            (chat_id, user_id, project_id),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def rename_chat_session(chat_id: str, user_id: str, project_id: str, title: str) -> bool:
    """Rename a chat session by updating its title.

    Returns:
        bool: True if the session existed and was renamed, False if not found.
    """
    checkpoint_path = settings.CHECKPOINT_DB_PATH
    if not Path(checkpoint_path).exists():
        return False

    conn = sqlite3.connect(checkpoint_path)
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT chat_id FROM chat_sessions WHERE chat_id = ? AND user_id = ? AND project_id = ?",
            (chat_id, user_id, project_id),
        )
        if not cursor.fetchone():
            return False

        cursor.execute(
            "UPDATE chat_sessions SET title = ? WHERE chat_id = ? AND user_id = ? AND project_id = ?",
            (title, chat_id, user_id, project_id),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def toggle_chat_pin(chat_id: str, user_id: str, project_id: str) -> Optional[bool]:
    """Toggle the pin status of a chat session.

    Returns:
        Optional[bool]: The new pin state, or None if the session was not found.
    """
    checkpoint_path = settings.CHECKPOINT_DB_PATH
    if not Path(checkpoint_path).exists():
        return None

    conn = sqlite3.connect(checkpoint_path)
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT is_pinned FROM chat_sessions WHERE chat_id = ? AND user_id = ? AND project_id = ?",
            (chat_id, user_id, project_id),
        )
        row = cursor.fetchone()
        if row is None:
            return None

        new_pin = 1 - row[0]
        cursor.execute(
            "UPDATE chat_sessions SET is_pinned = ? WHERE chat_id = ? AND user_id = ? AND project_id = ?",
            (new_pin, chat_id, user_id, project_id),
        )
        conn.commit()
        return bool(new_pin)
    finally:
        conn.close()
