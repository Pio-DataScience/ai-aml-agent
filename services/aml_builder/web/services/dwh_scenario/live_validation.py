"""Explicitly approved, bounded Oracle acceptance workflow for B8.

This module performs no work at import time. Live execution requires a
scope-specific approval object, a separately configured acceptance account,
and injected HTTP/local generators. It never changes DWH_SQL_MODE.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import re
import time
from typing import Any, Callable, ContextManager, Iterable, Mapping, Sequence
from urllib.parse import urlsplit

import sqlglot
from sqlglot import exp

from services.aml_builder.web.adapters.dwh_oracle_repo import (
    CandidateQueryResult,
    DwhOracleRepository,
    OracleRequestBudget,
)
from services.aml_builder.web.domain.dwh_scenario.contracts import (
    ScenarioSqlGenerator,
    ScenarioSqlRequest,
    ScenarioSqlResult,
)
from services.aml_builder.web.domain.dwh_scenario.sql_policy import (
    SqlPolicyError,
    validate_oracle_read,
)
from services.aml_builder.web.domain.dwh_scenario.validation import (
    validate_generated_sql,
)
from services.aml_builder.web.services.settings import Settings, settings


@dataclass(frozen=True)
class AcceptanceLimits:
    """Hard upper bounds for one manually approved acceptance run."""

    max_scenarios: int = 2
    max_objects: int = 4
    max_rows_per_query: int = 100
    statement_timeout_seconds: float = 10.0
    generator_timeout_seconds: float = 45.0
    overall_timeout_seconds: float = 120.0
    max_statements: int = 24

    def validate(self) -> None:
        """Reject limits above conservative hard ceilings.

        Raises:
            ValueError: If any requested limit is invalid or exceeds its cap.
        """
        maxima = {
            "max_scenarios": (self.max_scenarios, 3),
            "max_objects": (self.max_objects, 8),
            "max_rows_per_query": (self.max_rows_per_query, 250),
            "max_statements": (self.max_statements, 32),
        }
        if any(value < 1 or value > maximum for value, maximum in maxima.values()):
            raise ValueError("Acceptance count/row limit exceeds its hard cap")
        if not 0 < self.statement_timeout_seconds <= 10:
            raise ValueError("Statement timeout must be at most 10 seconds")
        if not 0 < self.generator_timeout_seconds <= 60:
            raise ValueError("Generator timeout must be at most 60 seconds")
        if not 0 < self.overall_timeout_seconds <= 180:
            raise ValueError("Overall timeout must be at most 180 seconds")


@dataclass(frozen=True)
class AcceptanceScenario:
    """One approved AML request included in the test scope."""

    case_id: str
    request: ScenarioSqlRequest


@dataclass(frozen=True)
class AcceptancePlan:
    """Complete, hashable scope reviewed before any live statement runs."""

    scenarios: tuple[AcceptanceScenario, ...]
    allowed_schemas: tuple[str, ...]
    allowed_objects: tuple[str, ...]
    expected_resource_plan: str
    http_endpoint: str
    generator_routes: str
    provider_data_approval_ref: str
    limits: AcceptanceLimits = field(default_factory=AcceptanceLimits)

    def validate(self) -> None:
        """Validate scope size, identifiers, time bounds and allowlists."""
        self.limits.validate()
        if not self.scenarios or len(self.scenarios) > self.limits.max_scenarios:
            raise ValueError("Scenario count is outside the approved limit")
        if not self.allowed_schemas or not self.allowed_objects:
            raise ValueError("Explicit schema and object allowlists are required")
        if len(self.allowed_objects) > self.limits.max_objects:
            raise ValueError("Object allowlist exceeds its hard cap")
        # Five baseline probes, one enabled-role grant inventory, four probes
        # per object, and at least one query for each generator candidate.
        # Two candidate executions plus two no-row projection probes per case.
        minimum_statements = 6 + 4 * len(self.allowed_objects) + 4 * len(self.scenarios)
        if self.limits.max_statements < minimum_statements:
            raise ValueError(
                "Statement budget is too small for the approved preflight scope"
            )
        if not self.expected_resource_plan.strip():
            raise ValueError("A DBA-approved Oracle Resource Manager plan is required")
        if not self.generator_routes.strip():
            raise ValueError(
                "HTTP/local generator routes must be included in the approved scope"
            )
        parsed_endpoint = urlsplit(self.http_endpoint)
        if (
            parsed_endpoint.scheme not in {"http", "https"}
            or not parsed_endpoint.hostname
            or parsed_endpoint.username
            or parsed_endpoint.password
            or parsed_endpoint.query
        ):
            raise ValueError("HTTP generator endpoint must be an explicit safe URL")
        if not self.provider_data_approval_ref.strip():
            raise ValueError("Provider data approval reference is required")
        if len({item.case_id for item in self.scenarios}) != len(self.scenarios):
            raise ValueError("Acceptance case identifiers must be unique")
        schemas = {value.upper() for value in self.allowed_schemas}
        for value in self.allowed_schemas:
            if not re.fullmatch(r"[A-Z][A-Z0-9_$#]{0,127}", value.upper()):
                raise ValueError("Unsupported schema identifier in acceptance scope")
        for value in self.allowed_objects:
            parts = value.upper().split(".")
            if (
                len(parts) != 2
                or parts[0] not in schemas
                or any(not re.fullmatch(r"[A-Z][A-Z0-9_$#]{0,127}", p) for p in parts)
            ):
                raise ValueError("Objects must be qualified and inside allowed schemas")
        allowed_objects = {value.upper() for value in self.allowed_objects}
        for scenario in self.scenarios:
            window = scenario.request.intent.time_window
            if (
                window is None
                or not window.is_rolling
                or window.unit != "DAYS"
                or window.value > 365
            ):
                raise ValueError(
                    "Acceptance scenarios require a rolling day window of at most 365 days"
                )
            if not scenario.request.confirmed_filters:
                raise ValueError(
                    "Acceptance scenarios require confirmed catalog filters"
                )
            for item in scenario.request.confirmed_filters:
                target_table = str(item.target.get("table", "")).upper()
                if not target_table or not str(item.target.get("column", "")).strip():
                    raise ValueError(
                        "Confirmed acceptance filters require catalog table and column targets"
                    )
                if target_table and not any(
                    value.endswith(f".{target_table}") for value in allowed_objects
                ):
                    raise ValueError(
                        "Confirmed filter target is outside the object allowlist"
                    )

    def scope_hash(self) -> str:
        """Return a deterministic digest binding approval to exact test scope."""
        self.validate()
        material = {
            "cases": [
                {
                    "case_id": item.case_id,
                    "request": item.request.model_dump(mode="json"),
                }
                for item in self.scenarios
            ],
            "schemas": sorted(value.upper() for value in self.allowed_schemas),
            "objects": sorted(value.upper() for value in self.allowed_objects),
            "resource_plan": self.expected_resource_plan,
            "http_endpoint": self.http_endpoint,
            "generator_routes": self.generator_routes,
            "provider_data_approval_ref": self.provider_data_approval_ref,
            "limits": asdict(self.limits),
        }
        encoded = json.dumps(material, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def scope_summary(self) -> dict[str, Any]:
        """Return the human-reviewable scope without SQL or customer data."""
        return {
            "scope_sha256": self.scope_hash(),
            "case_ids": [item.case_id for item in self.scenarios],
            "allowed_schemas": sorted(value.upper() for value in self.allowed_schemas),
            "allowed_objects": sorted(value.upper() for value in self.allowed_objects),
            "resource_manager_plan": self.expected_resource_plan,
            "http_endpoint": self.http_endpoint,
            "generator_routes": self.generator_routes,
            "provider_data_approval_ref": self.provider_data_approval_ref,
            "limits": asdict(self.limits),
        }


DISCOVERY_OBJECT_NAMES = (
    "PIO_BANKBI_DICTIONARY_TABLES",
    "PIO_BANKBI_DICTIONARY_COL",
    "PIO_BANKBI_DICT_JOIN_TABLES",
    "PIO_AML_TABLES",
    "PIO_AML_COLUMNS",
    "PIO_TRANSACTIONS",
    "PIO_EXPLANATION_CODE",
)
DISCOVERY_SQL = (
    "SELECT USER AS SESSION_USER FROM DUAL",
    "SELECT PRIVILEGE FROM SESSION_PRIVS ORDER BY PRIVILEGE FETCH FIRST 100 ROWS ONLY",
    "SELECT ROLE FROM SESSION_ROLES ORDER BY ROLE FETCH FIRST 100 ROWS ONLY",
    "SELECT GRANTEE, TABLE_SCHEMA AS OWNER, TABLE_NAME, PRIVILEGE "
    "FROM ALL_TAB_PRIVS WHERE TABLE_SCHEMA = :schema_owner AND TABLE_NAME IN "
    "(:n1, :n2, :n3, :n4, :n5, :n6, :n7) "
    "AND (GRANTEE = :expected_user OR GRANTEE = 'PUBLIC' OR GRANTEE IN "
    "(SELECT ROLE FROM SESSION_ROLES)) "
    "ORDER BY TABLE_SCHEMA, TABLE_NAME, GRANTEE, PRIVILEGE FETCH FIRST 100 ROWS ONLY",
    "SELECT GRANTEE, TABLE_SCHEMA AS OWNER, TABLE_NAME, COLUMN_NAME, PRIVILEGE "
    "FROM ALL_COL_PRIVS WHERE TABLE_SCHEMA = :schema_owner AND TABLE_NAME IN "
    "(:n1, :n2, :n3, :n4, :n5, :n6, :n7) "
    "AND (GRANTEE = :expected_user OR GRANTEE = 'PUBLIC' OR GRANTEE IN "
    "(SELECT ROLE FROM SESSION_ROLES)) "
    "ORDER BY TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, GRANTEE, PRIVILEGE "
    "FETCH FIRST 100 ROWS ONLY",
    "SELECT OWNER, OBJECT_NAME, OBJECT_TYPE, STATUS FROM ALL_OBJECTS WHERE "
    "OWNER = :schema_owner AND OBJECT_NAME IN (:n1, :n2, :n3, :n4, :n5, :n6, :n7) "
    "AND OBJECT_TYPE IN ('TABLE', 'VIEW', 'MATERIALIZED VIEW', 'SYNONYM') "
    "ORDER BY OWNER, OBJECT_NAME FETCH FIRST 50 ROWS ONLY",
    "SELECT OWNER, SYNONYM_NAME, TABLE_OWNER, TABLE_NAME, DB_LINK FROM ALL_SYNONYMS "
    "WHERE OWNER IN (:schema_owner, 'PUBLIC') AND SYNONYM_NAME IN "
    "(:n1, :n2, :n3, :n4, :n5, :n6, :n7) ORDER BY OWNER, SYNONYM_NAME "
    "FETCH FIRST 50 ROWS ONLY",
    "SELECT OWNER, TABLE_NAME, COLUMN_NAME, DATA_TYPE, DATA_LENGTH, NULLABLE "
    "FROM ALL_TAB_COLUMNS WHERE OWNER = :schema_owner AND TABLE_NAME IN "
    "(:n1, :n2, :n3, :n4, :n5, :n6, :n7) AND REGEXP_LIKE(UPPER(COLUMN_NAME), "
    ":column_pattern) ORDER BY TABLE_NAME, COLUMN_ID FETCH FIRST 250 ROWS ONLY",
    "SELECT VALUE FROM V$PARAMETER WHERE NAME = :parameter_name",
    "SELECT d.TABLE_NAME, d.COLUMN_NAME, d.LOOKUP_TABLE_NAME, "
    "a.LOOKUP_CODE_COLUMN, a.LOOKUP_DESC_COLUMN, a.LOOKUP_COUNTRY_COLUMN, "
    "a.LOOKUP_INST_COLUMN FROM BI_DWH.PIO_BANKBI_DICTIONARY_COL d LEFT JOIN "
    "BI_DWH.PIO_AML_COLUMNS a ON UPPER(a.COLUMN_NAME) = UPPER(d.COLUMN_NAME) "
    "WHERE UPPER(d.TABLE_NAME) = :table_name AND UPPER(d.COLUMN_NAME) = "
    ":column_name FETCH FIRST 5 ROWS ONLY",
    "SELECT EXPLANATION_CODE, DESC_ENG, DESC_NAT_LAN, LONG_DES_ENG "
    "FROM BI_DWH.PIO_EXPLANATION_CODE WHERE EXPLANATION_CODE = :code AND "
    "(COUNTRY_CODE = :country_code OR COUNTRY_CODE IS NULL) AND "
    "(INST_CODE = :inst_code OR INST_CODE IS NULL) FETCH FIRST 5 ROWS ONLY",
)
DISCOVERY_COLUMN_PATTERN = (
    "^(CUS_NUM|CUSTOMER_ID|CUSTOMER_NO|CLIENT_ID|CIF|TRA_DATE|TRA_DAY_DATE|"
    "VALUE_DATE|POST_DATE|TRANSACTION_DATE|TRA_AMT|EQU_TRA_AMT|AMOUNT|EXPL_CODE|"
    "TABLE_NAME|COLUMN_NAME|OBJECT_NAME|LOOKUP_TABLE_NAME|LOOKUP_CODE_COLUMN|"
    "LOOKUP_DESC_COLUMN|LOOKUP_COUNTRY_COLUMN|LOOKUP_INST_COLUMN|EXPLANATION_CODE|"
    "DESC_ENG|DESC_NAT_LAN|LONG_DES_ENG|COUNTRY_CODE|INST_CODE)$"
)
DISCOVERY_SQL_CONDITIONS = (
    "Always; verify the live user is BI_DWH",
    "Always; non-session system privileges are security warnings, not Stage 1 execution blockers",
    "Always; enumerate enabled roles",
    "Always; enumerate scoped object grants; write grants are security warnings",
    "Always; enumerate scoped column grants; write grants are security warnings",
    "Always; verify BI_DWH object existence before column lookup",
    "Always; report visible BI_DWH synonyms for the scoped names",
    "Always; return only relevant transaction, customer, dictionary, and code columns",
    "Optional; unavailable V$PARAMETER access is reported for DBA confirmation",
    "Only if both dictionary tables and every referenced lookup column exist; mapping is preliminary",
    "Only if PIO_EXPLANATION_CODE and every selected/filter column exist",
)


@dataclass(frozen=True)
class DiscoveryLimits:
    """Hard limits for metadata-only Stage 1 discovery."""

    statement_timeout_seconds: float = 10.0
    overall_timeout_seconds: float = 90.0
    max_statements: int = 12
    max_privilege_rows: int = 100
    max_object_rows: int = 50
    max_column_rows: int = 250
    max_mapping_rows: int = 5

    def validate(self) -> None:
        """Reject discovery limits outside the Stage 1 safety ceiling."""
        if not 0 < self.statement_timeout_seconds <= 10:
            raise ValueError("Discovery statement timeout must be at most 10 seconds")
        if not 0 < self.overall_timeout_seconds <= 90:
            raise ValueError("Discovery overall timeout must be at most 90 seconds")
        if not 12 <= self.max_statements <= 12:
            raise ValueError("Discovery statement budget must be exactly 12")
        if any(
            value < 1 or value > cap
            for value, cap in (
                (self.max_privilege_rows, 100),
                (self.max_object_rows, 50),
                (self.max_column_rows, 250),
                (self.max_mapping_rows, 5),
            )
        ):
            raise ValueError("Discovery metadata row limit exceeds its hard cap")


@dataclass(frozen=True)
class SchemaDiscoveryPlan:
    """Reviewable, hash-bound Stage 1 scope with no scenario SQL or LLM route."""

    schema_owner: str = "BI_DWH"
    expected_user: str = "BI_DWH"
    limits: DiscoveryLimits = field(default_factory=DiscoveryLimits)

    @property
    def provider_data_approval_ref(self) -> str:
        """Mark the plan as metadata-only with no provider data transfer."""
        return "not-applicable:metadata-only-no-llm"

    @property
    def credential_policy(self) -> str:
        """Describe the Stage 1 SELECT-only operation and privilege-warning policy."""
        return "select-only-operations; credential privileges are reported as warnings"

    @property
    def allowed_schemas(self) -> tuple[str, ...]:
        """Return the single approved schema for the discovery run."""
        return (self.schema_owner,)

    @property
    def allowed_objects(self) -> tuple[str, ...]:
        """Return only the fixed, schema-qualified Stage 1 object scope."""
        return tuple(f"{self.schema_owner}.{name}" for name in DISCOVERY_OBJECT_NAMES)

    @property
    def bind_values(self) -> dict[str, Any]:
        """Return fixed and explicitly bound metadata lookup values."""
        binds: dict[str, Any] = {
            "expected_user": self.expected_user,
            "schema_owner": self.schema_owner,
            "parameter_name": "resource_manager_plan",
            "column_pattern": DISCOVERY_COLUMN_PATTERN,
            "table_name": "PIO_TRANSACTIONS",
            "column_name": "EXPL_CODE",
            "code": "4",
            "country_code": "400",
            "inst_code": "1",
        }
        binds.update(
            {f"n{index}": name for index, name in enumerate(DISCOVERY_OBJECT_NAMES, 1)}
        )
        return binds

    def binds_for_statement(self, index: int) -> dict[str, Any]:
        """Return only the named bind values used by one manifest statement."""
        common_scope = {
            "schema_owner": self.schema_owner,
            **{
                f"n{position}": name
                for position, name in enumerate(DISCOVERY_OBJECT_NAMES, 1)
            },
        }
        per_statement = {
            0: {},
            1: {},
            2: {},
            3: {**common_scope, "expected_user": self.expected_user},
            4: {**common_scope, "expected_user": self.expected_user},
            5: common_scope,
            6: common_scope,
            7: {**common_scope, "column_pattern": DISCOVERY_COLUMN_PATTERN},
            8: {"parameter_name": "resource_manager_plan"},
            9: {"table_name": "PIO_TRANSACTIONS", "column_name": "EXPL_CODE"},
            10: {"code": "4", "country_code": "400", "inst_code": "1"},
        }
        if index not in per_statement:
            raise IndexError("Discovery statement index is outside the manifest")
        return per_statement[index]

    def validate(self) -> None:
        """Validate the fixed B8 Stage 1 scope and conservative limits."""
        self.limits.validate()
        if self.schema_owner != "BI_DWH" or self.expected_user != "BI_DWH":
            raise ValueError("Stage 1 schema owner and database user must be BI_DWH")
        if len(DISCOVERY_SQL) > self.limits.max_statements:
            raise ValueError(
                "Discovery SQL manifest must fit inside the 12-statement budget"
            )

    def scope_hash(self) -> str:
        """Hash exact SQL, binds, objects, limits, user and DBA-plan expectation."""
        self.validate()
        material = {
            "stage": "B8-stage-1-read-only-schema-discovery-v1",
            "schema": self.schema_owner,
            "expected_user": self.expected_user,
            "provider_data_approval_ref": self.provider_data_approval_ref,
            "objects": list(self.allowed_objects),
            "sql": list(DISCOVERY_SQL),
            "conditions": list(DISCOVERY_SQL_CONDITIONS),
            "credential_policy": self.credential_policy,
            "binds": self.bind_values,
            "limits": asdict(self.limits),
            "dwh_sql_mode": "http",
            "acceptance_enabled": False,
        }
        encoded = json.dumps(material, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def scope_summary(self) -> dict[str, Any]:
        """Return the exact review payload without credentials or customer data."""
        return {
            "stage": "B8-stage-1-read-only-schema-discovery-v1",
            "scope_sha256": self.scope_hash(),
            "schema_owner": self.schema_owner,
            "expected_user": self.expected_user,
            "provider_data_approval_ref": self.provider_data_approval_ref,
            "objects": list(self.allowed_objects),
            "sql": list(DISCOVERY_SQL),
            "conditions": list(DISCOVERY_SQL_CONDITIONS),
            "credential_policy": self.credential_policy,
            "binds": self.bind_values,
            "binds_by_statement": [
                self.binds_for_statement(index) for index in range(len(DISCOVERY_SQL))
            ],
            "limits": asdict(self.limits),
        }


@dataclass(frozen=True)
class LiveValidationApproval:
    """Explicit human approval for exactly one previously reviewed plan."""

    scope_sha256: str
    approved_by: str
    approved_at: str
    provider_data_approval_ref: str


@dataclass
class AcceptanceStatementBudget:
    """One shared statement cap covering preflight and both generators."""

    maximum: int
    used: int = 0

    def reserve(self) -> None:
        """Reserve a statement before Oracle execution or fail closed."""
        if self.used >= self.maximum:
            raise RuntimeError("Acceptance statement limit exceeded")
        self.used += 1


class DedicatedAcceptanceProvider:
    """Callable context provider backed only by the dedicated acceptance pool."""

    acceptance_read_only = True

    def __init__(self, pool: Any) -> None:
        """Store the explicitly configured one-connection acceptance pool."""
        self._pool = pool

    @contextmanager
    def __call__(self) -> Iterable[Any]:
        connection = self._pool.acquire()
        try:
            yield connection
        finally:
            self._pool.release(connection)


class AcceptanceOracleRepository:
    """Read-only candidate executor using A's policy and Oracle repository."""

    def __init__(
        self,
        *,
        connection_provider: Callable[[], ContextManager[Any]],
        plan: AcceptancePlan,
        statement_budget: AcceptanceStatementBudget,
        run_started_at: float,
    ) -> None:
        """Bind a dedicated provider, hard limits and policy scope to this run."""
        self.plan = plan
        self._calls = 0
        self._statement_budget = statement_budget
        self._run_started_at = run_started_at
        self._results: dict[str, CandidateQueryResult] = {}
        self._shapes: dict[str, tuple[str, ...]] = {}
        self._base = DwhOracleRepository(
            connection_provider=connection_provider,
            allowed_schemas=plan.allowed_schemas,
            allowed_objects=plan.allowed_objects,
            max_candidate_rows=plan.limits.max_rows_per_query,
            read_timeout_seconds=plan.limits.statement_timeout_seconds,
        )

    @property
    def statements_used(self) -> int:
        """Return the number of SQL statements this repository attempted."""
        return self._calls

    def captured(self, sql: str) -> CandidateQueryResult | None:
        """Return in-memory bounded evidence for a local generator candidate."""
        return self._results.get(_sha256(sql))

    def probe_result_shape(
        self, sql: str, *, budget: OracleRequestBudget
    ) -> tuple[str, ...] | None:
        """Resolve output columns with a bounded no-row SELECT probe.

        This is evidence about projection shape only. It is independently
        policy-checked and never fetches customer or transaction rows.
        """
        self._calls += 1
        try:
            self._statement_budget.reserve()
            bound_sql, bind_values, _audit = bind_sql_literals(sql)
            probe_sql = f"SELECT * FROM ({bound_sql}) b8_shape WHERE 1=0"
            validate_oracle_read(
                probe_sql,
                params=bind_values,
                allowed_schemas=self.plan.allowed_schemas,
                allowed_objects=self.plan.allowed_objects,
            )
            remaining = min(
                budget.deadline_seconds - (time.monotonic() - budget.started_at),
                self.plan.limits.overall_timeout_seconds
                - (time.monotonic() - self._run_started_at),
                self.plan.limits.statement_timeout_seconds,
            )
            if remaining <= 0:
                return None
            budget.consume()
            with self._base._connection_provider() as connection:
                cursor = connection.cursor()
                cursor.call_timeout = int(remaining * 1000)
                cursor.execute(probe_sql, bind_values)
                shape = tuple(
                    str(column[0]).upper() for column in (cursor.description or ())
                )
            self._shapes[_sha256(sql)] = shape
            return shape
        except Exception:
            return None

    def captured_shape(self, sql: str) -> tuple[str, ...] | None:
        """Return the no-row shape probe captured for a candidate."""
        return self._shapes.get(_sha256(sql))

    def execute_candidate(
        self,
        sql: str,
        params: dict[str, Any] | None = None,
        *,
        budget: OracleRequestBudget,
        required_projection: str = "CUS_NUM",
        confirmed_filters: list[dict[str, Any]] | None = None,
    ) -> CandidateQueryResult:
        """Bind literals, cap rows in Oracle, and delegate deterministic checks.

        The returned object contains only bounded rows in memory. Nothing here
        logs SQL, bind values or result values.
        """
        self._calls += 1
        try:
            self._statement_budget.reserve()
        except RuntimeError:
            return _failed_result(sql, "Acceptance statement limit exceeded")
        try:
            validate_oracle_read(
                sql,
                params=params,
                allowed_schemas=self.plan.allowed_schemas,
                allowed_objects=self.plan.allowed_objects,
                required_projection=required_projection,
                confirmed_filters=confirmed_filters,
            )
            bound_sql, bind_values, bind_audit = bind_sql_literals(sql)
            if params:
                overlap = set(params) & set(bind_values)
                if overlap:
                    return _failed_result(sql, "Generated SQL uses unsupported binds")
                bind_values.update(params)
            cap_name = "b8_result_cap"
            bounded_sql = _bounded_customer_sql(
                bound_sql, required_projection, cap_name
            )
            bind_values[cap_name] = self.plan.limits.max_rows_per_query + 1
            validate_oracle_read(
                bounded_sql,
                params=bind_values,
                allowed_schemas=self.plan.allowed_schemas,
                allowed_objects=self.plan.allowed_objects,
                required_projection=required_projection,
                confirmed_filters=confirmed_filters,
            )
            run_remaining = self.plan.limits.overall_timeout_seconds - (
                time.monotonic() - self._run_started_at
            )
            remaining = min(
                budget.deadline_seconds - (time.monotonic() - budget.started_at),
                run_remaining,
                self.plan.limits.statement_timeout_seconds,
            )
            if remaining <= 0:
                return _failed_result(sql, "Acceptance query deadline exceeded")
            budget.consume()
            bounded_budget = OracleRequestBudget(
                max_statements=1, deadline_seconds=remaining
            )
            evidence = self._base.execute_candidate(
                bounded_sql,
                bind_values,
                budget=bounded_budget,
                required_projection=required_projection,
                confirmed_filters=confirmed_filters,
            )
            evidence = CandidateQueryResult(
                sql=sql,
                success=evidence.success,
                columns=evidence.columns,
                rows=evidence.rows,
                row_count=evidence.row_count,
                truncated=evidence.truncated,
                elapsed_ms=evidence.elapsed_ms,
                error=evidence.error,
            )
            self._results[_sha256(sql)] = evidence
            self._results[_sha256(bounded_sql)] = evidence
            _ = bind_audit
            return evidence
        except (SqlPolicyError, ValueError, RuntimeError, TimeoutError) as exc:
            return _failed_result(sql, str(exc))


