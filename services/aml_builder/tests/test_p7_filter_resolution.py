"""Offline tests for deterministic P7 filter resolution."""

from services.aml_builder.web.domain.filter_resolution import (
    build_filter_prompt_section,
    build_service_b_intent,
    resolve_filter_mentions,
)
from services.aml_builder.web.services.schemas import AMLIntent


def _catalog():
    return {
        "dimensions": {
            "explanation_code": {
                "label": "Explanation code",
                "level": "transaction",
                "target": {"table": "PIO_TRANSACTIONS", "column": "EXPL_CODE"},
                "resolver": "semantic",
                "value_source": "existing_explanation_code_catalog",
                "match": "any_of",
                "polarity_allowed": False,
                "required": False,
                "status": "confirmed",
                "resolver_config": {
                    "match_threshold": 0.5,
                    "ambiguity_margin": 0.05,
                    "max_options": 5,
                },
            },
            "account_status": {
                "label": "Account status",
                "level": "account",
                "target": {"table": "TBD", "column": "TBD"},
                "resolver": "lookup",
                "value_source": "TBD",
                "match": "any_of",
                "polarity_allowed": True,
                "required": False,
                "status": "needs_advisor_input",
            },
        }
    }


def test_semantic_resolver_returns_deterministic_candidates_and_confidence():
    rows = resolve_filter_mentions(
        [
            {
                "dimension": "explanation_code",
                "raw_mention": "cash deposit",
                "polarity": "positive",
            }
        ],
        _catalog(),
        candidate_provider=lambda *_: [
            {"value": "110", "label": "Cash deposit", "confidence": 0.91},
            {"value": "111", "label": "Cash deposit reversal", "confidence": 0.73},
        ],
    )

    assert rows[0]["status"] == "resolved"
    assert rows[0]["values"] == ["110"]
    assert rows[0]["options"][0]["confidence"] == 0.91


def test_close_candidates_are_ambiguous_and_ordered_by_confidence_then_value():
    rows = resolve_filter_mentions(
        [
            {
                "dimension": "explanation_code",
                "raw_mention": "cash activity",
                "polarity": "positive",
            }
        ],
        _catalog(),
        candidate_provider=lambda *_: [
            {"value": "111", "label": "second", "confidence": 0.81},
            {"value": "110", "label": "first", "confidence": 0.83},
        ],
    )

    assert rows[0]["status"] == "ambiguous"
    assert [item["value"] for item in rows[0]["options"]] == ["110", "111"]


def test_lookup_failure_is_explicit_and_never_treated_as_empty_success():
    rows = resolve_filter_mentions(
        [
            {
                "dimension": "explanation_code",
                "raw_mention": "cash deposit",
                "polarity": "positive",
            }
        ],
        _catalog(),
        candidate_provider=lambda *_: (_ for _ in ()).throw(RuntimeError("offline")),
    )

    assert rows[0]["status"] == "lookup_failed"
    assert rows[0]["options"] == []


def test_lookup_uses_normalized_label_matching_and_enum_uses_static_options():
    catalog = _catalog()
    catalog["dimensions"]["account_status"].update(
        status="confirmed",
        value_source="test_lookup",
        resolver_config={
            "match_threshold": 0.9,
            "ambiguity_margin": 0.05,
            "max_options": 5,
        },
    )
    catalog["dimensions"]["account_status"]["target"] = {
        "table": "ACCOUNTS",
        "column": "STATUS",
    }
    lookup = resolve_filter_mentions(
        [
            {
                "dimension": "account_status",
                "raw_mention": "ACTIVE  ",
                "polarity": "positive",
            }
        ],
        catalog,
        candidate_provider=lambda *_: [{"value": "A", "label": "active"}],
    )
    assert lookup[0]["status"] == "resolved"
    assert lookup[0]["values"] == ["A"]

    enum_dimension = {
        "label": "Payment method",
        "level": "transaction",
        "target": {"table": "TX", "column": "METHOD"},
        "resolver": "enum",
        "value_source": "static",
        "match": "any_of",
        "polarity_allowed": False,
        "required": False,
        "resolver_config": {"options": ["cash", "wire"]},
        "status": "confirmed",
    }
    catalog["dimensions"]["payment_method"] = enum_dimension
    enum = resolve_filter_mentions(
        [
            {
                "dimension": "payment_method",
                "raw_mention": "WIRE",
                "polarity": "positive",
            }
        ],
        catalog,
    )
    assert enum[0]["status"] == "resolved"
    assert enum[0]["values"] == ["wire"]


def test_disabled_and_unknown_dimensions_become_blocking_rows():
    rows = resolve_filter_mentions(
        [
            {
                "dimension": "account_status",
                "raw_mention": "active",
                "polarity": "positive",
            },
            {
                "dimension": "made_up",
                "raw_mention": "special class",
                "polarity": "positive",
            },
        ],
        _catalog(),
    )

    assert [row["status"] for row in rows] == ["disabled", "disabled"]
    assert all(row["options"] == ["remove", "proceed_without_filter"] for row in rows)


def test_intent_schema_preserves_unknown_filter_dimensions_for_card_review():
    intent = AMLIntent.model_validate(
        {
            "scenario_name": "Review unsupported filter",
            "scenario_type": "CUSTOMER",
            "detection_logic": "Flag unusual activity",
            "filters": [
                {
                    "dimension": "unknown_dimension",
                    "raw_mention": "special class",
                    "polarity": "positive",
                }
            ],
        }
    )

    assert intent.filters[0].dimension == "unknown_dimension"


def test_prompt_lists_disabled_dimensions_without_physical_columns():
    prompt = build_filter_prompt_section(_catalog())

    assert "account_status: Account status (not yet supported)" in prompt
    assert "PIO_TRANSACTIONS" not in prompt


def test_service_b_payload_adds_only_resolved_filters_and_catalog_instructions():
    intent = {
        "explanation_codes": ["110"],
        "resolved_filters": [
            {
                "dimension": "explanation_code",
                "raw_mention": "cash deposit",
                "polarity": "positive",
                "status": "resolved",
                "values": ["110"],
            },
            {"dimension": "account_status", "status": "disabled", "values": []},
        ],
    }
    output = build_service_b_intent(intent, _catalog())

    assert output["explanation_codes"] == ["110"]
    assert "filters" not in output
    assert [item["dimension"] for item in output["resolved_filters"]] == [
        "explanation_code"
    ]
    assert output["resolved_filters"][0]["target"] == {
        "table": "PIO_TRANSACTIONS",
        "column": "EXPL_CODE",
    }
    assert "PIO_TRANSACTIONS.EXPL_CODE" in output["filter_instructions"]
