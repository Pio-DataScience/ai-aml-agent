"""P8 Oracle override audit rows share the deployment transaction."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

from services.aml_builder.web.adapters import oracle_repo


def test_each_acknowledged_conflict_is_audited_with_deployment(monkeypatch):
    executions = []

    class FakeCursor:
        def execute(self, sql, params=None):
            executions.append((sql, params or {}))

    @contextmanager
    def fake_transaction():
        yield SimpleNamespace(cursor=lambda: FakeCursor())

    monkeypatch.setattr(oracle_repo, "atomic_connection", fake_transaction)
    row = {
        "scenario_id": "PRD_NEW_V1",
        "scenario_key": "PRD_NEW",
        "version_no": 1,
        "status": "ACTIVE",
        "is_active": 1,
        "thread_id": "project_chat_user",
        "revision_id": "revision-1",
        "rev_no": 1,
        "metadata_json": {"COUNTRY_CODE": 1, "INST_CODE": 1},
        "receipt_json": {"conflict_acknowledgements": []},
        "scenario_name": "New scenario",
        "scenario_type": "CUSTOMER",
        "raw_sql": "SELECT 1",
        "created_by": "999",
        "created_by_number": 999,
        "actor_role": "manager",
        "audit_action": "CREATE",
        "override_conflicts": [
            {
                "revision_id": "revision-1",
                "other_scenario_id": "PRD_OLD_V1",
                "relation": "OVERLAP",
                "actor": "999",
                "role": "manager",
                "acknowledged_at": "2026-10-20T00:00:00+00:00",
                "reason": "Reviewed overlap",
            },
            {
                "revision_id": "revision-1",
                "other_scenario_id": "PRD_OLD_V2",
                "relation": "COVERED",
                "actor": "999",
                "role": "manager",
                "acknowledged_at": "2026-10-20T00:00:00+00:00",
                "reason": None,
            },
        ],
    }
    oracle_repo.OracleRegistryRepo().insert_version(row)
    audit_calls = [item for item in executions if "INSERT INTO PIO_AML_AGENT_SCENARIO_AUDIT" in item[0]]
    assert len(audit_calls) == 3
    overrides = [params for _sql, params in audit_calls if params["action"] == "OVERRIDE_CONFLICT"]
    assert len(overrides) == 2
    assert {params["actor"] for params in overrides} == {"999"}
    assert {params["actor_role"] for params in overrides} == {"manager"}
    assert '"revision_id": "revision-1"' in overrides[0]["diff_json"]
