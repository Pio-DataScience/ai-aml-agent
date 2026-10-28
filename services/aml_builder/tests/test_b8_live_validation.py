"""Offline-only B8 live-validation gates; every Oracle dependency is fake."""

from __future__ import annotations

from contextlib import contextmanager
import time

import pytest

from services.aml_builder.web.adapters.dwh_oracle_repo import (
    CandidateQueryResult,
    OracleRequestBudget,
)
from services.aml_builder.web.domain.dwh_scenario.contracts import (
    ScenarioSqlRequest,
    ScenarioSqlResult,
)
from services.aml_builder.web.services.dwh_scenario.live_validation import (
    AcceptanceLimits,
    AcceptanceOracleRepository,
    AcceptancePlan,
    AcceptanceScenario,
    AcceptanceStatementBudget,
    DISCOVERY_SQL,
    SchemaDiscoveryPlan,
    LiveValidationApproval,
    assess_stage1_compatibility,
    _compare_case,
    bind_sql_literals,
    _check_actual_dictionary_columns,
    create_acceptance_pool_provider,
    _safe_oracle_error_code,
    _validate_and_execute,
    _validate_approval,
    run_preflight,
    run_schema_discovery,
)
from services.aml_builder.web.domain.dwh_scenario.sql_policy import (
    validate_oracle_read,
)
from services.aml_builder.web.services.settings import Settings


def _plan() -> AcceptancePlan:
    """Build one small plan without live credentials or Oracle access."""
    request = ScenarioSqlRequest(
        intent={
            "scenario_name": "offline cash test",
            "scenario_type": "TRANSACTION",
            "detection_logic": "cash test",
            "transaction_type": "CASH DEPOSIT",
            "explanation_codes": ["4"],
            "ready_for_handoff": True,
            "time_window": {"unit": "DAYS", "value": 30, "is_rolling": True},
        },
        confirmed_filters=[
            {
                "dimension": "explanation_code",
                "status": "resolved",
                "values": ["4"],
                "target": {"table": "PIO_TRANSACTIONS", "column": "EXPL_CODE"},
            }
        ],
    )
    return AcceptancePlan(
        scenarios=(AcceptanceScenario("offline-cash-30d", request),),
        allowed_schemas=("BI_DWH",),
        allowed_objects=("BI_DWH.PIO_TRANSACTIONS",),
        expected_resource_plan="AML_ACCEPTANCE_LIMITED",
        http_endpoint="http://fake.local/chat/stream",
        generator_routes="offline HTTP fake + injected local fake/model",
        provider_data_approval_ref="offline test fixture approval",
        limits=AcceptanceLimits(max_rows_per_query=2, max_statements=16),
    )


def test_schema_discovery_plan_hashes_exact_scope_and_sql_policy() -> None:
    """Discovery statements are SELECT-only and approval-bound without I/O."""
    plan = SchemaDiscoveryPlan()
    scope_hash = plan.scope_hash()
    approval = LiveValidationApproval(
        scope_sha256=scope_hash,
        approved_by="offline-test",
        approved_at="2026-10-27T00:00:00Z",
        provider_data_approval_ref=plan.provider_data_approval_ref,
    )
    _validate_approval(plan, approval)
    assert scope_hash == plan.scope_hash()
    assert (
        scope_hash == "3b62f3ecccac746ee3ee4debc213f47c026d5c7a9c469a5c8ae73879cdf20cd4"
    )
    metadata_objects = (
        "DUAL", "SESSION_PRIVS", "SESSION_ROLES", "ALL_TAB_PRIVS",
        "ALL_COL_PRIVS", "V$PARAMETER", "ALL_OBJECTS", "ALL_SYNONYMS",
        "ALL_TAB_COLUMNS",
    ) + plan.allowed_objects
    for index, statement in enumerate(DISCOVERY_SQL):
        statement_binds = plan.binds_for_statement(index)
        validated = validate_oracle_read(
            statement,
            params=statement_binds,
            allowed_schemas=plan.allowed_schemas,
            allowed_objects=metadata_objects,
        )
        assert set(validated.binds) == set(statement_binds)
    assert len(DISCOVERY_SQL) <= plan.limits.max_statements == 12
    assert plan.allowed_schemas == ("BI_DWH",)
    assert all(name.startswith("BI_DWH.") for name in plan.allowed_objects)


