"""Fake-backed P9 change-management contract tests."""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from services.aml_builder.web.domain.versioning_service import (
    VersioningError,
    VersioningService,
    compare_version_payloads,
    intent_from_registry_row,
)


def _row(
    scenario_id="PRD_ABCDEF12_V2",
    version_no=2,
    status="ACTIVE",
    *,
    signature="sig-2",
    metadata_hash="meta-2",
):
    intent = {
        "scenario_name": "Structuring",
        "scenario_type": "TRANSACTION",
        "detection_logic": "Repeated deposits",
        "time_window": {"value": 30, "unit": "days"},
        "filters": [{"dimension": "explanation_code", "values": ["CASH"]}],
        "resolved_filters": [{"dimension": "explanation_code", "values": ["CASH"]}],
        "thresholds": [],
        "threshold": {"field": "transaction_amount", "operator": ">", "value_from": 10},
    }
    return {
        "scenario_id": scenario_id,
        "scenario_key": "PRD_ABCDEF12",
        "version_no": version_no,
        "status": status,
        "is_active": int(status == "ACTIVE"),
        "scenario_name": "Structuring",
        "created_by": "999",
        "created_at": "2026-01-01T00:00:00Z",
        "parent_scenario_id": "PRD_ABCDEF12_V1" if version_no > 1 else None,
        "signature_hash": signature,
        "metadata_hash": metadata_hash,
        "params_json": json.dumps({"intent": intent}),
        "filters_json": json.dumps(intent["resolved_filters"]),
        "metadata_json": json.dumps({"COUNTRY_CODE": 1}),
        "signature_json": json.dumps({"scenario_type": "TRANSACTION"}),
    }


class FakeRegistry:
    def __init__(self, rows):
        self.rows = {row["scenario_id"]: row for row in rows}
        self.audit = []
        self.status_calls = []

    def get_version(self, scenario_id):
        return self.rows.get(scenario_id)

    def list_versions(self, scenario_key):
        return sorted(
            [row for row in self.rows.values() if row["scenario_key"] == scenario_key],
            key=lambda row: row["version_no"],
            reverse=True,
        )

    def latest_version(self, scenario_key):
        versions = self.list_versions(scenario_key)
        return versions[0] if versions else None

    def latest_active_version(self, scenario_key):
        return next((row for row in self.rows.values() if row["scenario_key"] == scenario_key and row["status"] == "ACTIVE"), None)

    def set_statuses(self, changes, *, actor, actor_role, reason, audit_action, audit_detail=None):
        self.status_calls.append((changes, actor, actor_role, reason, audit_action, audit_detail))
        for scenario_id, expected, status in changes:
            row = self.rows[scenario_id]
            if row["status"] != expected:
                raise VersioningError("VERSION_STALE", "Version status changed.")
            row["status"] = status
            row["is_active"] = int(status == "ACTIVE")
            self.audit.append({
                "scenario_id": scenario_id, "action": audit_action, "actor": actor,
                "actor_role": actor_role, "reason": reason, "from_status": expected,
                "to_status": status, "ts": "2026-01-01T00:00:00Z",
                "detail": audit_detail or {},
            })


class FakeWorkflow:
    def __init__(self):
        self.drafts = []

    def find_open_child_chat(self, parent_scenario_id, parent_version, user_id):
        return next((
            item for item in self.drafts
            if item["parent_scenario_id"] == parent_scenario_id
            and item["parent_version"] == parent_version
            and item["user_id"] == user_id
        ), None)

    def create_version_chat(self, **values):
        self.drafts.append(values)
        return values


def _service(rows, *, allowed=True, conflict=None):
    registry = FakeRegistry(rows)
    workflow = FakeWorkflow()
    service = VersioningService(
        registry_repo=registry,
        workflow_repo=workflow,
        authorizer=lambda _actor, _action: allowed,
        conflict_checker=conflict or (lambda _signature, _scenario_id: {"status": "clear", "matches": []}),
    )
    return service, registry, workflow


def test_create_new_version_seeds_from_registry_and_reuses_same_users_open_draft():
    row = _row()
    service, _registry, workflow = _service([row])
    first = service.create_new_version(
        row["scenario_id"], project_id="project", user_id="999", actor_id="999", actor_role="manager"
    )
    again = service.create_new_version(
        row["scenario_id"], project_id="project", user_id="999", actor_id="999", actor_role="manager"
    )
    assert first["new_chat_id"] == again["new_chat_id"]
    assert first["parent_scenario_id"] == row["scenario_id"]
    assert first["parent_version"] == 2
    assert first["reserved_version"] == 3
    draft = workflow.drafts[0]
    assert draft["stage"] == "PARAMS_PENDING"
    assert draft["pending_card"]["intent"]["scenario_name"] == "Structuring"
    assert draft["pending_card"]["intent"]["resolved_filters"] == json.loads(row["filters_json"])
    assert draft["pending_card"]["metadata"] == json.loads(row["metadata_json"])


