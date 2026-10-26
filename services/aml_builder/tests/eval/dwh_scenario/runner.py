"""Score generated SQL against structural scenario constraints."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import sqlglot
from sqlglot import exp


Generator = Callable[[dict[str, Any]], dict[str, Any]]


def load_cases(path: Path) -> list[dict[str, Any]]:
    """Load line-delimited evaluation cases.

    Args:
        path: JSONL file containing one case object per nonempty line.

    Returns:
        Validated JSON case dictionaries.

    Raises:
        ValueError: If a case omits required top-level fields.
    """
    cases = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    required = {"scenario_description", "intent", "expected_constraints", "source"}
    for index, case in enumerate(cases, start=1):
        missing = required - case.keys()
        if missing:
            raise ValueError(f"Case {index} missing fields: {sorted(missing)}")
        if case["source"] not in {"synthetic", "officer"}:
            raise ValueError(f"Case {index} has invalid source {case['source']!r}")
    return cases


def score_case(case: dict[str, Any], generator: Generator) -> dict[str, Any]:
    """Generate and score one case without requiring live services.

    Args:
        case: Scenario description, AMLIntent JSON, and structural expectations.
        generator: Callable returning SQL and optional execution/call metrics.

    Returns:
        Per-case metric record. SQL text is not copied into the report.

    Raises:
        ValueError: If the generator returns a non-string SQL value.
    """
    started = time.perf_counter()
    output = generator(case)
    elapsed_ms = (time.perf_counter() - started) * 1000
    sql = output.get("sql")
    if not isinstance(sql, str):
        raise ValueError("Generator result must include string field 'sql'")
    try:
        parsed = sqlglot.parse(sql, read="oracle")
        tree = parsed[0] if len(parsed) == 1 else None
    except sqlglot.errors.ParseError:
        tree = None
    is_select = isinstance(tree, (exp.Select, exp.With))
    if isinstance(tree, exp.With):
        is_select = isinstance(tree.this, exp.Select)
    constraints = case["expected_constraints"]
    projection = constraints.get("required_projection", "CUS_NUM").upper()
    projection_ok = bool(tree and any(
        isinstance(node, exp.Column) and node.name.upper() == projection
        for node in tree.find_all(exp.Column)
    ))
    sql_upper = sql.upper()
    filters_ok = all(
        item["column"].upper() in sql_upper and str(item.get("value", "")) in sql
        for item in constraints.get("required_filters", [])
    )
    forbidden_ok = not any(token in sql_upper for token in constraints.get("forbidden_constructs", []))
    allowed_tables = {name.upper() for name in constraints.get("allowed_tables", [])}
    tables = {table.name.upper() for table in tree.find_all(exp.Table)} if tree else set()
    tables_ok = not allowed_tables or tables <= allowed_tables
    date_literals_ok = not constraints.get("forbid_date_text_literals") or "LAST MONTH" not in sql_upper
    expected_window = constraints.get("window", "none")
    if expected_window.startswith("rolling_days:"):
        days = expected_window.split(":", 1)[1]
        window_ok = f"TRA_DATE >= SYSDATE - {days}" in sql_upper
    elif expected_window == "must_clarify":
        window_ok = bool(output.get("clarification_requested", False))
    else:
        window_ok = True
    expected_baseline = constraints.get("baseline", "none")
    if expected_baseline.startswith("non_overlapping_days:"):
        _, duration, _, offset = expected_baseline.split(":")
        start = int(duration) + int(offset)
        baseline_ok = (
            f"TRA_DATE >= SYSDATE - {start}" in sql_upper
            and f"TRA_DATE < SYSDATE - {offset}" in sql_upper
        )
    else:
        baseline_ok = True
    parse_ok = tree is not None
    result = {
        "case": case["scenario_description"],
        "source": case["source"],
        "sql_parses": parse_ok,
        "select_only": is_select,
        "projection_present": projection_ok,
        "confirmed_filters_present": filters_ok,
        "window_semantics": window_ok,
        "baseline_semantics": baseline_ok,
        "forbidden_constructs_absent": forbidden_ok,
        "allowed_tables_only": tables_ok,
        "date_literals_clear": date_literals_ok,
        "execution_success": bool(output.get("execution_success", parse_ok and is_select)),
        "latency_ms": round(float(output.get("latency_ms", elapsed_ms)), 3),
        "llm_calls": int(output.get("llm_calls", 0)),
        "tool_calls": int(output.get("tool_calls", 0)),
        "tokens": int(output.get("tokens", 0)),
    }
    if "expected_sql" in case:
        result["exact_match"] = sql.strip().rstrip(";").casefold() == case["expected_sql"].strip().rstrip(";").casefold()
    return result


def evaluate(cases: Iterable[dict[str, Any]], generator: Generator) -> list[dict[str, Any]]:
    """Score an iterable of cases using the provided generator.

    Args:
        cases: Evaluation cases loaded from JSONL or supplied in memory.
        generator: Callable implementation under evaluation.

    Returns:
        One metrics dictionary per case.

    Raises:
        ValueError: If a generator returns malformed output.
    """
    return [score_case(case, generator) for case in cases]


def stub_generator(case: dict[str, Any]) -> dict[str, Any]:
    """Return deterministic candidate SQL for harness smoke checks.

    Args:
        case: Case containing optional `candidate_sql` for negative fixtures.

    Returns:
        Fake generator response with offline execution evidence and counters.

    Raises:
        None.
    """
    if "candidate_sql" in case:
        sql = case["candidate_sql"]
    else:
        filters = case["expected_constraints"].get("required_filters", [])
        where = " AND ".join(
            f"{item['column']} = '{item['value']}'" if "value" in item
            else f"{item['column']} IN ({', '.join(repr(value) for value in item.get('values', []))})"
            for item in filters
        )
        date_window = case["expected_constraints"].get("window", "")
        if date_window.startswith("rolling_days:"):
            days = date_window.split(":", 1)[1]
            where = " AND ".join(part for part in [where, f"TRA_DATE >= SYSDATE - {days}"] if part)
        sql = "SELECT CUS_NUM FROM BI_DWH.PIO_TRANSACTIONS" + (f" WHERE {where}" if where else "")
    return {"sql": sql, "execution_success": sql.lstrip().upper().startswith("SELECT"), "llm_calls": 1, "tool_calls": 1, "tokens": 128}


def main() -> None:
    """Run all bundled synthetic evaluation cases with the stub generator.

    Args:
        None.

    Returns:
        None.

    Raises:
        ValueError: If the case file or generator output is invalid.
    """
    cases = load_cases(Path(__file__).with_name("cases.jsonl"))
    results = evaluate(cases, stub_generator)
    print(json.dumps({"case_count": len(results), "results": results}, indent=2))


if __name__ == "__main__":
    main()
