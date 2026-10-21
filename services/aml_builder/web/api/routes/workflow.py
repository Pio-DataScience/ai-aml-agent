"""Own-chat workflow reads and typed actions with P2 identity checks."""

import json
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
from services.aml_builder.web.services.conflict_check import make_conflict_checker
from services.aml_builder.web.services.plan_renderer import render_plan
from services.aml_builder.web.workflow.cards import (
    commit_confirmed_card,
    create_revision_card,
    resolved_card_rows,
)
from services.aml_builder.web.adapters.oracle_repo import OracleRegistryRepo
from services.aml_builder.web.domain.versioning_service import VersioningError, VersioningService

router = APIRouter(tags=["Workflow"])


class ApprovalRequest(BaseModel):
    """Typed approval of one immutable current-revision artifact."""

    revision_id: str
    expected_hash: str = Field(min_length=1)
    message_position: int | None = Field(default=None, ge=0)


class ConfirmParamsRequest(BaseModel):
    """Explicit values or default acceptances for the current parameter card."""

    request_id: str = Field(min_length=1)
    values: dict[str, Any] = Field(default_factory=dict)
    accept_defaults: bool = False
    message_position: int | None = Field(default=None, ge=0)
    source: Literal["ui", "api"] = "api"


class DiscardCardRequest(BaseModel):
    """Discard one exact pending-card request."""

    request_id: str = Field(min_length=1)
    source: Literal["ui", "api"] = "api"


class ConflictAcknowledgementRequest(BaseModel):
    """Acknowledge a specific warning report for the current revision."""

    revision_id: str = Field(min_length=1)
    conflict_hash: str = Field(min_length=1)
    choice: Literal["proceed", "cancel"]
    reason: str | None = None


class CreateNewVersionRequest(BaseModel):
    """Select the deployed version that should seed a new version chat."""

    scenario_id: str = Field(min_length=1)


class RollbackRequest(BaseModel):
    """Request a guarded rollback to one superseded version."""

    scenario_id: str = Field(min_length=1)
    expected_active_version: int = Field(ge=1)
    reason: str = Field(min_length=5, max_length=500)
    acknowledge_warnings: bool = False


class RetireRequest(BaseModel):
    """Request soft retirement of one active version."""

    scenario_id: str = Field(min_length=1)
    reason: str = Field(min_length=5, max_length=500)


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


def _versioning_service(identity: Identity, repo: WorkflowRepo) -> VersioningService:
    """Build P9 operations with the current identity and P8 read-only conflict gate."""
    checker = make_conflict_checker(repo)

    def check_rollback_signature(signature: dict[str, Any], scenario_id: str) -> dict[str, Any]:
        target = OracleRegistryRepo().get_version(scenario_id) or {}
        active = OracleRegistryRepo().latest_active_version(str(target.get("scenario_key") or ""))
        excluded = {str(active["scenario_id"])} if active else set()
        return checker.check_signature(
            signature,
            scenario_key=str(target.get("scenario_key") or ""),
            exclude_scenario_ids=excluded,
        )

    return VersioningService(
        registry_repo=OracleRegistryRepo(),
        workflow_repo=repo,
        authorizer=lambda user_id, action: authorize(
            user_id, action, user_email=identity.user_email
        ),
        conflict_checker=check_rollback_signature,
    )


def _versioning_http_error(exc: VersioningError) -> HTTPException:
    """Map a typed versioning refusal to a structured HTTP error."""
    status_code = 403 if exc.code == "PERMISSION_DENIED" else 404 if exc.code == "VERSION_NOT_FOUND" else 409
    detail: dict[str, Any] = {"error_code": exc.code, "message": str(exc)}
    if exc.details is not None:
        detail["details"] = exc.details
    return HTTPException(status_code=status_code, detail=detail)


def _revision_summary(revision: Revision | None) -> dict[str, Any] | None:
    """Build the payload-free current revision summary."""
    return revision.payload_free_summary() if revision else None


