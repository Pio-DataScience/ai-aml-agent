"""P6 pending-card actions, audit snapshots, and confirmed-intent authority."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from services.aml_builder.web.api.dependencies import Identity
from services.aml_builder.web.api.routes import workflow
from services.aml_builder.web.workflow.repo import WorkflowRepo
from services.aml_builder.web.workflow.runtime import record_tool_success
from services.aml_builder.web.services.tools import confirmed_intent_for_tool


def _repo(tmp_path: Path, *, enforcement: str = "enforce") -> WorkflowRepo:
    repo = WorkflowRepo(
        tmp_path / "p6.sqlite",
        enforcement={"approvals": "enforce", "card_confirmation": enforcement, "conflict_check": "observe"},
    )
    repo.get_or_create_workflow("p1_c1_999", "p1", "c1", "999")
    return repo


def _pending(repo: WorkflowRepo, *, request_id: str = "request-1") -> dict:
    intent = {
        "scenario_name": "High value customers",
        "scenario_type": "CUSTOMER",
        "detection_logic": "High value transactions",
        "thresholds": [],
        "time_window": None,
        "clarifications": [],
        "clarification_questions": [],
        "ready_for_handoff": True,
        "applied_defaults": [],
    }
    card = {
        "request_id": request_id,
        "rows": [
            {"key": "scenario_type", "status": "provided", "recommended_value": "CUSTOMER", "value": "CUSTOMER", "provenance": "stated", "required": True},
            {"key": "threshold#1", "status": "missing", "recommended_value": None, "value": 12000, "provenance": "stated", "required": True},
            {"key": "window", "status": "missing", "recommended_value": None, "value": {"unit": "DAYS", "value": 30, "is_rolling": True}, "provenance": "stated", "required": True},
        ],
        "intent": intent,
        "metadata": {},
        "explanation_code_checkpoint": None,
        "base_revision_id": None,
        "created_at": "2026-10-20T00:00:00+00:00",
    }
    repo.set_pending_card("p1_c1_999", card)
    repo.set_stage("p1_c1_999", "PARAMS_PENDING", actor="999", action="parameter_card_pending")
    repo.add_event("p1_c1_999", "999", "card_shown", detail={"request_id": request_id, "rows": card["rows"]})
    return card


def test_incomplete_confirmation_is_422_and_records_refusal_event_not_audit(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _pending(repo)
    with pytest.raises(HTTPException) as error:
        workflow.confirm_params(
            "p1", "c1", "999",
            workflow.ConfirmParamsRequest(request_id="request-1", values={"scenario_type": "CUSTOMER"}),
            Identity("999", "", "p1"), repo,
        )
    assert error.value.status_code == 422
    assert error.value.detail["error_code"] == "PARAMS_INCOMPLETE"
    assert "threshold#1" in error.value.detail["missing"]
    assert repo.card_audit_repo.get("request-1") is None
    events = [event for event in repo.list_events("p1_c1_999") if event["action"] == "card_refused"]
    assert len(events) == 1
    assert "threshold#1" in json.loads(events[0]["detail_json"])["missing"]


def test_invalid_confirmation_is_422_with_distinct_params_invalid_code(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _pending(repo)
    request = workflow.ConfirmParamsRequest(
        request_id="request-1",
        values={
            "scenario_type": "CUSTOMER",
            "threshold#1": {"comparator": ">", "value": 10, "scope": "AGGREGATE"},
            "window": {"unit": "DAYS", "value": 500, "is_rolling": True},
        },
    )
    with pytest.raises(HTTPException) as error:
        workflow.confirm_params("p1", "c1", "999", request, Identity("999", "", "p1"), repo)
    assert error.value.status_code == 422
    assert error.value.detail["error_code"] == "PARAMS_INVALID"
    assert error.value.detail["reasons"]
    assert repo.current("p1_c1_999") is None


def test_confirmation_snapshots_audit_once_and_applies_confirmed_intent(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _pending(repo)
    with pytest.raises(HTTPException) as refused:
        workflow.confirm_params(
            "p1", "c1", "999",
            workflow.ConfirmParamsRequest(request_id="request-1", values={"scenario_type": "CUSTOMER"}),
            Identity("999", "", "p1"), repo,
        )
    assert refused.value.status_code == 422
    result = workflow.confirm_params(
        "p1", "c1", "999",
        workflow.ConfirmParamsRequest(
            request_id="request-1",
            values={
                "scenario_type": {"value": "CUSTOMER", "choice": "accepted_default"},
                "threshold#1": {"value": {"comparator": ">", "value": 12000, "scope": "AGGREGATE"}, "choice": "user_input"},
                "window": {"value": {"unit": "DAYS", "value": 30, "is_rolling": True}, "choice": "user_input"},
            },
        ),
        Identity("999", "", "p1"), repo,
    )
    assert result["stage"] == "PLAN_PENDING"
    revision = repo.current("p1_c1_999")
    assert revision is not None
    stored = json.loads(revision.card_json)
    assert stored["intent"]["ready_for_handoff"] is True
    assert stored["intent"]["thresholds"][0]["value_from"] == 12000
    assert revision.plan_md
    audit = repo.card_audit_repo.get("request-1")
    assert audit["outcome"] == "confirmed"
    assert audit["attempts"] == 2
    assert len(repo.card_audit_repo.list_for_thread("p1_c1_999")) == 1
    assert repo.get_workflow("p1_c1_999").pending_card is None


def test_stale_request_rejected_and_new_card_supersedes_previous(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _pending(repo, request_id="old")
    _pending(repo, request_id="new")
    with pytest.raises(HTTPException) as error:
        workflow.discard_card(
            "p1", "c1", "999", workflow.DiscardCardRequest(request_id="old"), Identity("999", "", "p1"), repo,
        )
    assert error.value.status_code == 409
    assert error.value.detail["error_code"] == "CARD_STALE"
    old = repo.card_audit_repo.get("old")
    assert old["outcome"] == "superseded"


def test_card_actions_require_scenario_create_and_discard_is_audited(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _pending(repo)
    with pytest.raises(HTTPException) as denied:
        workflow.discard_card(
            "p1", "c1", "1002", workflow.DiscardCardRequest(request_id="request-1"), Identity("1002", "", "p1"), repo,
        )
    assert denied.value.status_code == 403
    result = workflow.discard_card(
        "p1", "c1", "999", workflow.DiscardCardRequest(request_id="request-1"), Identity("999", "", "p1"), repo,
    )
    assert result["pending_card"] is None
    assert repo.card_audit_repo.get("request-1")["outcome"] == "discarded"


def test_auto_confirm_records_resolution_audit(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    payload = {
        "enriched_intent": {
            "scenario_name": "Customer transactions",
            "scenario_type": "CUSTOMER",
            "detection_logic": "Monitor customers",
            "thresholds": [{"field": "transaction_amount", "operator": ">", "value_from": 10000, "target_scope": "AGGREGATE", "provenance": "stated"}],
            "time_window": {"unit": "DAYS", "value": 30, "is_rolling": True},
            "aggregation": {"function": "SUM", "grain": "customer"},
            "clarifications": [], "clarification_questions": [], "ready_for_handoff": True,
            "applied_defaults": [],
        },
        "scenario_metadata": {},
        "explanation_code_checkpoint": None,
    }
    record_tool_success(repo, "p1_c1_999", "analyze_intent_and_discover_explanation_codes", payload, actor="999")
    current = repo.current("p1_c1_999")
    assert current and current.stage == "PLAN_PENDING"
    rows = repo.card_audit_repo.list_for_thread("p1_c1_999")
    assert len(rows) == 1 and rows[0]["outcome"] == "auto_confirmed"
    assert repo.get_workflow("p1_c1_999").pending_card is None


def test_fake_llm_ready_flag_with_missing_window_still_creates_pending_card(tmp_path: Path, monkeypatch) -> None:
    from types import SimpleNamespace

    from services.aml_builder.web.services import tools
    from services.aml_builder.web.workflow import runtime

    repo = _repo(tmp_path, enforcement="observe")
    intent = {
        "scenario_name": "High value customers",
        "scenario_type": "CUSTOMER",
        "detection_logic": "Customers with high-value transactions",
        "thresholds": [],
        "time_window": None,
        "clarifications": [],
        "clarification_questions": [],
        "ready_for_handoff": True,
        "applied_defaults": [],
    }

    class FakeLLM:
        def invoke(self, _messages):
            return SimpleNamespace(content=json.dumps(intent))

    monkeypatch.setattr(tools, "build_llm", lambda: FakeLLM())
    monkeypatch.setattr(runtime, "get_workflow_repo", lambda: repo)
    config = {"configurable": {"thread_id": "p1_c1_999", "user_id": "999", "project_id": "p1"}}
    result = json.loads(tools.analyze_intent_and_discover_explanation_codes.func(
        user_prompt="customers with high-value transactions", config=config,
    ))
    assert "error" not in result
    pending = repo.get_workflow("p1_c1_999").pending_card
    assert pending is not None
    assert any(row["key"] == "window" and row["status"] == "missing" for row in pending["rows"])


def test_pending_card_survives_new_repository_instance(tmp_path: Path) -> None:
    path = tmp_path / "reload.sqlite"
    repo = WorkflowRepo(path)
    repo.get_or_create_workflow("p1_c1_999", "p1", "c1", "999")
    card = _pending(repo)
    reopened = WorkflowRepo(path).get_workflow("p1_c1_999")
    assert reopened and reopened.pending_card["request_id"] == card["request_id"]
    assert reopened.pending_card["rows"] == card["rows"]


def test_shadow_and_persist_tool_intent_is_taken_from_revision_and_mismatch_is_logged(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    confirmed = {"scenario_name": "Confirmed", "scenario_type": "CUSTOMER", "thresholds": [], "time_window": {"unit": "DAYS", "value": 30}}
    revision = repo.create_revision("p1_c1_999", card={"intent": confirmed}, stage="PLAN_APPROVED", created_by="999")
    result = confirmed_intent_for_tool(
        repo, "p1_c1_999", "execute_oracle_dwh_shadow_test",
        json.dumps({"scenario_name": "Injected", "thresholds": [{"value_from": 0}]}),
    )
    assert result == confirmed
    persisted_result = confirmed_intent_for_tool(
        repo, "p1_c1_999", "persist_and_validate_scenario_in_dwh",
        json.dumps({"scenario_name": "Injected again", "thresholds": []}),
    )
    assert persisted_result == confirmed
    events = [event for event in repo.list_events("p1_c1_999") if event["action"] == "intent_arg_mismatch"]
    assert len(events) == 2
    details = [json.loads(event["detail_json"]) for event in events]
    assert details == [
        {"tool": "execute_oracle_dwh_shadow_test", "revision_id": revision.revision_id},
        {"tool": "persist_and_validate_scenario_in_dwh", "revision_id": revision.revision_id},
    ]


def test_existing_plan_confirmation_resets_current_revision_in_place(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    revision = repo.create_revision(
        "p1_c1_999",
        card={"intent": {"scenario_name": "Before", "scenario_type": "CUSTOMER", "thresholds": [], "time_window": {"unit": "DAYS", "value": 30}}},
        stage="PLAN_PENDING", created_by="999",
    )
    repo.update_revision(
        revision.revision_id,
        expected_version=repo.get_workflow("p1_c1_999").version,
        plan_md="# Old plan", plan_hash="old", plan_approved_at="approved",
        plan_approved_by="999", shadow_json={"validation_success": True}, sql_hash="sql",
        result_approved_at="result-approved", result_approved_by="999",
    )
    card = _pending(repo, request_id="next")
    card["base_revision_id"] = revision.revision_id
    repo.set_pending_card("p1_c1_999", card)
    resolved = workflow.confirm_params(
        "p1", "c1", "999",
        workflow.ConfirmParamsRequest(request_id="next", values={
            "scenario_type": {"value": "CUSTOMER", "choice": "accepted_default"},
            "threshold#1": {"value": {"comparator": ">", "value": 12000, "scope": "AGGREGATE"}, "choice": "user_input"},
            "window": {"value": {"unit": "DAYS", "value": 30, "is_rolling": True}, "choice": "user_input"},
        }),
        Identity("999", "", "p1"), repo,
    )
    current = repo.current("p1_c1_999")
    assert current and current.revision_id == revision.revision_id
    assert current.plan_hash and current.plan_hash != "old"
    assert current.plan_approved_at is None
    assert current.shadow_json is None and current.sql_hash is None
    assert current.result_approved_at is None
    assert resolved["current_revision"]["revision_id"] == revision.revision_id


def test_new_analysis_uses_latest_intent_and_marks_changed_rows(tmp_path: Path) -> None:
    from services.aml_builder.web.workflow.cards import prepare_card

    repo = _repo(tmp_path, enforcement="observe")
    original = {
        "scenario_name": "Existing",
        "scenario_type": "CUSTOMER",
        "detection_logic": "Amount threshold",
        "thresholds": [{"field": "transaction_amount", "operator": ">", "value_from": 5000, "target_scope": "AGGREGATE", "provenance": "stated"}],
        "aggregation": {"function": "SUM", "grain": "customer"},
        "time_window": {"unit": "DAYS", "value": 30, "is_rolling": True},
        "clarifications": [], "clarification_questions": [], "ready_for_handoff": True,
        "applied_defaults": [],
    }
    repo.create_revision("p1_c1_999", card={"intent": original, "metadata": {"category": "kept"}}, stage="PLAN_PENDING", created_by="999")
    incoming = {
        **original,
        "thresholds": [{**original["thresholds"][0], "value_from": 9000}],
        "clarifications": [{"dimension": "threshold", "why_it_matters": "Confirm the changed threshold.", "question": "Use 9000?"}],
        "clarification_questions": ["Use 9000?"],
    }
    card = prepare_card(repo, "p1_c1_999", {"enriched_intent": incoming, "scenario_metadata": {}}, actor="999")
    threshold = next(row for row in card["rows"] if row["key"] == "threshold#1")
    assert threshold["changed"] is True
    assert card["intent"]["scenario_name"] == "Existing"
    assert card["metadata"] == {"category": "kept"}