def test_create_new_version_rejects_missing_stored_intent_fields_with_field_names():
    row = _row()
    row["params_json"] = json.dumps({"intent": {"scenario_name": "Structuring"}})
    with pytest.raises(VersioningError) as error:
        intent_from_registry_row(row)
    assert error.value.code == "VERSION_SOURCE_INCOMPLETE"
    assert "scenario_type" in error.value.details


@pytest.mark.parametrize("status", ["SUPERSEDED", "RETIRED"])
def test_create_new_version_requires_latest_version_of_key(status):
    latest = _row(status=status)
    newer = _row("PRD_ABCDEF12_V3", 3, "ACTIVE")
    service, _registry, _workflow = _service([latest, newer])
    with pytest.raises(VersioningError) as error:
        service.create_new_version(
            latest["scenario_id"], project_id="project", user_id="999", actor_id="999", actor_role="manager"
        )
    assert error.value.code == "REVISE_STALE"


def test_rollback_requires_expected_active_version_and_acknowledges_warn_conflicts():
    active = _row("PRD_ABCDEF12_V3", 3, "ACTIVE")
    target = _row("PRD_ABCDEF12_V2", 2, "SUPERSEDED")
    service, registry, _workflow = _service(
        [active, target], conflict=lambda _signature, _id: {
            "status": "needs_ack", "blocking": False, "needs_ack": True,
            "matches": [{"scenario_id": "PRD_OTHER_V1", "relation": "COVERED"}],
        }
    )
    with pytest.raises(VersioningError) as stale:
        service.rollback(
            target["scenario_id"], expected_active_version=2, reason="restore", actor_id="999", actor_role="manager"
        )
    assert stale.value.code == "VERSION_STALE"
    result = service.rollback(
        target["scenario_id"], expected_active_version=3, reason="restore old rule",
        actor_id="999", actor_role="manager", acknowledge_warnings=True,
    )
    assert result["status"] == "ACTIVE"
    assert registry.rows[active["scenario_id"]]["status"] == "SUPERSEDED"
    assert registry.rows[target["scenario_id"]]["status"] == "ACTIVE"
    assert registry.audit[-1]["action"] == "ROLLBACK"
    assert registry.audit[-1]["reason"] == "restore old rule"
    assert registry.audit[-1]["actor"] == "999"
    assert registry.audit[-1]["actor_role"] == "manager"
    assert registry.audit[-1]["from_status"] == "SUPERSEDED"
    assert registry.audit[-1]["to_status"] == "ACTIVE"
    assert registry.audit[-1]["ts"]


def test_retire_is_soft_and_permission_checked():
    active = _row()
    service, registry, _workflow = _service([active])
    service.retire(active["scenario_id"], reason="policy change", actor_id="999", actor_role="manager")
    assert registry.rows[active["scenario_id"]]["status"] == "RETIRED"
    assert registry.rows[active["scenario_id"]]["is_active"] == 0
    assert registry.audit[-1]["action"] == "RETIRE"
    assert "DELETE" not in Path(__file__).parents[1].joinpath("web/adapters/oracle_repo.py").read_text(encoding="utf-8").upper()


@pytest.mark.parametrize(
    ("role", "action", "allowed"),
    [
        ("viewer", "scenario.revise", False), ("creator", "scenario.revise", False), ("manager", "scenario.revise", True),
        ("viewer", "scenario.rollback", False), ("creator", "scenario.rollback", False), ("manager", "scenario.rollback", True),
        ("viewer", "scenario.delete", False), ("creator", "scenario.delete", False), ("manager", "scenario.delete", True),
    ],
)
def test_change_management_permissions_leave_denied_actions_unchanged(role, action, allowed):
    permissions = {
        "viewer": {"chat.read"},
        "creator": {"chat.read", "scenario.create"},
        "manager": {"chat.read", "scenario.create", "scenario.revise", "scenario.rollback", "scenario.delete"},
    }
    active = _row()
    superseded = _row("PRD_ABCDEF12_V1", 1, "SUPERSEDED")
    service, registry, _workflow = _service([active, superseded], allowed=action in permissions[role])
    before = {key: dict(value) for key, value in registry.rows.items()}
    with pytest.raises(VersioningError) if not allowed else _no_error():
        if action == "scenario.revise":
            service.create_new_version(active["scenario_id"], project_id="p", user_id="u", actor_id="u", actor_role=role)
        elif action == "scenario.rollback":
            service.rollback(superseded["scenario_id"], expected_active_version=2, reason="restore policy", actor_id="u", actor_role=role)
        else:
            service.retire(active["scenario_id"], reason="retirement reason", actor_id="u", actor_role=role)
    if not allowed:
        assert registry.rows == before
        assert registry.status_calls == []