def _card_values(
    card: dict[str, Any], request: ConfirmParamsRequest
) -> tuple[dict[str, Any], list[str], list[str], list[dict[str, str]]]:
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
        elif (
            request.accept_defaults
            and row.get("status") in {"defaulted", "inferred"}
            and row.get("recommended_value") is not None
        ):
            values[key] = {
                "value": row["recommended_value"],
                "choice": "accepted_default",
                "provenance": row.get("provenance", "assumed_default"),
            }
        elif row.get("kind") == "filter" and row.get("status") == "resolved":
            values[key] = {
                "value": row.get("value", []),
                "choice": "provided",
                "provenance": row.get("provenance", "catalog_resolver"),
            }
    catalogs = load_catalogs(settings.CATALOG_DIR)
    missing, unresolved, invalid = validate_confirmed_values(
        rows, values, catalogs.params
    )
    if request.accept_defaults:
        for row in rows:
            if row.get("read_only") or row.get("key") in values:
                continue
            if row.get("status") == "provided" and row.get("value") is not None:
                key = str(row["key"])
                values[key] = {
                    "value": row["value"],
                    "choice": "provided",
                    "provenance": row.get("provenance", "stated"),
                }
        missing, unresolved, invalid = validate_confirmed_values(
            rows, values, catalogs.params
        )
    for key, submitted in values.items():
        if submitted.get("choice") not in {
            "accepted_default",
            "user_input",
            "provided",
            "system_default",
            "removed",
            "acknowledged",
        }:
            invalid.append({"key": key, "reason": "Unknown value choice."})
    return values, missing, unresolved, invalid


def _require_card_action(
    project_id: str,
    chat_id: str,
    user_id: str,
    request_id: str,
    action: str,
    identity: Identity,
    repo: WorkflowRepo,
) -> WorkflowRow:
    """Check ownership, creator permission, and workflow card guards."""
    workflow_row = _owned_workflow(project_id, chat_id, user_id, identity, repo)
    if not authorize(
        identity.user_id,
        "scenario.create",
        {"user_id": workflow_row.user_id},
        user_email=identity.user_email,
    ):
        raise HTTPException(
            status_code=403,
            detail={"error_code": "PERMISSION_DENIED", "message": "Permission denied."},
        )
    current = repo.current(workflow_row.thread_id)
    verdict = guard_tool(
        repo,
        workflow_row.thread_id,
        action,
        current.revision_id if current else None,
        authorized=True,
        request_id=request_id,
    )
    if not verdict.allowed:
        code = verdict.code.value if verdict.code else "STAGE_NOT_ALLOWED"
        raise HTTPException(
            status_code=409, detail={"error_code": code, "message": verdict.message}
        )
    return workflow_row


@router.post("/workflow/{project_id}/{chat_id}/{user_id}/actions/confirm_params")
def confirm_params(
    project_id: str,
    chat_id: str,
    user_id: str,
    request: ConfirmParamsRequest,
    identity: Identity = Depends(require_identity),
    repo: WorkflowRepo = Depends(get_workflow_repo),
) -> dict[str, Any]:
    """Resolve the exact pending card, persist a confirmed revision, and render its plan."""
    existing_workflow = _owned_workflow(project_id, chat_id, user_id, identity, repo)
    if not authorize(
        identity.user_id,
        "scenario.create",
        {"user_id": existing_workflow.user_id},
        user_email=identity.user_email,
    ):
        raise HTTPException(
            status_code=403,
            detail={"error_code": "PERMISSION_DENIED", "message": "Permission denied."},
        )
    prior = repo.get_card_confirmation(request.request_id)
    request_hash = stable_hash(
        {
            "values": request.values,
            "accept_defaults": request.accept_defaults,
        }
    )
    if prior is not None:
        if prior.get("confirmation_hash") != request_hash:
            raise HTTPException(
                status_code=409,
                detail={
                    "error_code": "CARD_STALE",
                    "message": "This card was already confirmed with different values.",
                },
            )
        revision = repo.get_revision(prior.get("revision_id"))
        if revision is None:
            raise HTTPException(
                status_code=409,
                detail={
                    "error_code": "CARD_STALE",
                    "message": "The confirmed revision is no longer available.",
                },
            )
        response = get_workflow_state(
            project_id, chat_id, user_id, identity, repo, include_current_revision=True
        )
        response.update(
            {
                "revision_summary": revision.payload_free_summary(),
                "plan_md": revision.plan_md,
                "stage_changed": {
                    "revision_id": revision.revision_id,
                    "stage": revision.stage,
                },
                "carry_over": prior.get("carry_over") or {"kept": [], "dropped": []},
                "history_count": repo.history_count(existing_workflow.thread_id),
            }
        )
        return response
    workflow_row = _require_card_action(
        project_id,
        chat_id,
        user_id,
        request.request_id,
        "confirm_params",
        identity,
        repo,
    )
    card = workflow_row.pending_card
    if card is None:
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "CARD_STALE",
                "message": "The parameter card is stale.",
            },
        )
    values, missing, unresolved, invalid = _card_values(card, request)
    if missing or unresolved:
        repo.add_event(
            workflow_row.thread_id,
            identity.user_id,
            "card_refused",
            detail={
                "request_id": request.request_id,
                "missing": missing,
                "unresolved": unresolved,
            },
        )
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "PARAMS_INCOMPLETE",
                "request_id": request.request_id,
                "missing": missing,
                "unresolved": unresolved,
            },
        )
    if invalid:
        repo.add_event(
            workflow_row.thread_id,
            identity.user_id,
            "card_refused",
            detail={
                "request_id": request.request_id,
                "missing": [],
                "unresolved": [],
                "reasons": invalid,
            },
        )
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "PARAMS_INVALID",
                "request_id": request.request_id,
                "reasons": invalid,
            },
        )
    try:
        result = commit_confirmed_card(
            repo,
            workflow_row.thread_id,
            request.request_id,
            values,
            actor=identity.user_id,
            role=get_role(identity.user_id, identity.user_email),
            source=request.source,
            resolved_rows=resolved_card_rows(card.get("rows", []), values),
            confirmation_hash=request_hash,
            conflict_checker=make_conflict_checker(repo),
            message_position=request.message_position,
        )
    except WorkflowConflict as exc:
        raise HTTPException(
            status_code=409, detail={
                "error_code": exc.code,
                "message": str(exc),
                "matches": exc.details.get("matches") if isinstance(exc.details, dict) else exc.details,
                "conflict_report": exc.details if isinstance(exc.details, dict) else None,
            }
        ) from exc
    response = get_workflow_state(
        project_id, chat_id, user_id, identity, repo, include_current_revision=True
    )
    revision = result["revision"]
    response.update(
        {
            "revision_summary": revision.payload_free_summary(),
            "plan_md": revision.plan_md,
            "stage_changed": {
                "revision_id": revision.revision_id,
                "stage": revision.stage,
            },
            "carry_over": result["carry_over"],
            "history_count": result["history_count"],
            "conflict_report": result.get("conflict_report"),
        }
    )
    return response


