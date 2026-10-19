"""Read-only workflow and revision APIs with P2 identity ownership checks."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from services.aml_builder.web.api.dependencies import (
    Identity,
    assert_identity_matches,
    require_identity,
)
from services.aml_builder.web.domain.policy import authorize
from services.aml_builder.web.workflow.models import Revision, WorkflowRow
from services.aml_builder.web.workflow.repo import WorkflowRepo
from services.aml_builder.web.workflow.runtime import get_workflow_repo

router = APIRouter(tags=["Workflow"])


def _project_id(value: str) -> str:
    """Normalize legacy no-project URL values to the chat thread convention."""
    return value if value not in {"0", "None"} else "no_project"


def _owned_workflow(
    project_id: str, chat_id: str, user_id: str, identity: Identity, repo: WorkflowRepo
) -> WorkflowRow:
    """Validate request identity and ensure the chat belongs to the caller."""
    assert_identity_matches(identity, path_user_id=user_id, path_project_id=project_id)
    project = _project_id(project_id)
    workflow = repo.workflow_for_owner(project, chat_id, user_id)
    if workflow is None:
        other_owner = repo.workflow_for_chat(project, chat_id)
        if other_owner and not authorize(
            identity.user_id,
            "chat.read",
            {"user_id": other_owner.user_id},
            user_email=identity.user_email,
        ):
            raise HTTPException(status_code=403, detail="Permission denied.")
        raise HTTPException(status_code=404, detail="Workflow not found.")
    if not authorize(
        identity.user_id,
        "chat.read",
        {"user_id": workflow.user_id},
        user_email=identity.user_email,
    ):
        raise HTTPException(status_code=403, detail="Permission denied.")
    return workflow


def _revision_summary(revision: Revision | None) -> dict[str, Any] | None:
    """Build the payload-free current revision summary."""
    return revision.payload_free_summary() if revision else None


@router.get("/workflow/{project_id}/{chat_id}/{user_id}/state")
def get_workflow_state(
    project_id: str,
    chat_id: str,
    user_id: str,
    identity: Identity = Depends(require_identity),
    repo: WorkflowRepo = Depends(get_workflow_repo),
) -> dict[str, Any]:
    """Return current state, pending card, and payload-free history summaries."""
    workflow = _owned_workflow(project_id, chat_id, user_id, identity, repo)
    current = repo.current(workflow.thread_id)
    history = repo.list_history(workflow.thread_id)
    return {
        "thread_id": workflow.thread_id,
        "stage": workflow.stage,
        "locked": workflow.locked,
        "version": workflow.version,
        "scenario_id": workflow.scenario_id,
        "scenario_key": workflow.scenario_key,
        "current_revision": _revision_summary(current),
        "pending_card": workflow.pending_card,
        "history": [revision.payload_free_summary() for revision in history],
    }


@router.get("/workflow/{project_id}/{chat_id}/{user_id}/revisions")
def list_workflow_revisions(
    project_id: str,
    chat_id: str,
    user_id: str,
    identity: Identity = Depends(require_identity),
    repo: WorkflowRepo = Depends(get_workflow_repo),
) -> dict[str, Any]:
    """List current and historical revision summaries without their payloads."""
    workflow = _owned_workflow(project_id, chat_id, user_id, identity, repo)
    return {
        "revisions": [
            revision.payload_free_summary()
            for revision in repo.list_revisions(workflow.thread_id)
        ]
    }


@router.get("/workflow/{project_id}/{chat_id}/{user_id}/revisions/{revision_id}")
def get_workflow_revision(
    project_id: str,
    chat_id: str,
    user_id: str,
    revision_id: str,
    identity: Identity = Depends(require_identity),
    repo: WorkflowRepo = Depends(get_workflow_repo),
) -> dict[str, Any]:
    """Read one full revision after verifying chat ownership."""
    workflow = _owned_workflow(project_id, chat_id, user_id, identity, repo)
    revision = repo.get_revision(revision_id)
    if revision is None or revision.thread_id != workflow.thread_id:
        raise HTTPException(status_code=404, detail="Revision not found.")
    return {key: getattr(revision, key) for key in revision.__dataclass_fields__}
