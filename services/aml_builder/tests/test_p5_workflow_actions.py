"""P5 typed approval action contracts and staged enforcement flow."""

from pathlib import Path
import json

import pytest
from fastapi import HTTPException

from services.aml_builder.web.api.dependencies import Identity
from services.aml_builder.web.api.routes import workflow
from services.aml_builder.web.workflow.repo import WorkflowRepo


@pytest.fixture(autouse=True)
def _mock_conflict_checker(monkeypatch):
    class ClearChecker:
        def check_intent(self, _intent, **_kwargs):
            return {"revision_id": None, "provisional": False, "status": "clear", "conflict_hash": "clear-hash", "blocking": False, "needs_ack": False, "matches": []}

    monkeypatch.setattr(workflow, "make_conflict_checker", lambda _repo: ClearChecker())


def _fixture(tmp_path: Path, *, role: str = "999"):
    repo = WorkflowRepo(tmp_path / "p5.sqlite", enforcement={"approvals": "enforce", "card_confirmation": "observe", "conflict_check": "observe"})
    repo.get_or_create_workflow(f"p1_c1_{role}", "p1", "c1", role)
    revision = repo.create_revision(f"p1_c1_{role}", card={"intent": {}}, stage="PLAN_PENDING", created_by=role)
    repo.update_revision(revision.revision_id, expected_version=repo.get_workflow(revision.thread_id).version, plan_md="# plan", plan_hash="plan-hash")
    return repo, revision, Identity(role, "", "p1")


def test_approve_plan_hash_mismatch_stale_revision_and_permission(tmp_path: Path) -> None:
    repo, revision, identity = _fixture(tmp_path)
    with pytest.raises(HTTPException) as mismatch:
        workflow.approve_plan("p1", "c1", identity.user_id, workflow.ApprovalRequest(revision_id=revision.revision_id, expected_hash="wrong"), identity, repo)
    assert mismatch.value.status_code == 409
    assert mismatch.value.detail["error_code"] == "HASH_MISMATCH"

    with pytest.raises(HTTPException) as stale:
        workflow.approve_plan("p1", "c1", identity.user_id, workflow.ApprovalRequest(revision_id="stale", expected_hash="plan-hash"), identity, repo)
    assert stale.value.status_code == 409
    assert stale.value.detail["error_code"] == "REVISION_STALE"

    viewer_repo, viewer_revision, viewer = _fixture(tmp_path, role="1002")
    with pytest.raises(HTTPException) as denied:
        workflow.approve_plan("p1", "c1", viewer.user_id, workflow.ApprovalRequest(revision_id=viewer_revision.revision_id, expected_hash="plan-hash"), viewer, viewer_repo)
    assert denied.value.status_code == 403


def test_repeated_plan_approval_is_idempotent_and_audited(tmp_path: Path) -> None:
    repo, revision, identity = _fixture(tmp_path)
    body = workflow.ApprovalRequest(revision_id=revision.revision_id, expected_hash="plan-hash")
    first = workflow.approve_plan("p1", "c1", identity.user_id, body, identity, repo)
    second = workflow.approve_plan("p1", "c1", identity.user_id, body, identity, repo)
    assert first == second
    assert first["stage"] == "PLAN_APPROVED"
    events = [event for event in repo.list_events(revision.thread_id) if event["action"] == "approve_plan"]
    assert len(events) == 1
    assert events[0]["actor"] == identity.user_id
    detail = json.loads(events[0]["detail_json"])
    assert detail == {
        "revision_id": revision.revision_id,
        "actor_role": "manager",
        "expected_hash": "plan-hash",
    }


def test_state_returns_current_revision_can_deploy_and_blockers(tmp_path: Path, monkeypatch) -> None:
    repo, _revision, identity = _fixture(tmp_path)
    state = workflow.get_workflow_state("p1", "c1", identity.user_id, identity, repo, include_current_revision=True)
    assert state["can_deploy"] is False
    assert "PLAN_NOT_APPROVED" in state["blockers"]
    assert "plan_md" in state["current_revision"]


def test_chat_approval_word_is_not_an_approval_tool(tmp_path: Path) -> None:
    from services.aml_builder.web.services.schemas import ChatMessage
    from services.aml_builder.web.workflow.runtime import record_tool_success

    repo, revision, _identity = _fixture(tmp_path)
    message = ChatMessage(role="user", content="approved")
    record_tool_success(repo, revision.thread_id, "user_message", {"content": message.content})
    current = repo.current(revision.thread_id)
    assert current and current.plan_approved_at is None
    assert repo.get_workflow(revision.thread_id).stage == "PLAN_PENDING"


