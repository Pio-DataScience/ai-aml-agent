"""Oracle registry-v2 repository with create-only schema initialization."""

from __future__ import annotations

import json
from typing import Any

from services.aml_builder.web.services.oracle import atomic_connection, get_connection, run_readonly, run_write
from services.aml_builder.web.services.settings import settings

REGISTRY_COLUMNS = frozenset(
    {
        "SCENARIO_ID", "SCENARIO_KEY", "VERSION_NO", "PARENT_SCENARIO_ID",
        "STATUS", "IS_ACTIVE", "THREAD_ID", "REVISION_ID", "REV_NO",
        "SIGNATURE_HASH", "PARAMS_HASH", "PLAN_HASH", "SQL_HASH", "METADATA_HASH",
        "SIGNATURE_JSON", "PARAMS_JSON", "FILTERS_JSON", "METADATA_JSON", "RECEIPT_JSON",
        "SCENARIO_NAME", "SCENARIO_TYPE", "DETECTION_LOGIC", "RAW_SQL",
        "TIME_WINDOW_DAYS", "CHANGE_REASON", "CREATED_BY", "CREATED_AT", "UPDATED_BY", "UPDATED_AT",
    }
)

PRODUCTION_TABLE = "PIO_AML_AGENT_PRODUCTION_SCENARIOS"
METADATA_TABLE = "PIO_AML_AGENT_SCENARIOS"

SCHEMA_DDLS = {
    "PIO_AML_AGENT_PRODUCTION_SCENARIOS": """
        CREATE TABLE PIO_AML_AGENT_PRODUCTION_SCENARIOS (
            SCENARIO_ID VARCHAR2(50) PRIMARY KEY,
            SCENARIO_KEY VARCHAR2(50) NOT NULL,
            VERSION_NO NUMBER NOT NULL,
            PARENT_SCENARIO_ID VARCHAR2(50),
            STATUS VARCHAR2(20) NOT NULL,
            IS_ACTIVE NUMBER(1) NOT NULL,
            THREAD_ID VARCHAR2(200) NOT NULL,
            REVISION_ID VARCHAR2(64) NOT NULL,
            REV_NO NUMBER NOT NULL,
            SIGNATURE_HASH VARCHAR2(64),
            PARAMS_HASH VARCHAR2(64), PLAN_HASH VARCHAR2(64),
            SQL_HASH VARCHAR2(64), METADATA_HASH VARCHAR2(64),
            SIGNATURE_JSON CLOB, PARAMS_JSON CLOB, FILTERS_JSON CLOB,
            METADATA_JSON CLOB, RECEIPT_JSON CLOB,
            CHANGE_REASON VARCHAR2(500),
            SCENARIO_NAME VARCHAR2(250) NOT NULL,
            SCENARIO_TYPE VARCHAR2(50) NOT NULL,
            DETECTION_LOGIC CLOB, RAW_SQL CLOB NOT NULL,
            TIME_WINDOW_DAYS NUMBER,
            CREATED_BY VARCHAR2(100), CREATED_AT TIMESTAMP DEFAULT SYSTIMESTAMP,
            UPDATED_BY VARCHAR2(100), UPDATED_AT TIMESTAMP
        )""",
    "PIO_AML_AGENT_SCENARIOS": """
        CREATE TABLE PIO_AML_AGENT_SCENARIOS (
            COUNTRY_CODE NUMBER NOT NULL, INST_CODE NUMBER NOT NULL,
            SCENARIO_CODE VARCHAR2(40) NOT NULL,
            SCENARIO_DES_ENG VARCHAR2(400), SCENARIO_DES_NAT_LAN VARCHAR2(400),
            ACTIVE_FLAG VARCHAR2(1) DEFAULT '1', RUN_FLAG VARCHAR2(1) DEFAULT '1',
            VIOLATION_LEVEL VARCHAR2(1), DEGREE_RISK_FLAG VARCHAR2(1),
            CREATED_BY NUMBER, CREATED_DATE DATE DEFAULT SYSDATE,
            UPDATED_BY NUMBER, UPDATED_DATE DATE,
            CATEGORY_CODE VARCHAR2(10), SCE_TYPE_CODE VARCHAR2(10),
            CLASS_CODE VARCHAR2(10),
            CONSTRAINT PIO_AML_AGENT_SCENARIOS_PK
                PRIMARY KEY (COUNTRY_CODE, INST_CODE, SCENARIO_CODE)
        )""",
    "PIO_AML_AGENT_SCENARIO_AUDIT": """
        CREATE TABLE PIO_AML_AGENT_SCENARIO_AUDIT (
            AUDIT_ID VARCHAR2(64) PRIMARY KEY,
            SCENARIO_ID VARCHAR2(50) NOT NULL,
            SCENARIO_KEY VARCHAR2(50) NOT NULL,
            ACTION VARCHAR2(30) NOT NULL,
            ACTOR VARCHAR2(100), ACTOR_ROLE VARCHAR2(30), TS TIMESTAMP DEFAULT SYSTIMESTAMP,
            FROM_STATUS VARCHAR2(20), TO_STATUS VARCHAR2(20), REASON VARCHAR2(500),
            DIFF_JSON CLOB
        )""",
    "PIO_AML_AGENT_SCHEMA_VERSION": """
        CREATE TABLE PIO_AML_AGENT_SCHEMA_VERSION (
            COMPONENT VARCHAR2(100) PRIMARY KEY,
            VERSION NUMBER NOT NULL,
            APPLIED_AT TIMESTAMP DEFAULT SYSTIMESTAMP
        )""",
}

