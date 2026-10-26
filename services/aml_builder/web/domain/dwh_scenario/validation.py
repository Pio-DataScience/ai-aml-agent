"""Pure Service B contract checks expressed against Service A's AML types."""

from __future__ import annotations

import re

import sqlglot
from sqlglot import exp

from services.aml_builder.web.domain.dwh_scenario.contracts import ScenarioSqlRequest


def validate_generated_sql(request: ScenarioSqlRequest, sql: str) -> list[str]:
    """Return structural findings corresponding to Service B's handoff checks.

    Args:
        request: Typed request already validated for handoff.
        sql: Candidate SQL returned by the generator.

    Returns:
        Human-readable validation findings; an empty list means these checks pass.
    """
    findings: list[str] = []
    upper_sql = sql.upper()
    try:
        statements = sqlglot.parse(sql, read="oracle")
        final_select = next(
            (
                statement
                for statement in reversed(statements)
                if isinstance(statement, exp.Select)
            ),
            None,
        )
        projected = (
            {
                projection.alias_or_name.upper()
                for projection in final_select.expressions
            }
            if final_select is not None
            else set()
        )
    except (sqlglot.errors.ParseError, ValueError):
        projected = set()
    if "CUS_NUM" not in projected:
        findings.append("Final SQL must return CUS_NUM for Service A alert creation")
    findings.extend(_transaction_code_findings(request, sql))
    findings.extend(_temporal_findings(request, upper_sql))
    return findings


def _transaction_code_findings(request: ScenarioSqlRequest, sql: str) -> list[str]:
    """Check code requirements without imposing them on all-transaction intents."""
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
    has_code_predicate = bool(re.search(r"\bEXPL_CODE\s*(?:=|IN\b)", sql, re.I))
    any_code_predicate = bool(
        re.search(r"\bEXPL_CODE\s*(?:=|<>|!=|IN\b|NOT\s+IN\b|LIKE\b)", sql, re.I)
    )
    if not explicit_types and not codes:
        return (
            ["SQL invents an EXPL_CODE restriction for an all/no-type intent"]
            if any_code_predicate
            else []
        )
    findings = []
    if not has_code_predicate:
        findings.append(
            "Specified transaction types or codes require an EXPL_CODE filter"
        )
        return findings
    actual = set(re.findall(r"\bEXPL_CODE\s*=\s*'([^']+)'", sql, re.I))
    for values in re.findall(r"\bEXPL_CODE\s+IN\s*\(([^)]*)\)", sql, re.I):
        actual.update(re.findall(r"'([^']+)'", values))
    missing = codes - actual
    if missing:
        findings.append(
            "Explicit explanation codes missing from SQL: " + ", ".join(sorted(missing))
        )
    return findings


def _temporal_findings(request: ScenarioSqlRequest, upper_sql: str) -> list[str]:
    """Check the unambiguous day-based rolling and baseline constraints."""
    intent = request.intent
    findings: list[str] = []
    date_column = r"\b(?:[A-Z][A-Z0-9_]*_DATE|DATE_[A-Z][A-Z0-9_]*)"
    window = intent.time_window
    if window and window.is_rolling and window.unit == "DAYS":
        duration = window.value
        if not re.search(
            rf"{date_column}\s*>=\s*SYSDATE\s*-\s*{duration}\b", upper_sql
        ):
            findings.append(
                f"Rolling {duration}-day window must use a date >= SYSDATE - {duration}"
            )
    baseline = intent.baseline_window
    if baseline and baseline.exclude_current_window and baseline.unit == "DAYS":
        offset = baseline.offset_days
        start = offset + baseline.duration
        lower = re.search(rf"{date_column}\s*>=\s*SYSDATE\s*-\s*{start}\b", upper_sql)
        upper = re.search(rf"{date_column}\s*<\s*SYSDATE\s*-\s*{offset}\b", upper_sql)
        if not lower or not upper:
            findings.append(
                f"Baseline must cover SYSDATE - {start} through before SYSDATE - {offset}"
            )
    return findings
