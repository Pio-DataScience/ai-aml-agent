"""Own-chat workflow reads and typed actions with P2 identity checks."""

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from services.aml_builder.web.api.dependencies import (
    Identity,
    assert_identity_matches,
    require_identity,
)
from services.aml_builder.web.domain.policy import authorize
from services.aml_builder.web.workflow.models import Revision, WorkflowRow
from services.aml_builder.web.workflow.repo import WorkflowRepo, WorkflowConflict
from services.aml_builder.web.workflow.runtime import get_workflow_repo
from services.aml_builder.web.workflow.guards import guard_tool
from services.aml_builder.web.services.config_loader import load_catalogs
from services.aml_builder.web.services.settings import settings
from services.aml_builder.web.domain.deploy_service import deployment_blockers
from services.aml_builder.web.domain.policy import get_role
from services.aml_builder.web.domain.param_gaps import validate_confirmed_values
from services.aml_builder.web.domain.hashing import stable_hash
from services.aml_builder.web.workflow.cards import commit_confirmed_card, resolved_card_rows

router = APIRouter(tags=["Workflow"])


class ApprovalRequest(BaseModel):
    """Typed approval of one immutable current-revision artifact."""

    revision_id: str
    expected_hash: str = Field(min_length=1)


class ConfirmParamsRequest(BaseModel):
    """Explicit values or default acceptances for the current parameter card."""

    request_id: str = Field(min_length=1)
    values: dict[str, Any] = Field(default_factory=dict)
    accept_defaults: bool = False
    source: Literal["ui", "api"] = "api"


class DiscardCardRequest(BaseModel):
    """Discard one exact pending-card request."""

    request_id: str = Field(min_length=1)
    source: Literal["ui", "api"] = "api"


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


def _card_values(card: dict[str, Any], request: ConfirmParamsRequest) -> tuple[dict[str, Any], list[str], list[str], list[dict[str, str]]]:
    """Expand explicit card inputs and evaluate required, ambiguous, and invalid rows."""
    rows = card.get("rows", [])
    values: dict[str, Any] = {}
    for row in rows:
        if row.get("read_only"):
            continue
        key = str(row.get("key", ""))
        submitted = request.values.get(key)
        if submitted is not None:
            if isinstance(submitted, dict) and "value" in submitted:
                resolved = dict(submitted)
            else:
                resolved = {"value": submitted}
            resolved.setdefault("choice", "user_input")
            resolved.setdefault("provenance", "user_input")
            values[key] = resolved
        elif request.accept_defaults and row.get("status") in {"defaulted", "inferred"} and row.get("recommended_value") is not None:
            values[key] = {
                "value": row["recommended_value"],
                "choice": "accepted_default",
                "provenance": row.get("provenance", "assumed_default"),
            }
    catalogs = load_catalogs(settings.CATALOG_DIR)
    missing, unresolved, invalid = validate_confirmed_values(rows, values, catalogs.params)
    if request.accept_defaults:
        for row in rows:
            if row.get("read_only") or row.get("key") in values:
                continue
            if row.get("status") == "provided" and row.get("value") is not None:
                key = str(row["key"])
                values[key] = {
                    "value": row["value"], "choice": "provided",
                    "provenance": row.get("provenance", "stated"),
                }
        missing, unresolved, invalid = validate_confirmed_values(rows, values, catalogs.params)
    for key, submitted in values.items():
        if submitted.get("choice") not in {"accepted_default", "user_input", "provided", "system_default"}:
            invalid.append({"key": key, "reason": "Unknown value choice."})
    return values, missing, unresolved, invalid


def _require_card_action(
    project_id: str, chat_id: str, user_id: str, request_id: str,
    action: str, identity: Identity, repo: WorkflowRepo,
) -> WorkflowRow:
    """Check ownership, creator permission, and workflow card guards."""
    workflow_row = _owned_workflow(project_id, chat_id, user_id, identity, repo)
    if not authorize(identity.user_id, "scenario.create", {"user_id": workflow_row.user_id}, user_email=identity.user_email):
        raise HTTPException(status_code=403, detail={"error_code": "PERMISSION_DENIED", "message": "Permission denied."})
    current = repo.current(workflow_row.thread_id)
    verdict = guard_tool(
        repo, workflow_row.thread_id, action,
        current.revision_id if current else None,
        authorized=True, request_id=request_id,
    )
    if not verdict.allowed:
        code = verdict.code.value if verdict.code else "STAGE_NOT_ALLOWED"
        raise HTTPException(status_code=409, detail={"error_code": code, "message": verdict.message})
    return workflow_row