SCHEMA_INDEXES = {
    "PIO_AML_AGENT_PROD_UQ_KEY_VERSION": "CREATE UNIQUE INDEX PIO_AML_AGENT_PROD_UQ_KEY_VERSION ON PIO_AML_AGENT_PRODUCTION_SCENARIOS (SCENARIO_KEY, VERSION_NO)",
    "PIO_AML_AGENT_PROD_UQ_THREAD": "CREATE UNIQUE INDEX PIO_AML_AGENT_PROD_UQ_THREAD ON PIO_AML_AGENT_PRODUCTION_SCENARIOS (THREAD_ID)",
    "PIO_AML_AGENT_PROD_UQ_ACTIVE_SIG": "CREATE UNIQUE INDEX PIO_AML_AGENT_PROD_UQ_ACTIVE_SIG ON PIO_AML_AGENT_PRODUCTION_SCENARIOS (CASE WHEN STATUS='ACTIVE' THEN SIGNATURE_HASH END)",
    "PIO_AML_AGENT_PROD_IX_KEY_STATUS": "CREATE INDEX PIO_AML_AGENT_PROD_IX_KEY_STATUS ON PIO_AML_AGENT_PRODUCTION_SCENARIOS (SCENARIO_KEY, STATUS)",
}


class RegistrySchemaError(RuntimeError):
    """Raised when an agent-owned registry table is incompatible with P4."""

    code = "REGISTRY_SCHEMA_INCOMPATIBLE"


class DuplicateActiveSignatureError(RuntimeError):
    """Raised when an active scenario already owns the signature hash."""

    code = "DUPLICATE_ACTIVE_SIGNATURE"


