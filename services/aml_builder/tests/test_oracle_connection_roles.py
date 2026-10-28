"""Offline checks for Oracle role separation and fail-closed routing."""

from __future__ import annotations

import pytest

from services.aml_builder.web.adapters import dwh_oracle_repo, oracle_repo
from services.aml_builder.web.services import oracle


class FakeCursor:
    """Record statements while providing an empty SELECT result."""

    description = [("VALUE",)]
    rowcount = 1
    call_timeout = None

    def __init__(self, statements: list[tuple[str, object]]) -> None:
        self.statements = statements

    def execute(self, sql: str, params: object = None) -> None:
        self.statements.append((sql, params))

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *_exc_info: object) -> None:
        return None

    def fetchall(self) -> list[tuple]:
        return []

    def fetchone(self) -> tuple | None:
        return None


class FakeConnection:
    """Minimal pooled connection fake."""

    def __init__(self, statements: list[tuple[str, object]]) -> None:
        self.statements = statements
        self.last_cursor: FakeCursor | None = None
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> FakeCursor:
        self.last_cursor = FakeCursor(self.statements)
        return self.last_cursor

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class FakePool:
    """Pool fake whose acquired connection identifies its role."""

    def __init__(self, role: str) -> None:
        self.role = role
        self.statements: list[tuple[str, object]] = []
        self.connection = FakeConnection(self.statements)
        self.acquired = 0

    def acquire(self) -> FakeConnection:
        self.acquired += 1
        return self.connection

    def release(self, _connection: FakeConnection) -> None:
        return None


def test_shadow_and_registry_operations_use_separate_pools(monkeypatch):
    registry = FakePool("registry")
    dwh = FakePool("dwh")
    monkeypatch.setattr(oracle, "_pool", registry)
    monkeypatch.setattr(oracle, "_shadow_pool", dwh)

    oracle.run_readonly("SELECT 1 FROM DUAL")
    oracle.run_write("INSERT INTO APP_REGISTRY (ID) VALUES (:id)", {"id": 1})
    oracle.run_shadow_readonly("SELECT 1 FROM DUAL", timeout_seconds=10)
    with dwh_oracle_repo.DwhOracleRepository()._connection_provider():
        pass

    assert registry.acquired == 2
    assert dwh.acquired == 2
    assert registry.connection.commits == 1
    assert [sql for sql, _ in registry.statements] == [
        "SELECT 1 FROM DUAL",
        "INSERT INTO APP_REGISTRY (ID) VALUES (:id)",
    ]
    assert [sql for sql, _ in dwh.statements] == ["SELECT 1 FROM DUAL"]
    assert dwh.connection.last_cursor.call_timeout == 10000


def test_missing_dwh_pool_never_falls_back_to_registry(monkeypatch):
    registry = FakePool("registry")
    monkeypatch.setattr(oracle, "_pool", registry)
    monkeypatch.setattr(oracle, "_shadow_pool", None)

    with pytest.raises(RuntimeError, match="DWH reader Oracle pool"):
        with oracle.get_shadow_connection():
            pytest.fail("DWH connection must fail before yielding")

    assert registry.acquired == 0


def test_dwh_pool_requires_dwh_credentials_and_ignores_registry_credentials(monkeypatch):
    created = []

    def create_pool(**kwargs):
        created.append(kwargs)
        return FakePool("dwh")

    monkeypatch.setattr(oracle.oracledb, "create_pool", create_pool)
    monkeypatch.setattr(oracle, "_shadow_pool", None)
    monkeypatch.setattr(oracle.settings, "DWH_ORACLE_DSN", None)
    monkeypatch.setattr(oracle.settings, "DWH_ORACLE_USER", None)
    monkeypatch.setattr(oracle.settings, "DWH_ORACLE_PASSWORD", None)
    monkeypatch.setattr(oracle.settings, "ORACLE_DSN", "registry-dsn")
    monkeypatch.setattr(oracle.settings, "ORACLE_USER", "registry-user")
    monkeypatch.setattr(oracle.settings, "ORACLE_PASSWORD", "registry-password")

    oracle.init_shadow_pool()

    assert created == []
    assert oracle._shadow_pool is None


def test_registry_repository_reads_use_registry_pool(monkeypatch):
    registry = FakePool("registry")
    dwh = FakePool("dwh")
    monkeypatch.setattr(oracle, "_pool", registry)
    monkeypatch.setattr(oracle, "_shadow_pool", dwh)

    repository = oracle_repo.OracleRegistryRepo()
    repository.get_by_thread("thread")
    repository.append_audit({"scenario_id": "id", "scenario_key": "key"})

    assert registry.acquired == 2
    assert dwh.acquired == 0
    assert "PIO_AML_AGENT_PRODUCTION_SCENARIOS" in registry.statements[0][0]
    assert "INSERT INTO PIO_AML_AGENT_SCENARIO_AUDIT" in registry.statements[1][0]


def test_registry_namespace_is_configurable(monkeypatch):
    registry = FakePool("registry")
    monkeypatch.setattr(oracle, "_pool", registry)
    monkeypatch.setattr(oracle, "_shadow_pool", None)
    monkeypatch.setattr(oracle.settings, "ORACLE_REGISTRY_NAMESPACE", "CLIENT_CASES")

    oracle_repo.OracleRegistryRepo().get_by_thread("thread")

    assert "CLIENT_CASES_PRODUCTION_SCENARIOS" in registry.statements[0][0]
    assert "PIO_AML_AGENT_PRODUCTION_SCENARIOS" not in registry.statements[0][0]


def test_connection_roles_apply_only_their_configured_default_schema(monkeypatch):
    registry_statements: list[tuple[str, object]] = []
    dwh_statements: list[tuple[str, object]] = []
    monkeypatch.setattr(oracle.settings, "ORACLE_REGISTRY_SCHEMA", "APP_OWNER")
    monkeypatch.setattr(oracle.settings, "DWH_ORACLE_SCHEMA", "DWH_OWNER")

    oracle._set_registry_schema(FakeConnection(registry_statements), None)
    oracle._set_dwh_schema(FakeConnection(dwh_statements), None)

    assert registry_statements == [("ALTER SESSION SET CURRENT_SCHEMA = APP_OWNER", None)]
    assert dwh_statements == [("ALTER SESSION SET CURRENT_SCHEMA = DWH_OWNER", None)]


def test_runtime_deployment_does_not_call_schema_provisioner():
    """The repository's explicit provisioner remains outside deploy paths."""
    from pathlib import Path

    deploy_source = (
        Path(__file__).parents[1] / "web" / "domain" / "deploy_service.py"
    ).read_text(encoding="utf-8")
    route_source = (
        Path(__file__).parents[1] / "web" / "api" / "routes" / "deploy.py"
    ).read_text(encoding="utf-8")

    assert "_ensure_schema" not in deploy_source
    assert "ensure_schema()" not in route_source


def test_schema_provisioning_requires_explicit_opt_in(monkeypatch):
    monkeypatch.setattr(
        oracle_repo.settings, "ORACLE_REGISTRY_SCHEMA_PROVISIONING_ENABLED", False
    )
    monkeypatch.setattr(
        oracle_repo, "run_readonly", lambda *_args, **_kwargs: pytest.fail("no SQL expected")
    )

    with pytest.raises(PermissionError, match="provisioning is disabled"):
        oracle_repo.OracleRegistryRepo().ensure_schema()