def test_result_shape_probe_is_no_row_and_policy_scoped() -> None:
    """Shape probing uses SELECT/WHERE 1=0 and never fetches result rows."""
    from services.aml_builder.web.services.dwh_scenario.live_validation import (
        AcceptanceOracleRepository,
        AcceptanceStatementBudget,
    )

    class Cursor:
        call_timeout = None
        description = [("CUS_NUM", str), ("TRA_AMT", float)]

        def execute(self, sql, binds):
            self.sql = sql
            self.binds = binds
            seen["sql"] = sql

        def fetchmany(self, num):
            raise AssertionError("no-row shape probe must not fetch rows")

    class Connection:
        def cursor(self):
            return Cursor()

    @contextmanager
    def provider():
        yield Connection()

    plan = _plan()
    repo = AcceptanceOracleRepository(
        connection_provider=provider,
        plan=plan,
        statement_budget=AcceptanceStatementBudget(3),
        run_started_at=time.monotonic(),
    )
    seen = {}
    shape = repo.probe_result_shape(
        "SELECT CUS_NUM, TRA_AMT FROM BI_DWH.PIO_TRANSACTIONS WHERE EXPL_CODE = '4'",
        budget=OracleRequestBudget(max_statements=1, deadline_seconds=5),
    )
    assert shape == ("CUS_NUM", "TRA_AMT")
    assert "WHERE 1=0" in seen["sql"]
    assert repo.statements_used == 1


def test_acceptance_bind_audit_redacts_every_value_by_default() -> None:
    """Even benign-looking binds are not copied to durable acceptance reports."""
    from services.aml_builder.web.services.dwh_scenario.live_validation import (
        _result_summary,
    )

    request = _plan().scenarios[0].request
    result = ScenarioSqlResult(sql="SELECT CUS_NUM FROM BI_DWH.PIO_TRANSACTIONS WHERE CIF = '123456'", approved=True)
    evidence = CandidateQueryResult(
        sql=result.sql, success=True, columns=("CUS_NUM",), rows=(), row_count=0
    )
    summary = _result_summary(result, evidence, 1, request, 10)
    assert summary["bind_values"]
    assert all(item["value"] == "<redacted>" for item in summary["bind_values"])
    assert "123456" not in str(summary)


def test_discovery_privilege_manifest_matches_oracle_dictionary_columns() -> None:
    """Privilege SQL uses TABLE_SCHEMA but preserves the owner output position."""
    plan = SchemaDiscoveryPlan()
    object_sql, column_sql = DISCOVERY_SQL[3:5]
    for sql in (object_sql, column_sql):
        assert "TABLE_SCHEMA AS OWNER" in sql
        assert "WHERE TABLE_SCHEMA = :schema_owner" in sql
        assert "OWNER = :schema_owner" not in sql
    assert "TABLE_NAME, PRIVILEGE FROM ALL_TAB_PRIVS" in object_sql
    assert "TABLE_NAME, COLUMN_NAME, PRIVILEGE FROM ALL_COL_PRIVS" in column_sql
    expected_binds = {
        "schema_owner",
        "expected_user",
        *(f"n{index}" for index in range(1, 8)),
    }
    assert set(plan.binds_for_statement(3)) == expected_binds
    assert set(plan.binds_for_statement(4)) == expected_binds


