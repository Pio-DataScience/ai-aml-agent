"""Revision operations exposed by the workflow repository."""

from typing import Any

from services.aml_builder.web.workflow.models import Revision
from services.aml_builder.web.workflow.repo import WorkflowRepo


def create_revision(
    repo: WorkflowRepo,
    thread_id: str,
    *,
    card: dict[str, Any] | None = None,
    stage: str = "EMPTY",
    created_by: str | None = None,
) -> Revision:
    """Create a current revision and move the prior one into history."""
    return repo.create_revision(
        thread_id, card=card, stage=stage, created_by=created_by
    )


def supersede_to_history(
    repo: WorkflowRepo, thread_id: str, new_revision_id: str
) -> None:
    """Mark the previous current revision as history."""
    repo.supersede_to_history(thread_id, new_revision_id)


def evict_over_cap(repo: WorkflowRepo, thread_id: str) -> list[str]:
    """Evict oldest undeployed history payloads beyond the configured cap."""
    return repo.evict_over_cap(thread_id)


def current(repo: WorkflowRepo, thread_id: str) -> Revision | None:
    """Return the current revision for a chat."""
    return repo.current(thread_id)


def list_history(repo: WorkflowRepo, thread_id: str) -> list[Revision]:
    """Return non-current revision history, newest first."""
    return repo.list_history(thread_id)
