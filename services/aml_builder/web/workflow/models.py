"""Typed records persisted by the workflow repository."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class WorkflowRow:
    """One chat's current workflow identity and state."""

    thread_id: str
    project_id: str
    chat_id: str
    user_id: str
    stage: str
    locked: bool
    version: int
    scenario_id: str
    scenario_key: str
    current_revision_id: str | None
    deployed_revision_id: str | None
    pending_card: dict[str, Any] | None
    created_at: str
    updated_at: str
    parent_scenario_id: str | None = None
    parent_version: int | None = None


@dataclass(frozen=True)
class Revision:
    """A revision row, including JSON payloads for its trio."""

    revision_id: str
    thread_id: str
    rev_no: int
    status: str
    stage: str
    parent_revision_id: str | None
    restored_from_revision_id: str | None
    card_json: str | None
    params_hash: str | None
    signature_json: str | None
    signature_hash: str | None
    conflict_json: str | None
    plan_md: str | None
    plan_hash: str | None
    plan_approved_at: str | None
    plan_approved_by: str | None
    shadow_json: str | None
    sql_hash: str | None
    result_approved_at: str | None
    result_approved_by: str | None
    metadata_json: str | None
    metadata_hash: str | None
    created_at: str
    created_by: str | None
    superseded_at: str | None
    superseded_by_revision_id: str | None
    evicted_at: str | None
    version: int

    def payload_free_summary(self) -> dict[str, Any]:
        """Return IDs and stage data without the revision's heavy payloads."""
        return {
            "revision_id": self.revision_id,
            "rev_no": self.rev_no,
            "status": self.status,
            "stage": self.stage,
            "parent_revision_id": self.parent_revision_id,
            "created_at": self.created_at,
            "superseded_at": self.superseded_at,
            "evicted_at": self.evicted_at,
        }


@dataclass(frozen=True)
class PendingCard:
    """One pending parameter card for a chat."""

    request_id: str
    rows: list[dict[str, Any]]
    base_revision_id: str | None
    created_at: str