def bind_sql_literals(sql: str) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
    """Replace SQL literals with named binds and describe safe audit metadata.

    Args:
        sql: One deterministic-policy-approved Oracle SELECT.

    Returns:
        Bound SQL, Oracle bind mapping, and bind audit rows. Sensitive columns
        are marked for masking in reports.

    Raises:
        ValueError: If parsing fails or the SQL already contains unknown binds.
    """
    try:
        statements = sqlglot.parse(sql, read="oracle")
    except (sqlglot.errors.ParseError, ValueError) as exc:
        raise ValueError(
            "Candidate SQL could not be parsed for bind conversion"
        ) from exc
    if len(statements) != 1 or statements[0] is None:
        raise ValueError("Exactly one SQL statement is required for bind conversion")
    root = statements[0]
    if any(root.find_all(exp.Placeholder)):
        raise ValueError("Candidate SQL contains binds without supplied values")
    values: dict[str, Any] = {}
    audit: list[dict[str, Any]] = []
    for index, literal in enumerate(list(root.find_all(exp.Literal))):
        name = f"b8_v{index}"
        value: Any = literal.this if literal.is_string else _numeric_value(literal.this)
        predicate = literal.parent
        while predicate is not None and not isinstance(predicate, exp.Predicate):
            predicate = predicate.parent
        relevant_columns = sorted(
            {column.name.upper() for column in predicate.find_all(exp.Column)}
            if predicate is not None
            else set()
        )
        sensitive = any(
            marker in column
            for column in relevant_columns
            for marker in (
                "CUS",
                "CUSTOMER",
                "ACCOUNT",
                "NAME",
                "PHONE",
                "EMAIL",
                "ADDRESS",
            )
        )
        values[name] = value
        audit.append(
            {
                "name": name,
                "value": value,
                "columns": relevant_columns,
                "sensitive": sensitive,
            }
        )
        literal.replace(exp.Placeholder(this=name))
    return root.sql(dialect="oracle"), values, audit


