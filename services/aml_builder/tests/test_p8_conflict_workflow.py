"""P8 confirmation, acknowledgement, and enforcement-mode regression tests."""

from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

from services.aml_builder.web.api.dependencies import Identity
from services.aml_builder.web.api.routes import workflow as workflow_api
from services.aml_builder.web.domain.hashing import stable_hash
from services.aml_builder.web.workflow.cards import commit_confirmed_card
from services.aml_builder.web.workflow.repo import WorkflowConflict, WorkflowRepo


def _repo(tmp_path):
    repo = WorkflowRepo(
        tmp_path / "p8.sqlite",
        enforcement={"approvals": "enforce", "card_confirmation": "enforce", "conflict_check": "enforce"},
    )
    repo.get_or_create_workflow("p1_c1_999", "p1", "c1", "999")
    intent = {
        "scenario_name": "Cash activity",
        "scenario_type": "CUSTOMER",
        "detection_logic": "Cash activity over a threshold",
        "aggregation": {"grain": "per customer", "function": "SUM"},
        "thresholds": [{"field": "amount", "operator": ">", "value_from": 5000, "target_scope": "AGGREGATE"}],
        "time_window": {"value": 30, "unit": "DAYS", "is_rolling": True},
        "resolved_filters": [],
    }
    card = {
        "request_id": "card-1",
        "rows": [],
        "intent": intent,
        "metadata": {},
        "explanation_code_checkpoint": None,
        "base_revision_id": None,
        "created_at": "2026-10-20T00:00:00+00:00",
    }
    repo.set_pending_card("p1_c1_999", card)
    repo.set_stage("p1_c1_999", "PARAMS_PENDING", actor="999")
    return repo, card


class _Checker:
    def __init__(self, report):
        self.report = report

    def check_intent(self, intent, **kwargs):
        return dict(self.report)


def _report(status, *, matches=None):
    return {
        "revision_id": None,
        "provisional": False,
        "status": status,
        "conflict_hash": stable_hash(matches or []),
        "blocking": status == "blocked",
        "needs_ack": status == "needs_ack",
        "matches": matches or [],
    }


def _confirm(repo, card, checker):
    return commit_confirmed_card(
        repo,
        "p1_c1_999",
        card["request_id"],
        {},
        actor="999",
        role="manager",
        source="api",
        confirmation_hash=stable_hash({}),
        conflict_checker=checker,
    )


def test_clean_conflict_check_confirms_and_renders_plan(tmp_path):
    repo, card = _repo(tmp_path)
    result = _confirm(repo, card, _Checker(_report("clear")))
    assert result["revision"].stage == "PLAN_PENDING"
    assert result["revision"].plan_md
    assert result["revision"].signature_hash


def test_identical_block_keeps_pending_card_and_creates_no_revision(tmp_path):
    repo, card = _repo(tmp_path)
    report = _report("blocked", matches=[{"scenario_id": "SCN-1", "relation": "IDENTICAL", "action": "block"}])
    with pytest.raises(WorkflowConflict) as error:
        _confirm(repo, card, _Checker(report))
    assert error.value.code == "CONFLICT_BLOCKED"
    assert error.value.details == report
    assert repo.get_workflow("p1_c1_999").pending_card["request_id"] == "card-1"
    assert repo.current("p1_c1_999") is None