class RegistryStaleError(RuntimeError):
    """An exact-key registry status/version precondition no longer holds."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class OracleRegistryRepo:
    """Read and write registry-v2 rows through Oracle transactions."""

    def ensure_schema(self) -> None:
        """Create missing v2 tables/indexes and record the schema version.

        Existing objects are never altered. The legacy shared registry tables are
        left untouched; this application creates its own agent-prefixed tables.
        """
        owner = settings.ORACLE_USER.upper()
        tables = _names(
            run_readonly(
                "SELECT TABLE_NAME FROM ALL_TABLES WHERE OWNER=UPPER(:owner)",
                {"owner": owner},
            )[1]
        )
        if "pio_aml_agent_production_scenarios" in tables:
            columns = _names(
                run_readonly(
                    "SELECT COLUMN_NAME FROM ALL_TAB_COLUMNS WHERE OWNER=UPPER(:owner) AND TABLE_NAME='PIO_AML_AGENT_PRODUCTION_SCENARIOS'",
                    {"owner": owner},
                )[1]
            )
            required_columns = {column.lower() for column in REGISTRY_COLUMNS}
            if not required_columns.issubset(columns):
                missing = sorted(required_columns - columns)
                raise RegistrySchemaError(
                    "PIO_AML_AGENT_PRODUCTION_SCENARIOS has an incompatible schema; review its definition with the owner before deploying. Missing: "
                    + ", ".join(missing)
                )

        for table_name, ddl in SCHEMA_DDLS.items():
            if table_name.lower() not in tables:
                _execute_ddl(ddl)
        indexes = _names(
            run_readonly(
                "SELECT INDEX_NAME FROM ALL_INDEXES WHERE OWNER=UPPER(:owner)",
                {"owner": owner},
            )[1]
        )
        for index_name, ddl in SCHEMA_INDEXES.items():
            if index_name.lower() not in indexes:
                _execute_ddl(ddl)
        version = run_readonly(
            "SELECT VERSION FROM PIO_AML_AGENT_SCHEMA_VERSION WHERE COMPONENT=:component",
            {"component": "scenario_registry"},
        )[1]
        if not version:
            run_write(
                "INSERT INTO PIO_AML_AGENT_SCHEMA_VERSION (COMPONENT, VERSION, APPLIED_AT) VALUES (:component, :version, SYSTIMESTAMP)",
                {"component": "scenario_registry", "version": 2},
            )
        elif int(version[0][0]) != 2:
            raise RuntimeError(
                "REGISTRY_SCHEMA_VERSION: existing registry schema version is not 2; resolve it with the owner before deployment."
            )

    def get_by_thread(self, thread_id: str) -> dict[str, Any] | None:
        """Find an existing deployment by its unique chat identifier."""
        columns, rows = run_readonly(
            "SELECT SCENARIO_ID, SCENARIO_NAME, THREAD_ID, REVISION_ID, PARENT_SCENARIO_ID, RECEIPT_JSON FROM PIO_AML_AGENT_PRODUCTION_SCENARIOS WHERE THREAD_ID=:thread_id",
            {"thread_id": thread_id},
        )
        return _row_dict(columns, rows[0]) if rows else None

    def get_version(self, scenario_id: str) -> dict[str, Any] | None:
        """Return one exact version with all stored comparison and seed JSON."""
        columns, rows = run_readonly(
            """SELECT SCENARIO_ID,SCENARIO_KEY,VERSION_NO,PARENT_SCENARIO_ID,STATUS,IS_ACTIVE,
            THREAD_ID,REVISION_ID,REV_NO,SIGNATURE_HASH,PARAMS_HASH,PLAN_HASH,SQL_HASH,METADATA_HASH,
            SIGNATURE_JSON,PARAMS_JSON,FILTERS_JSON,METADATA_JSON,RECEIPT_JSON,CHANGE_REASON,
            SCENARIO_NAME,SCENARIO_TYPE,DETECTION_LOGIC,RAW_SQL,TIME_WINDOW_DAYS,CREATED_BY,CREATED_AT,
            UPDATED_BY,UPDATED_AT FROM PIO_AML_AGENT_PRODUCTION_SCENARIOS WHERE SCENARIO_ID=:scenario_id""",
            {"scenario_id": scenario_id},
        )
        return _row_dict(columns, rows[0]) if rows else None

    def list_versions(self, scenario_key: str) -> list[dict[str, Any]]:
        """Return every version for a stable key, newest first."""
        columns, rows = run_readonly(
            """SELECT SCENARIO_ID,SCENARIO_KEY,VERSION_NO,PARENT_SCENARIO_ID,STATUS,IS_ACTIVE,
            SIGNATURE_HASH,METADATA_HASH,SIGNATURE_JSON,PARAMS_JSON,FILTERS_JSON,METADATA_JSON,
            RECEIPT_JSON,CHANGE_REASON,SCENARIO_NAME,SCENARIO_TYPE,CREATED_BY,CREATED_AT,UPDATED_BY,UPDATED_AT
            FROM PIO_AML_AGENT_PRODUCTION_SCENARIOS WHERE SCENARIO_KEY=:scenario_key
            ORDER BY VERSION_NO DESC""",
            {"scenario_key": scenario_key},
        )
        return [_row_dict(columns, row) for row in rows]

    def latest_version(self, scenario_key: str) -> dict[str, Any] | None:
        """Return the most recently numbered version for a stable key."""
        rows = self.list_versions(scenario_key)
        return rows[0] if rows else None

    def latest_active_version(self, scenario_key: str) -> dict[str, Any] | None:
        """Return the active version for a key, if one exists."""
        columns, rows = run_readonly(
            """SELECT SCENARIO_ID,SCENARIO_KEY,VERSION_NO,STATUS,IS_ACTIVE,SIGNATURE_HASH,
            SIGNATURE_JSON,METADATA_JSON FROM PIO_AML_AGENT_PRODUCTION_SCENARIOS
            WHERE SCENARIO_KEY=:scenario_key AND STATUS='ACTIVE'""",
            {"scenario_key": scenario_key},
        )
        if len(rows) > 1:
            raise RegistrySchemaError("More than one active version exists for a scenario key.")
        return _row_dict(columns, rows[0]) if rows else None

    def list_version_audit(self, scenario_key: str) -> list[dict[str, Any]]:
        """Return creation and status events for all versions of a key."""
        columns, rows = run_readonly(
            """SELECT SCENARIO_ID,ACTION,ACTOR,ACTOR_ROLE,TS,FROM_STATUS,TO_STATUS,REASON
            FROM PIO_AML_AGENT_SCENARIO_AUDIT WHERE SCENARIO_KEY=:scenario_key ORDER BY TS DESC""",
            {"scenario_key": scenario_key},
        )
        return [_row_dict(columns, row) for row in rows]

    def get_active_by_key(self, scenario_key: str) -> list[dict[str, Any]]:
        """Return active versions for one stable scenario key."""
        columns, rows = run_readonly(
            "SELECT SCENARIO_ID, SCENARIO_KEY, VERSION_NO, SIGNATURE_HASH, SIGNATURE_JSON FROM PIO_AML_AGENT_PRODUCTION_SCENARIOS WHERE SCENARIO_KEY=:scenario_key AND STATUS='ACTIVE'",
            {"scenario_key": scenario_key},
        )
        return [_row_dict(columns, row) for row in rows]

    def list_active_signatures(self, scenario_type: str | None = None) -> list[dict[str, Any]]:
        """Return active signatures for conflict checks."""
        sql = "SELECT SCENARIO_ID, SCENARIO_KEY, VERSION_NO, SCENARIO_NAME, SCENARIO_TYPE, THREAD_ID, SIGNATURE_HASH, SIGNATURE_JSON FROM PIO_AML_AGENT_PRODUCTION_SCENARIOS WHERE STATUS='ACTIVE'"
        params: dict[str, Any] = {}
        if scenario_type:
            sql += " AND UPPER(SCENARIO_TYPE)=UPPER(:scenario_type)"
            params["scenario_type"] = scenario_type
        columns, rows = run_readonly(sql, params)
        return [_row_dict(columns, row) for row in rows]

    def insert_version(self, row: dict[str, Any]) -> dict[str, Any]:
        """Insert a version and audit it, superseding an optional parent atomically."""
        try:
            with atomic_connection() as connection:
                cursor = connection.cursor()
                if row.get("parent_scenario_id"):
                    cursor.execute(
                        "SELECT SCENARIO_KEY,VERSION_NO,STATUS FROM PIO_AML_AGENT_PRODUCTION_SCENARIOS WHERE SCENARIO_ID=:scenario_id",
                        {"scenario_id": row["parent_scenario_id"]},
                    )
                    parent = cursor.fetchone()
                    cursor.execute(
                        "SELECT MAX(VERSION_NO) FROM PIO_AML_AGENT_PRODUCTION_SCENARIOS WHERE SCENARIO_KEY=:scenario_key",
                        {"scenario_key": row["scenario_key"]},
                    )
                    latest = cursor.fetchone()
                    expected_parent_status = str(row.get("parent_status") or "ACTIVE").upper()
                    if (
                        parent is None
                        or str(parent[0]) != str(row["scenario_key"])
                        or int(parent[1]) != int(row["parent_version"])
                        or int((latest or [None])[0] or 0) != int(row["parent_version"])
                        or str(parent[2]).upper() != expected_parent_status
                        or expected_parent_status not in {"ACTIVE", "RETIRED"}
                    ):
                        raise RegistryStaleError("REVISE_STALE", "Start again from the latest version.")
                if row.get("parent_scenario_id") and str(row.get("parent_status") or "ACTIVE").upper() == "ACTIVE":
                    self.supersede(cursor, row)
                cursor.execute(
                    """INSERT INTO PIO_AML_AGENT_PRODUCTION_SCENARIOS
                    (SCENARIO_ID,SCENARIO_KEY,VERSION_NO,PARENT_SCENARIO_ID,STATUS,IS_ACTIVE,
                     THREAD_ID,REVISION_ID,REV_NO,SIGNATURE_HASH,PARAMS_HASH,PLAN_HASH,SQL_HASH,
                     METADATA_HASH,SIGNATURE_JSON,PARAMS_JSON,FILTERS_JSON,METADATA_JSON,
                     RECEIPT_JSON,CHANGE_REASON,SCENARIO_NAME,SCENARIO_TYPE,DETECTION_LOGIC,
                     RAW_SQL,TIME_WINDOW_DAYS,CREATED_BY,CREATED_AT)
                    VALUES (:scenario_id,:scenario_key,:version_no,:parent_scenario_id,:status,:is_active,
                     :thread_id,:revision_id,:rev_no,:signature_hash,:params_hash,:plan_hash,:sql_hash,
                     :metadata_hash,:signature_json,:params_json,:filters_json,:metadata_json,
                     :receipt_json,:change_reason,:scenario_name,:scenario_type,:detection_logic,
                     :raw_sql,:time_window_days,:created_by,SYSTIMESTAMP)""",
                    _bind_row(row),
                )
                self._insert_metadata(cursor, row)
                self._append_audit_in_transaction(cursor, row)
                for override in row.get("override_conflicts", []):
                    self._append_audit_in_transaction(
                        cursor,
                        {
                            "scenario_id": row["scenario_id"],
                            "scenario_key": row["scenario_key"],
                            "audit_action": "OVERRIDE_CONFLICT",
                            "actor_id": override.get("actor"),
                            "actor_role": override.get("role"),
                            "reason": override.get("reason"),
                            "audit_diff_json": {
                                "revision_id": override.get("revision_id"),
                                "other_scenario_id": override.get("other_scenario_id"),
                                "relation": override.get("relation"),
                                "acknowledged_at": override.get("acknowledged_at"),
                            },
                        },
                    )
            return row
        except RegistryStaleError as exc:
            if row.get("parent_scenario_id") and exc.code == "VERSION_STALE":
                raise RegistryStaleError("REVISE_STALE", "Start again from the latest version.") from exc
            raise
        except Exception as exc:
            if "ORA-00001" in str(exc).upper() or "UNIQUE CONSTRAINT" in str(exc).upper():
                raise DuplicateActiveSignatureError("DUPLICATE_ACTIVE_SIGNATURE") from exc
            raise

    def supersede(self, cursor: Any, row: dict[str, Any]) -> None:
        """Supersede a parent on the caller's version-insert transaction."""
        self._supersede_in_transaction(cursor, row)

    def set_status(
        self,
        cursor: Any,
        scenario_id: str,
        status: str,
        *,
        actor: str,
        actor_role: str,
        reason: str = "",
        country_code: Any = None,
        institution_code: Any = None,
        from_status: str | None = None,
        audit_action: str | None = None,
        audit_detail: dict[str, Any] | None = None,
    ) -> None:
        """Set one exact-key status on a caller-owned audited transaction."""
        transitions = {
            ("ACTIVE", "SUPERSEDED", "SUPERSEDE"),
            ("ACTIVE", "SUPERSEDED", "ROLLBACK"),
            ("SUPERSEDED", "ACTIVE", "ROLLBACK"),
            ("ACTIVE", "RETIRED", "RETIRE"),
        }
        if (from_status, status, audit_action) not in transitions:
            raise ValueError(f"Unsupported scenario status: {status}")
        active = int(status == "ACTIVE")
        expected_active = int(from_status == "ACTIVE")
        cursor.execute(
            "SELECT SCENARIO_KEY,METADATA_JSON FROM PIO_AML_AGENT_PRODUCTION_SCENARIOS WHERE SCENARIO_ID=:scenario_id",
            {"scenario_id": scenario_id},
        )
        identity_row = cursor.fetchone()
        if identity_row is None:
            raise RegistryStaleError("VERSION_STALE", "The scenario status changed. Refresh version history and try again.")
        scenario_key = str(identity_row[0])
        metadata_value = identity_row[1]
        if hasattr(metadata_value, "read"):
            metadata_value = metadata_value.read()
        try:
            metadata = json.loads(metadata_value) if isinstance(metadata_value, str) else metadata_value
        except json.JSONDecodeError:
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        country = country_code if country_code is not None else _value(metadata, "COUNTRY_CODE", settings.AML_COUNTRY_CODE)
        institution = institution_code if institution_code is not None else _value(metadata, "INST_CODE", settings.AML_INST_CODE)
        binds = {
            "status": status, "active": active, "actor": actor,
            "scenario_id": scenario_id, "from_status": from_status,
        }
        cursor.execute(
            "UPDATE PIO_AML_AGENT_PRODUCTION_SCENARIOS SET STATUS=:status, IS_ACTIVE=:active, UPDATED_BY=:actor, UPDATED_AT=SYSTIMESTAMP WHERE SCENARIO_ID=:scenario_id AND STATUS=:from_status",
            binds,
        )
        if cursor.rowcount != 1:
            raise RegistryStaleError("VERSION_STALE", "The scenario status changed. Refresh version history and try again.")
        cursor.execute(
            "UPDATE PIO_AML_AGENT_SCENARIOS SET ACTIVE_FLAG=:active, UPDATED_BY=:actor, UPDATED_DATE=SYSDATE WHERE COUNTRY_CODE=:country AND INST_CODE=:institution AND SCENARIO_CODE=:scenario_id AND ACTIVE_FLAG=:expected_active",
            {
                "active": str(active),
                "actor": _number(actor, settings.AML_CREATED_BY),
                "country": country,
                "institution": institution,
                "scenario_id": scenario_id,
                "expected_active": str(expected_active),
            },
        )
        if cursor.rowcount != 1:
            raise RegistryStaleError("VERSION_STALE", "The scenario metadata status changed. Refresh version history and try again.")
        self._append_audit_in_transaction(
            cursor,
            {"scenario_id": scenario_id, "scenario_key": scenario_key, "audit_action": audit_action or status, "actor_id": actor, "actor_role": actor_role, "reason": reason, "from_status": from_status, "to_status": status, "audit_diff_json": audit_detail or {}},
        )

    def set_statuses(
        self,
        changes: list[tuple[str, str, str]],
        *,
        actor: str,
        actor_role: str,
        reason: str,
        audit_action: str,
        audit_detail: dict[str, Any] | None = None,
    ) -> None:
        """Apply one or more exact-key transitions and audits atomically."""
        with atomic_connection() as connection:
            cursor = connection.cursor()
            for scenario_id, expected, status in changes:
                self.set_status(
                    cursor, scenario_id, status, actor=actor, actor_role=actor_role,
                    reason=reason, from_status=expected, audit_action=audit_action,
                    audit_detail=audit_detail,
                )

    def append_audit(self, row: dict[str, Any]) -> None:
        """Append one immutable audit record in its own transaction."""
        with atomic_connection() as connection:
            self._append_audit_in_transaction(connection.cursor(), row)

    @staticmethod
    def _supersede_in_transaction(cursor: Any, row: dict[str, Any]) -> None:
        parent_id = row["parent_scenario_id"]
        cursor.execute(
            "SELECT SCENARIO_KEY, STATUS FROM PIO_AML_AGENT_PRODUCTION_SCENARIOS WHERE SCENARIO_ID=:scenario_id",
            {"scenario_id": parent_id},
        )
        parent = cursor.fetchone()
        if parent is None or str(parent[0]) != str(row["scenario_key"]):
            raise LookupError("Parent scenario does not match the requested stable scenario key")
        if str(parent[1]).upper() != "ACTIVE":
            raise RegistryStaleError("REVISE_STALE", "Start again from the latest version.")
        OracleRegistryRepo().set_status(
            cursor,
            parent_id,
            "SUPERSEDED",
            actor=row["created_by"],
            actor_role=row["actor_role"],
            from_status="ACTIVE",
            audit_action="SUPERSEDE",
        )

    @staticmethod
    def _insert_metadata(cursor: Any, row: dict[str, Any]) -> None:
        metadata = row["metadata_json"]
        country = _value(metadata, "COUNTRY_CODE", settings.AML_COUNTRY_CODE)
        institution = _value(metadata, "INST_CODE", settings.AML_INST_CODE)
        description = _value(metadata, "SCENARIO_DES_ENG", row["scenario_name"])
        cursor.execute(
            """INSERT INTO PIO_AML_AGENT_SCENARIOS
            (COUNTRY_CODE,INST_CODE,SCENARIO_CODE,SCENARIO_DES_ENG,SCENARIO_DES_NAT_LAN,
             ACTIVE_FLAG,RUN_FLAG,VIOLATION_LEVEL,DEGREE_RISK_FLAG,CREATED_BY,CREATED_DATE,
             UPDATED_BY,UPDATED_DATE,CATEGORY_CODE,SCE_TYPE_CODE,CLASS_CODE)
            VALUES (:country,:institution,:scenario_id,:description,:description,
             '1','1',:violation,:risk,:created_by,SYSDATE,:created_by,SYSDATE,
             :category,:scenario_type,:class_code)""",
            {
                "country": country, "institution": institution, "scenario_id": row["scenario_id"],
                "description": str(description)[:400], "violation": _value(metadata, "VIOLATION_LEVEL", "H"),
                "risk": _value(metadata, "DEGREE_RISK_FLAG", "H"), "created_by": row["created_by_number"],
                "category": _value(metadata, "CATEGORY_CODE", "999"),
                "scenario_type": _value(metadata, "SCE_TYPE_CODE", "1"),
                "class_code": _value(metadata, "CLASS_CODE", "1"),
            },
        )

    @staticmethod
    def _append_audit_in_transaction(cursor: Any, row: dict[str, Any]) -> None:
        cursor.execute(
            """INSERT INTO PIO_AML_AGENT_SCENARIO_AUDIT
            (AUDIT_ID,SCENARIO_ID,SCENARIO_KEY,ACTION,ACTOR,ACTOR_ROLE,TS,
             FROM_STATUS,TO_STATUS,REASON,DIFF_JSON)
            VALUES (RAWTOHEX(SYS_GUID()),:scenario_id,:scenario_key,:action,:actor,
             :actor_role,SYSTIMESTAMP,:from_status,:to_status,:reason,:diff_json)""",
            {
                "scenario_id": row["scenario_id"], "scenario_key": row.get("scenario_key", ""),
                "action": row.get("audit_action", "CREATE"), "actor": row.get("actor_id", row.get("created_by")),
                "actor_role": row.get("actor_role", ""), "from_status": row.get("from_status"),
                "to_status": row.get("to_status", row.get("status")), "reason": row.get("change_reason", row.get("reason")),
                "diff_json": json.dumps(
                    row.get("audit_diff_json", row.get("receipt_json", {})),
                    ensure_ascii=False,
                    default=str,
                ),
            },
        )