def test_stage1_compatibility_resolves_semantic_fields_not_all_aliases() -> None:
    """Only generator-required fields block; absent aliases and empty mapping do not."""
    objects = {
        name: {"exists": True}
        for name in (
            "PIO_BANKBI_DICTIONARY_TABLES",
            "PIO_BANKBI_DICTIONARY_COL",
            "PIO_BANKBI_DICT_JOIN_TABLES",
            "PIO_AML_TABLES",
            "PIO_AML_COLUMNS",
            "PIO_TRANSACTIONS",
            "PIO_EXPLANATION_CODE",
        )
    }
    columns = [
        ("BI_DWH", "PIO_TRANSACTIONS", "CUS_NUM", "VARCHAR2", 40, "Y"),
        ("BI_DWH", "PIO_TRANSACTIONS", "CIF", "VARCHAR2", 40, "Y"),
        ("BI_DWH", "PIO_TRANSACTIONS", "TRA_DATE", "DATE", 7, "Y"),
        ("BI_DWH", "PIO_TRANSACTIONS", "TRA_AMT", "NUMBER", 22, "Y"),
        ("BI_DWH", "PIO_TRANSACTIONS", "EQU_TRA_AMT", "NUMBER", 22, "Y"),
        ("BI_DWH", "PIO_TRANSACTIONS", "EXPL_CODE", "VARCHAR2", 40, "Y"),
    ]
    mappings = {
        "expl_code_dictionary": {"status": "OBSERVED", "rows": []},
        "explanation_code_4": {"status": "OBSERVED", "rows": [["4"]]},
    }

    assessment = assess_stage1_compatibility(
        objects=objects, columns=columns, mappings=mappings
    )

    assert assessment["status"] == "PASS"
    assert assessment["required_fields"]["customer_projection"]["column"] == "CUS_NUM"
    assert assessment["required_fields"]["rolling_window_date"]["column"] == "TRA_DATE"
    assert assessment["required_fields"]["confirmed_explanation_code_filter"][
        "column"
    ] == ("EXPL_CODE")
    assert assessment["amount_candidates"] == [
        {"column": "TRA_AMT", "data_type": "NUMBER"},
        {"column": "EQU_TRA_AMT", "data_type": "NUMBER"},
    ]
    assert assessment["expl_code_dictionary_mapping"] == "NO_MATCHING_DICTIONARY_ROW"


def test_stage1_compatibility_blocks_missing_or_type_incompatible_contract_fields() -> (
    None
):
    """Semantic roles fail closed when no actual compatible column resolves."""
    objects = {"PIO_TRANSACTIONS": {"exists": True}}
    columns = [
        ("BI_DWH", "PIO_TRANSACTIONS", "CUS_NUM", "CLOB", 40, "Y"),
        ("BI_DWH", "PIO_TRANSACTIONS", "TRA_DATE", "VARCHAR2", 40, "Y"),
        ("BI_DWH", "PIO_TRANSACTIONS", "EXPL_CODE", "BLOB", 40, "Y"),
    ]
    assessment = assess_stage1_compatibility(
        objects=objects,
        columns=columns,
        mappings={"expl_code_dictionary": {"status": "OBSERVED", "rows": []}},
    )
    assert assessment["status"] == "BLOCK"
    assert len(assessment["blockers"]) == 3


def test_error_report_exposes_only_vendor_code() -> None:
    """Safe diagnostics retain a vendor code without exposing error text."""

    class DriverError:
        code = 0

        def __str__(self):
            return "DPY-6005: connect to host db.internal failed"

    assert _safe_oracle_error_code(Exception(DriverError())) == "DPY-6005"