@router.post("/workflow/{project_id}/{chat_id}/{user_id}/actions/acknowledge_conflicts")
def acknowledge_conflicts(
    project_id: str,
    chat_id: str,
    user_id: str,
    request: ConflictAcknowledgementRequest,
    identity: Identity = Depends(require_identity),
    repo: WorkflowRepo = Depends(get_workflow_repo),
) -> dict[str, Any]:
    """Acknowledge the exact current warning report, or dismiss its panel."""
    workflow = _owned_workflow(project_id, chat_id, user_id, identity, repo)
    if not authorize(
        identity.user_id, "scenario.create", {"user_id": workflow.user_id},
        user_email=identity.user_email,
    ):
        raise HTTPException(
            status_code=403,
            detail={"error_code": "PERMISSION_DENIED", "message": "Permission denied."},
        )
    current = repo.current(workflow.thread_id)
    if current is None or current.revision_id != request.revision_id:
        raise HTTPException(
            status_code=409,
            detail={"error_code": "REVISION_STALE", "message": "The revision is no longer current."},
        )
    if request.choice == "cancel":
        return get_workflow_state(
            project_id, chat_id, user_id, identity, repo, include_current_revision=True
        )
    plan_md = None
    plan_hash = None
    if workflow.stage == "PARAMS_CONFIRMED":
        try:
            card = json.loads(current.card_json or "{}")
        except (json.JSONDecodeError, TypeError) as exc:
            raise HTTPException(
                status_code=409,
                detail={"error_code": "PLAN_NOT_READY", "message": "The confirmed intent is unavailable."},
            ) from exc
        intent = card.get("intent") if isinstance(card, dict) else None
        if not isinstance(intent, dict):
            raise HTTPException(
                status_code=409,
                detail={"error_code": "PLAN_NOT_READY", "message": "The confirmed intent is unavailable."},
            )
        rendered = render_plan(intent, card.get("explanation_code_checkpoint"))
        plan_md = rendered["plan_artifact"]
        plan_hash = stable_hash(plan_md)
    try:
        repo.acknowledge_conflicts(
            workflow.thread_id,
            request.revision_id,
            conflict_hash=request.conflict_hash,
            actor=identity.user_id,
            role=get_role(identity.user_id, identity.user_email),
            reason=request.reason,
            plan_md=plan_md,
            plan_hash=plan_hash,
        )
    except WorkflowConflict as exc:
        raise HTTPException(
            status_code=409, detail={"error_code": exc.code, "message": str(exc)}
        ) from exc
    return get_workflow_state(
        project_id, chat_id, user_id, identity, repo, include_current_revision=True
    )