def create_acceptance_pool_provider(
    plan: AcceptancePlan | SchemaDiscoveryPlan,
    approval: LiveValidationApproval,
    config: Settings = settings,
) -> tuple[DedicatedAcceptanceProvider, Any]:
    """Create a single-connection dedicated acceptance pool with no fallback.

    This function is explicit and is never called during app startup. The pool
    is not created unless its opt-in flag and all dedicated credentials exist.
    """
    _validate_approval(plan, approval)
    if not config.DWH_SQL_ACCEPTANCE_ENABLED:
        raise RuntimeError("DWH_SQL_ACCEPTANCE_ENABLED must be true")
    if config.DWH_SQL_MODE != "http":
        raise RuntimeError("B8 acceptance requires DWH_SQL_MODE=http to remain active")
    if isinstance(plan, AcceptancePlan) and (
        not config.DWH_SQL_ACCEPTANCE_RESOURCE_PLAN
        or config.DWH_SQL_ACCEPTANCE_RESOURCE_PLAN.casefold()
        != plan.expected_resource_plan.casefold()
    ):
        raise PermissionError(
            "Configured Resource Manager plan does not match approval"
        )
    if isinstance(plan, AcceptancePlan) and (
        config.PIOTECH_AI_URL.rstrip("/") != plan.http_endpoint.rstrip("/")
    ):
        raise PermissionError("Configured HTTP endpoint does not match approval")
    if not config.DWH_SQL_ALLOWED_SCHEMAS or not config.DWH_SQL_ALLOWED_OBJECTS:
        raise RuntimeError(
            "Configured DWH SQL schema and object allowlists are required"
        )
    if {item.upper() for item in plan.allowed_schemas} - {
        item.upper() for item in config.DWH_SQL_ALLOWED_SCHEMAS
    }:
        raise PermissionError("Acceptance scope exceeds configured schema allowlist")
    if {item.upper() for item in plan.allowed_objects} - {
        item.upper() for item in config.DWH_SQL_ALLOWED_OBJECTS
    }:
        raise PermissionError("Acceptance scope exceeds configured object allowlist")
    if not all(
        (
            config.DWH_SQL_ACCEPTANCE_DSN,
            config.DWH_SQL_ACCEPTANCE_USER,
            config.DWH_SQL_ACCEPTANCE_PASSWORD,
        )
    ):
        raise RuntimeError("Dedicated acceptance Oracle credentials are required")
    if isinstance(plan, SchemaDiscoveryPlan) and (
        config.DWH_SQL_ACCEPTANCE_USER.upper() != plan.expected_user.upper()
    ):
        raise PermissionError(
            "Configured acceptance user does not match approved scope"
        )
    import oracledb

    pool = oracledb.create_pool(
        user=config.DWH_SQL_ACCEPTANCE_USER,
        password=config.DWH_SQL_ACCEPTANCE_PASSWORD,
        dsn=config.DWH_SQL_ACCEPTANCE_DSN,
        min=1,
        max=1,
        increment=1,
        getmode=oracledb.POOL_GETMODE_WAIT,
        timeout=5,
    )

    return DedicatedAcceptanceProvider(pool), pool