def test_discovery_reports_missing_objects_and_optional_resource_view() -> None:
    """Metadata discovery continues while missing objects and V$ access report."""
    plan = SchemaDiscoveryPlan()
    approval = LiveValidationApproval(
        scope_sha256=plan.scope_hash(),
        approved_by="offline-test",
        approved_at="2026-10-27T00:00:00Z",
        provider_data_approval_ref=plan.provider_data_approval_ref,
    )

    class Cursor:
        call_timeout = None

        def execute(self, sql, _binds):
            if "SELECT USER AS SESSION_USER" in sql:
                self.rows = [("BI_DWH",)]
            elif "FROM SESSION_PRIVS" in sql:
                self.rows = [
                    ("CREATE SESSION",),
                    ("SELECT ANY TABLE",),
                    ("ALTER SYSTEM",),
                    ("CREATE ANY TABLE",),
                ]
            elif "FROM SESSION_ROLES" in sql and sql.startswith("SELECT ROLE"):
                self.rows = []
            elif "FROM ALL_TAB_PRIVS" in sql:
                self.rows = [("BI_DWH", "BI_DWH", "PIO_TRANSACTIONS", "UPDATE")]
            elif "FROM ALL_COL_PRIVS" in sql:
                self.rows = [
                    ("BI_DWH", "BI_DWH", "PIO_TRANSACTIONS", "CUS_NUM", "UPDATE")
                ]
            elif "FROM ALL_OBJECTS" in sql:
                self.rows = [("BI_DWH", "PIO_TRANSACTIONS", "TABLE", "VALID")]
            elif "FROM ALL_SYNONYMS" in sql:
                self.rows = []
            elif "FROM ALL_TAB_COLUMNS" in sql:
                self.rows = [
                    ("BI_DWH", "PIO_TRANSACTIONS", "CUS_NUM", "VARCHAR2", 20, "Y"),
                    ("BI_DWH", "PIO_TRANSACTIONS", "TRA_DATE", "DATE", 7, "Y"),
                    ("BI_DWH", "PIO_TRANSACTIONS", "TRA_AMT", "NUMBER", 22, "Y"),
                    ("BI_DWH", "PIO_TRANSACTIONS", "EXPL_CODE", "VARCHAR2", 40, "Y"),
                ]
            elif "FROM V$PARAMETER" in sql:
                raise RuntimeError("metadata privilege is unavailable")
            else:
                raise AssertionError("Conditional mapping SQL must be skipped")

        def fetchmany(self, size):
            return self.rows[:size]

    cursor = Cursor()

    class Connection:
        def cursor(self):
            return cursor

    class Provider:
        acceptance_read_only = True

        @contextmanager
        def __call__(self):
            yield Connection()

    config = Settings(
        ORACLE_DSN="offline",
        ORACLE_USER="offline",
        ORACLE_PASSWORD="offline",
        DWH_SQL_ACCEPTANCE_ENABLED=True,
        DWH_SQL_MODE="http",
        DWH_SQL_ACCEPTANCE_USER="BI_DWH",
        DWH_SQL_ALLOWED_SCHEMAS=("BI_DWH",),
        DWH_SQL_ALLOWED_OBJECTS=plan.allowed_objects,
        _env_file=None,
    )
    report = run_schema_discovery(plan, approval, provider=Provider(), config=config)
    assert report["status"] == "PASS"
    assert report["compatibility_status"] == "BLOCK"
    assert report["credential_system_privilege_status"] == "WARNING"
    assert report["effective_scoped_grants"]["status"] == "WARNING"
    assert {warning["type"] for warning in report["security_warnings"]} == {
        "elevated_system_privileges",
        "scoped_write_capable_grants",
    }
    assert report["missing_objects"] == sorted(
        set(name.split(".")[-1] for name in plan.allowed_objects) - {"PIO_TRANSACTIONS"}
    )
    assert report["resource_manager"]["status"] == "UNAVAILABLE"
    assert report["mappings"]["expl_code_dictionary"]["status"] == (
        "SKIPPED_MISSING_DEPENDENCY"
    )
    assert report["mappings"]["explanation_code_4"]["status"] == (
        "SKIPPED_MISSING_DEPENDENCY"
    )
    assert report["statements_used"] == 9


def test_scope_hash_binds_approval_to_limits_and_allowlists() -> None:
    """Changing any object or row cap changes the required approval digest."""
    plan = _plan()
    assert plan.scope_hash() == plan.scope_hash()
    altered = AcceptancePlan(
        scenarios=plan.scenarios,
        allowed_schemas=plan.allowed_schemas,
        allowed_objects=plan.allowed_objects,
        expected_resource_plan=plan.expected_resource_plan,
        http_endpoint=plan.http_endpoint,
        generator_routes=plan.generator_routes,
        provider_data_approval_ref=plan.provider_data_approval_ref,
            limits=AcceptanceLimits(max_rows_per_query=3, max_statements=16),
    )
    assert plan.scope_hash() != altered.scope_hash()
    assert plan.scope_summary()["case_ids"] == ["offline-cash-30d"]


def test_pool_creation_requires_exact_approval_before_oracledb_import() -> None:
    """A mismatched approval blocks even opening the dedicated connection pool."""
    plan = _plan()
    approval = LiveValidationApproval(
        scope_sha256="wrong-scope",
        approved_by="offline-test",
        approved_at="2026-10-27T00:00:00Z",
        provider_data_approval_ref=plan.provider_data_approval_ref,
    )
    config = Settings(
        ORACLE_DSN="unused",
        ORACLE_USER="unused",
        ORACLE_PASSWORD="unused",
        _env_file=None,
    )
    with pytest.raises(PermissionError, match="exact acceptance scope"):
        create_acceptance_pool_provider(plan, approval, config)


