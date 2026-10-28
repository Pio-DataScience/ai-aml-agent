"""Deterministic deployment orchestration over workflow and registry ports."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

from services.aml_builder.web.domain.hashing import stable_hash
from services.aml_builder.web.services.settings import settings

class DeployError(RuntimeError):
    """A typed deployment refusal or registry failure."""

    def __init__(self, code: str, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


class ConflictGate(Protocol):
    """Port for checking conflict acknowledgements before registry commit."""

    def check(
        self, thread_id: str, revision_id: str, *, exclude_scenario_ids: set[str] | None = None
    ) -> dict[str, Any]:
        """Return a status and acknowledgement list for the current revision."""


class _NotRunConflictGate:
    """Default conflict gate until P8 provides an authoritative checker."""

    def check(
        self, thread_id: str, revision_id: str, *, exclude_scenario_ids: set[str] | None = None
    ) -> dict[str, Any]:
        return {"status": "not_run", "acknowledgements": []}


def deployment_blockers(
    workflow: Any, revision: Any, *, user_can_create: bool,
    approval_enforcement: str = "enforce",
    conflict_check_required: bool = False,
    conflict_on_unavailable: str = "block",
) -> list[str]:
    """Return typed deploy blockers shared by the state API and DeployService."""
    blockers: list[str] = []
    if not user_can_create:
        blockers.append("PERMISSION_DENIED")
    if workflow is None or revision is None:
        return blockers + ["STAGE_NOT_ALLOWED"]
    if workflow.locked:
        blockers.append("CHAT_LOCKED_DEPLOYED")
    if workflow.pending_card:
        blockers.append("CARD_PENDING")
    if workflow.current_revision_id != revision.revision_id:
        blockers.append("REVISION_STALE")
    if workflow.stage != "METADATA_READY" or revision.stage != "METADATA_READY":
        blockers.append("STAGE_NOT_ALLOWED")
    conflict = _json_value(revision.conflict_json) or {}
    if conflict.get("blocking") or conflict.get("status") == "blocked":
        blockers.append("CONFLICT_BLOCKED")
    elif conflict.get("needs_ack") or conflict.get("status") == "needs_ack":
        blockers.append("CONFLICT_ACK_REQUIRED")
    elif conflict_check_required and conflict_on_unavailable == "block" and conflict.get("status") == "unavailable":
        blockers.append("CONFLICT_CHECK_UNAVAILABLE")
    shadow = _json_value(revision.shadow_json) or {}
    sql = shadow.get("raw_sql")
    if not shadow.get("validation_success"):
        blockers.append("SHADOW_NOT_PASSED")
    elif not sql or not revision.sql_hash or shadow.get("sql_hash") != revision.sql_hash or stable_hash_sql(sql) != revision.sql_hash:
        blockers.append("HASH_MISMATCH")
    if approval_enforcement == "enforce":
        if not revision.plan_approved_at:
            blockers.append("PLAN_NOT_APPROVED")
        if not revision.result_approved_at:
            blockers.append("RESULT_NOT_APPROVED")
    return blockers


@dataclass(frozen=True)
class DeploymentResult:
    """Committed version and its immutable deployment receipt."""

    scenario_id: str
    scenario_name: str
    receipt: dict[str, Any]
    repeated: bool = False


class DeployService:
    """Validate one current revision and commit it through repository ports."""

    def __init__(
        self,
        *,
        oracle_repo: Any,
        workflow_repo: Any,
        metadata_validator: Callable[[dict[str, Any]], tuple[bool, list[str]]],
        authorizer: Callable[[str, str], bool],
        conflict_gate: ConflictGate | None = None,
        enforcement: str | dict[str, str] | None = None,
        conflict_check_required: bool = False,
        conflict_on_unavailable: str = "block",
        numeric_created_by_default: int | None = None,
    ) -> None:
        """Configure adapters and the current P4 approval policy."""
        self.oracle_repo = oracle_repo
        self.workflow_repo = workflow_repo
        self.metadata_validator = metadata_validator
        self.authorizer = authorizer
        self.conflict_gate = conflict_gate or _NotRunConflictGate()
        self.enforcement = enforcement or getattr(workflow_repo, "enforcement", "observe")
        self.approval_enforcement = (
            self.enforcement.get("approvals", "observe")
            if isinstance(self.enforcement, dict)
            else self.enforcement
        )
        self.conflict_check_required = conflict_check_required
        self.conflict_on_unavailable = conflict_on_unavailable
        self.numeric_created_by_default = _numeric_user(
            settings.AML_CREATED_BY if numeric_created_by_default is None else numeric_created_by_default,
            fallback=999,
        )

    def deploy(
        self,
        thread_id: str,
        revision_id: str,
        actor_id: str,
        actor_role: str = "",
        *,
        raw_sql: str | None = None,
        metadata: dict[str, Any] | None = None,
        parent_scenario_id: str | None = None,
        parent_version: int | None = None,
        change_reason: str | None = None,
        scenario_id: str | None = None,
        scenario_key: str | None = None,
        version_no: int | None = None,
    ) -> DeploymentResult:
        """Deploy the current revision, completing a prior partial success idempotently.

        Args:
            thread_id: Deterministic workflow thread identifier.
            revision_id: Revision shown to the caller.
            actor_id: Authenticated user ID.
            actor_role: Resolved policy role recorded in the receipt.
            raw_sql: Optional caller-supplied SQL; otherwise use the stored shadow result.
            metadata: Optional caller-supplied metadata; otherwise use the revision copy.
            parent_scenario_id: Exact active parent to supersede for a revision deploy.
            change_reason: Optional version change reason.
            scenario_id: Pre-allocated version-specific registry ID.
            scenario_key: Pre-allocated stable registry key.
            version_no: Pre-allocated version number.

        Returns:
            DeploymentResult: Registry row and receipt.

        Raises:
            DeployError: When a guard, validation, or registry operation fails.
        """
        workflow = self.workflow_repo.get_workflow(thread_id)
        if workflow is None:
            raise DeployError("STAGE_NOT_ALLOWED", "Workflow row is missing.")
        revision = self.workflow_repo.current(thread_id)
        selected_scenario_id = scenario_id or workflow.scenario_id
        selected_scenario_key = scenario_key or workflow.scenario_key
        selected_version_no = version_no or _version_no(selected_scenario_id)
        change_reason = change_reason.strip() if isinstance(change_reason, str) else change_reason
        if selected_version_no > 1 and (not change_reason or not 5 <= len(change_reason.strip()) <= 500):
            raise DeployError("CHANGE_REASON_REQUIRED", "A change reason between 5 and 500 characters is required for versions after v1.")
        self._require_permission(actor_id, "scenario.revise" if parent_scenario_id else "scenario.create")
        if workflow.current_revision_id != revision_id or revision is None or revision.revision_id != revision_id:
            raise DeployError("REVISION_STALE", "The supplied revision is no longer current.")
        if workflow.pending_card:
            raise DeployError("CARD_PENDING", "Confirm or discard the pending parameter card first.")
        if workflow.locked and workflow.deployed_revision_id != revision_id:
            raise DeployError("CHAT_LOCKED_DEPLOYED", "This chat is locked after deployment.")
        if workflow.locked:
            existing = self.oracle_repo.get_by_thread(thread_id)
            if existing is None or existing.get("scenario_id") != selected_scenario_id or existing.get("revision_id") != revision_id:
                raise DeployError("CHAT_LOCKED_DEPLOYED", "This chat is locked after deployment.")
            return self._repeat_existing(existing)
        blockers = deployment_blockers(
            workflow, revision, user_can_create=True,
            approval_enforcement=self.approval_enforcement,
        )
        if blockers:
            code = blockers[0]
            messages = {
                "CARD_PENDING": "Confirm or discard the pending parameter card first.",
                "REVISION_STALE": "The supplied revision is no longer current.",
                "STAGE_NOT_ALLOWED": "Deployment requires METADATA_READY.",
                "SHADOW_NOT_PASSED": "The current revision has no successful shadow test.",
                "HASH_MISMATCH": "The current shadow SQL hash does not match the SQL being deployed.",
                "PLAN_NOT_APPROVED": "The current plan has not been approved.",
                "RESULT_NOT_APPROVED": "The current shadow result has not been approved.",
            }
            raise DeployError(code, messages.get(code, "Deployment is blocked by workflow state."))
        if revision.stage != "METADATA_READY" or workflow.stage != "METADATA_READY":
            raise DeployError("STAGE_NOT_ALLOWED", "Deployment requires METADATA_READY.")

        shadow = _json_value(revision.shadow_json) or {}
        sql = raw_sql if raw_sql is not None else shadow.get("raw_sql")
        sql_hash = revision.sql_hash
        shadow_sql_hash = shadow.get("sql_hash")
        if not shadow.get("validation_success"):
            raise DeployError("SHADOW_NOT_PASSED", "The current revision has no successful shadow test.")
        if (
            not sql
            or not sql_hash
            or sql_hash != shadow_sql_hash
            or stable_hash_sql(sql) != sql_hash
        ):
            raise DeployError("HASH_MISMATCH", "The current shadow SQL hash does not match the SQL being deployed.")

        approval_status = "approved"
        missing_approvals = []
        if not revision.plan_approved_at:
            missing_approvals.append("plan")
        if not revision.result_approved_at:
            missing_approvals.append("result")
        if missing_approvals:
            if self.approval_enforcement == "enforce":
                code = "PLAN_NOT_APPROVED" if "plan" in missing_approvals else "RESULT_NOT_APPROVED"
                raise DeployError(code, f"Missing approval(s): {', '.join(missing_approvals)}.")
            approval_status = "would_block"

        metadata_value = metadata if metadata is not None else (_json_value(revision.metadata_json) or {})
        effective_metadata_hash = stable_hash(metadata_value)
        valid, errors = self.metadata_validator(metadata_value)
        if not valid:
            raise DeployError("METADATA_INVALID", "Governance metadata is invalid.", details=errors)

        if parent_scenario_id:
            conflict = self.conflict_gate.check(
                thread_id, revision_id, exclude_scenario_ids={parent_scenario_id}
            )
        else:
            conflict = self.conflict_gate.check(thread_id, revision_id)
        if (
            self.conflict_check_required
            and conflict.get("status") == "unavailable"
            and self.conflict_on_unavailable == "block"
        ):
            raise DeployError(
                "CONFLICT_CHECK_UNAVAILABLE",
                "The active scenario registry could not be checked.",
            )
        if self.conflict_check_required and conflict.get("status") == "not_run":
            raise DeployError("CONFLICT_CHECK_REQUIRED", "The required conflict check has not run.")
        if conflict.get("blocking"):
            if hasattr(self.workflow_repo, "save_conflict_report"):
                self.workflow_repo.save_conflict_report(thread_id, revision_id, conflict)
            raise DeployError(
                "CONFLICT_BLOCKED",
                "A blocking active scenario conflict was found.",
                details=conflict.get("matches", []),
            )
        prior_conflict = _json_value(revision.conflict_json) or {}
        prior_acknowledgements = prior_conflict.get("acknowledgements", [])
        acknowledged = {
            (str(item.get("scenario_id")), str(item.get("relation"))): item
            for item in prior_acknowledgements if isinstance(item, dict)
        }
        warn_matches = [item for item in conflict.get("matches", []) if item.get("action") == "warn_confirm"]
        unacknowledged = [
            item for item in warn_matches
            if (str(item.get("scenario_id")), str(item.get("relation"))) not in acknowledged
        ]
        if unacknowledged:
            conflict["needs_ack"] = True
            conflict["status"] = "needs_ack"
            conflict["acknowledgements"] = list(prior_acknowledgements)
            if hasattr(self.workflow_repo, "save_conflict_report"):
                self.workflow_repo.save_conflict_report(thread_id, revision_id, conflict)
            raise DeployError(
                "CONFLICT_ACK_REQUIRED",
                "New or changed warning conflicts need acknowledgement.",
                details=conflict.get("matches", []),
            )
        conflict["acknowledgements"] = [
            acknowledged[(str(item.get("scenario_id")), str(item.get("relation")))]
            for item in warn_matches
        ]
        conflict["needs_ack"] = False
        conflict["status"] = "acknowledged" if warn_matches else conflict.get("status", "clear")
        override_conflicts = [
            {
                "revision_id": revision_id,
                "other_scenario_id": ack.get("scenario_id"),
                "relation": ack.get("relation"),
                "actor": ack.get("actor"),
                "role": ack.get("role"),
                "acknowledged_at": ack.get("time"),
                "reason": ack.get("reason"),
            }
            for ack in conflict["acknowledgements"]
        ]
        card_audits = []
        card_audit_repo = getattr(self.workflow_repo, "card_audit_repo", None)
        if card_audit_repo is not None:
            card_audits = [row.get("request_id") for row in card_audit_repo.list_for_thread(thread_id)]
        now = datetime.now(timezone.utc).isoformat()
        intent = _intent_from_revision(revision)
        scenario_name = str(intent.get("scenario_name") or "AML Detection Scenario")
        scenario_type = str(intent.get("scenario_type") or "CUSTOMER")
        scenario_id = selected_scenario_id
        scenario_key = selected_scenario_key
        if not _valid_version_identity(scenario_id, scenario_key, selected_version_no):
            raise DeployError("REGISTRY_ID_INVALID", "Pre-allocated scenario ID does not match registry v2 format.")
        parent_row = None
        if parent_scenario_id:
            get_version = getattr(self.oracle_repo, "get_version", None)
            parent_row = get_version(parent_scenario_id) if get_version else None
            if parent_row is None:
                raise DeployError("REVISE_STALE", "Start again from the latest version.")
            if str(parent_row.get("status", "")).upper() not in {"ACTIVE", "RETIRED"}:
                raise DeployError("REVISE_STALE", "Start again from the latest version.")
            parent_version = int(parent_row.get("version_no") or parent_version or 0)
            same_signature = bool(revision.signature_hash and parent_row.get("signature_hash") and revision.signature_hash == parent_row.get("signature_hash"))
            same_metadata = bool(effective_metadata_hash and parent_row.get("metadata_hash") and effective_metadata_hash == parent_row.get("metadata_hash"))
            if same_signature and same_metadata:
                raise DeployError("IDENTICAL_TO_PARENT", "The scenario and governance metadata are unchanged from the parent version.")
        change_scope = (
            "metadata" if parent_row and same_signature
            else "logic" if parent_row else "initial"
        )
        created_by_number = _numeric_user(actor_id, self.numeric_created_by_default)
        receipt = {
            "thread_id": thread_id,
            "revision_id": revision_id,
            "rev_no": revision.rev_no,
            "scenario_id": scenario_id,
            "scenario_key": scenario_key,
            "version_no": selected_version_no,
            "parent_scenario_id": parent_scenario_id,
            "parent_version": parent_version,
            "change_reason": change_reason,
            "change_scope": change_scope,
            "card_audit_request_ids": card_audits,
            "params_hash": revision.params_hash,
            "plan_hash": revision.plan_hash,
            "sql_hash": sql_hash,
            "metadata_hash": effective_metadata_hash,
            "signature_hash": revision.signature_hash,
            "params_confirmed_by": revision.created_by,
            "params_confirmed_at": revision.created_at,
            "plan_approved_by": revision.plan_approved_by,
            "plan_approved_at": revision.plan_approved_at,
            "result_approved_by": revision.result_approved_by,
            "result_approved_at": revision.result_approved_at,
            "deployed_by": actor_id,
            "deployed_at": now,
            "shadow_summary": {
                "validation_success": shadow.get("validation_success"),
                "header_alert_count": shadow.get("header_alert_count"),
                "transaction_detail_count": shadow.get("transaction_detail_count"),
            },
            "approval_status": approval_status,
            "missing_approvals": missing_approvals,
            "conflict": conflict,
            "conflict_acknowledgements": conflict.get("acknowledgements", []),
        }
        row = {
            "scenario_id": scenario_id,
            "scenario_key": scenario_key,
            "version_no": selected_version_no,
            "parent_scenario_id": parent_scenario_id,
            "parent_version": parent_version,
            "parent_status": parent_row.get("status") if parent_row else None,
            "status": "ACTIVE",
            "is_active": 1,
            "thread_id": thread_id,
            "revision_id": revision_id,
            "rev_no": revision.rev_no,
            "signature_hash": revision.signature_hash,
            "signature_json": revision.signature_json,
            "params_hash": revision.params_hash,
            "params_json": _json_value(revision.card_json),
            "filters_json": intent.get("resolved_filters", []),
            "plan_hash": revision.plan_hash,
            "sql_hash": sql_hash,
            "metadata_hash": effective_metadata_hash,
            "metadata_json": metadata_value,
            "receipt_json": receipt,
            "change_reason": change_reason,
            "scenario_name": scenario_name,
            "scenario_type": scenario_type,
            "detection_logic": intent.get("detection_logic", scenario_name),
            "raw_sql": sql,
            "time_window_days": _window_days(intent.get("time_window")),
            "created_by": str(actor_id),
            "created_by_number": created_by_number,
            "actor_role": actor_role,
            "audit_action": "REVISE" if parent_scenario_id else "CREATE",
            "override_conflicts": override_conflicts,
        }
        existing = self.oracle_repo.get_by_thread(thread_id)
        if existing is not None:
            if existing.get("scenario_id") != scenario_id or existing.get("revision_id") != revision_id:
                raise DeployError("REVISION_STALE", "A different revision is already deployed for this chat.")
            self.workflow_repo.mark_deployed(thread_id, revision_id)
            return self._repeat_existing(existing)
        self.oracle_repo.insert_version(row)
        self.workflow_repo.mark_deployed(thread_id, revision_id)
        return DeploymentResult(scenario_id, scenario_name, receipt, repeated=False)

    def _require_permission(self, actor_id: str, action: str) -> None:
        if not self.authorizer(actor_id, action):
            raise DeployError("PERMISSION_DENIED", "Permission denied.")

    def _repeat_existing(self, existing: dict[str, Any]) -> DeploymentResult:
        """Return a previous deployment without rewriting its registry row."""
        return DeploymentResult(
            scenario_id=existing["scenario_id"],
            scenario_name=existing.get("scenario_name", ""),
            receipt=_json_value(existing.get("receipt_json")) or existing.get("receipt", {}),
            repeated=True,
        )


def stable_hash_sql(sql: str) -> str:
    """Hash SQL using the repository's canonical SQL normalization."""
    from services.aml_builder.web.domain.hashing import normalize_sql

    return stable_hash(normalize_sql(sql))


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _intent_from_revision(revision: Any) -> dict[str, Any]:
    card = _json_value(revision.card_json) or {}
    if isinstance(card, dict):
        intent = card.get("intent", card)
        if isinstance(intent, dict) and isinstance(intent.get("payload"), dict):
            return intent["payload"]
        return intent if isinstance(intent, dict) else {}
    return {}


