"""P6 parameter catalog, gap detection, and confirmed-intent tests."""

from __future__ import annotations

from copy import deepcopy

import pytest

from services.aml_builder.web.domain.param_gaps import (
    apply_confirmed_params,
    build_param_rows,
    condition_matches,
    ready_for_handoff,
)


def _catalog() -> dict:
    return {
        "status": "demo",
        "skip_card_when_complete": True,
        "card": {"order": ["needs_input", "assumed_defaults"]},
        "ambiguity": {"block_on_optional": False},
        "params": {
            "scenario_type": {"label": "Scenario type", "required": True, "input": "enum", "options": ["CUSTOMER"], "default": None, "status": "demo", "prompt": "Choose a type."},
            "grain": {"label": "Grain", "required": True, "input": "enum", "options": ["customer"], "default": None, "status": "demo", "prompt": "Choose a grain."},
            "metric": {"label": "Metric", "required": True, "input": "enum", "options": ["transaction_amount"], "default": "transaction_amount", "status": "demo", "prompt": "Choose a metric."},
            "aggregation": {"label": "Aggregation", "required": True, "input": "enum", "options": ["SUM"], "default": "SUM", "status": "demo", "prompt": "Choose an aggregation."},
            "comparator": {"label": "Comparator", "required": True, "input": "enum", "options": [">", "between"], "default": ">", "status": "demo", "prompt": "Choose a comparator."},
            "threshold": {"label": "Threshold", "required": True, "input": "number", "default": None, "status": "demo", "prompt": "Enter a threshold."},
            "window": {"label": "Window", "required": True, "input": "duration", "default": None, "min_days": 1, "max_days": 365, "all_time_allowed": False, "status": "demo", "prompt": "Choose a time window."},
            "window_kind": {"label": "Window kind", "required": True, "input": "enum", "options": ["rolling", "calendar"], "default": "rolling", "status": "demo", "prompt": "Choose a window kind."},
            "transaction_scope": {"label": "Transaction scope", "required": True, "input": "enum", "options": ["specific", "all"], "default": "all", "status": "demo", "prompt": "Choose a scope."},
            "baseline": {"label": "Baseline", "required": False, "input": "duration", "default": None, "required_if": {"param": "relative", "equals": True}, "status": "demo", "prompt": "Choose a baseline."},
            "exclusions": {"label": "Exclusions", "required": False, "input": "text", "default": None, "status": "demo", "prompt": "List exclusions."},
            "expected_alert_range": {"label": "Expected alert range", "required": False, "input": "range", "default": None, "status": "demo", "prompt": "Estimate alert volume."},
        },
    }


def _intent(**overrides: object) -> dict:
    intent = {
        "scenario_name": "High value customers",
        "scenario_type": "CUSTOMER",
        "detection_logic": "Customers with high-value transactions",
        "thresholds": [],
        "time_window": None,
        "clarifications": [{"dimension": "threshold", "why_it_matters": "Changes alert volume.", "question": "What amount is high value?"}],
        "clarification_questions": ["What amount is high value?"],
        "ready_for_handoff": True,
        "applied_defaults": [],
    }
    intent.update(overrides)
    return intent


def test_short_high_value_request_detects_customer_ambiguity_and_missing_window() -> None:
    rows = build_param_rows(_intent(), _catalog())
    by_key = {row["key"]: row for row in rows}
    assert by_key["scenario_type"]["status"] == "provided"
    assert by_key["threshold#1"]["status"] == "ambiguous"
    assert by_key["window"]["status"] == "missing"
    assert ready_for_handoff(rows, _catalog()) is False


def test_model_ready_flag_cannot_clear_code_detected_window_gap() -> None:
    intent = _intent(clarifications=[], clarification_questions=[], ready_for_handoff=True)
    rows = build_param_rows(intent, _catalog())
    assert any(row["key"] == "window" and row["status"] == "missing" for row in rows)
    assert ready_for_handoff(rows, _catalog()) is False


