"""Offline tests for the versioned AML semantic intent contract."""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from services.aml_builder.web.services.plan_renderer import build_plan_markdown
from services.aml_builder.web.services.schemas import AMLIntent
from services.aml_builder.web.services.tools import _bind_channels_and_codes_to_intent


def complex_intent() -> dict:
    """Return a ratio scenario with explicit populations and time boundaries."""
    grain = {"entity": "CUSTOMER", "keys": ["CUS_NUM"], "period": "ROLLING_WINDOW"}
    current_window = {
        "window_id": "current_30d",
        "purpose": "OBSERVATION",
        "window_type": "ROLLING",
        "unit": "DAYS",
        "value": 30,
        "offset_value": 0,
        "anchor": "EVALUATION_DATE",
        "lower_inclusive": True,
        "upper_inclusive": False,
        "boundary_precision": "CALENDAR_DAY",
    }
    baseline_window = {
        "window_id": "baseline_180d",
        "purpose": "BASELINE",
        "window_type": "RELATIVE",
        "unit": "DAYS",
        "value": 180,
        "offset_value": 30,
        "offset_unit": "DAYS",
        "anchor": "EVALUATION_DATE",
        "lower_inclusive": True,
        "upper_inclusive": False,
        "boundary_precision": "CALENDAR_DAY",
    }
    numerator_filter = {
        "predicate_id": "high_value_only",
        "subject": "TRANSACTION",
        "field": "transaction_amount",
        "operator": ">=",
        "value_from": 10000,
        "evaluation_phase": "RECORD",
        "applies_to": "POPULATION",
        "population_ids": ["high_value_transfers"],
        "transaction_types": ["OUTWARD TRANSFER"],
        "explanation_codes": ["6"],
    }
    semantic_contract = {
        "contract_version": "1.0",
        "evaluation_grain": grain,
        "output_grain": grain,
        "populations": [
            {
                "population_id": "high_value_transfers",
                "role": "NUMERATOR",
                "entity": "TRANSACTION",
                "description": "High-value outward transfers in the current window",
                "filters": [numerator_filter],
                "transaction_types": ["OUTWARD TRANSFER"],
                "explanation_codes": ["6"],
                "time_window_id": "current_30d",
            },
            {
                "population_id": "all_outward_transfers",
                "role": "DENOMINATOR",
                "entity": "TRANSACTION",
                "description": "All outward transfers in the current window",
                "transaction_types": ["OUTWARD TRANSFER"],
                "explanation_codes": ["6"],
                "time_window_id": "current_30d",
            },
        ],
        "metrics": [
            {
                "metric_id": "high_value_share",
                "name": "Share of outward transfers that are high value",
                "metric_kind": "PERCENTAGE",
                "numerator_population_id": "high_value_transfers",
                "denominator_population_id": "all_outward_transfers",
                "grain": grain,
                "comparison": {"operator": ">=", "value_from": 0.9},
                "zero_denominator_policy": "EXCLUDE",
                "null_measure_policy": "EXCLUDE",
            }
        ],
        "global_filters": [],
        "exclusions": [],
        "time_windows": [current_window, baseline_window],
        "relationships": [
            {
                "relationship_id": "customer_transactions",
                "from_entity": "CUSTOMER",
                "to_entity": "TRANSACTION",
                "relationship": "customer owns transaction",
                "cardinality": "ONE_TO_MANY",
                "required": True,
                "purpose": "Evaluate transfer populations per customer",
            }
        ],
        "evidence": {
            "required": True,
            "output_grain": grain,
            "required_fields": ["CUS_NUM"],
            "include_matching_transactions": True,
            "transaction_fields": ["TRA_SEQ1", "TRA_DATE"],
        },
        "ambiguities": [],
        "unsupported_requirements": [],
    }
    return {
        "scenario_name": "High-value outward transfer share",
        "scenario_type": "CUSTOMER",
        "transaction_type": "OUTWARD TRANSFER",
        "transaction_types": ["OUTWARD TRANSFER"],
        "explanation_codes": ["6"],
        "detection_logic": "Flag customers whose high-value transfers are at least 90% of outward transfers.",
        "thresholds": [
            {
                "field": "high_value_share",
                "operator": ">=",
                "value_from": 0.9,
                "target_scope": "AGGREGATE",
                "transaction_type": "OUTWARD TRANSFER",
                "explanation_codes": ["6"],
            }
        ],
        "time_window": {"unit": "DAYS", "value": 30, "is_rolling": True},
        "semantic_contract": semantic_contract,
    }