@router.post("/workflow/{project_id}/{chat_id}/{user_id}/actions/change_params")
def change_conflict_params(
    project_id: str,
    chat_id: str,
    user_id: str,
    identity: Identity = Depends(require_identity),
    repo: WorkflowRepo = Depends(get_workflow_repo),
) -> dict[str, Any]:
    """Open a pending parameter card seeded from the current confirmed revision."""
    workflow = _owned_workflow(project_id, chat_id, user_id, identity, repo)
    if not authorize(identity.user_id, "scenario.create", {"user_id": workflow.user_id}, user_email=identity.user_email):
        raise HTTPException(status_code=403, detail={"error_code": "PERMISSION_DENIED", "message": "Permission denied."})
    if workflow.stage not in {"PARAMS_CONFIRMED", "PLAN_PENDING", "PLAN_APPROVED", "SHADOW_PASSED", "RESULT_APPROVED", "METADATA_READY"}:
        raise HTTPException(status_code=409, detail={"error_code": "STAGE_NOT_ALLOWED", "message": "Parameters cannot be changed at this stage."})
    try:
        create_revision_card(repo, workflow.thread_id, actor=identity.user_id)
    except WorkflowConflict as exc:
        raise HTTPException(status_code=409, detail={"error_code": exc.code, "message": str(exc)}) from exc
    return get_workflow_state(project_id, chat_id, user_id, identity, repo, include_current_revision=True)