class _no_error:
    """Context manager allowing the same table-driven action body for allowed roles."""

    def __enter__(self):
        return None

    def __exit__(self, exc_type, _exc, _tb):
        return exc_type is None


def test_compare_versions_returns_deterministic_stored_json_diff():
    left = {"intent": {"threshold": 10, "filters": ["A"]}, "metadata": {"risk": "H"}}
    right = {"intent": {"threshold": 20, "filters": ["A", "B"]}, "metadata": {"risk": "H"}}
    assert compare_version_payloads(left, right) == compare_version_payloads(left, right)
    assert compare_version_payloads(left, right)["changes"]


def test_alert_engine_only_selects_active_production_and_metadata_rows():
    source = Path(__file__).parents[1].joinpath("web/services/alert_engine.py").read_text(encoding="utf-8").upper()
    assert "IS_ACTIVE = 1" in source or "IS_ACTIVE=1" in source
    assert "ACTIVE_FLAG" in source


def test_history_read_is_available_to_viewer_creator_and_manager(tmp_path, monkeypatch):
    from services.aml_builder.web.api.dependencies import Identity
    from services.aml_builder.web.api.routes import workflow as workflow_route
    from services.aml_builder.web.workflow.repo import WorkflowRepo

    class FakeRegistry:
        def latest_version(self, _key):
            return {"scenario_id": "PRD_ABCDEF12_V2"}

        def list_versions(self, _key):
            return [{
                "version_no": 2, "scenario_id": "PRD_ABCDEF12_V2", "status": "ACTIVE",
                "scenario_name": "Rule", "created_by": "999", "created_at": "now",
                "parent_scenario_id": "PRD_ABCDEF12_V1", "change_reason": "Changed threshold",
            }]

        def list_version_audit(self, _key):
            return [{"scenario_id": "PRD_ABCDEF12_V2", "action": "REVISE", "actor": "999", "ts": "now", "reason": "Changed threshold"},
                    {"scenario_id": "PRD_ABCDEF12_V2", "action": "OVERRIDE_CONFLICT"}]

    monkeypatch.setattr(workflow_route, "OracleRegistryRepo", FakeRegistry)
    for user_id in ("1002", "1001", "999"):
        repo = WorkflowRepo(tmp_path / f"{user_id}.sqlite")
        repo.get_or_create_workflow(f"project_chat_{user_id}", "project", "chat", user_id)
        with repo._connection() as connection:
            connection.execute(
                "UPDATE scenario_workflow SET locked=1,stage='DEPLOYED',scenario_id=?,scenario_key=? WHERE thread_id=?",
                ("PRD_ABCDEF12_V2", "PRD_ABCDEF12", f"project_chat_{user_id}"),
            )
        result = workflow_route.list_scenario_versions(
            "project", "chat", user_id, Identity(user_id, "", "project"), repo
        )
        assert result["versions"][0]["last_status_change"]["action"] == "REVISE"
        assert result["versions"][0]["override_conflict_count"] == 1


def test_history_route_rejects_non_latest_chat(tmp_path, monkeypatch):
    from fastapi import HTTPException
    from services.aml_builder.web.api.dependencies import Identity
    from services.aml_builder.web.api.routes import workflow as workflow_route
    from services.aml_builder.web.workflow.repo import WorkflowRepo

    repo = WorkflowRepo(tmp_path / "stale.sqlite")
    repo.get_or_create_workflow("project_chat_999", "project", "chat", "999")
    with repo._connection() as connection:
        connection.execute(
            "UPDATE scenario_workflow SET locked=1,stage='DEPLOYED',scenario_id=?,scenario_key=? WHERE thread_id=?",
            ("PRD_ABCDEF12_V1", "PRD_ABCDEF12", "project_chat_999"),
        )

    class FakeRegistry:
        def latest_version(self, _key):
            return {"scenario_id": "PRD_ABCDEF12_V2"}

    monkeypatch.setattr(workflow_route, "OracleRegistryRepo", FakeRegistry)
    with pytest.raises(HTTPException) as error:
        workflow_route.list_scenario_versions("project", "chat", "999", Identity("999", "", "project"), repo)
    assert error.value.detail["error_code"] == "REVISE_STALE"


def test_new_version_code_has_no_delete_sql():
    source_paths = [
        Path(__file__).parents[1] / "web" / "domain" / "versioning_service.py",
        Path(__file__).parents[1] / "web" / "adapters" / "oracle_repo.py",
        Path(__file__).parents[1] / "web" / "api" / "routes" / "workflow.py",
    ]
    for source_path in source_paths:
        if source_path.exists():
            assert "DELETE FROM" not in source_path.read_text(encoding="utf-8").upper()


