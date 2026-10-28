"""Read-only Oracle repositories for the in-process DWH scenario package."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

from services.aml_builder.web.domain.dwh_scenario.sql_policy import (
    SqlPolicyError,
    validate_oracle_read,
)
from services.aml_builder.web.services import oracle
from services.aml_builder.web.services.settings import settings


@dataclass
class OracleRequestBudget:
    """Mutable statement and wall-time budget shared by one generation request."""

    max_statements: int = 20
    deadline_seconds: float = settings.DWH_SQL_DEADLINE_SECONDS
    started_at: float = field(default_factory=time.monotonic)
    statements_used: int = 0

    def consume(self) -> float:
        """Reserve one Oracle statement or fail before database access."""
        if time.monotonic() - self.started_at >= self.deadline_seconds:
            raise TimeoutError("DWH Oracle request wall-time budget exceeded")
        if self.statements_used >= self.max_statements:
            raise RuntimeError("DWH Oracle statement budget exceeded")
        self.statements_used += 1
        return self.deadline_seconds - (time.monotonic() - self.started_at)


@dataclass(frozen=True)
class CandidateQueryResult:
    """Execution evidence for one candidate query."""

    sql: str
    success: bool
    columns: tuple[str, ...] = ()
    rows: tuple[tuple[Any, ...], ...] = ()
    row_count: int = 0
    truncated: bool = False
    elapsed_ms: int = 0
    error: str | None = None


class DwhOracleRepository:
    """Use Service A's independently configured DWH reader pool."""

    def __init__(
        self,
        *,
        connection_provider: Any = oracle.get_dwh_connection,
        allowed_schemas: tuple[str, ...] | None = None,
        allowed_objects: tuple[str, ...] | None = None,
        max_candidate_rows: int | None = None,
        read_timeout_seconds: float | None = None,
    ) -> None:
        """Create a repository with an injectable connection context factory.

        Args:
            connection_provider: Context manager yielding a fake or pooled connection.
            allowed_schemas: Optional per-repository schema allowlist.
            allowed_objects: Optional per-repository object allowlist.
            max_candidate_rows: Optional stricter row cap for this repository.
            read_timeout_seconds: Optional stricter call timeout.
        """
        self._connection_provider = connection_provider
        self._allowed_schemas = allowed_schemas
        self._allowed_objects = allowed_objects
        self._max_candidate_rows = max_candidate_rows
        self._read_timeout_seconds = read_timeout_seconds

    def execute_candidate(
        self,
        sql: str,
        params: dict[str, Any] | None = None,
        *,
        budget: OracleRequestBudget,
        required_projection: str = "CUS_NUM",
        confirmed_filters: list[dict[str, Any]] | None = None,
    ) -> CandidateQueryResult:
        """Validate and execute candidate SQL with a bounded sample.

        Args:
            sql: SQL returned to the caller and executed unchanged.
            params: Named Oracle bind values.
            budget: Per-request statement and wall-time budget.
            required_projection: Required outer projection.
            confirmed_filters: Required confirmed filters.

        Returns:
            Success or failure evidence; zero rows is a successful result.
        """
        started = time.monotonic()
        try:
            validate_oracle_read(
                sql,
                params=params,
                allowed_schemas=(
                    self._allowed_schemas
                    if self._allowed_schemas is not None
                    else tuple(settings.DWH_SQL_ALLOWED_SCHEMAS)
                ),
                allowed_objects=(
                    self._allowed_objects
                    if self._allowed_objects is not None
                    else tuple(settings.DWH_SQL_ALLOWED_OBJECTS)
                ),
                required_projection=required_projection,
                confirmed_filters=confirmed_filters,
            )
            remaining = budget.consume()
            sample_limit = min(
                settings.DWH_SQL_MAX_CANDIDATE_ROWS,
                self._max_candidate_rows or settings.DWH_SQL_MAX_CANDIDATE_ROWS,
            )
            with self._connection_provider() as connection:
                cursor = connection.cursor()
                cursor.call_timeout = int(
                    min(
                        self._read_timeout_seconds
                        or settings.DWH_SQL_READ_TIMEOUT_SECONDS,
                        remaining,
                    )
                    * 1000
                )
                cursor.execute(sql, params or {})
                columns = tuple(item[0] for item in (cursor.description or ()))
                fetched = cursor.fetchmany(sample_limit + 1)
            truncated = len(fetched) > sample_limit
            rows = tuple(fetched[:sample_limit])
            return CandidateQueryResult(
                sql=sql,
                success=True,
                columns=columns,
                rows=rows,
                row_count=len(rows),
                truncated=truncated,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
        except (SqlPolicyError, TimeoutError) as exc:
            return CandidateQueryResult(
                sql=sql,
                success=False,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                error=str(exc),
            )
        except RuntimeError as exc:
            message = str(exc)
            safe_error = (
                message
                if message
                in {
                    "DWH Oracle request wall-time budget exceeded",
                    "DWH Oracle statement budget exceeded",
                }
                else "Oracle candidate execution failed"
            )
            return CandidateQueryResult(
                sql=sql,
                success=False,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                error=safe_error,
            )
        except Exception:
            return CandidateQueryResult(
                sql=sql,
                success=False,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                error="Oracle candidate execution failed",
            )

    def read_dictionary(
        self,
        sql: str,
        params: dict[str, Any] | None = None,
        *,
        budget: OracleRequestBudget,
        max_rows: int = 500,
    ) -> tuple[list[str], list[tuple[Any, ...]]]:
        """Execute one allowlisted dictionary SELECT with bounded fetches.

        Args:
            sql: Repository-owned query against a dictionary object.
            params: Named Oracle bind values.
            budget: Per-request shared budget.
            max_rows: Maximum returned rows.

        Returns:
            Column labels and bounded row sample.
        """
        return self._read_guarded(
            sql,
            params,
            budget=budget,
            max_rows=max_rows,
            allowed_objects=(
                "PIO_BANKBI_DICTIONARY_TABLES",
                "PIO_BANKBI_DICTIONARY_COL",
                "PIO_BANKBI_DICT_JOIN_TABLES",
                "PIO_AML_TABLES",
                "PIO_AML_COLUMNS",
            ),
        )

    def read_schema_metadata(
        self,
        schemas: tuple[str, ...],
        objects: tuple[str, ...],
        *,
        budget: OracleRequestBudget,
        max_rows: int = 2000,
    ) -> tuple[list[str], list[tuple[Any, ...]]]:
        """Read bounded Oracle object and column comments for configured scope.

        Args:
            schemas: Explicit schema allowlist.
            objects: Explicit schema-qualified object allowlist.
            budget: Shared Oracle statement and wall-time budget.
            max_rows: Maximum dictionary rows to fetch.

        Returns:
            Available table and column metadata; absent objects are not errors.
        """
        max_rows = _positive_limit(max_rows, 5000)
        normalized_schemas = tuple(dict.fromkeys(_identifier(item) for item in schemas))
        normalized_objects_list: list[tuple[str, str]] = []
        for item in objects:
            if item.count(".") != 1:
                raise ValueError("Objects must use schema-qualified identifiers")
            owner, name = item.split(".", 1)
            normalized_objects_list.append((_identifier(owner), _identifier(name)))
        normalized_objects = tuple(dict.fromkeys(normalized_objects_list))
        if not normalized_schemas or not normalized_objects:
            raise ValueError("Configured schema and object allowlists are required")
        if any(owner not in normalized_schemas for owner, _name in normalized_objects):
            raise ValueError("Object scope exceeds configured schema allowlist")
        params: dict[str, Any] = {}
        schema_binds: list[str] = []
        for index, schema in enumerate(normalized_schemas):
            key = f"schema_{index}"
            schema_binds.append(f":{key}")
            params[key] = schema
        object_binds: list[str] = []
        for index, (_owner, name) in enumerate(normalized_objects):
            key = f"object_{index}"
            object_binds.append(f":{key}")
            params[key] = name
        sql = (
            "SELECT o.OWNER, o.OBJECT_NAME AS TABLE_NAME, c.COLUMN_NAME, "
            "c.DATA_TYPE, c.DATA_LENGTH, c.DATA_PRECISION, c.DATA_SCALE, "
            "c.NULLABLE, tc.COMMENTS AS TABLE_COMMENT, "
            "cc.COMMENTS AS COLUMN_COMMENT, NVL(s.TABLE_OWNER,o.OWNER) AS TARGET_OWNER, "
            "NVL(s.TABLE_NAME,o.OBJECT_NAME) AS TARGET_NAME, o.OBJECT_TYPE "
            "FROM ALL_OBJECTS o LEFT JOIN ALL_SYNONYMS s "
            "ON s.OWNER=o.OWNER AND s.SYNONYM_NAME=o.OBJECT_NAME "
            "AND o.OBJECT_TYPE='SYNONYM' AND s.DB_LINK IS NULL "
            "JOIN ALL_TAB_COLUMNS c ON c.OWNER=NVL(s.TABLE_OWNER,o.OWNER) "
            "AND c.TABLE_NAME=NVL(s.TABLE_NAME,o.OBJECT_NAME) "
            "LEFT JOIN ALL_TAB_COMMENTS tc ON tc.OWNER=c.OWNER "
            "AND tc.TABLE_NAME=c.TABLE_NAME "
            "LEFT JOIN ALL_COL_COMMENTS cc ON cc.OWNER=c.OWNER "
            "AND cc.TABLE_NAME=c.TABLE_NAME AND cc.COLUMN_NAME=c.COLUMN_NAME "
            f"WHERE o.OWNER IN ({', '.join(schema_binds)}) "
            f"AND o.OBJECT_NAME IN ({', '.join(object_binds)}) "
            "AND o.OBJECT_TYPE IN ('TABLE','VIEW','MATERIALIZED VIEW','SYNONYM') "
            "ORDER BY o.OWNER, o.OBJECT_NAME, c.COLUMN_ID "
            f"FETCH FIRST {max_rows} ROWS ONLY"
        )
        return self._read_guarded(
            sql,
            params,
            budget=budget,
            max_rows=max_rows,
            allowed_objects=(
                "ALL_OBJECTS",
                "ALL_SYNONYMS",
                "ALL_TAB_COLUMNS",
                "ALL_TAB_COMMENTS",
                "ALL_COL_COMMENTS",
            ),
        )

    def read_schema_object_resolution(
        self,
        schemas: tuple[str, ...],
        objects: tuple[str, ...],
        *,
        budget: OracleRequestBudget,
        max_rows: int = 100,
    ) -> tuple[list[str], list[tuple[Any, ...]]]:
        """Resolve configured objects and local synonym targets from Oracle metadata."""
        max_rows = _positive_limit(max_rows, 500)
        normalized_schemas = tuple(dict.fromkeys(_identifier(item) for item in schemas))
        normalized_objects: list[tuple[str, str]] = []
        for item in objects:
            if item.count(".") != 1:
                raise ValueError("Objects must use schema-qualified identifiers")
            owner, name = item.split(".", 1)
            normalized_objects.append((_identifier(owner), _identifier(name)))
        if not normalized_schemas or not normalized_objects:
            raise ValueError("Configured schema and object allowlists are required")
        if any(owner not in normalized_schemas for owner, _name in normalized_objects):
            raise ValueError("Object scope exceeds configured schema allowlist")
        params: dict[str, Any] = {}
        schema_binds: list[str] = []
        for index, schema in enumerate(normalized_schemas):
            key = f"schema_{index}"
            schema_binds.append(f":{key}")
            params[key] = schema
        object_binds: list[str] = []
        for index, (_owner, name) in enumerate(normalized_objects):
            key = f"object_{index}"
            object_binds.append(f":{key}")
            params[key] = name
        sql = (
            "SELECT o.OWNER, o.OBJECT_NAME, o.OBJECT_TYPE, o.STATUS, "
            "s.TABLE_OWNER, s.TABLE_NAME, s.DB_LINK "
            "FROM ALL_OBJECTS o LEFT JOIN ALL_SYNONYMS s "
            "ON s.OWNER=o.OWNER AND s.SYNONYM_NAME=o.OBJECT_NAME "
            f"WHERE o.OWNER IN ({', '.join(schema_binds)}) "
            f"AND o.OBJECT_NAME IN ({', '.join(object_binds)}) "
            "AND o.OBJECT_TYPE IN ('TABLE','VIEW','MATERIALIZED VIEW','SYNONYM') "
            "ORDER BY o.OWNER,o.OBJECT_NAME "
            f"FETCH FIRST {max_rows} ROWS ONLY"
        )
        return self._read_guarded(
            sql,
            params,
            budget=budget,
            max_rows=max_rows,
            allowed_objects=("ALL_OBJECTS", "ALL_SYNONYMS"),
        )

    def probe_column_values(
        self,
        owner: str,
        table_name: str,
        column_name: str,
        *,
        budget: OracleRequestBudget,
        sample_rows: int = 1000,
    ) -> tuple[list[str], list[tuple[Any, ...]]]:
        """Return a bounded non-null value sample from an allowlisted column."""
        sample_rows = _positive_limit(sample_rows, 2000)
        owner = _identifier(owner)
        table_name = _identifier(table_name.split(".")[-1])
        column_name = _identifier(column_name.split(".")[-1])
        sql = (
            f'SELECT "{column_name}" FROM "{owner}"."{table_name}" '
            f'WHERE "{column_name}" IS NOT NULL AND ROWNUM <= {sample_rows}'
        )
        return self._read_guarded(sql, {}, budget=budget, max_rows=sample_rows)

    def lookup_business_codes(
        self,
        owner: str,
        table_name: str,
        code_column: str,
        description_column: str,
        *,
        budget: OracleRequestBudget,
        max_rows: int = 100,
        filters: dict[str, Any] | None = None,
    ) -> tuple[list[str], list[tuple[Any, ...]]]:
        """Read bounded code/description rows using metadata-resolved identifiers."""
        max_rows = _positive_limit(max_rows, 2000)
        owner = _identifier(owner)
        table_name = _identifier(table_name.split(".")[-1])
        code_column = _identifier(code_column)
        description_column = _identifier(description_column)
        filter_sql: list[str] = []
        params: dict[str, Any] = {}
        for index, (column, value) in enumerate((filters or {}).items()):
            safe_column = _identifier(column)
            bind = f"filter_{index}"
            filter_sql.append(f'"{safe_column}" = :{bind}')
            params[bind] = value
        where = f" WHERE {' AND '.join(filter_sql)}" if filter_sql else ""
        sql = (
            f'SELECT "{code_column}", "{description_column}" '
            f'FROM "{owner}"."{table_name}"{where} '
            f"FETCH FIRST {max_rows} ROWS ONLY"
        )
        return self._read_guarded(sql, params, budget=budget, max_rows=max_rows)

    def _read_guarded(
        self,
        sql: str,
        params: dict[str, Any] | None,
        *,
        budget: OracleRequestBudget,
        max_rows: int,
        allowed_objects: tuple[str, ...] = (),
    ) -> tuple[list[str], list[tuple[Any, ...]]]:
        """Run a repository read through the shared policy and execution budget."""
        validate_oracle_read(
            sql,
            params=params,
            allowed_schemas=(
                self._allowed_schemas
                if self._allowed_schemas is not None
                else tuple(settings.DWH_SQL_ALLOWED_SCHEMAS)
            ),
            allowed_objects=allowed_objects
            or self._allowed_objects
            or tuple(settings.DWH_SQL_ALLOWED_OBJECTS),
        )
        remaining = budget.consume()
        with self._connection_provider() as connection:
            cursor = connection.cursor()
            cursor.call_timeout = int(
                min(
                    self._read_timeout_seconds or settings.DWH_SQL_READ_TIMEOUT_SECONDS,
                    remaining,
                )
                * 1000
            )
            cursor.execute(sql, params or {})
            columns = [item[0] for item in (cursor.description or ())]
            rows = cursor.fetchmany(max_rows + 1)
        if len(rows) > max_rows:
            raise ValueError("Dictionary query exceeded its configured row limit")
        return columns, rows

    def dictionary_tables(
        self, *, budget: OracleRequestBudget, search: str = ""
    ) -> tuple[list[str], list[tuple[Any, ...]]]:
        """Read bounded dictionary table names and business descriptions."""
        return self.read_dictionary(
            "SELECT TABLE_NAME, OBJECT_NAME FROM PIO_BANKBI_DICTIONARY_TABLES "
            "WHERE (:search IS NULL OR UPPER(TABLE_NAME) LIKE '%' || UPPER(:search) "
            "OR UPPER(OBJECT_NAME) LIKE '%' || UPPER(:search)) "
            "FETCH FIRST 200 ROWS ONLY",
            {"search": search or None},
            budget=budget,
        )

    def dictionary_columns(
        self, table_name: str, *, budget: OracleRequestBudget, column_name: str = ""
    ) -> tuple[list[str], list[tuple[Any, ...]]]:
        """Read dictionary column metadata for one table."""
        return self.read_dictionary(
            "SELECT TABLE_NAME, COLUMN_NAME, OBJECT_NAME, LOOKUP_TABLE_NAME, FLAGS "
            "FROM PIO_BANKBI_DICTIONARY_COL WHERE UPPER(TABLE_NAME) LIKE '%' || :table_name "
            "AND (:column_name IS NULL OR UPPER(COLUMN_NAME)=UPPER(:column_name)) "
            "FETCH FIRST 500 ROWS ONLY",
            {
                "table_name": table_name.split(".")[-1],
                "column_name": column_name or None,
            },
            budget=budget,
        )

    def standard_joins(
        self, table_a: str, table_b: str, *, budget: OracleRequestBudget
    ) -> tuple[list[str], list[tuple[Any, ...]]]:
        """Read direct join metadata between two requested tables."""
        return self.read_dictionary(
            "SELECT FACT_TABLE, REF_TABLE, FACT_FORMULA FROM PIO_BANKBI_DICT_JOIN_TABLES "
            "WHERE (FACT_TABLE=:table_a AND REF_TABLE=:table_b) "
            "OR (FACT_TABLE=:table_b AND REF_TABLE=:table_a)",
            {"table_a": table_a.upper(), "table_b": table_b.upper()},
            budget=budget,
        )

    def business_code_metadata(
        self, column_name: str, *, budget: OracleRequestBudget, table_name: str = ""
    ) -> tuple[list[str], list[tuple[Any, ...]]]:
        """Read the configured lookup table and AML code/description columns."""
        return self.read_dictionary(
            "SELECT d.LOOKUP_TABLE_NAME, a.LOOKUP_CODE_COLUMN, a.LOOKUP_DESC_COLUMN, "
            "a.LOOKUP_COUNTRY_COLUMN, a.LOOKUP_INST_COLUMN "
            "FROM PIO_BANKBI_DICTIONARY_COL d LEFT JOIN BI_DWH.PIO_AML_COLUMNS a "
            "ON UPPER(a.COLUMN_NAME)=UPPER(d.COLUMN_NAME) "
            "WHERE UPPER(d.COLUMN_NAME)=UPPER(:column_name) "
            "AND (:table_name IS NULL OR UPPER(d.TABLE_NAME) LIKE '%' || UPPER(:table_name)) "
            "FETCH FIRST 1 ROWS ONLY",
            {
                "column_name": column_name,
                "table_name": table_name.split(".")[-1] or None,
            },
            budget=budget,
        )


def _identifier(value: str) -> str:
    """Validate one unquoted Oracle identifier before SQL composition."""
    normalized = value.strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9_$#]{0,127}", normalized):
        raise ValueError("Unsupported Oracle identifier")
    return normalized


def _positive_limit(value: int, maximum: int) -> int:
    """Validate a row limit and cap it to the repository maximum."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("Row limit must be a positive integer")
    return min(value, maximum)
