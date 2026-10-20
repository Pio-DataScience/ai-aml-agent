"""Workflow guard verdicts and observe/enforce policy behavior."""

from dataclasses import dataclass
from enum import StrEnum

from services.aml_builder.web.workflow.repo import WorkflowRepo


class GuardCode(StrEnum):
    """Stable workflow guard error codes."""

    STAGE_NOT_ALLOWED = "STAGE_NOT_ALLOWED"
    CHAT_LOCKED_DEPLOYED = "CHAT_LOCKED_DEPLOYED"
    PARAMS_UNCONFIRMED = "PARAMS_UNCONFIRMED"
    PARAMS_INCOMPLETE = "PARAMS_INCOMPLETE"
    CARD_STALE = "CARD_STALE"
    CARD_PENDING = "CARD_PENDING"
    REVISION_STALE = "REVISION_STALE"
    PLAN_NOT_APPROVED = "PLAN_NOT_APPROVED"
    RESULT_NOT_APPROVED = "RESULT_NOT_APPROVED"
    SHADOW_NOT_PASSED = "SHADOW_NOT_PASSED"
    HASH_MISMATCH = "HASH_MISMATCH"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    CONFLICT_BLOCKED = "CONFLICT_BLOCKED"
    CONFLICT_ACK_REQUIRED = "CONFLICT_ACK_REQUIRED"
    CONFLICT_CHECK_UNAVAILABLE = "CONFLICT_CHECK_UNAVAILABLE"
    CONFLICT_STALE = "CONFLICT_STALE"


@dataclass(frozen=True)
class GuardVerdict:
    """Outcome of evaluating a tool or action against current workflow state."""

    allowed: bool
    code: GuardCode | None = None
    message: str | None = None
    enforced: bool = False


_TOOL_STAGES: dict[str, frozenset[str] | None] = {
    "analyze_intent_and_discover_explanation_codes": None,
    "generate_scenario_execution_plan": frozenset({"CONFLICT_CHECKED", "PLAN_PENDING"}),
    "execute_oracle_dwh_shadow_test": frozenset({"PLAN_APPROVED"}),
    "prepare_scenario_metadata_for_persistence": frozenset(
        {"RESULT_APPROVED", "METADATA_READY"}
    ),
    "persist_and_validate_scenario_in_dwh": frozenset({"METADATA_READY"}),
    "query_production_scenario_registry": None,
    "confirm_params": frozenset({"PARAMS_PENDING"}),
    "discard_card": frozenset({"PARAMS_PENDING"}),
    "approve_plan": frozenset({"PLAN_PENDING"}),
    "approve_result": frozenset({"SHADOW_PASSED"}),
    "deploy": frozenset({"METADATA_READY"}),
    "acknowledge_conflicts": frozenset({"PARAMS_CONFIRMED", "METADATA_READY"}),
}


def _mode(repo: WorkflowRepo, tool_name: str) -> str:
    """Resolve the configured rollout mode for a gate or legacy test repository."""
    setting = repo.enforcement
    if isinstance(setting, dict):
        if tool_name in {"approve_plan", "approve_result", "deploy", "persist_and_validate_scenario_in_dwh"}:
            gate = "approvals"
        elif tool_name in {"confirm_params", "discard_card", "analyze_intent_and_discover_explanation_codes"}:
            gate = "card_confirmation"
        else:
            gate = "conflict_check"
        return setting.get(gate, "observe")
    return setting


def guard_tool(
    repo: WorkflowRepo,
    thread_id: str,
    tool_name: str,
    revision_id: str | None,
    *,
    authorized: bool = True,
    request_id: str | None = None,
) -> GuardVerdict:
    """Evaluate hard safety, stale-ID, pending-card, and stage rules.

    Args:
        repo: Repository that owns the current workflow state and audit events.
        thread_id: Deterministic chat thread identifier.
        tool_name: Tool or typed action name to check.
        revision_id: Revision identifier presented by the caller, if any.
        authorized: Result of the caller's P2 permission check.
        request_id: Pending-card request identifier for card actions.

    Returns:
        A typed verdict. Observe mode records ordinary would-block verdicts but
        allows the call to continue. Permission and deployed-chat locks always block.
    """
    workflow = repo.get_workflow(thread_id)
    if not authorized:
        return _record(
            repo, thread_id, GuardCode.PERMISSION_DENIED, "Permission denied.", True, tool_name
        )
    if workflow is None:
        return _record(
            repo,
            thread_id,
            GuardCode.STAGE_NOT_ALLOWED,
            "Workflow row is missing.",
            _mode(repo, tool_name) == "enforce", tool_name,
        )
    if workflow.locked and tool_name != "query_production_scenario_registry":
        return _record(
            repo,
            thread_id,
            GuardCode.CHAT_LOCKED_DEPLOYED,
            "This chat is locked after deployment.",
            True, tool_name,
        )
    current = repo.current(thread_id)
    if revision_id and current and revision_id != current.revision_id:
        return _record(
            repo,
            thread_id,
            GuardCode.REVISION_STALE,
            "The supplied revision is no longer current.",
            _mode(repo, tool_name) == "enforce", tool_name,
        )
    if request_id is not None:
        pending_id = (workflow.pending_card or {}).get("request_id")
        if pending_id != request_id:
            return _record(
                repo,
                thread_id,
                GuardCode.CARD_STALE,
                "The parameter card request is stale.",
                _mode(repo, tool_name) == "enforce", tool_name,
            )
    if (
        tool_name
        in {
            "approve_plan",
            "execute_oracle_dwh_shadow_test",
            "approve_result",
            "deploy",
            "persist_and_validate_scenario_in_dwh",
        }
        and workflow.pending_card
    ):
        return _record(
            repo,
            thread_id,
            GuardCode.CARD_PENDING,
            "Confirm or discard the pending parameter card first.",
            _mode(repo, tool_name) == "enforce", tool_name,
        )
    stages = _TOOL_STAGES.get(tool_name)
    if stages is not None and workflow.stage not in stages:
        return _record(
            repo,
            thread_id,
            GuardCode.STAGE_NOT_ALLOWED,
            f"{tool_name} is not allowed during {workflow.stage}.",
            _mode(repo, tool_name) == "enforce", tool_name,
        )
    return GuardVerdict(allowed=True)


def _record(
    repo: WorkflowRepo, thread_id: str, code: GuardCode, message: str, hard: bool, tool_name: str
) -> GuardVerdict:
    """Persist a block or would-block event and return its verdict."""
    mode = _mode(repo, tool_name)
    action = "guard_blocked" if hard or mode == "enforce" else "would_block"
    repo.add_event(
        thread_id,
        "system",
        action,
        detail={"error_code": code.value, "message": message},
    )
    return GuardVerdict(
        allowed=not hard and mode == "observe",
        code=code,
        message=message,
        enforced=hard or mode == "enforce",
    )