def test_optional_llm_ambiguity_is_visible_but_does_not_block() -> None:
    catalog = _catalog()
    catalog["params"]["exclusions"]["required"] = False
    rows = build_param_rows(
        _intent(time_window={"unit": "DAYS", "value": 30, "is_rolling": True},
                clarifications=[{"dimension": "exclusions", "why_it_matters": "Changes scope.", "question": "Which exclusions?"}],
                clarification_questions=["Which exclusions?"],
                aggregation={"function": "SUM", "grain": "customer"},
                thresholds=[{"field": "transaction_amount", "operator": ">", "value_from": 1000, "target_scope": "AGGREGATE"}]),
        catalog,
    )
    optional = next(row for row in rows if row["key"] == "exclusions")
    assert optional["status"] == "ambiguous"
    assert ready_for_handoff(rows, catalog) is True


def test_repeated_thresholds_are_indexed_and_uneditable_structures_are_read_only() -> None:
    intent = _intent(
        time_window={"unit": "DAYS", "value": 30, "is_rolling": True},
        clarifications=[], clarification_questions=[],
        thresholds=[
            {"field": "transaction_amount", "operator": ">", "value_from": 5000, "target_scope": "DETAIL", "transaction_type": "CASH"},
            {"field": "transaction_count", "operator": ">=", "value_from": 4, "target_scope": "AGGREGATE"},
        ],
        baseline_window={"unit": "MONTHS", "value": 6},
        semantic_conditions=[{"text": "cash only"}],
    )
    rows = build_param_rows(intent, _catalog())
    by_key = {row["key"]: row for row in rows}
    assert by_key["threshold#1"]["value"] == {"comparator": ">", "value": 5000, "value_to": None, "scope": "DETAIL", "transaction_type": "CASH"}
    assert by_key["threshold#2"]["value"]["value"] == 4
    assert by_key["baseline_window"]["read_only"] is True
    assert by_key["semantic_conditions"]["read_only"] is True
    assert "change via chat" in by_key["semantic_conditions"]["hint"].lower()


@pytest.mark.parametrize(
    ("context", "condition", "expected"),
    [({"relative": True}, {"param": "relative", "equals": True}, True),
     ({"relative": False}, {"param": "relative", "equals": True}, False),
     ({"relative": True}, {"param": "x == True", "equals": True}, False)],
)
def test_required_if_uses_safe_structured_condition(context, condition, expected) -> None:
    assert condition_matches(context, condition) is expected


def test_apply_confirmed_params_sets_provenance_and_clears_clarification() -> None:
    source = _intent(
        thresholds=[{"field": "transaction_amount", "operator": ">", "value_from": None, "target_scope": "AGGREGATE"}]
    )
    result = apply_confirmed_params(
        source,
        {"threshold#1": {"value": 12000, "choice": "user_input"},
         "window": {"value": {"unit": "DAYS", "value": 30, "is_rolling": True}, "choice": "user_input"},
         "scenario_type": {"value": "CUSTOMER", "choice": "accepted_default"}},
    )
    assert result["thresholds"][0]["value_from"] == 12000
    assert result["thresholds"][0]["provenance"] == "user_input"
    assert result["time_window"]["value"] == 30
    assert result["time_window"]["provenance"] == "user_input"
    assert result["clarifications"] == []
    assert result["clarification_questions"] == []
    assert result["ready_for_handoff"] is True
    assert isinstance(result["applied_defaults"], list)


def test_inferred_untouched_value_is_accepted_default_with_inferred_provenance() -> None:
    result = apply_confirmed_params(
        _intent(),
        {"metric": {"value": "transaction_amount", "choice": "accepted_default", "provenance": "inferred"},
         "window": {"value": {"unit": "DAYS", "value": 30, "is_rolling": True}, "choice": "user_input"}},
    )
    assert result["field_provenance"]["metric"] == "inferred"
    assert any("metric" in value for value in result["applied_defaults"])


def test_all_time_and_out_of_range_windows_are_invalid() -> None:
    rows = build_param_rows(
        _intent(time_window={"unit": "DAYS", "value": 500, "is_rolling": True},
                clarifications=[], clarification_questions=[]),
        _catalog(),
    )
    window = next(row for row in rows if row["key"] == "window")
    assert window["validation_errors"]


def test_build_param_rows_does_not_mutate_intent() -> None:
    intent = _intent()
    before = deepcopy(intent)
    build_param_rows(intent, _catalog())
    assert intent == before
