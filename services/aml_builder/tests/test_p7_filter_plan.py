"""Plan rendering coverage for P7 resolved filters."""

from services.aml_builder.web.services.plan_renderer import render_plan


def test_plan_lists_resolved_filters_and_customer_baseline_semantics():
    rendered = render_plan(
        {
            "scenario_name": "Customer deposits",
            "scenario_type": "CUSTOMER",
            "detection_logic": "Flag activity",
            "thresholds": [
                {
                    "field": "transaction_amount",
                    "operator": ">",
                    "value_from": 1000,
                    "target_scope": "DETAIL",
                }
            ],
            "resolved_filters": [
                {
                    "dimension": "customer_profession",
                    "level": "customer",
                    "target": {"table": "CUSTOMERS", "column": "PROFESSION"},
                    "values": ["ACCOUNTANT"],
                    "polarity": "positive",
                    "status": "resolved",
                }
            ],
        }
    )

    assert "## Resolved Filters" in rendered["plan_artifact"]
    assert "CUSTOMERS.PROFESSION" in rendered["plan_artifact"]
    assert "baseline population" in rendered["plan_artifact"]
    assert {"condition": "Resolved Filters"} in rendered["plan_conditions"]


def test_customer_attribute_threshold_does_not_get_transaction_baseline_note():
    rendered = render_plan(
        {
            "scenario_name": "Young customers",
            "scenario_type": "CUSTOMER",
            "detection_logic": "Age threshold",
            "thresholds": [
                {
                    "field": "customer_age",
                    "operator": "<",
                    "value_from": 21,
                    "target_scope": "DETAIL",
                }
            ],
            "resolved_filters": [
                {
                    "dimension": "customer_profession",
                    "level": "customer",
                    "values": ["ACCOUNTANT"],
                    "polarity": "positive",
                    "status": "resolved",
                }
            ],
        }
    )

    assert "baseline population" not in rendered["plan_artifact"]