def test_registry_supersedes_parent_before_same_signature_version_insert(monkeypatch):
    from services.aml_builder.web.adapters import oracle_repo

    statements = []

    class Cursor:
        rowcount = 1
        current = None

        def execute(self, sql, _params=None):
            self.current = sql
            statements.append(sql)
            normalized = " ".join(sql.upper().split())
            if "SELECT SCENARIO_KEY,VERSION_NO,STATUS" in normalized:
                self.result = ("PRD_ABCDEF12", 1, "ACTIVE")
            elif "SELECT MAX(VERSION_NO)" in normalized:
                self.result = (1,)
            elif "SELECT SCENARIO_KEY, STATUS" in normalized:
                self.result = ("PRD_ABCDEF12", "ACTIVE")
            elif "SELECT SCENARIO_KEY,METADATA_JSON" in normalized:
                self.result = ("PRD_ABCDEF12", '{"COUNTRY_CODE":1,"INST_CODE":2}')
            else:
                self.result = None

        def fetchone(self):
            return self.result

    cursor = Cursor()

    @contextmanager
    def transaction():
        yield SimpleNamespace(cursor=lambda: cursor)

    monkeypatch.setattr(oracle_repo, "atomic_connection", transaction)
    row = {
        "scenario_id": "PRD_ABCDEF12_V2", "scenario_key": "PRD_ABCDEF12", "version_no": 2,
        "parent_scenario_id": "PRD_ABCDEF12_V1", "parent_version": 1, "parent_status": "ACTIVE",
        "status": "ACTIVE", "is_active": 1, "thread_id": "p_c_u", "revision_id": "r2", "rev_no": 1,
        "signature_hash": "same-signature", "params_hash": "params", "plan_hash": "plan", "sql_hash": "sql",
        "metadata_hash": "changed-metadata", "signature_json": {"signature": "same"},
        "params_json": {"intent": {"scenario_name": "Rule"}}, "filters_json": [],
        "metadata_json": {"COUNTRY_CODE": 1, "INST_CODE": 2}, "receipt_json": {}, "change_reason": "metadata update",
        "scenario_name": "Rule", "scenario_type": "TRANSACTION", "detection_logic": "Rule",
        "raw_sql": "SELECT 1", "time_window_days": 30, "created_by": "999", "created_by_number": 999,
        "actor_role": "manager", "audit_action": "REVISE",
    }
    oracle_repo.OracleRegistryRepo().insert_version(row)
    update_position = next(index for index, sql in enumerate(statements) if "UPDATE PIO_AML_AGENT_PRODUCTION_SCENARIOS" in sql)
    insert_position = next(index for index, sql in enumerate(statements) if "INSERT INTO PIO_AML_AGENT_PRODUCTION_SCENARIOS" in sql)
    assert update_position < insert_position
    assert "STATUS=:FROM_STATUS" in statements[update_position].upper()
    assert any("UPDATE PIO_AML_AGENT_SCENARIOS" in sql for sql in statements[:insert_position])
    assert any("INSERT INTO PIO_AML_AGENT_SCENARIO_AUDIT" in sql for sql in statements[:insert_position])


def test_registry_parent_change_after_another_deploy_is_typed_stale(monkeypatch):
    from services.aml_builder.web.adapters import oracle_repo

    statements = []

    class Cursor:
        def __init__(self):
            self.result = None
            self.rowcount = 1

        def execute(self, sql, _params=None):
            statements.append(sql)
            normalized = " ".join(sql.upper().split())
            if "SELECT SCENARIO_KEY,VERSION_NO,STATUS" in normalized:
                self.result = ("PRD_ABCDEF12", 1, "SUPERSEDED")
            elif "SELECT MAX(VERSION_NO)" in normalized:
                self.result = (2,)
            else:
                self.result = None

        def fetchone(self):
            return self.result

    cursor = Cursor()

    @contextmanager
    def transaction():
        yield SimpleNamespace(cursor=lambda: cursor)

    monkeypatch.setattr(oracle_repo, "atomic_connection", transaction)
    with pytest.raises(oracle_repo.RegistryStaleError) as error:
        oracle_repo.OracleRegistryRepo().insert_version({
            "scenario_id": "PRD_ABCDEF12_V2", "scenario_key": "PRD_ABCDEF12", "version_no": 2,
            "parent_scenario_id": "PRD_ABCDEF12_V1", "parent_version": 1, "parent_status": "ACTIVE",
        })
    assert error.value.code == "REVISE_STALE"
    assert not any("INSERT INTO" in sql or "UPDATE " in sql for sql in statements)
