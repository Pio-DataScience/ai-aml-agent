"""Confirmation behavior for filter rows in the existing P6 card flow."""

from services.aml_builder.web.domain.param_gaps import (
    apply_confirmed_params,
    build_param_rows,
    ready_for_handoff,
    validate_confirmed_values,
)
from services.aml_builder.web.domain.hashing import stable_hash


PARAMS = {"params": {}}
FILTERS = {"dimensions": {"explanation_code": {"label": "Explanation code"}}}


def test_filter_rows_block_confirmation_until_resolved_or_explicitly_removed():
    intent = {
        "resolved_filters": [
            {
                "dimension": "explanation_code",
                "raw_mention": "cash deposit",
                "polarity": "positive",
                "level": "transaction",
                "status": "ambiguous",
                "options": [
                    {"value": "110", "label": "Cash deposit", "confidence": 0.9}
                ],
                "values": [],
            }
        ]
    }
    rows = build_param_rows(intent, PARAMS, filters_catalog=FILTERS)

    assert rows[0]["kind"] == "filter"
    assert rows[0]["dimension"] == "explanation_code"
    assert not ready_for_handoff(rows, PARAMS)
    values = {rows[0]["key"]: {"value": ["110"], "choice": "user_input"}}
    assert validate_confirmed_values(rows, values, PARAMS) == ([], [], [])

    removed = {rows[0]["key"]: {"value": None, "choice": "removed"}}
    assert validate_confirmed_values(rows, removed, PARAMS) == ([], [], [])
    confirmed = apply_confirmed_params(intent, removed)
    assert confirmed["resolved_filters"] == []
    assert confirmed["filters"] == []


def test_filter_confirmation_persists_selected_values_and_acknowledged_disabled_filter():
    intent = {
        "resolved_filters": [
            {
                "dimension": "future_dimension",
                "raw_mention": "special class",
                "polarity": "positive",
                "level": "unknown",
                "status": "disabled",
                "options": ["remove", "proceed_without_filter"],
                "values": [],
            }
        ]
    }
    rows = build_param_rows(intent, PARAMS, filters_catalog=FILTERS)
    key = rows[0]["key"]
    values = {key: {"value": "proceed_without_filter", "choice": "acknowledged"}}
    assert validate_confirmed_values(rows, values, PARAMS) == ([], [], [])
    confirmed = apply_confirmed_params(intent, values)
    assert confirmed["resolved_filters"][0]["acknowledged_without_filter"] is True


def test_confirmed_filter_changes_are_part_of_revision_parameter_hash():
    first = apply_confirmed_params(
        {
            "resolved_filters": [
                {
                    "dimension": "explanation_code",
                    "raw_mention": "cash",
                    "status": "ambiguous",
                    "options": [{"value": "110"}],
                    "values": [],
                }
            ]
        },
        {"filter#1": {"value": ["110"], "choice": "user_input"}},
    )
    second = apply_confirmed_params(
        {
            "resolved_filters": [
                {
                    "dimension": "explanation_code",
                    "raw_mention": "cash",
                    "status": "ambiguous",
                    "options": [{"value": "1414"}],
                    "values": [],
                }
            ]
        },
        {"filter#1": {"value": ["1414"], "choice": "user_input"}},
    )

    assert stable_hash(first) != stable_hash(second)
    assert first["filters"][0]["dimension"] == "explanation_code"
    assert first["explanation_codes"] == ["110"]
