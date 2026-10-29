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
    _derive_temporal_binds(request, root, bind_value, params)

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
            if not target_columns:
                continue
            if not all(isinstance(node, exp.Placeholder) for node in values):
                raise SqlPolicyError(
                    "Confirmed filter values must use approved bind placeholders"
                )
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
            [*root.find_all(exp.Where), *root.find_all(exp.Having)]
            if threshold.target_scope == "DETAIL"
            else root.find_all(exp.Having)
        )
        matched = False
        for clause in clauses:
            if threshold.target_scope == "DETAIL" and isinstance(clause, exp.Having):
                # A detail predicate inside an arbitrary aggregate/HAVING tree
                # is accepted only by the exact existential MAX(CASE ...) rule.
                pass
            else:
                for predicate in clause.find_all(predicate_type):
                    sides = (predicate.this, predicate.expression)
                    field_sides = [
                        index for index, node in enumerate(sides)
                        if _threshold_field_matches(node, field_name)
                    ]
                    if not field_sides:
                        continue
                    bind_sides = [
                        sides[1 - index] for index in field_sides
                        if isinstance(sides[1 - index], exp.Placeholder)
                    ]
                    if len(bind_sides) != 1 or len(field_sides) != 1:
                        raise SqlPolicyError(
                            "Threshold values must use one approved bind placeholder"
                        )
                    bind_value(bind_sides[0], expected)
                    matched = True
            if threshold.target_scope == "DETAIL":
                for predicate in clause.find_all(exp.EQ):
                    if not isinstance(predicate.expression, exp.Literal):
                        continue
                    if predicate.expression.is_string or predicate.expression.this != "1":
                        continue
                    maximum = predicate.this
                    if not isinstance(maximum, exp.Max) or not isinstance(maximum.this, exp.Case):
                        continue
                    case = maximum.this
                    branches = case.args.get("ifs") or []
                    default = case.args.get("default")
                    if (
                        len(branches) != 1
                        or not isinstance(default, exp.Literal)
                        or default.is_string
                        or default.this != "0"
                    ):
                        continue
                    branch = branches[0]
                    result = branch.args.get("true")
                    if not isinstance(result, exp.Literal) or result.is_string or result.this != "1":
                        continue
                    condition = _unwrap_parens(branch.this)
                    if (
                        not isinstance(condition, predicate_type)
                        or not _threshold_field_matches(condition, field_name)
                    ):
                        continue
                    placeholders = [
                        node for node in (condition.this, condition.expression)
                        if isinstance(node, exp.Placeholder)
                    ]
                    if len(placeholders) == 1:
                        bind_value(placeholders[0], expected)
                        matched = True
        if not matched:
            raise SqlPolicyError(
                "A threshold predicate is missing or differs from intent"
            )


def _threshold_field_matches(node: exp.Expression, field_name: str) -> bool:
    """Match a threshold to its source column or explicit COUNT aggregate."""
    normalized = re.sub(r"\s+", "", field_name.upper())
    counts = [node] if isinstance(node, exp.Count) else []
    counts.extend(node.find_all(exp.Count))
    if normalized in {"COUNT(*)", "COUNT(1)"}:
        return any(
            isinstance(item, exp.Count)
            and not item.args.get("distinct")
            and (
                isinstance(item.this, exp.Star)
                or isinstance(item.this, exp.Literal)
                and item.this.this == "1"
            )
            for item in counts
        )
    if normalized.startswith("COUNT(DISTINCT") and normalized.endswith(")"):
        column_name = normalized[len("COUNT(DISTINCT"):-1]
        return any(
            isinstance(item, exp.Count)
            and (
                bool(item.args.get("distinct"))
                or isinstance(item.this, exp.Distinct)
            )
            and any(
                isinstance(column, exp.Column) and column.name.upper() == column_name
                for column in (
                    item.this.find_all(exp.Column)
                    if isinstance(item.this, exp.Distinct)
                    else item.find_all(exp.Column)
                )
            )
            for item in counts
        )
    return any(
        isinstance(column, exp.Column) and column.name.upper() == normalized
        for column in node.find_all(exp.Column)
    )