def _window_days(window: Any) -> int | None:
    if isinstance(window, dict):
        value = window.get("value")
        unit = str(window.get("unit", "days")).casefold()
        try:
            multiplier = {"day": 1, "days": 1, "week": 7, "weeks": 7, "month": 30, "months": 30, "year": 365, "years": 365}[unit]
            return int(value) * multiplier if value is not None else None
        except (KeyError, TypeError, ValueError):
            return None
    return None


def _numeric_user(user_id: Any, fallback: Any) -> int:
    try:
        return int(user_id)
    except (TypeError, ValueError):
        try:
            return int(fallback)
        except (TypeError, ValueError):
            return 999


def _version_no(scenario_id: str) -> int:
    """Read a version number from the version-specific scenario identifier."""
    import re

    match = re.fullmatch(r"PRD_[0-9A-F]{8}_V([1-9][0-9]*)", scenario_id)
    return int(match.group(1)) if match else 0


def _valid_version_identity(scenario_id: str, scenario_key: str, version_no: int) -> bool:
    import re

    return bool(re.fullmatch(rf"PRD_[0-9A-F]{{8}}_V{version_no}", scenario_id)) and bool(
        re.fullmatch(r"PRD_[0-9A-F]{8}", scenario_key)
    ) and scenario_id.startswith(scenario_key + "_")