def test_bind_conversion_preserves_confirmed_filter_policy() -> None:
    """Literal AML filter and temporal values become validated named binds."""
    sql = (
        "SELECT CUS_NUM FROM BI_DWH.PIO_TRANSACTIONS "
        "WHERE EXPL_CODE = '4' AND TRA_DATE >= SYSDATE - 30"
    )
    bound_sql, binds, audit = bind_sql_literals(sql)
    assert ":b8_v" in bound_sql
    assert set(binds.values()) == {"4", 30}
    assert len(audit) == 2
    validate_oracle_read(
        bound_sql,
        params=binds,
        allowed_schemas=("BI_DWH",),
        allowed_objects=("BI_DWH.PIO_TRANSACTIONS",),
        required_projection="CUS_NUM",
        confirmed_filters=[
            {
                "target": {"table": "PIO_TRANSACTIONS", "column": "EXPL_CODE"},
                "values": ["4"],
                "polarity": "positive",
            }
        ],
    )


def test_acceptance_repository_binds_and_caps_inside_oracle() -> None:
    """The fake cursor sees bound values, a server-side row cap and timeout."""

    class Cursor:
        description = [("CUS_NUM",)]
        call_timeout = None
        executed = ""
        binds = None

        def execute(self, sql, params):
            self.executed, self.binds = sql, params

        def fetchmany(self, size):
            return [("101",), ("202",), ("303",)][:size]

    cursor = Cursor()

    class Connection:
        def cursor(self):
            return cursor

    @contextmanager
    def provider():
        yield Connection()

    plan = _plan()
    repository = AcceptanceOracleRepository(
        connection_provider=provider,
        plan=plan,
        statement_budget=AcceptanceStatementBudget(12),
        run_started_at=time.monotonic(),
    )
    result = repository.execute_candidate(
        "SELECT CUS_NUM FROM BI_DWH.PIO_TRANSACTIONS "
        "WHERE EXPL_CODE = '4' AND TRA_DATE >= SYSDATE - 30",
        budget=OracleRequestBudget(max_statements=2, deadline_seconds=10),
        confirmed_filters=[
            {
                "target": {"table": "PIO_TRANSACTIONS", "column": "EXPL_CODE"},
                "values": ["4"],
                "polarity": "positive",
            }
        ],
    )
    assert result.success and result.truncated and result.row_count == 2
    assert "ROWNUM <= :b8_result_cap" in cursor.executed
    assert "'4'" not in cursor.executed and ":b8_v" in cursor.executed
    assert cursor.binds["b8_result_cap"] == 3
    assert cursor.call_timeout <= 10_000


def test_readonly_preflight_checks_identity_privileges_plan_and_dictionary() -> None:
    """Fake data dictionary and resource settings pass the read-only preflight."""

    class Cursor:
        description = []
        call_timeout = None

        def __init__(self):
            self.rows = []

        def execute(self, sql, binds):
            if "SELECT USER FROM DUAL" in sql:
                self.rows = [("B8_READONLY",)]
            elif "FROM SESSION_PRIVS" in sql:
                self.rows = [("CREATE SESSION",)]
            elif sql.startswith("SELECT ROLE FROM SESSION_ROLES"):
                self.rows = [("B8_READ_ROLE",)]
            elif "FROM V$PARAMETER" in sql:
                self.rows = [("AML_ACCEPTANCE_LIMITED",)]
            elif "FROM ALL_OBJECTS" in sql:
                self.rows = [("TABLE",)]
            elif "FROM ALL_TAB_COLUMNS" in sql:
                self.rows = [("CUS_NUM",), ("EXPL_CODE",), ("TRA_DATE",)]
            elif "FROM ALL_TAB_PRIVS" in sql:
                self.rows = []
            elif "FROM ALL_COL_PRIVS" in sql:
                self.rows = []
            elif 'SELECT 1 FROM "BI_DWH"."PIO_TRANSACTIONS"' in sql:
                self.rows = []
            else:
                raise AssertionError("Unexpected preflight statement")

        def fetchmany(self, size):
            return self.rows[:size]

    cursor = Cursor()

    class Connection:
        def cursor(self):
            return cursor

    @contextmanager
    def provider():
        yield Connection()

    report = run_preflight(
        provider,
        _plan(),
        expected_user="B8_READONLY",
        statement_timeout_seconds=10,
        statement_budget=AcceptanceStatementBudget(12),
    )
    assert report["status"] == "PASS", report
    assert report["statements_used"] == 10
    assert (
        report["checks"]["dictionary_and_object_grants"]["objects"][
            "BI_DWH.PIO_TRANSACTIONS"
        ]["select_access"]
        is True
    )