def _execute_ddl(sql: str) -> None:
    with get_connection() as connection:
        connection.cursor().execute(sql)


def _names(rows: list[tuple]) -> set[str]:
    return {str(row[0]).lower() for row in rows}


def _row_dict(columns: list[str], row: tuple) -> dict[str, Any]:
    values = {name.lower(): value for name, value in zip(columns, row)}
    for key, value in list(values.items()):
        if hasattr(value, "read"):
            values[key] = value.read()
    return values


def _bind_row(row: dict[str, Any]) -> dict[str, Any]:
    bound = dict(row)
    for key in ("signature_json", "params_json", "filters_json", "metadata_json", "receipt_json"):
        if bound.get(key) is not None and not isinstance(bound[key], str):
            bound[key] = json.dumps(bound[key], ensure_ascii=False, default=str)
    return bound


def _value(data: Any, key: str, default: Any) -> Any:
    return data.get(key, default) if isinstance(data, dict) else default


def _number(value: Any, default: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(default)
        except (TypeError, ValueError):
            return 999


def _lookup_scenario_key(cursor: Any, scenario_id: str) -> str:
    """Read the stable key for one exact scenario primary key."""
    cursor.execute(
        "SELECT SCENARIO_KEY FROM PIO_AML_AGENT_PRODUCTION_SCENARIOS WHERE SCENARIO_ID=:scenario_id",
        {"scenario_id": scenario_id},
    )
    row = cursor.fetchone()
    if row is None:
        raise LookupError("Scenario was not found by primary key")
    return str(row[0])