def test_warn_requires_ack_before_plan_and_writes_local_ack_event(tmp_path):
    repo, card = _repo(tmp_path)
    match = {"scenario_id": "SCN-2", "relation": "OVERLAP", "action": "warn_confirm"}
    report = _report("needs_ack", matches=[match])
    result = _confirm(repo, card, _Checker(report))
    revision = result["revision"]
    assert revision.stage == "PARAMS_CONFIRMED"
    assert revision.plan_md is None
    assert repo.get_workflow("p1_c1_999").stage == "PARAMS_CONFIRMED"
    acknowledged = repo.acknowledge_conflicts(
        "p1_c1_999",
        revision.revision_id,
        conflict_hash=report["conflict_hash"],
        actor="999",
        role="manager",
        reason="Reviewed overlap",
        plan_md="# Reviewed plan",
        plan_hash=stable_hash("# Reviewed plan"),
    )
    assert acknowledged.stage == "PLAN_PENDING"
    stored = json.loads(acknowledged.conflict_json)
    assert stored["status"] == "acknowledged"
    assert stored["acknowledgements"][0]["actor"] == "999"
    assert stored["acknowledgements"][0]["role"] == "manager"
    assert stored["acknowledgements"][0]["reason"] == "Reviewed overlap"
    events = [item for item in repo.list_events("p1_c1_999") if item["action"] == "conflict_acknowledged"]
    assert len(events) == 1
    assert json.loads(events[0]["detail_json"])["revision_id"] == revision.revision_id


def test_unavailable_registry_does_not_block_confirmation(tmp_path):
    repo, card = _repo(tmp_path)
    report = _report("unavailable")
    report["warning"] = "registry unavailable"
    result = _confirm(repo, card, _Checker(report))
    assert result["revision"].stage == "PLAN_PENDING"
    assert json.loads(result["revision"].conflict_json)["status"] == "unavailable"


def test_full_route_flow_clean_pass_blocked_match_and_warn_acknowledgement(tmp_path, monkeypatch):
    clean_repo, clean_card = _repo(tmp_path / "clean")
    monkeypatch.setattr(workflow_api, "make_conflict_checker", lambda _repo: _Checker(_report("clear")))
    clean = workflow_api.confirm_params(
        "p1", "c1", "999",
        workflow_api.ConfirmParamsRequest(request_id=clean_card["request_id"], values={}),
        Identity("999", "", "p1"), clean_repo,
    )
    assert clean["stage"] == "PLAN_PENDING"
    assert clean["current_revision"]["plan_md"]

    blocked_repo, blocked_card = _repo(tmp_path / "blocked")
    blocked_report = _report("blocked", matches=[{"scenario_id": "DUP", "relation": "IDENTICAL", "action": "block"}])
    monkeypatch.setattr(workflow_api, "make_conflict_checker", lambda _repo: _Checker(blocked_report))
    with pytest.raises(HTTPException) as blocked:
        workflow_api.confirm_params(
            "p1", "c1", "999",
            workflow_api.ConfirmParamsRequest(request_id=blocked_card["request_id"], values={}),
            Identity("999", "", "p1"), blocked_repo,
        )
    assert blocked.value.detail["error_code"] == "CONFLICT_BLOCKED"
    assert blocked_repo.get_workflow("p1_c1_999").pending_card["request_id"] == blocked_card["request_id"]

    warn_repo, warn_card = _repo(tmp_path / "warn")
    warn_report = _report("needs_ack", matches=[{"scenario_id": "WARN", "relation": "OVERLAP", "action": "warn_confirm"}])
    monkeypatch.setattr(workflow_api, "make_conflict_checker", lambda _repo: _Checker(warn_report))
    pending_ack = workflow_api.confirm_params(
        "p1", "c1", "999",
        workflow_api.ConfirmParamsRequest(request_id=warn_card["request_id"], values={}),
        Identity("999", "", "p1"), warn_repo,
    )
    assert pending_ack["stage"] == "PARAMS_CONFIRMED"
    assert pending_ack["current_revision"]["plan_md"] is None
    acknowledged = workflow_api.acknowledge_conflicts(
        "p1", "c1", "999",
        workflow_api.ConflictAcknowledgementRequest(
            revision_id=pending_ack["current_revision"]["revision_id"],
            conflict_hash=warn_report["conflict_hash"],
            choice="proceed",
        ),
        Identity("999", "", "p1"), warn_repo,
    )
    assert acknowledged["stage"] == "PLAN_PENDING"
    assert acknowledged["current_revision"]["plan_md"]
