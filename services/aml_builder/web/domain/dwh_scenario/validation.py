"""Pure Service B contract checks expressed against Service A's AML types."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any

import sqlglot
from sqlglot import exp

from services.aml_builder.web.domain.dwh_scenario.contracts import ScenarioSqlRequest
from services.aml_builder.web.domain.dwh_scenario.sql_policy import SqlPolicyError


def derive_candidate_bind_values(
    request: ScenarioSqlRequest, sql: str
) -> dict[str, Any]:
    """Derive allowed bind values from confirmed filters and temporal intent.

    Args:
        request: Validated AML intent and confirmed filter values.
        sql: Candidate SQL whose placeholders are assigned by predicate role.

    Returns:
        A bind dictionary for B3 validation and bounded execution.

    Raises:
        SqlPolicyError: If a placeholder has no unambiguous contract value.
    """
    try:
        statements = sqlglot.parse(sql, read="oracle")
    except (sqlglot.errors.ParseError, ValueError) as exc:
        raise SqlPolicyError("SQL could not be parsed as Oracle SQL") from exc
    if len(statements) != 1 or statements[0] is None:
        raise SqlPolicyError("Exactly one SQL statement is required")

    root = statements[0]
    params: dict[str, Any] = {}

    def bind_value(node: exp.Expression, value: Any) -> None:
        """Assign one placeholder only when its contract value is consistent."""
        if not isinstance(node, exp.Placeholder):
            return
        name = node.name
        if name in params and params[name] != value:
            raise SqlPolicyError("A SQL bind is assigned conflicting contract values")
        params[name] = value

    _derive_confirmed_filter_binds(request, root, bind_value)
    _derive_threshold_binds(request, root, bind_value)
    _derive_temporal_binds(request, root, bind_value)

    placeholders = {node.name for node in root.find_all(exp.Placeholder)}
    if placeholders != set(params):
        raise SqlPolicyError("SQL contains binds outside confirmed filter and temporal contracts")
    return params


def _derive_confirmed_filter_binds(
    request: ScenarioSqlRequest,
    root: exp.Expression,
    bind_value: Any,
) -> None:
    """Associate filter placeholders with values already confirmed by the user."""
    aliases = {
        table.alias_or_name.casefold(): table.name.casefold()
        for table in root.find_all(exp.Table)
    }
    for item in request.confirmed_filters:
        if item.status != "resolved" or not item.values:
            continue
        target = item.target or {}
        column_name = str(target.get("column") or "").casefold()
        table_name = str(target.get("table") or "").split(".")[-1].casefold()
        if not column_name:
            continue
        for predicate in root.find_all(exp.Predicate):
            if isinstance(predicate, exp.EQ):
                operands = [predicate.this, predicate.expression]
                target_columns = [
                    node for node in operands
                    if isinstance(node, exp.Column)
                    and _column_matches(node, column_name, table_name, aliases)
                ]
                values = [node for node in operands if node not in target_columns]
            elif isinstance(predicate, exp.In) and predicate.args.get("query") is None:
                target_columns = (
                    [predicate.this]
                    if isinstance(predicate.this, exp.Column)
                    and _column_matches(predicate.this, column_name, table_name, aliases)
                    else []
                )
                values = list(predicate.expressions)
            else:
                continue
            if not target_columns or not all(
                isinstance(node, exp.Placeholder) for node in values
            ):
                continue
            if len(values) != len(item.values):
                raise SqlPolicyError("Confirmed filter bind count differs from its values")
            for node, value in zip(values, item.values):
                bind_value(node, value)


def _derive_threshold_binds(
    request: ScenarioSqlRequest,
    root: exp.Expression,
    bind_value: Any,
) -> None:
    """Associate threshold placeholders with their structured intent values."""
    operator_types: dict[str, type[exp.Expression]] = {
        ">": exp.GT,
        "<": exp.LT,
        ">=": exp.GTE,
        "<=": exp.LTE,
        "=": exp.EQ,
    }
    for threshold in request.intent.thresholds:
        field_name = str(threshold.field or "").upper()
        expected = threshold.value_from
        predicate_type = operator_types.get(threshold.operator)
        if not field_name or expected is None or predicate_type is None:
            continue
        clauses = (
            root.find_all(exp.Where)
            if threshold.target_scope == "DETAIL"
            else root.find_all(exp.Having)
        )
        for clause in clauses:
            for predicate in clause.find_all(predicate_type):
                sides = (predicate.this, predicate.expression)
                if not any(
                    isinstance(node, exp.Column)
                    and node.name.upper() == field_name
                    for node in sides
                ):
                    continue
                for node in sides:
                    if isinstance(node, exp.Placeholder):
                        bind_value(node, expected)


def _derive_temporal_binds(
    request: ScenarioSqlRequest,
    root: exp.Expression,
    bind_value: Any,
) -> None:
    """Derive rolling-date binds from an anchor date or a SYSDATE duration."""
    window = request.intent.time_window
    if not window or window.unit != "DAYS" or not window.is_rolling:
        return
    date_columns = [
        node for node in root.find_all(exp.Column)
        if re.search(r"(?:_DATE$|^DATE_)", node.name.upper())
    ]
    if not date_columns:
        return

    anchor_text = request.intent.anchor_date
    if anchor_text:
        if request.intent.baseline_window:
            raise SqlPolicyError("Anchored baseline date binds are not supported")
        try:
            anchor = date.fromisoformat(str(anchor_text)[:10])
        except ValueError as exc:
            raise SqlPolicyError("Temporal anchor date is invalid") from exc
        lower_value = anchor - timedelta(days=window.value)
        lower: list[tuple[exp.Expression, str]] = []
        upper: list[tuple[exp.Expression, str]] = []
        for predicate in root.find_all(exp.Predicate):
            if not isinstance(predicate, (exp.GTE, exp.LT)):
                continue
            if not isinstance(predicate.this, exp.Column):
                continue
            if predicate.this.name.upper() not in {
                node.name.upper() for node in date_columns
            }:
                continue
            if not isinstance(predicate.expression, exp.Placeholder):
                continue
            target = lower if isinstance(predicate, exp.GTE) else upper
            target.append((predicate.expression, predicate.this.name.casefold()))
        compatible = [
            (lo, hi) for lo in lower for hi in upper if lo[1] == hi[1]
        ]
        if len(compatible) != 1:
            raise SqlPolicyError(
                "Anchored rolling window requires one half-open date bind pair"
            )
        bind_value(compatible[0][0][0], lower_value)
        bind_value(compatible[0][1][0], anchor)
        return

    if request.intent.baseline_window:
        # Existing baseline SQL remains supported when expressed with literal
        # SYSDATE arithmetic. Bind mapping for overlapping baseline ranges is
        # ambiguous without an explicit evaluation anchor, so fail closed.
        temporal_placeholders = [
            node for node in root.find_all(exp.Placeholder)
            if isinstance(node.parent, exp.Sub)
            and isinstance(node.parent.this, exp.CurrentTimestamp)
            and node.parent.this.args.get("sysdate")
        ]
        if temporal_placeholders:
            raise SqlPolicyError(
                "Baseline duration binds require an explicit anchor date"
            )
        return

    for predicate in root.find_all(exp.GTE):
        if not isinstance(predicate.this, exp.Column):
            continue
        if predicate.this.name.upper() not in {
            node.name.upper() for node in date_columns
        }:
            continue
        expression = predicate.expression
        if (
            isinstance(expression, exp.Sub)
            and isinstance(expression.this, exp.CurrentTimestamp)
            and expression.this.args.get("sysdate")
        ):
            bind_value(expression.expression, window.value)


def _column_matches(
    column: exp.Column,
    expected_column: str,
    expected_table: str,
    aliases: dict[str, str],
) -> bool:
    """Check a SQL column against a confirmed table/column identity."""
    if column.name.casefold() != expected_column:
        return False
    if not expected_table:
        return True
    return not column.table or aliases.get(
        column.table.casefold(), column.table.casefold()
    ) == expected_table


def validate_generated_sql(
    request: ScenarioSqlRequest,
    sql: str,
    *,
    params: dict[str, Any] | None = None,
) -> list[str]:
    """Return structural findings corresponding to Service B's handoff checks.

    Args:
        request: Typed request already validated for handoff.
        sql: Candidate SQL returned by the generator.

    Returns:
        Human-readable validation findings; an empty list means these checks pass.
    """
    findings: list[str] = []
    try:
        statements = sqlglot.parse(sql, read="oracle")
        final_select = statements[0] if len(statements) == 1 else None
        projected = (
            {
                projection.alias_or_name.upper()
                for projection in final_select.expressions
            }
            if isinstance(final_select, exp.Select)
            else set()
        )
        root = final_select
    except (sqlglot.errors.ParseError, ValueError):
        projected = set()
        root = None
    if "CUS_NUM" not in projected:
        findings.append("Final SQL must return CUS_NUM for Service A alert creation")
    if params is None:
        try:
            params = derive_candidate_bind_values(request, sql)
        except SqlPolicyError as exc:
            findings.append(f"SQL bind contract is invalid: {exc}")
            params = {}
    if root is not None:
        findings.extend(_transaction_code_findings(request, root))
        findings.extend(_temporal_findings(request, root, params))
    return findings


def _transaction_code_findings(
    request: ScenarioSqlRequest, root: exp.Expression
) -> list[str]:
    """Check that explicit transaction codes have confirmed filter contracts."""
    intent = request.intent
    types = [intent.transaction_type, *(intent.transaction_types or [])]
    codes = set(intent.explanation_codes or [])
    for bound in (intent.explanation_codes_by_type or {}).values():
        codes.update(bound)
    for condition in [*intent.thresholds, *intent.semantic_conditions]:
        types.append(getattr(condition, "transaction_type", None))
        codes.update(getattr(condition, "explanation_codes", None) or [])
    explicit_types = [
        str(value).strip().upper()
        for value in types
        if value
        and str(value).strip().upper() not in {"ALL", "ALL TRANSACTIONS", "ANY"}
    ]
    code_columns = [
        node for node in root.find_all(exp.Column)
        if node.name.upper() == "EXPL_CODE"
    ]
    has_code_predicate = any(
        isinstance(predicate, (exp.EQ, exp.In))
        and any(
            isinstance(node, exp.Column) and node.name.upper() == "EXPL_CODE"
            for node in predicate.find_all(exp.Column)
        )
        for predicate in root.find_all(exp.Predicate)
    )
    any_code_predicate = bool(code_columns)
    if not explicit_types and not codes:
        return (
            ["SQL invents an EXPL_CODE restriction for an all/no-type intent"]
            if any_code_predicate
            else []
        )
    confirmed_codes = {
        str(value)
        for item in request.confirmed_filters
        if item.status == "resolved"
        and str((item.target or {}).get("column") or "").upper() == "EXPL_CODE"
        for value in item.values
    }
    if not codes.issubset(confirmed_codes):
        return ["Specified transaction codes require a matching confirmed EXPL_CODE filter"]
    findings = []
    if not has_code_predicate:
        findings.append(
            "Specified transaction types or codes require an EXPL_CODE filter"
        )
        return findings
    return findings


def _temporal_findings(
    request: ScenarioSqlRequest,
    root: exp.Expression,
    params: dict[str, Any],
) -> list[str]:
    """Check rolling and baseline dates against literal or bound contracts."""
    intent = request.intent
    findings: list[str] = []
    window = intent.time_window
    if window and window.is_rolling and window.unit == "DAYS":
        duration = window.value
        anchor_text = intent.anchor_date
        if anchor_text:
            try:
                anchor = date.fromisoformat(str(anchor_text)[:10])
            except ValueError:
                return ["Temporal anchor date is invalid"]
            expected_start = anchor - timedelta(days=duration)
            predicates = list(root.find_all(exp.Predicate))
            lower = [
                predicate for predicate in predicates
                if isinstance(predicate, exp.GTE)
                and isinstance(predicate.this, exp.Column)
                and isinstance(predicate.expression, exp.Placeholder)
                and params.get(predicate.expression.name) == expected_start
            ]
            upper = [
                predicate for predicate in predicates
                if isinstance(predicate, exp.LT)
                and isinstance(predicate.this, exp.Column)
                and isinstance(predicate.expression, exp.Placeholder)
                and params.get(predicate.expression.name) == anchor
            ]
            if not any(
                lo.this.name.casefold() == hi.this.name.casefold()
                and re.search(r"(?:_DATE$|^DATE_)", lo.this.name.upper())
                for lo in lower for hi in upper
            ):
                findings.append(
                    f"Rolling {duration}-day window must bind the half-open date range"
                )
        elif not _has_rolling_day_predicate(root, duration, params):
            findings.append(
                f"Rolling {duration}-day window must use its confirmed date-duration bind"
            )
    baseline = intent.baseline_window
    if baseline and baseline.exclude_current_window and baseline.unit == "DAYS":
        offset = baseline.offset_days
        start = offset + baseline.duration
        lower = any(
            isinstance(predicate, exp.GTE)
            and _has_sysdate_offset(predicate, start, params)
            for predicate in root.find_all(exp.Predicate)
        )
        upper = any(
            isinstance(predicate, exp.LT)
            and _has_sysdate_offset(predicate, offset, params)
            for predicate in root.find_all(exp.Predicate)
        )
        if not lower or not upper:
            findings.append(
                f"Baseline must cover SYSDATE - {start} through before SYSDATE - {offset}"
            )
    return findings


def _has_rolling_day_predicate(
    root: exp.Expression, duration: int, params: dict[str, Any]
) -> bool:
    """Return whether SQL contains a matching `date >= SYSDATE - duration` bound."""
    for predicate in root.find_all(exp.GTE):
        if not isinstance(predicate.this, exp.Column) or not re.search(
            r"(?:_DATE$|^DATE_)", predicate.this.name.upper()
        ):
            continue
        if _has_sysdate_offset(predicate, duration, params):
            return True
    return False


def _has_sysdate_offset(
    predicate: exp.Expression, expected: int, params: dict[str, Any]
) -> bool:
    """Match a date comparison to a literal or bound SYSDATE offset."""
    if not isinstance(predicate.this, exp.Column) or not re.search(
        r"(?:_DATE$|^DATE_)", predicate.this.name.upper()
    ):
        return False
    expression = predicate.expression
    if not (
        isinstance(expression, exp.Sub)
        and isinstance(expression.this, exp.CurrentTimestamp)
        and expression.this.args.get("sysdate")
    ):
        return False
    offset = expression.expression
    if isinstance(offset, exp.Literal) and not offset.is_string:
        try:
            return int(offset.this) == expected
        except (TypeError, ValueError):
            return False
    if isinstance(offset, exp.Placeholder):
        return params.get(offset.name) == expected
    return False