def run_preflight(
    provider: Callable[[], ContextManager[Any]],
    plan: AcceptancePlan,
    *,
    expected_user: str,
    statement_timeout_seconds: float,
    statement_budget: AcceptanceStatementBudget,
) -> dict[str, Any]:
    """Verify identity, grants, data dictionary and resource plan using reads.

    Every query is bounded and uses bind values. Any inaccessible or
    incompatible check returns BLOCK before generator SQL is executed.
    """
    checks: dict[str, Any] = {}
    statement_count = 0
    preflight_started = time.monotonic()
    allowed_metadata_objects = (
        "DUAL",
        "SESSION_PRIVS",
        "ALL_TAB_PRIVS",
        "ALL_COL_PRIVS",
        "SESSION_ROLES",
        "ALL_OBJECTS",
        "ALL_TAB_COLUMNS",
        "V$PARAMETER",
    )

    def query(
        sql: str, binds: dict[str, Any] | None = None, limit: int = 250
    ) -> list[tuple[Any, ...]]:
        nonlocal statement_count
        remaining = plan.limits.overall_timeout_seconds - (
            time.monotonic() - preflight_started
        )
        if remaining <= 0:
            raise TimeoutError("Acceptance preflight deadline exceeded")
        validate_oracle_read(
            sql,
            params=binds,
            allowed_objects=allowed_metadata_objects,
        )
        statement_count += 1
        statement_budget.reserve()
        with provider() as connection:
            cursor = connection.cursor()
            cursor.call_timeout = int(min(statement_timeout_seconds, remaining) * 1000)
            cursor.execute(sql, binds or {})
            rows = list(cursor.fetchmany(limit + 1))
            if len(rows) > limit:
                raise RuntimeError("Preflight result exceeded its configured bound")
            return rows

    try:
        identity_rows = query("SELECT USER FROM DUAL", limit=1)
        actual_user = str(identity_rows[0][0]).upper() if identity_rows else ""
        checks["identity"] = {
            "status": "PASS" if actual_user == expected_user.upper() else "BLOCK",
            "expected_user": expected_user.upper(),
            "actual_user": actual_user,
        }
        if actual_user != expected_user.upper():
            return {
                "status": "BLOCK",
                "checks": checks,
                "statements_used": statement_count,
            }
        privileges = {
            str(row[0]).upper()
            for row in query("SELECT PRIVILEGE FROM SESSION_PRIVS", limit=500)
        }
        dangerous = sorted(privileges - {"CREATE SESSION"})
        checks["session_privileges"] = {
            "status": "PASS" if not dangerous else "BLOCK",
            "write_or_broad_privileges": dangerous,
        }
        if dangerous:
            return {
                "status": "BLOCK",
                "checks": checks,
                "statements_used": statement_count,
            }
        enabled_roles = {
            str(row[0]).upper()
            for row in query("SELECT ROLE FROM SESSION_ROLES", limit=500)
        }
        global_object_writes = query(
            "SELECT OWNER, TABLE_NAME, PRIVILEGE FROM ALL_TAB_PRIVS "
            "WHERE (GRANTEE IN (USER, 'PUBLIC') OR GRANTEE IN "
            "(SELECT ROLE FROM SESSION_ROLES)) "
            "AND PRIVILEGE NOT IN ('SELECT', 'READ')",
            limit=500,
        )
        global_column_writes = query(
            "SELECT OWNER, TABLE_NAME, COLUMN_NAME, PRIVILEGE FROM ALL_COL_PRIVS "
            "WHERE (GRANTEE IN (USER, 'PUBLIC') OR GRANTEE IN "
            "(SELECT ROLE FROM SESSION_ROLES)) "
            "AND PRIVILEGE NOT IN ('SELECT', 'READ')",
            limit=500,
        )
        checks["global_write_grants"] = {
            "status": (
                "PASS"
                if not global_object_writes and not global_column_writes
                else "BLOCK"
            ),
            "object_grant_count": len(global_object_writes),
            "column_grant_count": len(global_column_writes),
            "enabled_role_count": len(enabled_roles),
        }
        if global_object_writes or global_column_writes:
            return {
                "status": "BLOCK",
                "checks": checks,
                "statements_used": statement_count,
            }
        resource_rows = query(
            "SELECT VALUE FROM V$PARAMETER WHERE NAME = :parameter_name",
            {"parameter_name": "resource_manager_plan"},
            limit=1,
        )
        active_plan = str(resource_rows[0][0]).strip() if resource_rows else ""
        checks["resource_manager"] = {
            "status": (
                "PASS"
                if active_plan.casefold() == plan.expected_resource_plan.casefold()
                else "BLOCK"
            ),
            "expected_plan": plan.expected_resource_plan,
            "active_plan": active_plan,
        }
        if active_plan.casefold() != plan.expected_resource_plan.casefold():
            return {
                "status": "BLOCK",
                "checks": checks,
                "statements_used": statement_count,
            }
        object_results: dict[str, Any] = {}
        for qualified in plan.allowed_objects:
            owner, name = qualified.upper().split(".")
            objects = query(
                "SELECT OBJECT_TYPE FROM ALL_OBJECTS WHERE OWNER = :owner "
                "AND OBJECT_NAME = :object_name AND OBJECT_TYPE IN "
                "('TABLE', 'VIEW', 'MATERIALIZED VIEW')",
                {"owner": owner, "object_name": name},
                limit=2,
            )
            columns = query(
                "SELECT COLUMN_NAME FROM ALL_TAB_COLUMNS WHERE OWNER = :owner "
                "AND TABLE_NAME = :object_name ORDER BY COLUMN_ID",
                {"owner": owner, "object_name": name},
                limit=500,
            )
            write_grants = query(
                "SELECT PRIVILEGE FROM ALL_TAB_PRIVS WHERE OWNER = :owner "
                "AND TABLE_NAME = :object_name AND PRIVILEGE NOT IN ('SELECT', 'READ')",
                {"owner": owner, "object_name": name},
                limit=100,
            )
            probe_sql = f'SELECT 1 FROM "{owner}"."{name}" WHERE 1 = 0'
            validate_oracle_read(
                probe_sql,
                allowed_schemas=plan.allowed_schemas,
                allowed_objects=plan.allowed_objects,
            )
            statement_budget.reserve()
            statement_count += 1
            with provider() as connection:
                cursor = connection.cursor()
                remaining = plan.limits.overall_timeout_seconds - (
                    time.monotonic() - preflight_started
                )
                if remaining <= 0:
                    raise TimeoutError("Acceptance preflight deadline exceeded")
                cursor.call_timeout = int(
                    min(statement_timeout_seconds, remaining) * 1000
                )
                cursor.execute(probe_sql, {})
                select_access = cursor.fetchmany(1) == []
            object_results[qualified] = {
                "exists": bool(objects),
                "columns": [str(row[0]).upper() for row in columns],
                "write_grants": [str(row[0]).upper() for row in write_grants],
                "select_access": select_access,
            }
        compatible = all(
            item["exists"] and not item["write_grants"] and item["select_access"]
            for item in object_results.values()
        )
        checks["dictionary_and_object_grants"] = {
            "status": "PASS" if compatible else "BLOCK",
            "objects": object_results,
        }
    except Exception as exc:
        checks["preflight_error"] = {
            "status": "BLOCK",
            "error": type(exc).__name__,
            "detail": str(exc)[:160],
        }
    blocked = any(value.get("status") == "BLOCK" for value in checks.values())
    return {
        "status": "BLOCK" if blocked else "PASS",
        "checks": checks,
        "statements_used": statement_count,
    }