@router.post("/workflow/{project_id}/{chat_id}/{user_id}/actions/confirm_params")
def confirm_params(
    project_id: str, chat_id: str, user_id: str, request: ConfirmParamsRequest,
    identity: Identity = Depends(require_identity),
    repo: WorkflowRepo = Depends(get_workflow_repo),
) -> dict[str, Any]:
    """Resolve the exact pending card, persist a confirmed revision, and render its plan."""
    existing_workflow = _owned_workflow(project_id, chat_id, user_id, identity, repo)
    if not authorize(identity.user_id, "scenario.create", {"user_id": existing_workflow.user_id}, user_email=identity.user_email):
        raise HTTPException(status_code=403, detail={"error_code": "PERMISSION_DENIED", "message": "Permission denied."})
    prior = repo.get_card_confirmation(request.request_id)
    request_hash = stable_hash({
        "values": request.values,
        "accept_defaults": request.accept_defaults,
    })
    if prior is not None:
        if prior.get("confirmation_hash") != request_hash:
            raise HTTPException(status_code=409, detail={"error_code": "CARD_STALE", "message": "This card was already confirmed with different values."})
        revision = repo.get_revision(prior.get("revision_id"))
        if revision is None:
            raise HTTPException(status_code=409, detail={"error_code": "CARD_STALE", "message": "The confirmed revision is no longer available."})
        response = get_workflow_state(project_id, chat_id, user_id, identity, repo, include_current_revision=True)
        response.update({
            "revision_summary": revision.payload_free_summary(),
            "plan_md": revision.plan_md,
            "stage_changed": {"revision_id": revision.revision_id, "stage": revision.stage},
            "carry_over": prior.get("carry_over") or {"kept": [], "dropped": []},
            "history_count": repo.history_count(existing_workflow.thread_id),
            "action_message": f"[ACTION] card_confirmed revision {revision.rev_no}",
        })
        return response
    workflow_row = _require_card_action(
        project_id, chat_id, user_id, request.request_id, "confirm_params", identity, repo
    )
    card = workflow_row.pending_card
    if card is None:
        raise HTTPException(status_code=409, detail={"error_code": "CARD_STALE", "message": "The parameter card is stale."})
    values, missing, unresolved, invalid = _card_values(card, request)
    if missing or unresolved:
        repo.add_event(
            workflow_row.thread_id, identity.user_id, "card_refused",
            detail={"request_id": request.request_id, "missing": missing, "unresolved": unresolved},
        )
        raise HTTPException(status_code=422, detail={
            "error_code": "PARAMS_INCOMPLETE", "request_id": request.request_id,
            "missing": missing, "unresolved": unresolved,
        })
    if invalid:
        repo.add_event(
            workflow_row.thread_id, identity.user_id, "card_refused",
            detail={"request_id": request.request_id, "missing": [], "unresolved": [], "reasons": invalid},
        )
        raise HTTPException(status_code=422, detail={
            "error_code": "PARAMS_INVALID", "request_id": request.request_id,
            "reasons": invalid,
        })
    try:
        result = commit_confirmed_card(
            repo, workflow_row.thread_id, request.request_id, values,
            actor=identity.user_id,
            role=get_role(identity.user_id, identity.user_email),
            source=request.source,
            resolved_rows=resolved_card_rows(card.get("rows", []), values),
            confirmation_hash=request_hash,
        )
    except WorkflowConflict as exc:
        raise HTTPException(status_code=409, detail={"error_code": exc.code, "message": str(exc)}) from exc
    response = get_workflow_state(
        project_id, chat_id, user_id, identity, repo, include_current_revision=True
    )
    revision = result["revision"]
    response.update({
        "revision_summary": revision.payload_free_summary(),
        "plan_md": revision.plan_md,
        "stage_changed": {"revision_id": revision.revision_id, "stage": revision.stage},
        "carry_over": result["carry_over"],
        "history_count": result["history_count"],
        "action_message": result["action_message"],
    })
    return response


@router.post("/workflow/{project_id}/{chat_id}/{user_id}/actions/discard_card")
def discard_card(
    project_id: str, chat_id: str, user_id: str, request: DiscardCardRequest,
    identity: Identity = Depends(require_identity),
    repo: WorkflowRepo = Depends(get_workflow_repo),
) -> dict[str, Any]:
    """Discard and permanently audit one pending card."""
    workflow_row = _require_card_action(
        project_id, chat_id, user_id, request.request_id, "discard_card", identity, repo
    )
    card = workflow_row.pending_card or {}
    try:
        repo.resolve_pending_card(
            workflow_row.thread_id, request.request_id,
            outcome="discarded", actor=identity.user_id,
            role=get_role(identity.user_id, identity.user_email),
            source=request.source,
            revision_id=card.get("base_revision_id"),
        )
    except WorkflowConflict as exc:
        raise HTTPException(status_code=409, detail={"error_code": exc.code, "message": str(exc)}) from exc
    repo.set_stage(
        workflow_row.thread_id,
        card.get("base_stage") or (repo.current(workflow_row.thread_id).stage if repo.current(workflow_row.thread_id) else "EMPTY"),
        actor=identity.user_id,
        action="discard_card_stage_restored",
    )
    return get_workflow_state(
        project_id, chat_id, user_id, identity, repo, include_current_revision=True
    )


