"""Tests for case loading and the pluggable structural evaluator."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .runner import evaluate, load_cases, stub_generator


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