def run_schema_discovery(
    plan: SchemaDiscoveryPlan,
    approval: LiveValidationApproval,
    *,
    provider: Callable[[], ContextManager[Any]],
    config: Settings = settings,
) -> dict[str, Any]:
    """Execute the approved metadata-only Stage 1 discovery on the shared pool.

    Args:
        plan: Exact BI_DWH schema/object/SQL scope authorized by the owner.
        approval: Approval whose digest and metadata-only reference match plan.
        provider: Existing dedicated acceptance pool provider; no pool is created
            or replaced here.
        config: Runtime configuration checked before the first SQL statement.

    Returns:
        A redacted metadata report. Missing objects and conditional mapping skips
        are reported without aborting discovery of other available objects.

    Raises:
        PermissionError: If approval, mode, user, or provider scope mismatches.
        RuntimeError: If acceptance is disabled or configured allowlists are absent.
    """
    _validate_approval(plan, approval)
    if not config.DWH_SQL_ACCEPTANCE_ENABLED:
        raise RuntimeError("DWH_SQL_ACCEPTANCE_ENABLED must be true")
    if config.DWH_SQL_MODE != "http":
        raise RuntimeError("B8 discovery requires DWH_SQL_MODE=http")
    if not getattr(provider, "acceptance_read_only", False):
        raise PermissionError("A dedicated acceptance Oracle pool is required")
    if (
        not config.DWH_SQL_ACCEPTANCE_USER
        or config.DWH_SQL_ACCEPTANCE_USER.upper() != plan.expected_user.upper()
    ):
        raise PermissionError("Configured acceptance user does not match BI_DWH scope")
    if not config.DWH_SQL_ALLOWED_SCHEMAS or not config.DWH_SQL_ALLOWED_OBJECTS:
        raise RuntimeError(
            "Configured DWH SQL schema and object allowlists are required"
        )
    if set(item.upper() for item in plan.allowed_schemas) - set(
        item.upper() for item in config.DWH_SQL_ALLOWED_SCHEMAS
    ):
        raise PermissionError("Discovery scope exceeds the configured schema allowlist")
    if set(item.upper() for item in plan.allowed_objects) - set(
        item.upper() for item in config.DWH_SQL_ALLOWED_OBJECTS
    ):
        raise PermissionError("Discovery scope exceeds the configured object allowlist")

    metadata_objects = (
        "DUAL",
        "SESSION_PRIVS",
        "SESSION_ROLES",
        "ALL_TAB_PRIVS",
        "ALL_COL_PRIVS",
        "ALL_OBJECTS",
        "ALL_SYNONYMS",
        "ALL_TAB_COLUMNS",
        "V$PARAMETER",
    )
    policy_objects = metadata_objects + plan.allowed_objects
    started = time.monotonic()
    budget = AcceptanceStatementBudget(plan.limits.max_statements)
    report: dict[str, Any] = {
        "stage": "B8-stage-1-read-only-schema-discovery-v1",
        "scope_sha256": plan.scope_hash(),
        "schema_owner": plan.schema_owner,
        "expected_user": plan.expected_user,
        "status": "BLOCK",
        "compatibility_status": "BLOCK",
        "objects": {},
        "missing_objects": [],
        "missing_relevant_columns": {},
        "mappings": {},
        "requires_dba_confirmation": [],
        "security_warnings": [],
        "statements_used": 0,
    }

    def execute(index: int, row_limit: int) -> list[tuple[Any, ...]]:
        """Validate and execute one bounded SELECT from the approved manifest."""
        remaining = plan.limits.overall_timeout_seconds - (time.monotonic() - started)
        if remaining <= 0:
            raise TimeoutError("Stage 1 overall deadline exceeded")
        statement = DISCOVERY_SQL[index]
        statement_binds = plan.binds_for_statement(index)
        validate_oracle_read(
            statement,
            params=statement_binds,
            allowed_schemas=plan.allowed_schemas,
            allowed_objects=policy_objects,
        )
        with provider() as connection:
            cursor = connection.cursor()
            cursor.call_timeout = int(
                min(plan.limits.statement_timeout_seconds, remaining) * 1000
            )
            budget.reserve()
            cursor.execute(statement, statement_binds)
            rows = list(cursor.fetchmany(row_limit + 1))
        if len(rows) > row_limit:
            raise RuntimeError("Stage 1 metadata result exceeded its row cap")
        return rows

    try:
        user_rows = execute(0, 1)
        actual_user = str(user_rows[0][0]).upper() if user_rows else ""
        report["actual_user"] = actual_user
        if actual_user != plan.expected_user.upper():
            report["blockers"] = ["Connected Oracle user did not match BI_DWH"]
            return _finish_discovery_report(report, budget, started)

        privileges = sorted(
            str(row[0]).upper() for row in execute(1, plan.limits.max_privilege_rows)
        )
        unexpected_system_privileges = sorted(set(privileges) - {"CREATE SESSION"})
        roles = sorted(str(row[0]).upper() for row in execute(2, 100))
        report["system_privileges"] = privileges
        report["enabled_roles"] = roles
        if unexpected_system_privileges:
            report["security_warnings"].append(
                {
                    "type": "elevated_system_privileges",
                    "privileges": unexpected_system_privileges,
                }
            )
        report["credential_system_privilege_status"] = (
            "WARNING"
            if unexpected_system_privileges
            else "NO_ELEVATED_PRIVILEGES_OBSERVED"
        )

        object_grants = execute(3, plan.limits.max_privilege_rows)
        column_grants = execute(4, plan.limits.max_privilege_rows)
        grant_rows = object_grants + column_grants
        write_grants = [
            row for row in grant_rows if str(row[-1]).upper() not in {"SELECT", "READ"}
        ]
        report["effective_scoped_grants"] = {
            "object_rows": [list(row) for row in object_grants],
            "column_rows": [list(row) for row in column_grants],
            "status": "WARNING" if write_grants else "NO_WRITE_GRANTS_OBSERVED",
        }
        if write_grants:
            report["security_warnings"].append(
                {
                    "type": "scoped_write_capable_grants",
                    "grant_count": len(write_grants),
                }
            )

        object_rows = execute(5, plan.limits.max_object_rows)
        object_types = {str(row[1]).upper(): str(row[2]).upper() for row in object_rows}
        report["objects"] = {
            name: {
                "exists": name in object_types,
                "object_type": object_types.get(name),
            }
            for name in DISCOVERY_OBJECT_NAMES
        }
        report["missing_objects"] = sorted(
            set(DISCOVERY_OBJECT_NAMES) - set(object_types)
        )

        synonym_rows = execute(6, plan.limits.max_object_rows)
        report["synonyms"] = [list(row) for row in synonym_rows]

        column_rows = execute(7, plan.limits.max_column_rows)
        columns_by_table: dict[str, set[str]] = {}
        for row in column_rows:
            columns_by_table.setdefault(str(row[1]).upper(), set()).add(
                str(row[2]).upper()
            )
        relevant_columns = {
            "PIO_TRANSACTIONS": {
                "CUS_NUM",
                "CUSTOMER_ID",
                "CUSTOMER_NO",
                "CLIENT_ID",
                "CIF",
                "TRA_DATE",
                "TRA_DAY_DATE",
                "VALUE_DATE",
                "POST_DATE",
                "TRANSACTION_DATE",
                "TRA_AMT",
                "EQU_TRA_AMT",
                "AMOUNT",
                "EXPL_CODE",
            },
            "PIO_BANKBI_DICTIONARY_COL": {
                "TABLE_NAME",
                "COLUMN_NAME",
                "LOOKUP_TABLE_NAME",
            },
            "PIO_AML_COLUMNS": {
                "COLUMN_NAME",
                "LOOKUP_CODE_COLUMN",
                "LOOKUP_DESC_COLUMN",
                "LOOKUP_COUNTRY_COLUMN",
                "LOOKUP_INST_COLUMN",
            },
            "PIO_EXPLANATION_CODE": {
                "EXPLANATION_CODE",
                "DESC_ENG",
                "DESC_NAT_LAN",
                "LONG_DES_ENG",
                "COUNTRY_CODE",
                "INST_CODE",
            },
        }
        missing_columns = {
            table: sorted(required - columns_by_table.get(table, set()))
            for table, required in relevant_columns.items()
            if required - columns_by_table.get(table, set())
        }
        report["columns"] = [list(row) for row in column_rows]
        report["missing_relevant_columns"] = missing_columns

        try:
            resource_rows = execute(8, 1)
            active_plan = str(resource_rows[0][0]).strip() if resource_rows else ""
            report["resource_manager"] = {
                "status": "OBSERVED" if active_plan else "UNAVAILABLE",
                "active_plan": active_plan or None,
            }
            report["requires_dba_confirmation"].append(
                "Active plan presence does not verify DBA resource directives or limits"
            )
        except Exception as exc:
            report["resource_manager"] = {
                "status": "UNAVAILABLE",
                "error_type": type(exc).__name__,
                "error_code": _safe_oracle_error_code(exc),
            }
            report["requires_dba_confirmation"].append(
                "V$PARAMETER.resource_manager_plan was unavailable"
            )

        dictionary_required = {
            "PIO_BANKBI_DICTIONARY_COL": {
                "TABLE_NAME",
                "COLUMN_NAME",
                "LOOKUP_TABLE_NAME",
            },
            "PIO_AML_COLUMNS": {
                "COLUMN_NAME",
                "LOOKUP_CODE_COLUMN",
                "LOOKUP_DESC_COLUMN",
                "LOOKUP_COUNTRY_COLUMN",
                "LOOKUP_INST_COLUMN",
            },
        }
        dictionary_ready = all(
            table in object_types and not required - columns_by_table.get(table, set())
            for table, required in dictionary_required.items()
        )
        if dictionary_ready:
            try:
                dictionary_rows = execute(9, plan.limits.max_mapping_rows)
                report["mappings"]["expl_code_dictionary"] = {
                    "status": "OBSERVED",
                    "preliminary": True,
                    "rows": [list(row) for row in dictionary_rows],
                }
            except Exception as exc:
                report["mappings"]["expl_code_dictionary"] = {
                    "status": "QUERY_FAILED",
                    "error_type": type(exc).__name__,
                    "preliminary": True,
                }
        else:
            report["mappings"]["expl_code_dictionary"] = {
                "status": "SKIPPED_MISSING_DEPENDENCY",
                "preliminary": True,
            }

        explanation_required = {
            "EXPLANATION_CODE",
            "DESC_ENG",
            "DESC_NAT_LAN",
            "LONG_DES_ENG",
            "COUNTRY_CODE",
            "INST_CODE",
        }
        if (
            "PIO_EXPLANATION_CODE" in object_types
            and not explanation_required
            - columns_by_table.get("PIO_EXPLANATION_CODE", set())
        ):
            try:
                explanation_rows = execute(10, plan.limits.max_mapping_rows)
                report["mappings"]["explanation_code_4"] = {
                    "status": "OBSERVED",
                    "rows": [list(row) for row in explanation_rows],
                }
            except Exception as exc:
                report["mappings"]["explanation_code_4"] = {
                    "status": "QUERY_FAILED",
                    "error_type": type(exc).__name__,
                }
        else:
            report["mappings"]["explanation_code_4"] = {
                "status": "SKIPPED_MISSING_DEPENDENCY"
            }
        mapping_errors = [
            name
            for name, mapping in report["mappings"].items()
            if mapping["status"] == "QUERY_FAILED"
        ]
        report["status"] = "BLOCK" if mapping_errors else "PASS"
        if mapping_errors:
            report["blockers"] = [
                "Approved conditional mapping SQL failed: " + ", ".join(mapping_errors)
            ]
        compatibility = assess_stage1_compatibility(
            objects=report["objects"],
            columns=report["columns"],
            mappings=report["mappings"],
        )
        report["compatibility_assessment"] = compatibility
        report["compatibility_status"] = compatibility["status"]
        return _finish_discovery_report(report, budget, started)
    except Exception as exc:
        report["blockers"] = [f"Discovery stopped: {type(exc).__name__}"]
        report["error_code"] = _safe_oracle_error_code(exc)
        return _finish_discovery_report(report, budget, started)


