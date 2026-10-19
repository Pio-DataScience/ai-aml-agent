"""SQLite persistence for workflow state, revision history, audit, and chats."""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from services.aml_builder.web.workflow.models import Revision, WorkflowRow


class WorkflowConflict(RuntimeError):
    """Raised when a workflow write uses a stale optimistic-lock version."""


def _now() -> str:
    """Return a timezone-aware UTC timestamp in ISO format."""
    return datetime.now(timezone.utc).isoformat()


class WorkflowRepo:
    """Manage workflow and revision state in the app-state SQLite database."""

    def __init__(
        self,
        path: Path | str,
        *,
        history_cap: int = 5,
        enforcement: str = "observe",
        busy_timeout_ms: int = 10000,
    ) -> None:
        """Configure a repository instance.

        Args:
            path: App-state database path, independent from the graph checkpoint.
            history_cap: Maximum undeployed history revisions retained per chat.
            enforcement: Guard mode, either observe or enforce.
            busy_timeout_ms: SQLite lock wait for each connection.
        """
        if history_cap < 0:
            raise ValueError("history_cap must be non-negative")
        if enforcement not in {"observe", "enforce"}:
            raise ValueError("enforcement must be 'observe' or 'enforce'")
        self.path = Path(path)
        self.history_cap = history_cap
        self.enforcement = enforcement
        self.busy_timeout_ms = busy_timeout_ms
        self.card_audit_repo = CardAuditRepo(self)

    def _connect(self) -> sqlite3.Connection:
        """Open a configured SQLite connection."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=self.busy_timeout_ms / 1000)
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout_ms)}")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Open and reliably close a connection for a short operation."""
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        """Create the workflow schema and record its SQLite schema version."""
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS scenario_workflow (
                    thread_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    locked INTEGER NOT NULL DEFAULT 0,
                    version INTEGER NOT NULL DEFAULT 1,
                    scenario_id TEXT NOT NULL,
                    scenario_key TEXT NOT NULL,
                    parent_scenario_id TEXT,
                    parent_version INTEGER,
                    current_revision_id TEXT,
                    deployed_revision_id TEXT,
                    pending_card_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS scenario_revision (
                    revision_id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL REFERENCES scenario_workflow(thread_id),
                    rev_no INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    parent_revision_id TEXT,
                    restored_from_revision_id TEXT,
                    card_json TEXT,
                    params_hash TEXT,
                    signature_json TEXT,
                    signature_hash TEXT,
                    conflict_json TEXT,
                    plan_md TEXT,
                    plan_hash TEXT,
                    plan_approved_at TEXT,
                    plan_approved_by TEXT,
                    shadow_json TEXT,
                    sql_hash TEXT,
                    result_approved_at TEXT,
                    result_approved_by TEXT,
                    metadata_json TEXT,
                    metadata_hash TEXT,
                    created_at TEXT NOT NULL,
                    created_by TEXT,
                    superseded_at TEXT,
                    superseded_by_revision_id TEXT,
                    evicted_at TEXT,
                    version INTEGER NOT NULL DEFAULT 1,
                    UNIQUE(thread_id, rev_no)
                );
                CREATE INDEX IF NOT EXISTS idx_revision_thread_status_no
                    ON scenario_revision(thread_id, status, rev_no);
                CREATE TABLE IF NOT EXISTS param_card_audit (
                    request_id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    revision_id TEXT,
                    user_id TEXT NOT NULL,
                    user_role TEXT NOT NULL,
                    source TEXT NOT NULL,
                    shown_at TEXT NOT NULL,
                    resolved_at TEXT,
                    outcome TEXT NOT NULL,
                    rows_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workflow_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    thread_id TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    from_stage TEXT,
                    to_stage TEXT,
                    detail_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_workflow_events_thread
                    ON workflow_events(thread_id, id);
                CREATE TABLE IF NOT EXISTS chat_sessions (
                    project_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    pinned INTEGER NOT NULL DEFAULT 0,
                    deleted INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(project_id, chat_id, user_id)
                );
                CREATE INDEX IF NOT EXISTS idx_chat_sessions_user_project
                    ON chat_sessions(user_id, project_id, deleted, pinned, updated_at);
                """
            )
            user_version = connection.execute("PRAGMA user_version").fetchone()[0]
            if user_version < 1:
                connection.execute("PRAGMA user_version = 1")
            connection.commit()
        finally:
            connection.close()

    def get_or_create_workflow(
        self, thread_id: str, project_id: str, chat_id: str, user_id: str
    ) -> WorkflowRow:
        """Return the chat workflow row, allocating its scenario identity once."""
        self.initialize()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM scenario_workflow WHERE thread_id = ?", (thread_id,)
            ).fetchone()
            if row is None:
                now = _now()
                scenario_key = f"PRD_{uuid.uuid4().hex[:8].upper()}"
                scenario_id = scenario_key
                connection.execute(
                    """INSERT INTO scenario_workflow
                    (thread_id, project_id, chat_id, user_id, stage, locked, version,
                     scenario_id, scenario_key, created_at, updated_at)
                    VALUES (?, ?, ?, ?, 'EMPTY', 0, 1, ?, ?, ?, ?)""",
                    (
                        thread_id,
                        project_id,
                        chat_id,
                        user_id,
                        scenario_id,
                        scenario_key,
                        now,
                        now,
                    ),
                )
                self._event(
                    connection,
                    thread_id,
                    user_id,
                    "workflow_created",
                    None,
                    "EMPTY",
                    {
                        "scenario_id": scenario_id,
                        "scenario_key": scenario_key,
                    },
                )
                row = connection.execute(
                    "SELECT * FROM scenario_workflow WHERE thread_id = ?", (thread_id,)
                ).fetchone()
            connection.commit()
            return self._workflow(row)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get_workflow(self, thread_id: str) -> WorkflowRow | None:
        """Fetch one workflow row by its deterministic LangGraph thread ID."""
        self.initialize()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM scenario_workflow WHERE thread_id = ?", (thread_id,)
            ).fetchone()
        return self._workflow(row) if row else None

    @staticmethod
    def _workflow(row: sqlite3.Row) -> WorkflowRow:
        """Decode a workflow table row."""
        return WorkflowRow(
            thread_id=row["thread_id"],
            project_id=row["project_id"],
            chat_id=row["chat_id"],
            user_id=row["user_id"],
            stage=row["stage"],
            locked=bool(row["locked"]),
            version=row["version"],
            scenario_id=row["scenario_id"],
            scenario_key=row["scenario_key"],
            current_revision_id=row["current_revision_id"],
            deployed_revision_id=row["deployed_revision_id"],
            pending_card=(
                json.loads(row["pending_card_json"])
                if row["pending_card_json"]
                else None
            ),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def create_revision(
        self,
        thread_id: str,
        *,
        card: dict[str, Any] | None = None,
        stage: str = "EMPTY",
        created_by: str | None = None,
    ) -> Revision:
        """Create a revision, supersede the prior current revision, and enforce the cap."""
        self.initialize()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            workflow = connection.execute(
                "SELECT * FROM scenario_workflow WHERE thread_id = ?", (thread_id,)
            ).fetchone()
            if workflow is None:
                raise KeyError(f"Workflow not found: {thread_id}")
            if workflow["locked"]:
                raise WorkflowConflict(
                    "A deployed workflow cannot create another revision"
                )
            prior_row = self._revision_by_id(
                connection, workflow["current_revision_id"]
            )
            prior = self._revision(prior_row) if prior_row else None
            now = _now()
            rev_no = connection.execute(
                "SELECT COALESCE(MAX(rev_no), 0) + 1 FROM scenario_revision WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()[0]
            revision_id = uuid.uuid4().hex
            if prior and prior.status != "deployed":
                connection.execute(
                    """UPDATE scenario_revision SET status='history', superseded_at=?,
                    superseded_by_revision_id=?, version=version+1 WHERE revision_id=?""",
                    (now, revision_id, prior.revision_id),
                )
            new_card = (
                card
                if card is not None
                else (
                    json.loads(prior.card_json) if prior and prior.card_json else None
                )
            )
            connection.execute(
                """INSERT INTO scenario_revision
                (revision_id, thread_id, rev_no, status, stage, parent_revision_id,
                 card_json, metadata_json, metadata_hash, created_at, created_by,
                 restored_from_revision_id)
                VALUES (?, ?, ?, 'current', ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    revision_id,
                    thread_id,
                    rev_no,
                    stage,
                    prior.revision_id if prior else None,
                    (
                        json.dumps(new_card, ensure_ascii=False)
                        if new_card is not None
                        else None
                    ),
                    prior.metadata_json if prior else None,
                    prior.metadata_hash if prior else None,
                    now,
                    created_by,
                    None,
                ),
            )
            connection.execute(
                """UPDATE scenario_workflow SET current_revision_id=?, stage=?, version=version+1,
                updated_at=? WHERE thread_id=?""",
                (revision_id, stage, now, thread_id),
            )
            self._event(
                connection,
                thread_id,
                created_by or "system",
                "revision_created",
                prior.stage if prior else "EMPTY",
                stage,
                {
                    "revision_id": revision_id,
                    "rev_no": rev_no,
                    "parent_revision_id": prior.revision_id if prior else None,
                },
            )
            self._evict_on_connection(connection, thread_id)
            connection.commit()
            return self.get_revision(revision_id)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def supersede_to_history(self, thread_id: str, new_revision_id: str) -> None:
        """Mark the former current revision as history before selecting a new current one."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            workflow = connection.execute(
                "SELECT current_revision_id FROM scenario_workflow WHERE thread_id=?",
                (thread_id,),
            ).fetchone()
            if workflow is None:
                raise KeyError(thread_id)
            old_id = workflow["current_revision_id"]
            if old_id and old_id != new_revision_id:
                connection.execute(
                    "UPDATE scenario_revision SET status='history', superseded_at=?, superseded_by_revision_id=? WHERE revision_id=? AND status!='deployed'",
                    (_now(), new_revision_id, old_id),
                )
            connection.execute(
                "UPDATE scenario_workflow SET current_revision_id=?, version=version+1, updated_at=? WHERE thread_id=?",
                (new_revision_id, _now(), thread_id),
            )
            self._evict_on_connection(connection, thread_id)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def evict_over_cap(self, thread_id: str) -> list[str]:
        """Evict oldest history payloads beyond the configured cap."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            evicted = self._evict_on_connection(connection, thread_id)
            connection.commit()
            return evicted
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _evict_on_connection(
        self, connection: sqlite3.Connection, thread_id: str
    ) -> list[str]:
        """Apply history retention on an already-open transaction."""
        rows = connection.execute(
            "SELECT revision_id FROM scenario_revision WHERE thread_id=? AND status='history' ORDER BY rev_no DESC",
            (thread_id,),
        ).fetchall()
        evicted_ids = [row["revision_id"] for row in rows[self.history_cap :]]
        for revision_id in evicted_ids:
            connection.execute(
                """UPDATE scenario_revision SET status='evicted', evicted_at=?, card_json=NULL,
                signature_json=NULL, conflict_json=NULL, plan_md=NULL, shadow_json=NULL,
                metadata_json=NULL, version=version+1 WHERE revision_id=? AND status='history'""",
                (_now(), revision_id),
            )
            self._event(
                connection,
                thread_id,
                "system",
                "revision_evicted",
                None,
                None,
                {"revision_id": revision_id},
            )
        return evicted_ids

    def current(self, thread_id: str) -> Revision | None:
        """Return the current revision for one chat."""
        workflow = self.get_workflow(thread_id)
        return (
            self.get_revision(workflow.current_revision_id)
            if workflow and workflow.current_revision_id
            else None
        )

    def list_history(self, thread_id: str) -> list[Revision]:
        """Return history and tombstone revisions, newest first."""
        self.initialize()
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM scenario_revision WHERE thread_id=? AND status IN ('history','evicted') ORDER BY rev_no DESC",
                (thread_id,),
            ).fetchall()
        return [self._revision(row) for row in rows]

    def list_revisions(self, thread_id: str) -> list[Revision]:
        """Return all revisions, newest first."""
        self.initialize()
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM scenario_revision WHERE thread_id=? ORDER BY rev_no DESC",
                (thread_id,),
            ).fetchall()
        return [self._revision(row) for row in rows]

    def get_revision(self, revision_id: str | None) -> Revision | None:
        """Return a revision by its opaque ID."""
        if not revision_id:
            return None
        self.initialize()
        with self._connection() as connection:
            row = self._revision_by_id(connection, revision_id)
        return self._revision(row) if row else None

    def update_revision(
        self, revision_id: str, *, expected_version: int, **changes: Any
    ) -> Revision:
        """Update revision fields with a workflow-row optimistic lock."""
        allowed = {
            "card_json",
            "params_hash",
            "signature_json",
            "signature_hash",
            "conflict_json",
            "plan_md",
            "plan_hash",
            "plan_approved_at",
            "plan_approved_by",
            "shadow_json",
            "sql_hash",
            "result_approved_at",
            "result_approved_by",
            "metadata_json",
            "metadata_hash",
            "stage",
        }
        invalid = set(changes) - allowed
        if invalid:
            raise ValueError(f"Unsupported revision fields: {sorted(invalid)}")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT thread_id FROM scenario_revision WHERE revision_id=?",
                (revision_id,),
            ).fetchone()
            if row is None:
                raise KeyError(revision_id)
            workflow = connection.execute(
                "SELECT version, stage FROM scenario_workflow WHERE thread_id=?",
                (row["thread_id"],),
            ).fetchone()
            if workflow["version"] != expected_version:
                raise WorkflowConflict(
                    f"Expected workflow version {expected_version}; found {workflow['version']}"
                )
            assignments = [f"{key}=?" for key in changes]
            values = [
                self._serialize_field(key, value) for key, value in changes.items()
            ]
            assignments.append("version=version+1")
            connection.execute(
                f"UPDATE scenario_revision SET {', '.join(assignments)} WHERE revision_id=?",
                [*values, revision_id],
            )
            new_stage = changes.get("stage", workflow["stage"])
            connection.execute(
                "UPDATE scenario_workflow SET version=version+1, stage=?, updated_at=? WHERE thread_id=?",
                (new_stage, _now(), row["thread_id"]),
            )
            self._event(
                connection,
                row["thread_id"],
                "system",
                "revision_updated",
                workflow["stage"],
                new_stage,
                {"revision_id": revision_id, "fields": sorted(changes)},
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_revision(revision_id)

    @staticmethod
    def _serialize_field(key: str, value: Any) -> Any:
        """Serialize structured JSON fields while preserving scalar fields."""
        json_fields = {
            "card_json",
            "signature_json",
            "conflict_json",
            "shadow_json",
            "metadata_json",
        }
        if key in json_fields and value is not None and not isinstance(value, str):
            return json.dumps(value, ensure_ascii=False)
        return value

    def set_stage(
        self,
        thread_id: str,
        stage: str,
        *,
        actor: str = "system",
        action: str = "stage_changed",
    ) -> Revision | None:
        """Advance workflow and current revision stage after an observed tool success."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT stage,current_revision_id,version FROM scenario_workflow WHERE thread_id=?",
                (thread_id,),
            ).fetchone()
            if row is None:
                return None
            if row["current_revision_id"]:
                connection.execute(
                    "UPDATE scenario_revision SET stage=?, version=version+1 WHERE revision_id=?",
                    (stage, row["current_revision_id"]),
                )
            connection.execute(
                "UPDATE scenario_workflow SET stage=?,version=version+1,updated_at=? WHERE thread_id=?",
                (stage, _now(), thread_id),
            )
            self._event(
                connection,
                thread_id,
                actor,
                action,
                row["stage"],
                stage,
                {"revision_id": row["current_revision_id"]},
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.current(thread_id)

    def ensure_initial_revision(
        self,
        thread_id: str,
        *,
        created_by: str | None = None,
        card: dict[str, Any] | None = None,
    ) -> Revision:
        """Create revision one on the first successful intent extraction."""
        current = self.current(thread_id)
        if current:
            return current
        return self.create_revision(
            thread_id, card=card, stage="INTAKE", created_by=created_by
        )

    def set_pending_card(self, thread_id: str, card: dict[str, Any] | None) -> None:
        """Set or clear the chat's single pending card slot."""
        self.initialize()
        connection = self._connect()
        try:
            connection.execute(
                "UPDATE scenario_workflow SET pending_card_json=?,version=version+1,updated_at=? WHERE thread_id=?",
                (
                    json.dumps(card, ensure_ascii=False) if card is not None else None,
                    _now(),
                    thread_id,
                ),
            )
            connection.commit()
        finally:
            connection.close()

    def mark_deployed(self, thread_id: str, revision_id: str) -> None:
        """Lock the chat and mark its current revision deployed."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT current_revision_id,stage FROM scenario_workflow WHERE thread_id=?",
                (thread_id,),
            ).fetchone()
            if current is None:
                raise KeyError(thread_id)
            if current["current_revision_id"] != revision_id:
                raise WorkflowConflict("Cannot deploy a stale revision")
            connection.execute(
                "UPDATE scenario_revision SET status='deployed',stage='DEPLOYED',version=version+1 WHERE revision_id=?",
                (revision_id,),
            )
            connection.execute(
                "UPDATE scenario_workflow SET locked=1,stage='DEPLOYED',deployed_revision_id=?,version=version+1,updated_at=? WHERE thread_id=?",
                (revision_id, _now(), thread_id),
            )
            self._event(
                connection,
                thread_id,
                "system",
                "deployed",
                current["stage"],
                "DEPLOYED",
                {"revision_id": revision_id},
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def mark_locked(self, thread_id: str) -> None:
        """Set the immutable deployed lock for a chat."""
        self.initialize()
        with self._connection() as connection:
            connection.execute(
                "UPDATE scenario_workflow SET locked=1,stage='DEPLOYED',version=version+1 WHERE thread_id=?",
                (thread_id,),
            )

    def add_event(
        self,
        thread_id: str,
        actor: str,
        action: str,
        *,
        from_stage: str | None = None,
        to_stage: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Append an immutable workflow event."""
        self.initialize()
        with self._connection() as connection:
            self._event(
                connection, thread_id, actor, action, from_stage, to_stage, detail or {}
            )

    @staticmethod
    def _event(
        connection: sqlite3.Connection,
        thread_id: str,
        actor: str,
        action: str,
        from_stage: str | None,
        to_stage: str | None,
        detail: dict[str, Any],
    ) -> None:
        """Insert an event inside the caller's transaction."""
        connection.execute(
            "INSERT INTO workflow_events(thread_id,ts,actor,action,from_stage,to_stage,detail_json) VALUES(?,?,?,?,?,?,?)",
            (
                thread_id,
                _now(),
                actor,
                action,
                from_stage,
                to_stage,
                json.dumps(detail, ensure_ascii=False),
            ),
        )

    def list_events(self, thread_id: str) -> list[dict[str, Any]]:
        """Read ordered event rows for a workflow."""
        self.initialize()
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM workflow_events WHERE thread_id=? ORDER BY id",
                (thread_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_superseded_revision_numbers(self, thread_id: str) -> dict[str, int]:
        """Return revision IDs that are history/evicted and their display numbers."""
        return {
            revision.revision_id: revision.rev_no
            for revision in self.list_history(thread_id)
        }

    @staticmethod
    def _revision_by_id(
        connection: sqlite3.Connection, revision_id: str | None
    ) -> sqlite3.Row | None:
        """Read a revision row inside an open connection."""
        if not revision_id:
            return None
        return connection.execute(
            "SELECT * FROM scenario_revision WHERE revision_id=?", (revision_id,)
        ).fetchone()

    @staticmethod
    def _revision(row: sqlite3.Row) -> Revision:
        """Decode a revision table row."""
        return Revision(**{key: row[key] for key in Revision.__dataclass_fields__})

    def workflow_for_owner(
        self, project_id: str, chat_id: str, user_id: str
    ) -> WorkflowRow | None:
        """Find a workflow by the composite chat identity."""
        self.initialize()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM scenario_workflow WHERE project_id=? AND chat_id=? AND user_id=?",
                (project_id, chat_id, user_id),
            ).fetchone()
        return self._workflow(row) if row else None

    def workflow_for_chat(self, project_id: str, chat_id: str) -> WorkflowRow | None:
        """Find an existing chat regardless of owner for authorization checks."""
        self.initialize()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM scenario_workflow WHERE project_id=? AND chat_id=? LIMIT 1",
                (project_id, chat_id),
            ).fetchone()
        return self._workflow(row) if row else None

    def upsert_chat_session(
        self, project_id: str, chat_id: str, user_id: str, title: str
    ) -> None:
        """Upsert sidebar metadata keyed by project, chat, and owner."""
        now = _now()
        self.initialize()
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO chat_sessions(project_id,chat_id,user_id,title,pinned,deleted,created_at,updated_at)
                VALUES(?,?,?,?,0,0,?,?) ON CONFLICT(project_id,chat_id,user_id)
                DO UPDATE SET updated_at=excluded.updated_at,deleted=0""",
                (project_id, chat_id, user_id, title or "New Chat", now, now),
            )

    def list_chat_sessions(
        self, user_id: str, project_id: str | None = None
    ) -> list[dict[str, Any]]:
        """List visible chats for one user, optionally within one project."""
        self.initialize()
        with self._connection() as connection:
            if project_id and project_id not in {"0", "None"}:
                rows = connection.execute(
                    "SELECT * FROM chat_sessions WHERE user_id=? AND project_id=? AND deleted=0 ORDER BY pinned DESC,updated_at DESC",
                    (user_id, project_id),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM chat_sessions WHERE user_id=? AND deleted=0 ORDER BY pinned DESC,updated_at DESC",
                    (user_id,),
                ).fetchall()
        return [
            {
                "chat_id": r["chat_id"],
                "project_id": r["project_id"],
                "title": r["title"],
                "updated_at": r["updated_at"],
                "is_pinned": bool(r["pinned"]),
                "is_deleted": bool(r["deleted"]),
            }
            for r in rows
        ]

    def update_chat_session(
        self, project_id: str, chat_id: str, user_id: str, **changes: Any
    ) -> bool | None:
        """Update supported sidebar fields, returning None when the row is absent."""
        fields = {"title": "title", "is_deleted": "deleted", "is_pinned": "pinned"}
        assignments = [f"{fields[key]}=?" for key in changes]
        values = [
            int(value) if key in {"is_deleted", "is_pinned"} else value
            for key, value in changes.items()
        ]
        if not assignments:
            return False
        assignments.append("updated_at=?")
        values.append(_now())
        with self._connection() as connection:
            cursor = connection.execute(
                f"UPDATE chat_sessions SET {', '.join(assignments)} WHERE project_id=? AND chat_id=? AND user_id=?",
                [*values, project_id, chat_id, user_id],
            )
        return bool(cursor.rowcount)

    def toggle_chat_pin(
        self, project_id: str, chat_id: str, user_id: str
    ) -> bool | None:
        """Toggle pin state for any existing sidebar row, including soft-deleted chats."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT pinned FROM chat_sessions WHERE project_id=? AND chat_id=? AND user_id=?",
                (project_id, chat_id, user_id),
            ).fetchone()
            if row is None:
                return None
            new_pin = 1 - row["pinned"]
            connection.execute(
                "UPDATE chat_sessions SET pinned=?,updated_at=? WHERE project_id=? AND chat_id=? AND user_id=?",
                (new_pin, _now(), project_id, chat_id, user_id),
            )
        return bool(new_pin)