def _derive_temporal_binds(
    request: ScenarioSqlRequest,
    root: exp.Expression,
    bind_value: Any,
    params: dict[str, Any],
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
        baseline = request.intent.baseline_window
        if baseline.unit != "DAYS" or not baseline.exclude_current_window:
            raise SqlPolicyError("Only non-overlapping day baselines are supported")

        def bind_offset(operator: type[exp.Expression], expected: int) -> bool:
            for predicate in root.find_all(operator):
                if not isinstance(predicate.this, exp.Column):
                    continue
                if predicate.this.name.upper() not in {
                    node.name.upper() for node in date_columns
                }:
                    continue
                expression = _unwrap_parens(predicate.expression)
                if not (
                    isinstance(expression, exp.Sub)
                    and isinstance(expression.this, exp.CurrentTimestamp)
                    and expression.this.args.get("sysdate")
                ):
                    continue
                offset_expression = _unwrap_parens(expression.expression)
                if isinstance(offset_expression, exp.Add):
                    operands = [
                        _unwrap_parens(offset_expression.this),
                        _unwrap_parens(offset_expression.expression),
                    ]
                    if not all(isinstance(node, exp.Placeholder) for node in operands):
                        continue
                    known_values = [params.get(node.name) for node in operands]
                    unknown = [
                        node for node, value in zip(operands, known_values)
                        if value is None
                    ]
                    known_total = sum(
                        value for value in known_values
                        if isinstance(value, (int, float))
                    )
                    if not unknown and known_total == expected:
                        return True
                    if (
                        len(unknown) == 2
                        and expected == window.value + baseline.duration
                    ):
                        bind_value(operands[0], window.value)
                        bind_value(operands[1], baseline.duration)
                        return True
                    if len(unknown) != 1 or known_total >= expected:
                        continue
                    bind_value(unknown[0], expected - known_total)
                    return True
                if not isinstance(offset_expression, exp.Placeholder):
                    continue
                placeholder = offset_expression
                existing = params.get(placeholder.name)
                if existing is not None:
                    if existing == expected:
                        return True
                    continue
                bind_value(placeholder, expected)
                return True
            return False

        if not bind_offset(exp.GTE, window.value):
            raise SqlPolicyError("Recent baseline-comparison window is missing")
        if not bind_offset(exp.GTE, baseline.duration + baseline.offset_days):
            raise SqlPolicyError("Prior baseline-comparison window is missing")
        if not bind_offset(exp.LT, baseline.offset_days):
            raise SqlPolicyError("Prior baseline upper boundary is missing")
        return

    for predicate in root.find_all(exp.GTE):
        if not isinstance(predicate.this, exp.Column):
            continue
        if predicate.this.name.upper() not in {
            node.name.upper() for node in date_columns
        }:
            continue
        expression = _unwrap_parens(predicate.expression)
        if (
            isinstance(expression, exp.Sub)
            and isinstance(expression.this, exp.CurrentTimestamp)
            and expression.this.args.get("sysdate")
        ):
            bind_value(_unwrap_parens(expression.expression), window.value)


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
        findings.extend(_customer_grain_findings(request, root))
        findings.extend(_aggregate_projection_findings(request, root))
    return findings


def _customer_grain_findings(
    request: ScenarioSqlRequest, root: exp.Expression
) -> list[str]:
    """Reject transaction-level projections from customer scenario results."""
    if request.intent.scenario_type.upper() != "CUSTOMER":
        return []
    select = root if isinstance(root, exp.Select) else next(root.find_all(exp.Select), None)
    if select is None:
        return ["Customer scenario requires a customer-grain SELECT result"]
    detail_columns: list[str] = []
    for projection in select.expressions:
        expression = projection.this if isinstance(projection, exp.Alias) else projection
        if isinstance(expression, exp.Column) and expression.name.upper() == "CUS_NUM":
            continue
        if any(
            isinstance(node, (exp.Sum, exp.Count, exp.Avg, exp.Min, exp.Max))
            for node in [expression, *expression.walk()]
        ):
            continue
        detail_columns.append(projection.alias_or_name)
    if not detail_columns:
        return []
    group = select.args.get("group")
    group_expressions = list(group.expressions) if group is not None else []
    if (
        len(group_expressions) == 1
        and isinstance(group_expressions[0], exp.Column)
        and group_expressions[0].name.upper() == "CUS_NUM"
    ):
        return ["Customer-grain results cannot project transaction-level columns"]
    nested_selects = [
        item.this for item in root.find_all(exp.Subquery)
        if isinstance(item.this, exp.Select)
    ]
    nested_selects.extend(
        item.this for item in root.find_all(exp.CTE)
        if isinstance(item.this, exp.Select)
    )
    if nested_selects and all(
        (group := nested.args.get("group")) is not None
        and len(group.expressions) == 1
        and isinstance(group.expressions[0], exp.Column)
        and group.expressions[0].name.upper() == "CUS_NUM"
        for nested in nested_selects
    ):
        return []
    return ["Customer-grain results cannot project transaction-level columns"]


def _aggregate_projection_findings(
    request: ScenarioSqlRequest, root: exp.Expression
) -> list[str]:
    """Require customer-grain aggregate evidence implied by confirmed intent.

    Aggregate thresholds and explicit per-customer aggregation are result
    contracts, not merely HAVING filters: each measure must be returned with
    an explicit evidence alias alongside CUS_NUM.
    """
    expected: list[tuple[type[exp.Expression], str, str | None]] = []
    for threshold in request.intent.thresholds:
        if threshold.target_scope != "AGGREGATE":
            continue
        field = re.sub(r"\s+", "", str(threshold.field or "").upper())
        if field in {"COUNT(*)", "COUNT(1)"}:
            expected.append((exp.Count, "COUNT", None))
        elif field.startswith("COUNT(DISTINCT") and field.endswith(")"):
            expected.append((exp.Count, "COUNT_DISTINCT", field[len("COUNT(DISTINCT"):-1]))
        elif field in {"TRA_AMT", "SUM(TRA_AMT)"}:
            expected.append((exp.Sum, "SUM", "TRA_AMT"))
        elif field in {"AVG(TRA_AMT)"}:
            expected.append((exp.Avg, "AVG", "TRA_AMT"))
        else:
            return ["Aggregate threshold has no verified result evidence mapping"]

    aggregation = request.intent.aggregation
    comparison_count = False
    if aggregation and str(aggregation.grain or "").casefold() == "per customer":
        function = str(aggregation.function or "").upper().replace(" ", "_")
        metric = str(aggregation.metric or "").casefold()
        if function == "AVG" and "amount" in metric:
            expected.append((exp.Avg, "AVG", "TRA_AMT"))
        elif function in {"COUNT", "COUNT_DISTINCT"} and "date" in metric:
            expected.append((exp.Count, "COUNT_DISTINCT", "TRA_DATE"))
        elif function == "COUNT" and metric == "transaction count comparison" and request.intent.baseline_window:
            expected.append((exp.Sum, "CONDITIONAL_COUNT", "TRA_DATE"))
            comparison_count = True
        elif function == "COUNT":
            expected.append((exp.Count, "COUNT", None))
        elif function == "SUM" and "amount" in metric:
            expected.append((exp.Sum, "SUM", "TRA_AMT"))
        else:
            return ["Per-customer aggregation has no verified result evidence mapping"]

    select = root if isinstance(root, exp.Select) else next(root.find_all(exp.Select), None)
    if not expected:
        if select is None:
            return []
        for projection in select.expressions:
            expression = projection.this if isinstance(projection, exp.Alias) else projection
            if any(
                isinstance(node, (exp.Sum, exp.Count, exp.Avg, exp.Min, exp.Max))
                for node in [expression, *expression.walk()]
            ):
                return ["SQL projects an aggregate measure not requested by intent"]
        return []
    if select is None:
        return ["Aggregate intent requires an explicit customer-grain measure projection"]
    group = select.args.get("group")
    group_expressions = list(group.expressions) if group is not None else []
    if (
        len(group_expressions) != 1
        or not isinstance(group_expressions[0], exp.Column)
        or group_expressions[0].name.upper() != "CUS_NUM"
    ):
        return ["Aggregate intent requires grouping at CUS_NUM granularity"]
    projections = [item for item in select.expressions if isinstance(item, exp.Alias)]

    def matches(projection: exp.Expression, kind: str, column: str | None) -> bool:
        expression = projection.this if isinstance(projection, exp.Alias) else projection
        if kind == "CONDITIONAL_COUNT":
            return _is_conditional_count_expression(expression, column)
        if kind == "COUNT":
            return isinstance(expression, exp.Count) and not expression.args.get("distinct") and (
                isinstance(expression.this, exp.Star)
                or isinstance(expression.this, exp.Literal) and expression.this.this == "1"
            )
        if kind == "COUNT_DISTINCT":
            return isinstance(expression, exp.Count) and (
                bool(expression.args.get("distinct")) or isinstance(expression.this, exp.Distinct)
            ) and any(node.name.upper() == column for node in expression.find_all(exp.Column))
        return isinstance(expression, expected_type(kind)) and any(
            node.name.upper() == column for node in expression.find_all(exp.Column)
        )

    def expected_type(kind: str) -> type[exp.Expression]:
        return {"SUM": exp.Sum, "AVG": exp.Avg}[kind]

    for _node_type, kind, column in expected:
        if not any(matches(item, kind, column) for item in projections):
            label = f"{kind}({column})" if column else "COUNT(*)"
            return [f"Aggregate intent requires an aliased {label} projection"]
    allowances = {key: 1 for key in set((kind, column) for _, kind, column in expected)}
    if comparison_count:
        allowances[("CONDITIONAL_COUNT", "TRA_DATE")] = 2
    for projection in select.expressions:
        expression = projection.this if isinstance(projection, exp.Alias) else projection
        has_aggregate = any(
            isinstance(node, (exp.Sum, exp.Count, exp.Avg, exp.Min, exp.Max))
            for node in [expression, *expression.walk()]
        )
        if not has_aggregate:
            continue
        matched_key = next(
            (
                key for key, count in allowances.items()
                if count > 0 and matches(projection, key[0], key[1])
            ),
            None,
        )
        if not isinstance(projection, exp.Alias) or matched_key is None:
            return ["SQL projects an aggregate measure not requested by intent"]
        allowances[matched_key] -= 1
    if comparison_count:
        qualifying_aliases = {
            item.alias_or_name.casefold()
            for item in projections
            if matches(item, "CONDITIONAL_COUNT", "TRA_DATE")
        }
        if len(qualifying_aliases) < 2:
            return [
                "Transaction count comparison requires aliased current and baseline count measures"
            ]
    return []


def _is_conditional_count_expression(
    expression: exp.Expression, date_column: str | None
) -> bool:
    """Recognize date-conditioned SUM/COUNT CASE expressions as count evidence."""
    if not isinstance(expression, (exp.Sum, exp.Count)) or not isinstance(expression.this, exp.Case):
        return False
    case = expression.this
    branches = case.args.get("ifs") or []
    default = case.args.get("default")
    if len(branches) != 1:
        return False
    branch = branches[0]
    one = branch.args.get("true")
    condition = branch.this
    return bool(
        isinstance(one, exp.Literal)
        and not one.is_string
        and one.this == "1"
        and (
            isinstance(expression, exp.Count) and default is None
            or isinstance(expression, exp.Sum)
            and isinstance(default, exp.Literal)
            and not default.is_string
            and default.this == "0"
        )
        and date_column
        and any(
            isinstance(column, exp.Column) and column.name.upper() == date_column
            for column in condition.find_all(exp.Column)
        )
        and any(condition.find_all(exp.Predicate))
    )


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
        if (
            not intent.baseline_window
            and not anchor_text
            and not _has_rolling_half_open_range(
            root, duration, params
            )
        ):
            findings.append("Rolling day window requires a half-open upper bound at SYSDATE")
    baseline = intent.baseline_window
    if baseline and baseline.exclude_current_window and baseline.unit == "DAYS":
        offset = baseline.offset_days
        start = offset + baseline.duration
        recent_range = _has_rolling_half_open_range(root, window.value, params)
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
        if not recent_range or not lower or not upper:
            findings.append(
                f"Recent and baseline windows must be complete half-open ranges: "
                f"SYSDATE - {window.value} to SYSDATE and SYSDATE - {start} "
                f"to before SYSDATE - {offset}"
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


def _has_rolling_half_open_range(
    root: exp.Expression, duration: int, params: dict[str, Any]
) -> bool:
    """Require matching date-column lower and strict SYSDATE upper bounds."""
    lower_columns = {
        predicate.this.name.upper()
        for predicate in root.find_all(exp.GTE)
        if _has_sysdate_offset(predicate, duration, params)
    }
    upper_columns = {
        predicate.this.name.upper()
        for predicate in root.find_all(exp.LT)
        if isinstance(predicate.this, exp.Column)
        and isinstance(predicate.expression, exp.CurrentTimestamp)
        and predicate.expression.args.get("sysdate")
    }
    return bool(lower_columns & upper_columns)


def _has_sysdate_offset(
    predicate: exp.Expression, expected: int, params: dict[str, Any]
) -> bool:
    """Match a date comparison to a literal or bound SYSDATE offset."""
    if not isinstance(predicate.this, exp.Column) or not re.search(
        r"(?:_DATE$|^DATE_)", predicate.this.name.upper()
    ):
        return False
    expression = _unwrap_parens(predicate.expression)
    if not (
        isinstance(expression, exp.Sub)
        and isinstance(expression.this, exp.CurrentTimestamp)
        and expression.this.args.get("sysdate")
    ):
        return False
    offset = _unwrap_parens(expression.expression)
    if isinstance(offset, exp.Literal) and not offset.is_string:
        try:
            return int(offset.this) == expected
        except (TypeError, ValueError):
            return False
    if isinstance(offset, exp.Placeholder):
        return params.get(offset.name) == expected
    if isinstance(offset, exp.Add):
        operands = [_unwrap_parens(offset.this), _unwrap_parens(offset.expression)]
        return all(isinstance(item, exp.Placeholder) for item in operands) and sum(
            params.get(item.name, -1) for item in operands
        ) == expected
    return False


def _unwrap_parens(node: exp.Expression) -> exp.Expression:
    """Remove syntax-only parentheses around a temporal expression."""
    while isinstance(node, exp.Paren):
        node = node.this
    return node