def test_readonly_preflight_blocks_broad_privileges() -> None:
    """A read-capable but broadly privileged account cannot execute acceptance."""

    class Cursor:
        call_timeout = None

        def execute(self, sql, _binds):
            if "SELECT USER FROM DUAL" in sql:
                self.rows = [("B8_READONLY",)]
            elif "FROM SESSION_PRIVS" in sql:
                self.rows = [("CREATE SESSION",), ("SELECT ANY TABLE",)]
            else:
                self.rows = []

        def fetchmany(self, size):
            return self.rows[:size]

    cursor = Cursor()

    class Connection:
        def cursor(self):
            return cursor

    @contextmanager
    def provider():
        yield Connection()

    report = run_preflight(
        provider,
        _plan(),
        expected_user="B8_READONLY",
        statement_timeout_seconds=10,
        statement_budget=AcceptanceStatementBudget(12),
    )
    assert report["status"] == "BLOCK"
    assert report["checks"]["session_privileges"]["status"] == "BLOCK"


def test_readonly_preflight_blocks_write_grants_in_enabled_roles() -> None:
    """Write grants inherited through an enabled role block acceptance."""

    class Cursor:
        call_timeout = None

        def execute(self, sql, _binds):
            if "SELECT USER FROM DUAL" in sql:
                self.rows = [("B8_READONLY",)]
            elif "FROM SESSION_PRIVS" in sql:
                self.rows = [("CREATE SESSION",)]
            elif sql.startswith("SELECT ROLE FROM SESSION_ROLES"):
                self.rows = [("B8_READ_ROLE",)]
            elif "FROM ALL_TAB_PRIVS" in sql:
                self.rows = [("BI_DWH", "OTHER_TABLE", "UPDATE")]
            else:
                self.rows = []

        def fetchmany(self, size):
            return self.rows[:size]

    cursor = Cursor()

    class Connection:
        def cursor(self):
            return cursor

    @contextmanager
    def provider():
        yield Connection()

    report = run_preflight(
        provider,
        _plan(),
        expected_user="B8_READONLY",
        statement_timeout_seconds=10,
        statement_budget=AcceptanceStatementBudget(12),
    )
    assert report["status"] == "BLOCK"
    assert report["checks"]["global_write_grants"]["object_grant_count"] == 1


def test_candidate_columns_must_exist_in_actual_dictionary_scope() -> None:
    """Dictionary validation accepts known columns and rejects unknown ones."""
    plan = _plan()
    preflight = {
        "checks": {
            "dictionary_and_object_grants": {
                "objects": {
                    "BI_DWH.PIO_TRANSACTIONS": {
                        "exists": True,
                        "columns": ["CUS_NUM", "EXPL_CODE", "TRA_DATE"],
                    }
                }
            }
        }
    }
    valid = (
        "SELECT CUS_NUM FROM BI_DWH.PIO_TRANSACTIONS "
        "WHERE EXPL_CODE = '4' AND TRA_DATE >= SYSDATE - 30"
    )
    invalid = "SELECT UNKNOWN_COL FROM BI_DWH.PIO_TRANSACTIONS"
    assert _check_actual_dictionary_columns(valid, plan, preflight) is None
    assert _check_actual_dictionary_columns(invalid, plan, preflight)
    cte_sql = (
        "WITH CASH AS (SELECT CUS_NUM, EXPL_CODE, TRA_DATE "
        "FROM BI_DWH.PIO_TRANSACTIONS) SELECT CUS_NUM FROM CASH "
        "WHERE EXPL_CODE = '4' AND TRA_DATE >= SYSDATE - 30"
    )
    assert _check_actual_dictionary_columns(cte_sql, plan, preflight) is None

    multi_object_plan = AcceptancePlan(
        scenarios=plan.scenarios,
        allowed_schemas=plan.allowed_schemas,
        allowed_objects=("BI_DWH.PIO_TRANSACTIONS", "BI_DWH.PIO_OTHER"),
        expected_resource_plan=plan.expected_resource_plan,
        http_endpoint=plan.http_endpoint,
        generator_routes=plan.generator_routes,
        provider_data_approval_ref=plan.provider_data_approval_ref,
        limits=AcceptanceLimits(max_rows_per_query=2, max_statements=16),
    )
    cross_object_sql = (
        "SELECT T.EXPL_CODE FROM BI_DWH.PIO_TRANSACTIONS T "
        "JOIN BI_DWH.PIO_OTHER O ON T.CUS_NUM = O.CUS_NUM"
    )
    two_object_preflight = {
        "checks": {
            "dictionary_and_object_grants": {
                "objects": {
                    "BI_DWH.PIO_TRANSACTIONS": {"columns": ["CUS_NUM", "TRA_DATE"]},
                    "BI_DWH.PIO_OTHER": {"columns": ["CUS_NUM", "EXPL_CODE"]},
                }
            }
        }
    }
    assert _check_actual_dictionary_columns(
        cross_object_sql, multi_object_plan, two_object_preflight
    )