class CardAuditRepo:
    """Append-only card audit interface backed by the workflow SQLite database."""

    def __init__(self, workflow_repo: WorkflowRepo) -> None:
        """Bind the audit repository to a workflow store."""
        self.workflow_repo = workflow_repo

    def append(
        self,
        *,
        request_id: str,
        thread_id: str,
        revision_id: str | None,
        user_id: str,
        user_role: str,
        source: str,
        outcome: str,
        rows: list[dict[str, Any]],
        shown_at: str | None = None,
        resolved_at: str | None = None,
    ) -> None:
        """Append one permanent audit row; duplicate request IDs are rejected."""
        self.workflow_repo.initialize()
        with self.workflow_repo._connection() as connection:
            connection.execute(
                """INSERT INTO param_card_audit
                (request_id,thread_id,revision_id,user_id,user_role,source,shown_at,resolved_at,outcome,rows_json)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    request_id,
                    thread_id,
                    revision_id,
                    user_id,
                    user_role,
                    source,
                    shown_at or _now(),
                    resolved_at,
                    outcome,
                    json.dumps(rows, ensure_ascii=False),
                ),
            )

    def get(self, request_id: str) -> dict[str, Any] | None:
        """Read an audit row by its unique request ID."""
        self.workflow_repo.initialize()
        with self.workflow_repo._connection() as connection:
            row = connection.execute(
                "SELECT * FROM param_card_audit WHERE request_id=?", (request_id,)
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["rows"] = json.loads(result.pop("rows_json"))
        return result

    def list_for_thread(self, thread_id: str) -> list[dict[str, Any]]:
        """Read append-only audit rows for one thread."""
        self.workflow_repo.initialize()
        with self.workflow_repo._connection() as connection:
            rows = connection.execute(
                "SELECT request_id FROM param_card_audit WHERE thread_id=? ORDER BY shown_at",
                (thread_id,),
            ).fetchall()
        return [self.get(row["request_id"]) for row in rows]
