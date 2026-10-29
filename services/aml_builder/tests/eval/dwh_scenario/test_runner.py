"""Tests for case loading and the pluggable structural evaluator."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import sqlglot
from sqlglot import exp

from .runner import (
    evaluate,
    load_cases,
    semantic_pair_matches,
    semantic_signature,
    stub_generator,
)
from services.aml_builder.web.domain.dwh_scenario.contracts import ScenarioSqlRequest
from services.aml_builder.web.domain.dwh_scenario.validation import (
    derive_candidate_bind_values,
)
from services.aml_builder.web.domain.dwh_scenario.sql_policy import (
    SqlPolicyError,
    validate_oracle_read,
)


def test_synthetic_case_set_and_negative_safety_cases() -> None:
    """Load the bundled cases and confirm safety checks detect negative fixtures.

    Args:
        None.

    Returns:
        None.

    Raises:
        AssertionError: If case coverage or negative safety scoring regresses.
    """
    cases = load_cases(Path(__file__).with_name("cases.jsonl"))
    assert 20 <= len(cases) <= 30
    assert all(case["source"] == "synthetic" for case in cases)
    results = evaluate(cases, stub_generator)
    write_case = next(result for result in results if result["case"] == "Negative write SQL request")
    filter_case = next(result for result in results if result["case"] == "Missing confirmed explanation code filter")
    assert write_case["select_only"] is False
    assert write_case["forbidden_constructs_absent"] is False
    assert filter_case["confirmed_filters_present"] is False


def test_exact_match_metric_is_optional() -> None:
    """Report exact match only when a case supplies expected SQL.

    Args:
        None.

    Returns:
        None.

    Raises:
        AssertionError: If expected SQL is matched or emitted when absent.
    """
    base: dict[str, Any] = {
        "scenario_description": "Stub", "intent": {}, "expected_constraints": {}, "source": "synthetic"
    }
    result = evaluate([base], stub_generator)[0]
    assert "exact_match" not in result
    with_expected = {**base, "expected_sql": "SELECT CUS_NUM FROM BI_DWH.PIO_TRANSACTIONS"}
    assert evaluate([with_expected], stub_generator)[0]["exact_match"] is True


def test_b9_broad_synthetic_pairs_pass_deterministic_semantic_checks() -> None:
    """Validate grounded offline A/B SQL pairs using the shared AST policy."""
    cases = [
        json.loads(line)
        for line in Path(__file__).with_name("b9_cases.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert 10 <= len(cases) <= 15
    assert len({case["category"] for case in cases}) == 12
    for case in cases:
        candidates = []
        request = ScenarioSqlRequest(
            intent=case["intent"], confirmed_filters=case["confirmed_filters"]
        )
        for key in ("sql_a", "sql_b"):
            validated = validate_oracle_read(
                case[key], params=case["params"], allowed_schemas=("BI_DWH",),
                allowed_objects=("BI_DWH.PIO_TRANSACTIONS",),
                required_projection="CUS_NUM", confirmed_filters=case["filters"],
            )
            assert all(name in case["params"] for name in validated.binds)
            tree = sqlglot.parse_one(case[key], read="oracle")
            outer = tree if isinstance(tree, exp.Select) else None
            assert outer is not None
            aliases = {item.alias_or_name.upper() for item in outer.expressions}
            assert "CUS_NUM" in aliases
            if case["shape"] == "customer_with_aggregate":
                assert outer.args.get("group") is not None
                assert any(
                    list(outer.find_all(kind))
                    for kind in (exp.Sum, exp.Count, exp.Avg)
                )
            if case["shape"] == "customer_empty_is_valid":
                assert outer.args.get("where") is not None
            candidates.append(case[key])
        assert semantic_pair_matches(
            candidates[0], derive_candidate_bind_values(request, candidates[0]),
            candidates[1], derive_candidate_bind_values(request, candidates[1]),
        ), case["scenario"]


def test_b9_semantic_checks_detect_inverted_missing_and_extra_predicates() -> None:
    """Ensure confirmed-filter, projection and semantic differences fail closed."""
    valid = "SELECT CUS_NUM FROM BI_DWH.PIO_TRANSACTIONS WHERE EXPL_CODE = :code"
    inverted = "SELECT CUS_NUM FROM BI_DWH.PIO_TRANSACTIONS WHERE EXPL_CODE <> :code"
    extra = valid + " AND TRA_AMT > :amount"
    filter_contract = [{"target": {"table": "PIO_TRANSACTIONS", "column": "EXPL_CODE"}, "values": ["4"]}]
    validate_oracle_read(
        valid, params={"code": "4"}, allowed_schemas=("BI_DWH",),
        allowed_objects=("BI_DWH.PIO_TRANSACTIONS",), required_projection="CUS_NUM",
        confirmed_filters=filter_contract,
    )
    for candidate, params in ((inverted, {"code": "4"}),
                              ("SELECT TRA_DATE FROM BI_DWH.PIO_TRANSACTIONS", {})):
        try:
            validate_oracle_read(
                candidate, params=params, allowed_schemas=("BI_DWH",),
                allowed_objects=("BI_DWH.PIO_TRANSACTIONS",), required_projection="CUS_NUM",
                confirmed_filters=filter_contract,
            )
        except SqlPolicyError:
            continue
        raise AssertionError("Semantic SQL defect unexpectedly passed policy")
    assert semantic_signature(valid) != semantic_signature(inverted)
    assert semantic_signature(valid) != semantic_signature(extra)


def test_semantic_signature_ignores_alias_labels_but_compares_having_and_shape() -> None:
    first = (
        "SELECT CUS_NUM, SUM(TRA_AMT) AS TOTAL_AMOUNT "
        "FROM BI_DWH.PIO_TRANSACTIONS GROUP BY CUS_NUM HAVING SUM(TRA_AMT) >= :cs_b1"
    )
    renamed = (
        "SELECT t.CUS_NUM, SUM(t.TRA_AMT) AS AMT_SUM "
        "FROM BI_DWH.PIO_TRANSACTIONS t GROUP BY t.CUS_NUM "
        "HAVING SUM(t.TRA_AMT) >= :cs_b1"
    )
    changed_having = renamed.replace(":cs_b1", ":cs_b2")
    extra_projection = renamed.replace("AS AMT_SUM", "AS AMT_SUM") + ""
    extra_projection = extra_projection.replace(
        "SELECT t.CUS_NUM, SUM(t.TRA_AMT) AS AMT_SUM",
        "SELECT t.CUS_NUM, SUM(t.TRA_AMT) AS AMT_SUM, COUNT(*) AS TXN_COUNT",
    )
    assert semantic_signature(first) == semantic_signature(renamed)
    assert semantic_signature(first) != semantic_signature(changed_having)
    assert semantic_signature(first) != semantic_signature(extra_projection)
    different_bind_names = first.replace(":cs_b1", ":cs_b2").replace("TOTAL_AMOUNT", "AMOUNT")
    assert semantic_signature(first, {"cs_b1": 50000}) == semantic_signature(
        different_bind_names, {"cs_b2": 50000}
    )
    assert semantic_signature(first, {"cs_b1": 50000}) != semantic_signature(
        different_bind_names, {"cs_b2": 60000}
    )


def test_semantic_signature_normalizes_only_outer_sysdate_offset_parentheses() -> None:
    """Ignore harmless date-boundary parentheses but retain arithmetic roles."""
    plain = (
        "SELECT CUS_NUM FROM BI_DWH.PIO_TRANSACTIONS "
        "WHERE TRA_DATE >= SYSDATE - :cs_b1 AND TRA_DATE < SYSDATE"
    )
    parenthesized = (
        "SELECT CUS_NUM FROM BI_DWH.PIO_TRANSACTIONS "
        "WHERE TRA_DATE >= (SYSDATE - :cs_b2) AND TRA_DATE < SYSDATE"
    )
    assert semantic_signature(plain, {"cs_b1": 14}) == semantic_signature(
        parenthesized, {"cs_b2": 14}
    )

    sum_offset = (
        "SELECT CUS_NUM FROM BI_DWH.PIO_TRANSACTIONS "
        "WHERE TRA_DATE >= SYSDATE - (:current_days + :baseline_days)"
    )
    different_arithmetic = (
        "SELECT CUS_NUM FROM BI_DWH.PIO_TRANSACTIONS "
        "WHERE TRA_DATE >= SYSDATE - :current_days + :baseline_days"
    )
    assert semantic_signature(
        sum_offset, {"current_days": 30, "baseline_days": 30}
    ) != semantic_signature(
        different_arithmetic, {"current_days": 30, "baseline_days": 30}
    )
