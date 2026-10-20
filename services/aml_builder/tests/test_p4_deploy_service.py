"""Fake-backed P4 deployment contract tests; these never open a database."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from services.aml_builder.web.domain.deploy_service import (
    DeployError,
    DeployService,
    stable_hash_sql,
)
from services.aml_builder.web.workflow.models import Revision, WorkflowRow


def _fixture(*, enforcement: str = "enforce"):
    workflow = WorkflowRow(
        thread_id="project_chat_user",
        project_id="project",
        chat_id="chat",
        user_id="user",
        stage="METADATA_READY",
        locked=False,
        version=4,
        scenario_id="PRD_ABCDEF12_V1",
        scenario_key="PRD_ABCDEF12",
        current_revision_id="revision-1",
        deployed_revision_id=None,
        pending_card=None,
        created_at="now",
        updated_at="now",
    )
    revision = Revision(
        revision_id="revision-1", thread_id=workflow.thread_id, rev_no=1,
        status="current", stage="METADATA_READY", parent_revision_id=None,
        restored_from_revision_id=None, card_json=json.dumps({"intent": {
            "scenario_name": "Scenario", "scenario_type": "CUSTOMER",
            "detection_logic": "Detection", "time_window": {"value": 30, "unit": "days"},
        }}), params_hash="params-hash", signature_json=None, signature_hash=None,
        conflict_json=None, plan_md="# Plan", plan_hash="plan-hash",
        plan_approved_at="approved", plan_approved_by="user",
        shadow_json=json.dumps({"validation_success": True, "raw_sql": "SELECT 1", "sql_hash": stable_hash_sql("SELECT 1")}),
        sql_hash=stable_hash_sql("SELECT 1"), result_approved_at="approved", result_approved_by="user",
        metadata_json=json.dumps({"COUNTRY_CODE": 1}), metadata_hash="metadata-hash",
        created_at="now", created_by="user", superseded_at=None,
        superseded_by_revision_id=None, evicted_at=None, version=1,
    )

    class FakeWorkflow:
        def __init__(self):
            self.enforcement = enforcement
            self.workflow = workflow
            self.revision = revision
            self.fail_mark = False
            self.mark_calls = 0

        def get_workflow(self, thread_id):
            return self.workflow if thread_id == workflow.thread_id else None

        def current(self, thread_id):
            return self.revision if thread_id == workflow.thread_id else None

        def get_revision(self, revision_id):
            return self.revision if revision_id == self.revision.revision_id else None

        def save_conflict_report(self, thread_id, revision_id, report, *, actor="system"):
            self.revision = replace(self.revision, conflict_json=json.dumps(report))

        def mark_deployed(self, thread_id, revision_id):
            self.mark_calls += 1
            if self.fail_mark:
                raise RuntimeError("simulated local state failure")
            self.workflow = replace(
                self.workflow, locked=True, stage="DEPLOYED",
                deployed_revision_id=revision_id,
            )
            self.revision = replace(self.revision, status="deployed", stage="DEPLOYED")

    class FakeOracle:
        def __init__(self):
            self.rows = {}
            self.metadata_active = {}
            self.audit = []
            self.calls = 0
            self.fail_duplicate = False
            self.fail_parent_commit = False

        def get_by_thread(self, thread_id):
            return next((row for row in self.rows.values() if row["thread_id"] == thread_id), None)

        def insert_version(self, row):
            self.calls += 1
            if self.fail_duplicate:
                raise DeployError("DUPLICATE_ACTIVE_SIGNATURE", "duplicate")
            old_rows = {key: dict(value) for key, value in self.rows.items()}
            old_metadata = dict(self.metadata_active)
            old_audit = list(self.audit)
            if row.get("parent_scenario_id"):
                parent = self.rows[row["parent_scenario_id"]]
                parent["status"] = "SUPERSEDED"
                parent["is_active"] = 0
                self.metadata_active[row["parent_scenario_id"]] = False
                self.audit.append({"scenario_id": row["parent_scenario_id"], "action": "SUPERSEDE"})
                if self.fail_parent_commit:
                    self.rows = old_rows
                    self.metadata_active = old_metadata
                    self.audit = old_audit
                    raise RuntimeError("simulated atomic rollback")
            self.rows[row["scenario_id"]] = row
            self.audit.append({"scenario_id": row["scenario_id"], "action": row["audit_action"]})
            return row

    workflow_repo = FakeWorkflow()
    oracle_repo = FakeOracle()
    service = DeployService(
        oracle_repo=oracle_repo,
        workflow_repo=workflow_repo,
        metadata_validator=lambda _metadata: (True, []),
        authorizer=lambda _actor, _action: True,
        enforcement=enforcement,
    )
    command = {
        "thread_id": workflow.thread_id,
        "revision_id": revision.revision_id,
        "actor_id": "user",
        "actor_role": "creator",
        "raw_sql": "SELECT 1",
        "metadata": {"COUNTRY_CODE": 1},
    }
    return service, workflow_repo, oracle_repo, command


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (lambda w, r: (replace(w, locked=True), r), "CHAT_LOCKED_DEPLOYED"),
        (lambda w, r: (replace(w, pending_card={"request_id": "pending"}), r), "CARD_PENDING"),
        (lambda w, r: (w, replace(r, stage="PLAN_PENDING")), "STAGE_NOT_ALLOWED"),
        (lambda w, r: (w, replace(r, shadow_json=json.dumps({"validation_success": False}))), "SHADOW_NOT_PASSED"),
        (lambda w, r: (w, replace(r, sql_hash="wrong")), "HASH_MISMATCH"),
    ],
)
def test_deploy_hard_guards(mutation, code):
    service, workflow_repo, _oracle, command = _fixture()
    workflow_repo.workflow, workflow_repo.revision = mutation(
        workflow_repo.workflow, workflow_repo.revision
    )
    with pytest.raises(DeployError) as error:
        service.deploy(**command)
    assert error.value.code == code


def test_deploy_rejects_stale_revision_before_registry_write():
    service, _workflow_repo, oracle, command = _fixture()
    command["revision_id"] = "stale-revision"
    with pytest.raises(DeployError) as error:
        service.deploy(**command)
    assert error.value.code == "REVISION_STALE"
    assert oracle.calls == 0


def test_enforce_mode_requires_approvals_but_observe_records_would_block():
    service, workflow_repo, oracle, command = _fixture(enforcement="enforce")
    workflow_repo.revision = replace(
        workflow_repo.revision, plan_approved_at=None, result_approved_at=None
    )
    with pytest.raises(DeployError) as error:
        service.deploy(**command)
    assert error.value.code == "PLAN_NOT_APPROVED"
    assert oracle.calls == 0

    service, workflow_repo, _oracle, command = _fixture(enforcement="enforce")
    workflow_repo.revision = replace(
        workflow_repo.revision, result_approved_at=None
    )
    with pytest.raises(DeployError) as result_error:
        service.deploy(**command)
    assert result_error.value.code == "RESULT_NOT_APPROVED"

    service, workflow_repo, oracle, command = _fixture(enforcement="observe")
    workflow_repo.revision = replace(
        workflow_repo.revision, plan_approved_at=None, result_approved_at=None
    )
    result = service.deploy(**command)
    assert result.receipt["approval_status"] == "would_block"
    assert oracle.calls == 1


def test_retry_after_workflow_lock_failure_completes_without_rewriting():
    service, workflow, oracle, command = _fixture()
    workflow.fail_mark = True
    with pytest.raises(RuntimeError, match="local state failure"):
        service.deploy(**command)
    assert oracle.calls == 1
    workflow.fail_mark = False

    result = service.deploy(**command)
    assert result.scenario_id == "PRD_ABCDEF12_V1"
    assert oracle.calls == 1
    assert workflow.workflow.locked
    assert workflow.mark_calls == 2


def test_parent_supersession_is_included_in_the_single_repository_operation():
    service, _workflow, oracle, command = _fixture()
    oracle.rows["PRD_ABCDEF12_V1"] = {
        "scenario_id": "PRD_ABCDEF12_V1", "status": "ACTIVE", "is_active": 1,
        "thread_id": "parent-thread",
    }
    oracle.metadata_active["PRD_ABCDEF12_V1"] = True
    command.update({
        "parent_scenario_id": "PRD_ABCDEF12_V1",
        "scenario_id": "PRD_ABCDEF12_V2",
        "scenario_key": "PRD_ABCDEF12",
        "version_no": 2,
        "change_reason": "Clarified detection threshold",
    })
    result = service.deploy(**command)
    saved = oracle.rows[result.scenario_id]
    assert saved["parent_scenario_id"] == "PRD_ABCDEF12_V1"
    assert saved["version_no"] == 2
    assert saved["audit_action"] == "REVISE"
    assert oracle.rows["PRD_ABCDEF12_V1"]["status"] == "SUPERSEDED"
    assert oracle.rows["PRD_ABCDEF12_V1"]["is_active"] == 0
    assert oracle.metadata_active["PRD_ABCDEF12_V1"] is False
    assert [entry["action"] for entry in oracle.audit] == ["SUPERSEDE", "REVISE"]


def test_parent_supersession_rolls_back_as_one_fake_repository_operation():
    service, _workflow, oracle, command = _fixture()
    oracle.rows["PRD_ABCDEF12_V1"] = {
        "scenario_id": "PRD_ABCDEF12_V1", "status": "ACTIVE", "is_active": 1,
        "thread_id": "parent-thread",
    }
    oracle.metadata_active["PRD_ABCDEF12_V1"] = True
    oracle.fail_parent_commit = True
    command.update({
        "parent_scenario_id": "PRD_ABCDEF12_V1",
        "scenario_id": "PRD_ABCDEF12_V2",
        "scenario_key": "PRD_ABCDEF12",
        "version_no": 2,
        "change_reason": "Clarified detection threshold",
    })
    with pytest.raises(RuntimeError, match="atomic rollback"):
        service.deploy(**command)
    assert oracle.rows["PRD_ABCDEF12_V1"]["status"] == "ACTIVE"
    assert oracle.metadata_active["PRD_ABCDEF12_V1"] is True
    assert oracle.audit == []
    assert "PRD_ABCDEF12_V2" not in oracle.rows


def test_permission_metadata_and_duplicate_errors_are_typed():
    service, _workflow, oracle, command = _fixture()
    service.authorizer = lambda _actor, _action: False
    with pytest.raises(DeployError) as denied:
        service.deploy(**command)
    assert denied.value.code == "PERMISSION_DENIED"

    service, _workflow, _oracle, command = _fixture()
    service.metadata_validator = lambda _metadata: (False, ["bad metadata"])
    with pytest.raises(DeployError) as invalid:
        service.deploy(**command)
    assert invalid.value.code == "METADATA_INVALID"

    service, _workflow, oracle, command = _fixture()
    oracle.fail_duplicate = True
    with pytest.raises(DeployError) as duplicate:
        service.deploy(**command)
    assert duplicate.value.code == "DUPLICATE_ACTIVE_SIGNATURE"


def test_nullable_signature_and_conflict_gate_are_in_receipt():
    service, _workflow, oracle, command = _fixture(enforcement="observe")
    result = service.deploy(**command)
    assert oracle.rows[result.scenario_id]["signature_hash"] is None
    assert result.receipt["conflict"] == {"status": "not_run", "acknowledgements": [], "needs_ack": False}


def test_required_conflict_gate_rejects_default_not_run_status():
    service, _workflow, oracle, command = _fixture()
    service.conflict_check_required = True
    with pytest.raises(DeployError) as error:
        service.deploy(**command)
    assert error.value.code == "CONFLICT_CHECK_REQUIRED"
    assert oracle.calls == 0


def test_scenario_id_format_matches_versioned_registry():
    service, workflow, _oracle, command = _fixture()
    assert workflow.workflow.scenario_id == "PRD_ABCDEF12_V1"
    assert workflow.workflow.scenario_key == "PRD_ABCDEF12"
    assert service


def test_registry_sql_has_no_forbidden_mutation_statements_and_keeps_alert_columns():
    root = Path(__file__).parents[3]
    adapter = root / "services" / "aml_builder" / "web" / "adapters" / "oracle_repo.py"
    ddl = root / "scripts" / "registry_v2.sql"
    source = adapter.read_text(encoding="utf-8")
    sql_script = ddl.read_text(encoding="utf-8")
    forbidden = ("DELETE FROM", "DROP TABLE", "DROP INDEX", "TRUNCATE", "ALTER TABLE")
    for marker in forbidden:
        assert marker not in source.upper()
    for column in (
        "SCENARIO_ID", "SCENARIO_NAME", "SCENARIO_TYPE", "RAW_SQL",
        "TIME_WINDOW_DAYS", "IS_ACTIVE", "CREATED_AT",
    ):
        assert column in sql_script.upper()