def test_complex_semantics_round_trip_without_population_loss() -> None:
    """Numerator and denominator filters remain distinct after validation."""
    intent = AMLIntent.model_validate(complex_intent()).model_dump()
    contract = intent["semantic_contract"]
    numerator, denominator = contract["populations"]
    assert numerator["filters"][0]["field"] == "transaction_amount"
    assert denominator["filters"] == []
    assert contract["metrics"][0]["zero_denominator_policy"] == "EXCLUDE"
    assert contract["time_windows"][0]["upper_inclusive"] is False
    assert contract["evidence"]["transaction_fields"] == ["TRA_SEQ1", "TRA_DATE"]


def test_blocking_semantic_ambiguity_prevents_handoff() -> None:
    """Material ambiguity overrides an incorrectly optimistic ready flag."""
    payload = complex_intent()
    payload["semantic_contract"]["ambiguities"] = [
        {
            "code": "ZERO_DENOMINATOR_POLICY",
            "field_path": "semantic_contract.metrics[0].zero_denominator_policy",
            "why_it_matters": "It changes which customers are returned.",
            "question": "How should customers with no outward transfers be treated?",
            "options": ["Exclude them", "Treat the ratio as zero"],
            "blocking": True,
        }
    ]
    payload["semantic_contract"]["metrics"][0][
        "zero_denominator_policy"
    ] = "NEEDS_USER"
    intent = AMLIntent.model_validate(payload)
    assert intent.ready_for_handoff is False
    assert intent.clarification_needed is True


def test_legacy_intent_remains_valid() -> None:
    """Existing intent payloads do not need the new nested contract."""
    payload = complex_intent()
    payload.pop("semantic_contract")
    intent = AMLIntent.model_validate(payload)
    assert intent.semantic_contract is None
    assert intent.ready_for_handoff is True


def test_explicit_contract_disables_legacy_equal_value_deduplication() -> None:
    """Equal values on distinct semantic phases are not treated as duplicates."""
    payload = complex_intent()
    payload["aggregation"] = {
        "metric": "transaction_amount",
        "function": "SUM",
        "grain": "PER CUSTOMER",
    }
    payload["thresholds"] = [
        {
            "field": "transaction_amount",
            "operator": ">=",
            "value_from": 100,
            "target_scope": "DETAIL",
        },
        {
            "field": "cumulative_transaction_volume",
            "operator": ">=",
            "value_from": 100,
            "target_scope": "AGGREGATE",
        },
    ]
    intent = AMLIntent.model_validate(payload)
    assert [threshold.target_scope for threshold in intent.thresholds] == [
        "DETAIL",
        "AGGREGATE",
    ]


def test_dangling_population_reference_is_rejected() -> None:
    """Metric population references cannot silently point nowhere."""
    payload = deepcopy(complex_intent())
    payload["semantic_contract"]["metrics"][0][
        "denominator_population_id"
    ] = "missing_population"
    with pytest.raises(ValidationError, match="unknown populations"):
        AMLIntent.model_validate(payload)


def test_plan_renders_semantics_without_prescribing_sql_clauses() -> None:
    """The review plan exposes semantic details without WHERE/HAVING claims."""
    intent = AMLIntent.model_validate(complex_intent()).model_dump()
    plan = build_plan_markdown(intent, None)
    assert "Explicit Semantic Contract" in plan
    assert "high_value_transfers" in plan
    assert "all_outward_transfers" in plan
    assert "EXCLUDE" in plan
    assert "SQL: `WHERE`" not in plan
    assert "SQL: `HAVING`" not in plan


def test_explanation_code_enrichment_preserves_population_scope() -> None:
    """Discovered codes bind only to populations naming the matching type."""
    payload = complex_intent()
    numerator, denominator = payload["semantic_contract"]["populations"]
    numerator["explanation_codes"] = []
    denominator["transaction_types"] = []
    denominator["explanation_codes"] = []
    _bind_channels_and_codes_to_intent(
        payload,
        discovered_codes=[],
        codes_by_type={"OUTWARD TRANSFER": ["6"]},
    )
    assert numerator["explanation_codes"] == ["6"]
    assert denominator["explanation_codes"] == []