def _finish_discovery_report(
    report: dict[str, Any], budget: AcceptanceStatementBudget, started: float
) -> dict[str, Any]:
    """Attach bounded execution counts and elapsed time to a Stage 1 report."""
    report["statements_used"] = budget.used
    report["elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
    return report


def assess_stage1_compatibility(
    *,
    objects: Mapping[str, Mapping[str, Any]],
    columns: Sequence[Sequence[Any]],
    mappings: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Assess discovered schema against the SQL generator's semantic contract.

    Args:
        objects: Stage 1 object report keyed by unqualified object name.
        columns: ALL_TAB_COLUMNS rows as owner, table, column, type, length, nullable.
        mappings: Conditional Stage 1 mapping query reports.

    Returns:
        Required semantic resolutions, optional amount candidates, and status.
        Alternate column names that are absent are not treated as missing fields.
    """
    types_by_table: dict[str, dict[str, str]] = {}
    for row in columns:
        if len(row) < 4:
            continue
        owner, table, column, data_type = (str(value).upper() for value in row[:4])
        if owner == "BI_DWH":
            types_by_table.setdefault(table, {})[column] = data_type

    fields = types_by_table.get("PIO_TRANSACTIONS", {})
    character_types = {"CHAR", "NCHAR", "VARCHAR", "VARCHAR2", "NVARCHAR2"}
    numeric_types = {
        "NUMBER",
        "FLOAT",
        "BINARY_FLOAT",
        "BINARY_DOUBLE",
        "INTEGER",
        "DECIMAL",
    }

    def compatible(name: str, accepted: set[str], *, prefix: str = "") -> bool:
        value = fields.get(name, "")
        return value in accepted or bool(prefix and value.startswith(prefix))

    required: dict[str, dict[str, Any]] = {}
    customer_type = fields.get("CUS_NUM", "")
    required["customer_projection"] = {
        "status": (
            "PASS"
            if "CUS_NUM" in fields
            and (customer_type in character_types | numeric_types)
            else "BLOCK"
        ),
        "object": "BI_DWH.PIO_TRANSACTIONS",
        "column": "CUS_NUM",
        "data_type": customer_type or None,
        "contract": "The generator and Service A handoff require the CUS_NUM projection.",
    }

    date_candidates = (
        "TRA_DATE",
        "TRA_DAY_DATE",
        "VALUE_DATE",
        "POST_DATE",
        "TRANSACTION_DATE",
    )
    selected_date = next(
        (
            name
            for name in date_candidates
            if re.search(r"(?:_DATE$|^DATE_)", name)
            and compatible(name, {"DATE"}, prefix="TIMESTAMP")
        ),
        None,
    )
    required["rolling_window_date"] = {
        "status": "PASS" if selected_date else "BLOCK",
        "object": "BI_DWH.PIO_TRANSACTIONS",
        "column": selected_date,
        "data_type": fields.get(selected_date) if selected_date else None,
        "contract": "The rolling-window validator accepts date-named columns; Oracle type must be DATE or TIMESTAMP.",
    }

    code_type = fields.get("EXPL_CODE", "")
    required["confirmed_explanation_code_filter"] = {
        "status": (
            "PASS"
            if "EXPL_CODE" in fields and (code_type in character_types | numeric_types)
            else "BLOCK"
        ),
        "object": "BI_DWH.PIO_TRANSACTIONS",
        "column": "EXPL_CODE",
        "data_type": code_type or None,
        "contract": "Explicit transaction codes require an EXPL_CODE predicate and confirmed filter validation.",
    }

    amount_candidates = [
        {
            "column": name,
            "data_type": fields[name],
        }
        for name in ("TRA_AMT", "EQU_TRA_AMT", "AMOUNT")
        if name in fields and compatible(name, numeric_types)
    ]
    mapping_errors = sorted(
        name
        for name, evidence in mappings.items()
        if evidence.get("status") == "QUERY_FAILED"
    )
    missing_objects = sorted(
        name for name, evidence in objects.items() if not evidence.get("exists")
    )
    blockers = [
        f"{name} did not resolve to an actual column with a compatible type"
        for name, evidence in required.items()
        if evidence["status"] != "PASS"
    ]
    if missing_objects:
        blockers.append("Missing scoped objects: " + ", ".join(missing_objects))
    if mapping_errors:
        blockers.append(
            "Conditional metadata query failed: " + ", ".join(mapping_errors)
        )

    dictionary_mapping = mappings.get("expl_code_dictionary", {})
    dictionary_rows = dictionary_mapping.get("rows") or []
    dictionary_mapping_status = dictionary_mapping.get("status")
    if dictionary_mapping_status == "OBSERVED":
        dictionary_mapping_result = (
            "PRELIMINARY_MATCH" if dictionary_rows else "NO_MATCHING_DICTIONARY_ROW"
        )
    elif dictionary_mapping_status == "QUERY_FAILED":
        dictionary_mapping_result = "QUERY_FAILED"
    else:
        dictionary_mapping_result = "NOT_CHECKED"
    return {
        "status": "BLOCK" if blockers else "PASS",
        "required_fields": required,
        "amount_candidates": amount_candidates,
        "amount_requirement": "Scenario-dependent; numeric target must be selected from confirmed intent/filter semantics.",
        "missing_objects": missing_objects,
        "failed_mapping_queries": mapping_errors,
        "expl_code_dictionary_mapping": dictionary_mapping_result,
        "blockers": blockers,
    }


def _safe_oracle_error_code(exc: Exception) -> str | None:
    """Extract only a vendor error code, excluding messages and connection data."""
    for item in exc.args:
        code = getattr(item, "code", None)
        if isinstance(code, int) and code > 0:
            return f"ORA-{code:05d}"
        match = re.search(r"\b(?:ORA|DPY|DPI)-\d{4,5}\b", str(item).upper())
        if match:
            return match.group(0)
    return None


async def run_live_validation(
    plan: AcceptancePlan,
    approval: LiveValidationApproval,
    *,
    expected_user: str,
    provider: Callable[[], ContextManager[Any]],
    http_generator: ScenarioSqlGenerator,
    local_generator_factory: Callable[
        [AcceptanceOracleRepository], ScenarioSqlGenerator
    ],
    config: Settings = settings,
) -> dict[str, Any]:
    """Run a specifically approved, bounded HTTP/local SQL acceptance suite.

    No live work occurs without a matching approval digest. The local factory
    receives the dedicated acceptance repository; it must not use A's primary
    or legacy shadow Oracle pool.
    """
    _validate_approval(plan, approval)
    if not config.DWH_SQL_ACCEPTANCE_ENABLED:
        raise RuntimeError("Dedicated Oracle acceptance mode is disabled")
    if config.DWH_SQL_MODE != "http":
        raise RuntimeError("B8 acceptance requires DWH_SQL_MODE=http to remain active")
    if config.PIOTECH_AI_URL.rstrip("/") != plan.http_endpoint.rstrip("/"):
        raise PermissionError(
            "HTTP endpoint does not match the approved acceptance scope"
        )
    if not getattr(provider, "acceptance_read_only", False):
        raise PermissionError("A dedicated acceptance Oracle pool is required")
    if (
        not config.DWH_SQL_ACCEPTANCE_USER
        or expected_user.upper() != config.DWH_SQL_ACCEPTANCE_USER.upper()
    ):
        raise PermissionError(
            "Expected identity must match the dedicated acceptance user"
        )
    if not config.DWH_SQL_ACCEPTANCE_RESOURCE_PLAN:
        raise RuntimeError("Configured DBA Resource Manager plan is required")
    if (
        config.DWH_SQL_ACCEPTANCE_RESOURCE_PLAN.casefold()
        != plan.expected_resource_plan.casefold()
    ):
        raise PermissionError(
            "Approved plan does not match configured Resource Manager plan"
        )
    if not config.DWH_SQL_ALLOWED_SCHEMAS or not config.DWH_SQL_ALLOWED_OBJECTS:
        raise RuntimeError(
            "Configured DWH SQL schema and object allowlists are required"
        )
    if {item.upper() for item in plan.allowed_schemas} - {
        item.upper() for item in config.DWH_SQL_ALLOWED_SCHEMAS
    }:
        raise PermissionError("Acceptance scope exceeds configured schema allowlist")
    if {item.upper() for item in plan.allowed_objects} - {
        item.upper() for item in config.DWH_SQL_ALLOWED_OBJECTS
    }:
        raise PermissionError("Acceptance scope exceeds configured object allowlist")

    started = time.monotonic()
    statement_budget = AcceptanceStatementBudget(plan.limits.max_statements)
    report: dict[str, Any] = {
        "run_id": hashlib.sha256(
            f"{approval.scope_sha256}:{approval.approved_at}".encode()
        ).hexdigest()[:24],
        "scope_sha256": approval.scope_sha256,
        "approved_by": approval.approved_by,
        "approved_at": approval.approved_at,
        "provider_data_approval_ref": approval.provider_data_approval_ref,
        "generator_routes": plan.generator_routes,
        "mode_before": config.DWH_SQL_MODE,
        "status": "BLOCK",
        "cases": [],
    }
    preflight = run_preflight(
        provider,
        plan,
        expected_user=expected_user,
        statement_timeout_seconds=plan.limits.statement_timeout_seconds,
        statement_budget=statement_budget,
    )
    report["preflight"] = preflight
    if preflight["status"] != "PASS":
        report["blockers"] = [
            "Read-only identity, grants, dictionary, or resource-plan preflight failed"
        ]
        report["elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
        return report

    repository = AcceptanceOracleRepository(
        connection_provider=provider,
        plan=plan,
        statement_budget=statement_budget,
        run_started_at=started,
    )
    for scenario in plan.scenarios:
        if time.monotonic() - started > plan.limits.overall_timeout_seconds:
            report["blockers"] = ["Overall acceptance deadline exceeded"]
            break
        remaining = plan.limits.overall_timeout_seconds - (time.monotonic() - started)
        if remaining <= 0:
            report["blockers"] = ["Overall acceptance deadline exceeded"]
            break
        http_result, http_elapsed = await _timed_generate(
            http_generator,
            scenario.request,
            min(plan.limits.generator_timeout_seconds, remaining),
        )
        http_evidence = _validate_and_execute(
            repository, scenario.request, http_result, plan, preflight
        )
        local_repo = AcceptanceOracleRepository(
            connection_provider=provider,
            plan=plan,
            statement_budget=statement_budget,
            run_started_at=started,
        )
        try:
            local_generator = local_generator_factory(local_repo)
        except Exception as exc:
            report["blockers"] = [
                f"Local generator initialization failed: {type(exc).__name__}"
            ]
            break
        if getattr(local_generator, "_repository", None) is not local_repo:
            report["blockers"] = [
                "Local generator did not use the dedicated acceptance repository"
            ]
            break
        remaining = plan.limits.overall_timeout_seconds - (time.monotonic() - started)
        if remaining <= 0:
            report["blockers"] = ["Overall acceptance deadline exceeded"]
            break
        local_result, local_elapsed = await _timed_generate(
            local_generator,
            scenario.request,
            min(plan.limits.generator_timeout_seconds, remaining),
        )
        local_evidence = local_repo.captured(local_result.sql or "")
        if local_evidence is None:
            local_evidence = _validate_and_execute(
                local_repo, scenario.request, local_result, plan, preflight
            )
        shape_budget = OracleRequestBudget(
            max_statements=2,
            deadline_seconds=max(0.001, plan.limits.overall_timeout_seconds),
        )
        http_shape = repository.probe_result_shape(
            http_result.sql or "", budget=shape_budget
        ) if http_result.approved and http_result.sql else None
        local_shape = local_repo.probe_result_shape(
            local_result.sql or "", budget=shape_budget
        ) if local_result.approved and local_result.sql else None
        case_report = _compare_case(
            scenario,
            http_result,
            http_evidence,
            http_elapsed,
            local_result,
            local_evidence,
            local_elapsed,
            plan,
            http_shape=http_shape,
            local_shape=local_shape,
        )
        report["cases"].append(case_report)
    report["statements_used"] = statement_budget.used
    report["elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
    report["status"] = (
        "PASS"
        if len(report["cases"]) == len(plan.scenarios)
        and all(item["status"] == "PASS" for item in report["cases"])
        and report["elapsed_ms"] <= plan.limits.overall_timeout_seconds * 1000
        else "BLOCK"
    )
    return report


def _validate_and_execute(
    repository: AcceptanceOracleRepository,
    request: ScenarioSqlRequest,
    result: ScenarioSqlResult,
    plan: AcceptancePlan,
    preflight: dict[str, Any],
) -> CandidateQueryResult:
    """Validate generator approval, filters and temporal contract before read."""
    sql = result.sql
    if not result.approved or not sql:
        return _failed_result(sql or "", "Generator did not approve a SQL candidate")
    findings = validate_generated_sql(request, sql)
    if findings:
        return _failed_result(sql, "Generated SQL violates AML temporal/code semantics")
    column_issue = _check_actual_dictionary_columns(sql, plan, preflight)
    if column_issue:
        return _failed_result(sql, column_issue)
    return repository.execute_candidate(
        sql,
        budget=OracleRequestBudget(
            max_statements=plan.limits.max_statements,
            deadline_seconds=plan.limits.overall_timeout_seconds,
        ),
        required_projection="CUS_NUM",
        confirmed_filters=[
            item.model_dump(mode="json") for item in request.confirmed_filters
        ],
    )


def _check_actual_dictionary_columns(
    sql: str,
    plan: AcceptancePlan,
    preflight: dict[str, Any],
) -> str | None:
    """Require fully qualified live objects and dictionary-confirmed columns."""
    try:
        statements = sqlglot.parse(sql, read="oracle")
    except (sqlglot.errors.ParseError, ValueError):
        return "Candidate SQL could not be parsed for dictionary validation"
    if len(statements) != 1 or statements[0] is None:
        return "Candidate SQL must contain exactly one statement"
    root = statements[0]
    actual = (
        preflight.get("checks", {})
        .get("dictionary_and_object_grants", {})
        .get("objects", {})
    )
    allowed = {name.upper() for name in plan.allowed_objects}
    cte_outputs = {
        cte.alias_or_name.upper(): {
            projection.alias_or_name.upper()
            for projection in cte.this.selects
            if projection.alias_or_name
        }
        for cte in root.find_all(exp.CTE)
        if cte.alias_or_name and isinstance(cte.this, exp.Select)
    }
    referenced: set[str] = set()
    source_objects: dict[str, str] = {}
    for table in root.find_all(exp.Table):
        if table.name.upper() in cte_outputs:
            continue
        if not table.db:
            return "Acceptance SQL must schema-qualify every object"
        qualified = f"{table.db}.{table.name}".upper()
        referenced.add(qualified)
        source_objects[table.alias_or_name.upper()] = qualified
        source_objects[table.name.upper()] = qualified
    if not referenced or not referenced <= allowed:
        return "Candidate SQL references an object outside the approved scope"
    columns_by_object = {
        name: {
            str(column).upper() for column in actual.get(name, {}).get("columns", [])
        }
        for name in referenced
    }
    cte_names = set(cte_outputs)
    projection_aliases = {
        projection.alias_or_name.upper()
        for select in root.find_all(exp.Select)
        for projection in select.expressions
        if isinstance(projection, exp.Alias) and projection.alias_or_name
    }
    for column in root.find_all(exp.Column):
        column_name = column.name.upper()
        if column_name == "ROWNUM" or column_name in projection_aliases:
            continue
        qualifier = column.table.upper()
        if qualifier in cte_names:
            if column_name not in cte_outputs[qualifier]:
                return (
                    "Candidate SQL references a column absent from its CTE projection"
                )
            continue
        if qualifier:
            qualified_object = source_objects.get(qualifier)
            if (
                qualified_object is None
                or column_name not in columns_by_object[qualified_object]
            ):
                return "Candidate SQL references a column absent from its live object"
            continue
        if column_name in set().union(*cte_outputs.values()):
            continue
        matches = sum(column_name in names for names in columns_by_object.values())
        if matches != 1:
            return (
                "Candidate SQL has an absent or ambiguous unqualified dictionary column"
            )
    return None


def _compare_case(
    scenario: AcceptanceScenario,
    http_result: ScenarioSqlResult,
    http_evidence: CandidateQueryResult,
    http_elapsed: float,
    local_result: ScenarioSqlResult,
    local_evidence: CandidateQueryResult,
    local_elapsed: float,
    plan: AcceptancePlan,
    *,
    http_shape: tuple[str, ...] | None = None,
    local_shape: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Compare actual bounded customer sets and evidence without row values."""
    http_customers = _customer_set(http_evidence)
    local_customers = _customer_set(local_evidence)
    truncated = http_evidence.truncated or local_evidence.truncated
    discrepancies = []
    if not http_evidence.success or not local_evidence.success:
        discrepancies.append("execution_failed")
    if truncated:
        discrepancies.append("result_truncated_customer_set_incomplete")
    if http_customers != local_customers:
        discrepancies.append("customer_set_mismatch")
    if http_shape is None or local_shape is None:
        discrepancies.append("result_shape_probe_missing")
    elif http_shape != local_shape:
        discrepancies.append("result_shape_mismatch")
    if not http_result.approved or not local_result.approved:
        discrepancies.append("generator_approval_missing")
    return {
        "case_id": scenario.case_id,
        "status": "PASS" if not discrepancies else "BLOCK",
        "discrepancies": discrepancies,
        "http": _result_summary(
            http_result,
            http_evidence,
            http_elapsed,
            scenario.request,
            plan.limits.max_rows_per_query + 1,
        ),
        "local": _result_summary(
            local_result,
            local_evidence,
            local_elapsed,
            scenario.request,
            plan.limits.max_rows_per_query + 1,
        ),
        "customer_set_equal": http_customers == local_customers and not truncated,
        "result_shape_equal": (
            http_shape is not None and local_shape is not None and http_shape == local_shape
        ),
        "result_shape": {
            "http_columns": list(http_shape or ()),
            "local_columns": list(local_shape or ()),
        },
        "customer_set_counts": {
            "http_distinct": len(http_customers),
            "local_distinct": len(local_customers),
            "symmetric_difference": len(http_customers ^ local_customers),
        },
        "statements_used": 4,
    }


def _result_summary(
    result: ScenarioSqlResult,
    evidence: CandidateQueryResult,
    generator_elapsed_ms: float,
    request: ScenarioSqlRequest,
    result_cap: int,
) -> dict[str, Any]:
    """Return auditable, redacted SQL and execution metadata for a candidate."""
    sql = result.sql or ""
    try:
        bound_sql, _, bind_audit = bind_sql_literals(sql)
        executed_sql = _bounded_customer_sql(bound_sql, "CUS_NUM", "b8_result_cap")
    except ValueError:
        bound_sql, executed_sql, bind_audit = sql, sql, []
    findings = validate_generated_sql(request, sql) if sql else ["missing_sql"]
    bind_audit.append(
        {
            "name": "b8_result_cap",
            "value": result_cap,
            "columns": [],
            "sensitive": False,
        }
    )
    return {
        "approved": result.approved,
        "sql_sha256": _sha256(bound_sql),
        "executed_sql_sha256": _sha256(executed_sql),
        "generator_execution": {
            "row_count": result.execution.row_count,
            "truncated": result.execution.truncated,
            "error_present": bool(result.execution.error),
        },
        "execution_success": evidence.success,
        "row_count": evidence.row_count,
        "truncated": evidence.truncated,
        "execution_ms": evidence.elapsed_ms,
        "generator_ms": round(generator_elapsed_ms, 3),
        "confirmed_filter_semantics": "PASS" if evidence.success else "BLOCK",
        "temporal_semantics": (
            "PASS"
            if not any(item.startswith(("Rolling ", "Baseline ")) for item in findings)
            else "BLOCK"
        ),
        "bind_values": [
            {
                "name": item["name"],
                # Bind values can encode customer, institution, or strategy data
                # regardless of the referenced column name. Default to redacted.
                "value": "<redacted>",
                "columns": item["columns"],
            }
            for item in bind_audit
        ],
        "error": evidence.error,
    }


async def _timed_generate(
    generator: ScenarioSqlGenerator,
    request: ScenarioSqlRequest,
    timeout_seconds: float,
) -> tuple[ScenarioSqlResult, float]:
    """Run one generator inside the caller's bounded timeout."""
    import asyncio

    started = time.monotonic()
    try:
        result = await asyncio.wait_for(
            generator.generate(request), timeout=timeout_seconds
        )
    except asyncio.TimeoutError:
        result = ScenarioSqlResult(error="Generator timeout")
    except Exception as exc:
        result = ScenarioSqlResult(error=f"Generator failed: {type(exc).__name__}")
    return result, (time.monotonic() - started) * 1000


def _validate_approval(
    plan: AcceptancePlan | SchemaDiscoveryPlan, approval: LiveValidationApproval
) -> None:
    """Require approver, provider-data decision and exact plan digest to match."""
    plan.validate()
    if (
        approval.scope_sha256 != plan.scope_hash()
        or not approval.approved_by.strip()
        or approval.provider_data_approval_ref != plan.provider_data_approval_ref
    ):
        raise PermissionError("Approval does not match the exact acceptance scope")


def _customer_set(evidence: CandidateQueryResult) -> set[str]:
    """Read CUS_NUM values into a private in-memory set for exact comparison."""
    index = next(
        (
            position
            for position, name in enumerate(evidence.columns)
            if name.upper() == "CUS_NUM"
        ),
        None,
    )
    if not evidence.success or index is None:
        return set()
    return {str(row[index]) for row in evidence.rows if row[index] is not None}


def _bounded_customer_sql(sql: str, projection: str, bind_name: str) -> str:
    """Deduplicate customer identifiers in Oracle, then enforce a row cap."""
    return (
        f"SELECT {projection} FROM (SELECT DISTINCT {projection} FROM ({sql}) "
        f"B8_SOURCE) B8_CUSTOMERS WHERE ROWNUM <= :{bind_name}"
    )


def _failed_result(sql: str, message: str) -> CandidateQueryResult:
    """Construct sanitized failure evidence without including SQL or values."""
    return CandidateQueryResult(
        sql=sql,
        success=False,
        elapsed_ms=0,
        error=message[:180],
    )


def _numeric_value(value: str) -> Any:
    """Convert numeric SQL literals to int/float Oracle bind values."""
    try:
        integer = int(value)
        if str(integer) == value:
            return integer
        return float(value)
    except ValueError:
        return value


def _sha256(value: str) -> str:
    """Return a lowercase SHA-256 digest for an SQL statement."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json_safe(value: Any) -> Any:
    """Render primitive bind values safely for a returned audit report."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def serialize_acceptance_report(report: dict[str, Any]) -> str:
    """Serialize a redacted acceptance report for an owner-approved audit file."""
    return json.dumps(report, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