@router.post("/workflow/{project_id}/{chat_id}/{user_id}/actions/discard_card")
def discard_card(
    project_id: str,
    chat_id: str,
    user_id: str,
    request: DiscardCardRequest,
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
            workflow_row.thread_id,
            request.request_id,
            outcome="discarded",
            actor=identity.user_id,
            role=get_role(identity.user_id, identity.user_email),
            source=request.source,
            revision_id=card.get("base_revision_id"),
        )
    except WorkflowConflict as exc:
        raise HTTPException(
            status_code=409, detail={"error_code": exc.code, "message": str(exc)}
        ) from exc
    repo.set_stage(
        workflow_row.thread_id,
        card.get("base_stage")
        or (
            repo.current(workflow_row.thread_id).stage
            if repo.current(workflow_row.thread_id)
            else "EMPTY"
        ),
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
    user_can_create = authorize(
        identity.user_id, "scenario.create", user_email=identity.user_email
    )
    policy = load_catalogs(settings.CATALOG_DIR).policy
    blockers = deployment_blockers(
        workflow,
        current,
        user_can_create=user_can_create,
        approval_enforcement=policy.workflow.enforcement.approvals,
        conflict_check_required=policy.workflow.conflict_check_required,
        conflict_on_unavailable=policy.conflict_check.on_unavailable,
    )
    current_data = (
        {key: getattr(current, key) for key in current.__dataclass_fields__}
        if include_current_revision and current
        else _revision_summary(current)
    )
    try:
        conflict_report = json.loads(current.conflict_json) if current and current.conflict_json else None
    except (json.JSONDecodeError, TypeError):
        conflict_report = None
    approval_notes: list[dict[str, Any]] = []
    for event in repo.list_events(workflow.thread_id):
        action = event.get("action")
        if action not in {"approve_plan", "approve_result", "card_confirmed"}:
            continue
        try:
            detail = json.loads(event.get("detail_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        revision_id = detail.get("revision_id")
        revision = repo.get_revision(revision_id) if isinstance(revision_id, str) else None
        if revision is None:
            continue
        note_action = "confirm_params" if action == "card_confirmed" else action
        approval_notes.append(
            {
                "id": event["id"],
                "ts": event["ts"],
                "action": note_action,
                "revision_no": revision.rev_no,
                "message_position": detail.get("message_position"),
                **(
                    {"plan_ready": bool(revision.plan_md)}
                    if action == "card_confirmed"
                    else {}
                ),
            }
        )
    return {
        "thread_id": workflow.thread_id,
        "stage": workflow.stage,
        "locked": workflow.locked,
        "version": workflow.version,
        "scenario_id": workflow.scenario_id,
        "scenario_key": workflow.scenario_key,
        "parent_scenario_id": workflow.parent_scenario_id,
        "parent_version": workflow.parent_version,
        "current_revision": current_data,
        "can_deploy": not blockers,
        "blockers": blockers,
        "pending_card": workflow.pending_card,
        "confirmed_cards": [
            audit
            for audit in repo.card_audit_repo.list_for_thread(workflow.thread_id)
            if audit and audit.get("outcome") in {"confirmed", "auto_confirmed"}
        ],
        "conflict_report": conflict_report,
        "history": [revision.payload_free_summary() for revision in history],
        "approval_notes": approval_notes,
    }


@router.post("/workflow/{project_id}/{chat_id}/{user_id}/actions/create_new_version")
def create_new_version(
    project_id: str,
    chat_id: str,
    user_id: str,
    request: CreateNewVersionRequest,
    identity: Identity = Depends(require_identity),
    repo: WorkflowRepo = Depends(get_workflow_repo),
) -> dict[str, Any]:
    """Create a child chat seeded from the selected deployed registry version."""
    workflow = _owned_workflow(project_id, chat_id, user_id, identity, repo)
    if not workflow.locked or workflow.scenario_id != request.scenario_id:
        raise HTTPException(status_code=409, detail={"error_code": "REVISE_STALE", "message": "Start again from the latest version."})
    try:
        return _versioning_service(identity, repo).create_new_version(
            request.scenario_id,
            project_id=workflow.project_id,
            user_id=workflow.user_id,
            actor_id=identity.user_id,
            actor_role=get_role(identity.user_id, identity.user_email),
        )
    except VersioningError as exc:
        raise _versioning_http_error(exc) from exc


@router.get("/workflow/{project_id}/{chat_id}/{user_id}/versions")
def list_scenario_versions(
    project_id: str,
    chat_id: str,
    user_id: str,
    identity: Identity = Depends(require_identity),
    repo: WorkflowRepo = Depends(get_workflow_repo),
) -> dict[str, Any]:
    """List the latest version's full lineage and last status-change audit data."""
    workflow = _owned_workflow(project_id, chat_id, user_id, identity, repo)
    if not workflow.locked:
        raise HTTPException(status_code=409, detail={"error_code": "STAGE_NOT_ALLOWED", "message": "Version history is available from a deployed scenario."})
    registry = OracleRegistryRepo()
    latest = registry.latest_version(workflow.scenario_key)
    if latest is None or latest["scenario_id"] != workflow.scenario_id:
        raise HTTPException(status_code=409, detail={"error_code": "REVISE_STALE", "message": "Version history is available from the latest version only."})
    versions = registry.list_versions(workflow.scenario_key)
    audit = registry.list_version_audit(workflow.scenario_key)
    shown_actions = {"CREATE", "REVISE", "SUPERSEDE", "ROLLBACK", "RETIRE"}
    result = []
    for version in versions:
        events = [item for item in audit if item.get("scenario_id") == version["scenario_id"]]
        status_events = [item for item in events if item.get("action") in shown_actions and item.get("action") not in {"CREATE", "REVISE"}]
        creation = next((item for item in reversed(events) if item.get("action") in {"CREATE", "REVISE"}), None)
        result.append({
            "version": version["version_no"],
            "id": version["scenario_id"],
            "status": version["status"],
            "name": version["scenario_name"],
            "creator": creation.get("actor") if creation else version.get("created_by"),
            "created_at": creation.get("ts") if creation else version.get("created_at"),
            "parent": version.get("parent_scenario_id"),
            "change_reason": version.get("change_reason"),
            "last_status_change": status_events[0] if status_events else creation,
            "override_conflict_count": sum(item.get("action") == "OVERRIDE_CONFLICT" for item in events),
        })
    return {"versions": result}


@router.post("/workflow/{project_id}/{chat_id}/{user_id}/actions/rollback_version")
def rollback_version(
    project_id: str,
    chat_id: str,
    user_id: str,
    request: RollbackRequest,
    identity: Identity = Depends(require_identity),
    repo: WorkflowRepo = Depends(get_workflow_repo),
) -> dict[str, Any]:
    """Rollback one superseded version after the P8 conflict check."""
    _owned_workflow(project_id, chat_id, user_id, identity, repo)
    try:
        return _versioning_service(identity, repo).rollback(
            request.scenario_id, expected_active_version=request.expected_active_version,
            reason=request.reason, actor_id=identity.user_id,
            actor_role=get_role(identity.user_id, identity.user_email),
            acknowledge_warnings=request.acknowledge_warnings,
        )
    except VersioningError as exc:
        raise _versioning_http_error(exc) from exc


@router.post("/workflow/{project_id}/{chat_id}/{user_id}/actions/retire_version")
def retire_version(
    project_id: str,
    chat_id: str,
    user_id: str,
    request: RetireRequest,
    identity: Identity = Depends(require_identity),
    repo: WorkflowRepo = Depends(get_workflow_repo),
) -> dict[str, Any]:
    """Soft-retire one active registry version."""
    _owned_workflow(project_id, chat_id, user_id, identity, repo)
    try:
        _versioning_service(identity, repo).retire(
            request.scenario_id, reason=request.reason, actor_id=identity.user_id,
            actor_role=get_role(identity.user_id, identity.user_email),
        )
    except VersioningError as exc:
        raise _versioning_http_error(exc) from exc
    return {"scenario_id": request.scenario_id, "status": "RETIRED"}


def _approve(
    gate: str,
    project_id: str,
    chat_id: str,
    user_id: str,
    request: ApprovalRequest,
    identity: Identity,
    repo: WorkflowRepo,
) -> dict[str, Any]:
    """Check shared workflow guards and persist one typed approval."""
    workflow_row = _owned_workflow(project_id, chat_id, user_id, identity, repo)
    if not authorize(
        identity.user_id,
        "scenario.create",
        {"user_id": workflow_row.user_id},
        user_email=identity.user_email,
    ):
        raise HTTPException(
            status_code=403,
            detail={"error_code": "PERMISSION_DENIED", "message": "Permission denied."},
        )
    action = "approve_plan" if gate == "plan" else "approve_result"
    current = repo.current(workflow_row.thread_id)
    if current is None or current.revision_id != request.revision_id:
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "REVISION_STALE",
                "message": "The revision is no longer current.",
            },
        )
    expected = current.plan_hash if gate == "plan" else current.sql_hash
    if expected != request.expected_hash:
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "HASH_MISMATCH",
                "message": "The artifact hash changed. Refresh state and try again.",
            },
        )
    approved = (
        current.plan_approved_at if gate == "plan" else current.result_approved_at
    )
    if approved:
        return get_workflow_state(
            project_id, chat_id, user_id, identity, repo, include_current_revision=True
        )
    verdict = guard_tool(
        repo, workflow_row.thread_id, action, request.revision_id, authorized=True
    )
    if not verdict.allowed:
        code = verdict.code.value if verdict.code else "STAGE_NOT_ALLOWED"
        raise HTTPException(
            status_code=409, detail={"error_code": code, "message": verdict.message}
        )
    try:
        repo.approve_revision(
            workflow_row.thread_id,
            request.revision_id,
            approval=gate,
            actor=identity.user_id,
            role=get_role(identity.user_id, identity.user_email),
            expected_hash=request.expected_hash,
            message_position=request.message_position,
        )
    except WorkflowConflict as exc:
        raise HTTPException(
            status_code=409, detail={"error_code": exc.code, "message": str(exc)}
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=409, detail={"error_code": "HASH_MISMATCH", "message": str(exc)}
        ) from exc
    return get_workflow_state(
        project_id, chat_id, user_id, identity, repo, include_current_revision=True
    )