@router.get("/workflow/{project_id}/{chat_id}/{user_id}/state")
def get_workflow_state(
    project_id: str,
    chat_id: str,
    user_id: str,
    identity: Identity = Depends(require_identity),
    repo: WorkflowRepo = Depends(get_workflow_repo),
    include_current_revision: bool = False,
) -> dict[str, Any]:
    """Return current state, pending card, and payload-free history summaries."""
    workflow = _owned_workflow(project_id, chat_id, user_id, identity, repo)
    current = repo.current(workflow.thread_id)
    history = repo.list_history(workflow.thread_id)
    user_can_create = authorize(identity.user_id, "scenario.create", user_email=identity.user_email)
    blockers = deployment_blockers(workflow, current, user_can_create=user_can_create, approval_enforcement=load_catalogs(settings.CATALOG_DIR).policy.workflow.enforcement.approvals)
    current_data = ({key: getattr(current, key) for key in current.__dataclass_fields__} if include_current_revision and current else _revision_summary(current))
    return {
        "thread_id": workflow.thread_id,
        "stage": workflow.stage,
        "locked": workflow.locked,
        "version": workflow.version,
        "scenario_id": workflow.scenario_id,
        "scenario_key": workflow.scenario_key,
        "current_revision": current_data,
        "can_deploy": not blockers,
        "blockers": blockers,
        "pending_card": workflow.pending_card,
        "history": [revision.payload_free_summary() for revision in history],
    }


def _approve(
    gate: str, project_id: str, chat_id: str, user_id: str, request: ApprovalRequest,
    identity: Identity, repo: WorkflowRepo,
) -> dict[str, Any]:
    """Check shared workflow guards and persist one typed approval."""
    workflow_row = _owned_workflow(project_id, chat_id, user_id, identity, repo)
    if not authorize(identity.user_id, "scenario.create", {"user_id": workflow_row.user_id}, user_email=identity.user_email):
        raise HTTPException(status_code=403, detail={"error_code": "PERMISSION_DENIED", "message": "Permission denied."})
    action = "approve_plan" if gate == "plan" else "approve_result"
    current = repo.current(workflow_row.thread_id)
    if current is None or current.revision_id != request.revision_id:
        raise HTTPException(status_code=409, detail={"error_code": "REVISION_STALE", "message": "The revision is no longer current."})
    expected = current.plan_hash if gate == "plan" else current.sql_hash
    if expected != request.expected_hash:
        raise HTTPException(status_code=409, detail={"error_code": "HASH_MISMATCH", "message": "The artifact hash changed. Refresh state and try again."})
    approved = current.plan_approved_at if gate == "plan" else current.result_approved_at
    if approved:
        return get_workflow_state(project_id, chat_id, user_id, identity, repo, include_current_revision=True)
    verdict = guard_tool(repo, workflow_row.thread_id, action, request.revision_id, authorized=True)
    if not verdict.allowed:
        code = verdict.code.value if verdict.code else "STAGE_NOT_ALLOWED"
        raise HTTPException(status_code=409, detail={"error_code": code, "message": verdict.message})
    try:
        repo.approve_revision(workflow_row.thread_id, request.revision_id, approval=gate, actor=identity.user_id, role=get_role(identity.user_id, identity.user_email), expected_hash=request.expected_hash)
    except WorkflowConflict as exc:
        raise HTTPException(status_code=409, detail={"error_code": exc.code, "message": str(exc)}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"error_code": "HASH_MISMATCH", "message": str(exc)}) from exc
    return get_workflow_state(project_id, chat_id, user_id, identity, repo, include_current_revision=True)


@router.post("/workflow/{project_id}/{chat_id}/{user_id}/actions/approve_plan")
def approve_plan(project_id: str, chat_id: str, user_id: str, request: ApprovalRequest, identity: Identity = Depends(require_identity), repo: WorkflowRepo = Depends(get_workflow_repo)) -> dict[str, Any]:
    """Approve the current plan using its expected hash."""
    return _approve("plan", project_id, chat_id, user_id, request, identity, repo)


@router.post("/workflow/{project_id}/{chat_id}/{user_id}/actions/approve_result")
def approve_result(project_id: str, chat_id: str, user_id: str, request: ApprovalRequest, identity: Identity = Depends(require_identity), repo: WorkflowRepo = Depends(get_workflow_repo)) -> dict[str, Any]:
    """Approve a successful shadow result using its SQL hash."""
    return _approve("result", project_id, chat_id, user_id, request, identity, repo)


@router.get("/ui/copy")
def get_ui_copy(identity: Identity = Depends(require_identity)) -> dict[str, Any]:
    """Return editable user-facing copy to an authenticated UI client."""
    if not authorize(identity.user_id, "chat.read", {"user_id": identity.user_id}, user_email=identity.user_email):
        raise HTTPException(status_code=403, detail="Permission denied.")
    return load_catalogs(settings.CATALOG_DIR).ui_copy.model_dump()


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
