"""Fail-closed Oracle SQL policy for DWH scenario reads."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import sqlglot
from sqlglot import exp

_ALLOWED_ORACLE_ANONYMOUS_FUNCTIONS = {
    "DECODE",
    "GREATEST",
    "INSTR",
    "LEAST",
    "LENGTH",
    "LISTAGG",
    "NVL",
    "REGEXP_LIKE",
    "REPLACE",
    "ROUND",
    "SUBSTR",
    "TO_CHAR",
    "TO_DATE",
    "TO_NUMBER",
    "TRUNC",
}


class SqlPolicyError(ValueError):
    """Raised when SQL is malformed or violates the read-only policy."""


@dataclass(frozen=True)
class ValidatedQuery:
    """A parsed query approved for read-only execution."""

    sql: str
    binds: tuple[str, ...]
    tables: tuple[str, ...]


def validate_oracle_read(
    sql: str,
    *,
    params: dict[str, Any] | None = None,
    allowed_schemas: tuple[str, ...] = (),
    allowed_objects: tuple[str, ...] = (),
    required_projection: str | None = None,
    confirmed_filters: list[dict[str, Any]] | None = None,
) -> ValidatedQuery:
    """Parse and validate exactly one Oracle SELECT before execution.

    Args:
        sql: Candidate SQL, retained byte-for-byte for execution.
        params: Named bind values supplied to the Oracle driver.
        allowed_schemas: Schema names allowed for referenced objects.
        allowed_objects: Optional schema-qualified object allowlist.
        required_projection: Projection name required by the contract.
        confirmed_filters: Confirmed filters with target and expected values.

    Returns:
        Parsed query metadata for execution.

    Raises:
        SqlPolicyError: If any construct is malformed, ambiguous or unsupported.
    """
    if not isinstance(sql, str) or not sql.strip():
        raise SqlPolicyError("SQL is empty")
    if _contains_comment(sql):
        raise SqlPolicyError("SQL comments are not permitted")
    try:
        statements = sqlglot.parse(sql, read="oracle")
    except (sqlglot.errors.ParseError, ValueError) as exc:
        raise SqlPolicyError("SQL could not be parsed as Oracle SQL") from exc
    if len(statements) != 1 or statements[0] is None:
        raise SqlPolicyError("Exactly one SQL statement is required")
    root = statements[0]
    if not isinstance(root, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
        raise SqlPolicyError("Only SELECT queries, including CTEs, are permitted")
    forbidden = (
        exp.Insert,
        exp.Update,
        exp.Delete,
        exp.Merge,
        exp.Create,
        exp.Drop,
        exp.Alter,
        exp.Command,
        exp.Transaction,
        exp.Commit,
        exp.Rollback,
    )
    if any(any(root.find_all(node_type)) for node_type in forbidden):
        raise SqlPolicyError("Nested writes or procedural statements are forbidden")
    if any(root.find_all(exp.Into)) or any(root.find_all(exp.Lock)):
        raise SqlPolicyError("SELECT INTO and locking reads are not permitted")
    if any(
        isinstance(node, exp.Dot) and any(node.find_all(exp.Func))
        for node in root.walk()
    ) or any(column.name.upper() == "NEXTVAL" for column in root.find_all(exp.Column)):
        raise SqlPolicyError("Qualified functions and sequence reads are not permitted")
    unsupported_functions = {
        function.name.upper()
        for function in root.find_all(exp.Anonymous)
        if function.name.upper() not in _ALLOWED_ORACLE_ANONYMOUS_FUNCTIONS
    }
    if unsupported_functions:
        raise SqlPolicyError("SQL uses an unsupported or user-defined function")

    bind_names = tuple(sorted({node.name for node in root.find_all(exp.Placeholder)}))
    supplied = {str(key).lstrip(":").casefold() for key in (params or {})}
    missing = {name.lstrip(":").casefold() for name in bind_names} - supplied
    if missing:
        raise SqlPolicyError("SQL contains unbound parameters")

    schemas = {item.upper() for item in allowed_schemas}
    objects = {item.upper() for item in allowed_objects}
    cte_names = {
        cte.alias_or_name.upper() for cte in root.find_all(exp.CTE) if cte.alias_or_name
    }
    tables: list[str] = []
    for table in root.find_all(exp.Table):
        if not table.db and table.name.upper() in cte_names:
            continue
        if not table.name:
            raise SqlPolicyError("SQL contains an unresolved object reference")
        schema = table.db.upper() if table.db else ""
        name = table.name.upper()
        qualified = f"{schema}.{name}" if schema else name
        if not schema and name not in objects:
            raise SqlPolicyError(
                "Unqualified objects require an explicit object allowlist"
            )
        if schema and schema not in schemas:
            raise SqlPolicyError("SQL references a schema outside the allowlist")
        if objects and qualified not in objects and name not in objects:
            raise SqlPolicyError("SQL references an object outside the allowlist")
        tables.append(qualified)

    if required_projection:
        select = _outer_select(root)
        if select is None or required_projection.casefold() not in {
            projection.alias_or_name.casefold() for projection in select.expressions
        }:
            raise SqlPolicyError(
                f"Required projection {required_projection} is missing"
            )
    for item in confirmed_filters or []:
        _validate_filter(root, item, params or {})
    return ValidatedQuery(sql=sql, binds=bind_names, tables=tuple(tables))


def _outer_select(root: exp.Expression) -> exp.Select | None:
    """Return outer SELECT for projection validation."""
    if isinstance(root, exp.Select):
        return root
    if isinstance(root, (exp.Union, exp.Intersect, exp.Except)):
        return root.this if isinstance(root.this, exp.Select) else None
    return None


def _contains_comment(sql: str) -> bool:
    """Detect SQL comments outside quoted strings and identifiers."""
    index = 0
    quote: str | None = None
    while index < len(sql):
        char = sql[index]
        if quote:
            if char == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    index += 2
                    continue
                quote = None
        elif char in {"'", '"'}:
            quote = char
        elif sql.startswith("--", index) or sql.startswith("/*", index):
            return True
        index += 1
    return False


def _validate_filter(
    root: exp.Expression, item: dict[str, Any], params: dict[str, Any]
) -> None:
    """Require an AST predicate with the confirmed target and values."""
    target = item.get("target") or {}
    column_name = str(target.get("column") or "").casefold()
    table_name = str(target.get("table") or "").split(".")[-1].casefold()
    expected = {str(value).casefold() for value in item.get("values", [])}
    if not column_name or not expected:
        raise SqlPolicyError("Confirmed filter is missing a target or values")
    aliases = {
        table.alias.casefold(): table.name.casefold()
        for table in root.find_all(exp.Table)
    }
    cte_names = {
        cte.alias_or_name.casefold()
        for cte in root.find_all(exp.CTE)
        if cte.alias_or_name
    }
    physical_sources = [
        table
        for table in root.find_all(exp.Table)
        if table.name.casefold() not in cte_names
    ]
    for disjunction in root.find_all(exp.Or):
        if any(
            column.name.casefold() == column_name
            for column in disjunction.find_all(exp.Column)
        ):
            raise SqlPolicyError(
                "Confirmed filters inside OR expressions are ambiguous"
            )
    where_predicates = [
        predicate
        for clause in root.find_all(exp.Where)
        for predicate in clause.find_all(exp.Predicate)
    ]
    matches = []
    for predicate in where_predicates:
        cols = list(predicate.find_all(exp.Column))
        if not any(
            col.name.casefold() == column_name
            and (
                not table_name
                or (not col.table and len(physical_sources) == 1)
                or aliases.get(col.table.casefold(), col.table.casefold()) == table_name
            )
            for col in cols
        ):
            continue
        values = []
        if isinstance(predicate, exp.EQ):
            values = [
                (
                    predicate.expression
                    if isinstance(predicate.this, exp.Column)
                    else predicate.this
                )
            ]
        elif isinstance(predicate, exp.In) and predicate.args.get("query") is None:
            values = list(predicate.expressions)
        actual: set[str] = set()
        for value in values:
            if isinstance(value, exp.Literal):
                actual.add(str(value.this).casefold())
            elif isinstance(value, exp.Placeholder):
                key = value.name
                if key not in params:
                    key = next(
                        (
                            k
                            for k in params
                            if str(k).lstrip(":").casefold() == key.casefold()
                        ),
                        key,
                    )
                if key not in params:
                    continue
                bound = params[key]
                actual.update(
                    str(v).casefold()
                    for v in (
                        bound if isinstance(bound, (list, tuple, set)) else [bound]
                    )
                )
        if actual == expected:
            negated = False
            parent = predicate.parent
            while isinstance(parent, (exp.Paren, exp.Not)):
                if isinstance(parent, exp.Not):
                    negated = not negated
                parent = parent.parent
            expected_negative = str(
                item.get("polarity", "positive")
            ).casefold() == "negative" and bool(item.get("polarity_allowed"))
            if negated == expected_negative:
                matches.append(predicate)
    if not matches:
        raise SqlPolicyError(
            "A confirmed filter is missing or differs from its bound values"
        )