@router.post("/workflow/{project_id}/{chat_id}/{user_id}/actions/approve_plan")
def approve_plan(
    project_id: str,
    chat_id: str,
    user_id: str,
    request: ApprovalRequest,
    identity: Identity = Depends(require_identity),
    repo: WorkflowRepo = Depends(get_workflow_repo),
) -> dict[str, Any]:
    """Approve the current plan using its expected hash."""
    return _approve("plan", project_id, chat_id, user_id, request, identity, repo)


@router.post("/workflow/{project_id}/{chat_id}/{user_id}/actions/approve_result")
def approve_result(
    project_id: str,
    chat_id: str,
    user_id: str,
    request: ApprovalRequest,
    identity: Identity = Depends(require_identity),
    repo: WorkflowRepo = Depends(get_workflow_repo),
) -> dict[str, Any]:
    """Approve a successful shadow result using its SQL hash."""
    return _approve("result", project_id, chat_id, user_id, request, identity, repo)


@router.get("/ui/copy")
def get_ui_copy(identity: Identity = Depends(require_identity)) -> dict[str, Any]:
    """Return editable user-facing copy to an authenticated UI client."""
    if not authorize(
        identity.user_id,
        "chat.read",
        {"user_id": identity.user_id},
        user_email=identity.user_email,
    ):
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
