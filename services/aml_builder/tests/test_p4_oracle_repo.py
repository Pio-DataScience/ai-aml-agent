"""Offline schema initialization checks using patched SELECT/DDL ports."""

from __future__ import annotations

import re
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.aml_builder.web.adapters import oracle_repo


def test_legacy_shared_registry_is_ignored_and_agent_registry_created(monkeypatch):
    monkeypatch.setattr(oracle_repo.settings, "ORACLE_REGISTRY_SCHEMA_PROVISIONING_ENABLED", True)
    calls = []

    def fake_select(sql, params=None):
        calls.append(sql)
        if "ALL_TABLES" in sql:
            return [], [("PIO_AML_PRODUCTION_SCENARIOS",)]
        if "PIO_AML_AGENT_SCHEMA_VERSION" in sql:
            return [], [(2,)]
        return [], []

    monkeypatch.setattr(oracle_repo, "run_readonly", fake_select)
    created = []
    monkeypatch.setattr(oracle_repo, "_execute_ddl", created.append)
    monkeypatch.setattr(oracle_repo, "run_write", lambda *_args, **_kwargs: pytest.fail("schema version already exists"))
    oracle_repo.OracleRegistryRepo().ensure_schema()
    assert len(created) == len(oracle_repo.SCHEMA_DDLS) + len(oracle_repo.SCHEMA_INDEXES)
    assert all("PIO_AML_PRODUCTION_SCENARIOS" not in sql for sql in created)


def test_incompatible_agent_registry_fails_with_typed_schema_error(monkeypatch):
    monkeypatch.setattr(oracle_repo.settings, "ORACLE_REGISTRY_SCHEMA_PROVISIONING_ENABLED", True)
    def fake_select(sql, _params=None):
        if "ALL_TABLES" in sql:
            return [], [("PIO_AML_AGENT_PRODUCTION_SCENARIOS",)]
        return [], [("SCENARIO_ID",), ("RAW_SQL",)]

    monkeypatch.setattr(oracle_repo, "run_readonly", fake_select)
    monkeypatch.setattr(oracle_repo, "_execute_ddl", lambda _sql: pytest.fail("must not create over incompatible table"))
    with pytest.raises(oracle_repo.RegistrySchemaError) as error:
        oracle_repo.OracleRegistryRepo().ensure_schema()
    assert error.value.code == "REGISTRY_SCHEMA_INCOMPATIBLE"


def test_function_index_keeps_null_signatures_unindexed():
    index = oracle_repo.SCHEMA_INDEXES["PIO_AML_AGENT_PROD_UQ_ACTIVE_SIG"]
    assert "CASE WHEN STATUS='ACTIVE' THEN SIGNATURE_HASH END" in index
    assert "SIGNATURE_HASH IS NOT NULL" not in index


def test_unique_active_signature_violation_maps_to_typed_error(monkeypatch):
    class FakeCursor:
        def execute(self, sql, _params=None):
            if "INSERT INTO PIO_AML_AGENT_PRODUCTION_SCENARIOS" in sql:
                raise RuntimeError(
                    "ORA-00001: unique constraint (OWNER.PIO_AML_AGENT_PROD_UQ_ACTIVE_SIG) violated"
                )

    @contextmanager
    def fake_transaction():
        yield SimpleNamespace(cursor=lambda: FakeCursor())

    monkeypatch.setattr(oracle_repo, "atomic_connection", fake_transaction)
    with pytest.raises(oracle_repo.DuplicateActiveSignatureError) as error:
        oracle_repo.OracleRegistryRepo().insert_version({"parent_scenario_id": None})
    assert error.value.code == "DUPLICATE_ACTIVE_SIGNATURE"


def test_registry_ddl_preserves_every_alert_engine_selected_column():
    ddl = oracle_repo.SCHEMA_DDLS["PIO_AML_AGENT_PRODUCTION_SCENARIOS"].upper()
    engine_path = Path(__file__).parents[1] / "web" / "services" / "alert_engine.py"
    engine_sql = engine_path.read_text(encoding="utf-8").upper()
    selected_production_columns = set(re.findall(r"\bp\.([A-Z_]+)", engine_sql))
    alert_columns = {
        "SCENARIO_ID": "VARCHAR2(50)",
        "SCENARIO_NAME": "VARCHAR2(250)",
        "SCENARIO_TYPE": "VARCHAR2(50)",
        "RAW_SQL": "CLOB",
        "TIME_WINDOW_DAYS": "NUMBER",
        "IS_ACTIVE": "NUMBER(1)",
        "CREATED_AT": "TIMESTAMP",
    }
    for column, data_type in alert_columns.items():
        assert f"{column} {data_type}" in " ".join(ddl.split())
    assert selected_production_columns <= set(alert_columns)


def test_agent_metadata_ddl_preserves_alert_engine_selected_columns():
    ddl = oracle_repo.SCHEMA_DDLS["PIO_AML_AGENT_SCENARIOS"].upper()
    engine_path = Path(__file__).parents[1] / "web" / "services" / "alert_engine.py"
    engine_sql = engine_path.read_text(encoding="utf-8").upper()
    selected = set(re.findall(r"\bs\.([A-Z_]+)", engine_sql))
    assert selected <= set(re.findall(r"\b([A-Z_]+)\s+(?:NUMBER|VARCHAR2|DATE)", ddl))


def test_application_adapter_source_has_no_forbidden_schema_or_row_removal_sql():
    web_root = Path(__file__).parents[1] / "web"
    source_paths = [
        *((web_root / "adapters").glob("*.py")),
        web_root / "domain" / "deploy_service.py",
        web_root / "domain" / "versioning_service.py",
        web_root / "api" / "routes" / "deploy.py",
        web_root / "api" / "routes" / "workflow.py",
    ]
    for prohibited in (r"\bDELETE\s+FROM\b", r"\bDROP\s+TABLE\b", r"\bDROP\s+INDEX\b", r"\bTRUNCATE\b", r"\bALTER\s+TABLE\b"):
        for source_path in source_paths:
            assert not re.search(prohibited, source_path.read_text(encoding="utf-8").upper())