def test_unapproved_generator_sql_is_never_executed() -> None:
    """Generator approval is required before repository execution is possible."""
    plan = _plan()
    result = ScenarioSqlResult(
        sql=(
            "SELECT CUS_NUM FROM BI_DWH.PIO_TRANSACTIONS "
            "WHERE EXPL_CODE = '4' AND TRA_DATE >= SYSDATE - 30"
        ),
        approved=False,
    )

    class NeverExecute:
        def execute_candidate(self, *_args, **_kwargs):
            raise AssertionError("Unapproved SQL reached Oracle execution")

    evidence = _validate_and_execute(
        NeverExecute(),
        plan.scenarios[0].request,
        result,
        plan,
        {"checks": {}},
    )
    assert evidence.success is False
    assert evidence.error == "Generator did not approve a SQL candidate"


def test_customer_set_comparison_is_value_private_and_handles_zero_rows() -> None:
    """Actual in-memory set equality is reported without emitting customer IDs."""
    plan = _plan()
    scenario = plan.scenarios[0]
    result = ScenarioSqlResult(
        sql=(
            "SELECT CUS_NUM FROM BI_DWH.PIO_TRANSACTIONS "
            "WHERE EXPL_CODE = '4' AND TRA_DATE >= SYSDATE - 30"
        ),
        approved=True,
    )
    http = CandidateQueryResult(
        sql=result.sql,
        success=True,
        columns=("CUS_NUM",),
        rows=(("customer-101",), ("customer-202",)),
        row_count=2,
    )
    local = CandidateQueryResult(
        sql=result.sql,
        success=True,
        columns=("CUS_NUM",),
        rows=(("customer-202",), ("customer-101",)),
        row_count=2,
    )
    report = _compare_case(
        scenario, result, http, 10, result, local, 12, plan,
        http_shape=("CUS_NUM",), local_shape=("CUS_NUM",),
    )
    serialized = str(report)
    assert report["status"] == "PASS"
    assert report["customer_set_equal"] is True
    assert report["customer_set_counts"]["symmetric_difference"] == 0
    assert report["result_shape_equal"] is True
    assert "customer-101" not in serialized and "customer-202" not in serialized

    empty = CandidateQueryResult(
        sql=result.sql,
        success=True,
        columns=("CUS_NUM",),
        rows=(),
        row_count=0,
    )
    empty_report = _compare_case(
        scenario, result, empty, 1, result, empty, 1, plan,
        http_shape=("CUS_NUM",), local_shape=("CUS_NUM",),
    )
    assert empty_report["status"] == "PASS"
    assert empty_report["customer_set_counts"]["http_distinct"] == 0
    mismatch = _compare_case(
        scenario, result, http, 10, result, local, 12, plan,
        http_shape=("CUS_NUM",), local_shape=("CUS_NUM", "TRA_AMT"),
    )
    assert mismatch["status"] == "BLOCK"
    assert "result_shape_mismatch" in mismatch["discrepancies"]