def test_current_flow_uses_real_guards_with_per_gate_policy(tmp_path: Path) -> None:
    from services.aml_builder.web.workflow.guards import guard_tool
    from services.aml_builder.web.workflow.runtime import record_tool_success

    from services.aml_builder.web.services.config_loader import load_catalogs
    from services.aml_builder.web.services.settings import settings

    gate_modes = load_catalogs(settings.CATALOG_DIR).policy.workflow.enforcement.model_dump()
    assert gate_modes == {
        "approvals": "enforce",
        "card_confirmation": "enforce",
        "conflict_check": "enforce",
    }
    repo = WorkflowRepo(tmp_path / "full-flow.sqlite", enforcement=gate_modes)
    repo.get_or_create_workflow("p1_flow_999", "p1", "flow", "999")
    assert guard_tool(repo, "p1_flow_999", "analyze_intent_and_discover_explanation_codes", None).allowed
    intent = {
        "scenario_name": "Flow",
        "scenario_type": "CUSTOMER",
        "detection_logic": "Customers with large transactions",
        "thresholds": [{"field": "transaction_amount", "operator": ">", "value_from": 5000, "target_scope": "AGGREGATE", "provenance": "stated"}],
        "time_window": None,
        "aggregation": {"function": "SUM", "grain": "customer"},
        "clarifications": [], "clarification_questions": [], "ready_for_handoff": True,
        "applied_defaults": [],
    }
    record_tool_success(repo, "p1_flow_999", "analyze_intent_and_discover_explanation_codes", {"enriched_intent": intent, "scenario_metadata": {}, "explanation_code_checkpoint": None}, actor="999")
    assert repo.get_workflow("p1_flow_999").stage == "PARAMS_PENDING"
    assert repo.current("p1_flow_999") is None
    card = repo.get_workflow("p1_flow_999").pending_card
    assert card and any(row["key"] == "window" and row["status"] == "missing" for row in card["rows"])
    values = {}
    for row in card["rows"]:
        if row.get("read_only") or row["key"] == "window":
            continue
        if row.get("status") in {"provided", "inferred"} and row.get("value") is not None:
            values[row["key"]] = {
                "value": row["value"],
                "choice": "provided" if row["status"] == "provided" else "accepted_default",
                "provenance": row.get("provenance"),
            }
        elif row.get("status") == "defaulted" and row.get("recommended_value") is not None:
            values[row["key"]] = {"value": row["recommended_value"], "choice": "accepted_default", "provenance": row.get("provenance")}
    values["window"] = {"value": {"unit": "DAYS", "value": 30, "is_rolling": True}, "choice": "user_input"}
    confirmed = workflow.confirm_params(
        "p1", "flow", "999",
        workflow.ConfirmParamsRequest(request_id=card["request_id"], values=values),
        Identity("999", "", "p1"), repo,
    )
    revision_id = confirmed["current_revision"]["revision_id"]
    revision = repo.current("p1_flow_999")
    assert revision and revision.stage == "PLAN_PENDING" and revision.plan_md
    assert guard_tool(repo, "p1_flow_999", "generate_scenario_execution_plan", revision_id).allowed
    from services.aml_builder.web.domain.hashing import stable_hash
    repo.update_revision(revision_id, expected_version=repo.get_workflow("p1_flow_999").version, plan_md="# Flow", plan_hash=stable_hash("# Flow"))
    record_tool_success(repo, "p1_flow_999", "generate_scenario_execution_plan", {"plan_artifact": "# Flow"})
    identity = Identity("999", "", "p1")
    plan_hash = stable_hash("# Flow")
    state = workflow.approve_plan("p1", "flow", "999", workflow.ApprovalRequest(revision_id=revision_id, expected_hash=plan_hash), identity, repo)
    assert state["stage"] == "PLAN_APPROVED"
    assert guard_tool(repo, "p1_flow_999", "execute_oracle_dwh_shadow_test", revision_id).allowed
    from services.aml_builder.web.domain.hashing import stable_hash, normalize_sql
    sql = "SELECT 1 FROM DUAL"
    sql_hash = stable_hash(normalize_sql(sql))
    repo.update_revision(revision_id, expected_version=repo.get_workflow("p1_flow_999").version, shadow_json={"validation_success": True, "raw_sql": sql, "sql_hash": sql_hash}, sql_hash=sql_hash, stage="SHADOW_PASSED")
    assert guard_tool(repo, "p1_flow_999", "approve_result", revision_id).allowed
    state = workflow.approve_result("p1", "flow", "999", workflow.ApprovalRequest(revision_id=revision_id, expected_hash=sql_hash), identity, repo)
    assert state["stage"] == "RESULT_APPROVED"
    assert guard_tool(repo, "p1_flow_999", "prepare_scenario_metadata_for_persistence", revision_id).allowed
    record_tool_success(repo, "p1_flow_999", "prepare_scenario_metadata_for_persistence", {"fields": []})
    assert guard_tool(repo, "p1_flow_999", "deploy", revision_id).allowed
