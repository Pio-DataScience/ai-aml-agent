"""Offline tests for B3's Oracle SQL policy and candidate repository."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from services.aml_builder.web.adapters.dwh_oracle_repo import (
    DwhOracleRepository,
    OracleRequestBudget,
)
from services.aml_builder.web.domain.dwh_scenario.sql_policy import (
    SqlPolicyError,
    validate_oracle_read,
)
from services.aml_builder.web.services import settings as settings_module


def policy(sql: str, **kwargs):
    """Run policy against the disposable BI_DWH test allowlist."""
    return validate_oracle_read(
        sql, allowed_schemas=("BI_DWH",), allowed_objects=("BI_DWH.TX",), **kwargs
    )


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM BI_DWH.TX",
        "UPDATE BI_DWH.TX SET X=1",
        "INSERT INTO BI_DWH.TX (X) VALUES (1)",
        "CREATE TABLE BI_DWH.X (A NUMBER)",
        "BEGIN NULL; END;",
        "SELECT CUS_NUM FROM BI_DWH.TX; DELETE FROM BI_DWH.TX",
        "SELECT CUS_NUM FROM BI_DWH.TX -- comment\n WHERE 1=1",
        "SELECT CUS_NUM FROM BI_DWH.TX /* hidden */",
        "SELECT CUS_NUM FROM BI_DWH.TX FOR UPDATE",
        "SELECT DBMS_LOCK.SLEEP(1), CUS_NUM FROM BI_DWH.TX",
        "SELECT CUSTOM_SIDE_EFFECT(CUS_NUM) FROM BI_DWH.TX",
        "SELECT BI_DWH.TX_SEQ.NEXTVAL AS CUS_NUM FROM BI_DWH.TX",
        "SELECT CUS_NUM FROM OTHER.TX",
        "SELECT CUS_NUM FROM BI_DWH.OTHER",
        "SELECT CUS_NUM FROM TX",
        "SELECT CUS_NUM FROM BI_DWH.TX WHERE C=:missing",
    ],
)
def test_policy_rejects_unsafe_ambiguous_or_unsupported_sql(sql):
    """Writes, comments, multi-statements and unapproved names fail closed."""
    with pytest.raises(SqlPolicyError):
        policy(sql)


def test_cte_aliases_quoted_identifiers_and_literals_are_supported():
    """Oracle CTE, quoted names, aliases and comment-like string literals parse."""
    result = policy(
        "WITH \"q\" AS (SELECT CUS_NUM, '--literal' AS LABEL FROM BI_DWH.TX "
        'WHERE COUNTRY = :country) SELECT q.CUS_NUM AS "CUS_NUM" FROM "q" q',
        params={"country": "JO"},
        required_projection="CUS_NUM",
    )
    assert result.sql.startswith("WITH")
    assert result.binds == ("country",)
    assert result.tables == ("BI_DWH.TX",)


def test_common_oracle_functions_are_supported():
    """A reviewed set of deterministic Oracle built-ins remains usable."""
    assert policy(
        "SELECT CUS_NUM, NVL(COUNTRY, 'JO') AS COUNTRY FROM BI_DWH.TX"
    ).sql.startswith("SELECT")


def test_confirmed_filter_requires_matching_bound_value():
    """Confirmed filter predicates are checked against their named bind values."""
    confirmed = [{"target": {"table": "TX", "column": "COUNTRY"}, "values": ["JO"]}]
    assert (
        policy(
            "SELECT CUS_NUM FROM BI_DWH.TX WHERE COUNTRY=:country",
            params={"country": "JO"},
            confirmed_filters=confirmed,
        ).sql
        == "SELECT CUS_NUM FROM BI_DWH.TX WHERE COUNTRY=:country"
    )
    with pytest.raises(SqlPolicyError):
        policy(
            "SELECT CUS_NUM FROM BI_DWH.TX WHERE COUNTRY=:country",
            params={"country": "US"},
            confirmed_filters=confirmed,
        )
    with pytest.raises(SqlPolicyError):
        policy(
            "SELECT CUS_NUM FROM BI_DWH.TX WHERE COUNTRY=:country OR 1=1",
            params={"country": "JO"},
            confirmed_filters=confirmed,
        )
    with pytest.raises(SqlPolicyError):
        policy(
            "SELECT CUS_NUM FROM BI_DWH.TX WHERE NOT COUNTRY=:country",
            params={"country": "JO"},
            confirmed_filters=confirmed,
        )


def test_unqualified_confirmed_filter_is_rejected_when_joined_sources_are_ambiguous():
    """A confirmed target column must be qualified when several tables join."""
    with pytest.raises(SqlPolicyError):
        validate_oracle_read(
            "SELECT TX.CUS_NUM FROM BI_DWH.TX TX "
            "JOIN BI_DWH.CUSTOMERS C ON TX.CUS_NUM=C.CUS_NUM "
            "WHERE COUNTRY=:country",
            params={"country": "JO"},
            allowed_schemas=("BI_DWH",),
            allowed_objects=("BI_DWH.TX", "BI_DWH.CUSTOMERS"),
            confirmed_filters=[
                {"target": {"table": "TX", "column": "COUNTRY"}, "values": ["JO"]}
            ],
        )


class FakeCursor:
    """Oracle cursor fake that tracks exact SQL and bounded fetches."""

    def __init__(self, rows=(), error=None):
        self.description = [("CUS_NUM",)]
        self.rows = list(rows)
        self.error = error
        self.executed = None
        self.call_timeout = None
        self.fetch_limit = None

    def execute(self, sql, params):
        self.executed = (sql, params)
        if self.error:
            raise self.error

    def fetchmany(self, size):
        self.fetch_limit = size
        return self.rows[:size]


class FakeConnection:
    """Connection fake yielding one cursor."""

    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


@pytest.fixture
def fake_settings(monkeypatch):
    """Configure small candidate bounds without external settings files."""
    monkeypatch.setattr(
        settings_module.settings, "DWH_SQL_ALLOWED_SCHEMAS", ("BI_DWH",)
    )
    monkeypatch.setattr(
        settings_module.settings,
        "DWH_SQL_ALLOWED_OBJECTS",
        ("BI_DWH.TX", "BI_DWH.COUNTRIES"),
    )
    monkeypatch.setattr(settings_module.settings, "DWH_SQL_MAX_CANDIDATE_ROWS", 2)
    monkeypatch.setattr(settings_module.settings, "DWH_SQL_READ_TIMEOUT_SECONDS", 3.0)


def repository(cursor):
    """Build repository over an injected fake Oracle connection."""

    @contextmanager
    def provider():
        yield FakeConnection(cursor)

    return DwhOracleRepository(connection_provider=provider)


def test_candidate_execution_preserves_sql_bounds_fetch_and_marks_truncated(
    fake_settings,
):
    """The exact reviewed SQL is executed and only a bounded sample is returned."""
    sql = "SELECT CUS_NUM FROM BI_DWH.TX WHERE COUNTRY=:country"
    cursor = FakeCursor([(1,), (2,), (3,)])
    result = repository(cursor).execute_candidate(
        sql, {"country": "JO"}, budget=OracleRequestBudget()
    )
    assert result.success is True
    assert result.sql == cursor.executed[0] == sql
    assert cursor.executed[1] == {"country": "JO"}
    assert cursor.fetch_limit == 3
    assert result.rows == ((1,), (2,))
    assert result.row_count == 2
    assert result.truncated is True
    assert cursor.call_timeout == 3000


def test_empty_result_is_successful_and_failures_are_structured(fake_settings):
    """No rows is success; Oracle errors do not leak query/result data."""
    empty = repository(FakeCursor()).execute_candidate(
        "SELECT CUS_NUM FROM BI_DWH.TX", budget=OracleRequestBudget()
    )
    assert empty.success and empty.row_count == 0 and not empty.truncated
    failed = repository(
        FakeCursor(error=RuntimeError("secret detail"))
    ).execute_candidate("SELECT CUS_NUM FROM BI_DWH.TX", budget=OracleRequestBudget())
    assert not failed.success
    assert failed.error == "Oracle candidate execution failed"
    assert "secret" not in failed.error


def test_budget_blocks_statement_before_database_use(fake_settings):
    """A consumed statement budget fails closed without acquiring a connection."""
    cursor = FakeCursor()
    budget = OracleRequestBudget(max_statements=0)
    result = repository(cursor).execute_candidate(
        "SELECT CUS_NUM FROM BI_DWH.TX", budget=budget
    )
    assert not result.success
    assert "statement budget" in result.error
    assert cursor.executed is None


def test_repository_identifier_reads_share_policy_and_named_binds(fake_settings):
    """Probe and code lookup identifiers are constrained and values are bound."""
    cursor = FakeCursor([("JO",)])
    repo = repository(cursor)
    repo.probe_column_values("BI_DWH", "TX", "COUNTRY", budget=OracleRequestBudget())
    sql, params = cursor.executed
    assert '"BI_DWH"."TX"' in sql
    assert "ROWNUM <= 1000" in sql
    assert params == {}

    with pytest.raises(ValueError, match="identifier"):
        repo.probe_column_values(
            "BI_DWH", "TX; DELETE FROM TX", "COUNTRY", budget=OracleRequestBudget()
        )

    repo.lookup_business_codes(
        "BI_DWH",
        "COUNTRIES",
        "COUNTRY_CODE",
        "COUNTRY_NAME",
        budget=OracleRequestBudget(),
        filters={"COUNTRY_CODE": "JO"},
    )
    sql, params = cursor.executed
    assert "FETCH FIRST 100 ROWS ONLY" in sql
    assert params == {"filter_0": "JO"}
