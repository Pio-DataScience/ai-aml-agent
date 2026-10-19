"""Regression checks for ALL/ANY transaction explanation-code discovery."""

import json
from types import SimpleNamespace

from services.aml_builder.web.services import tools as aml_tools


def test_any_transaction_scope_skips_explanation_code_search(monkeypatch):
    """ALL/ANY must not trigger catalog search or retain discovered code filters."""
    intent_json = {
        "scenario_name": "All transaction activity",
        "scenario_type": "CUSTOMER",
        "transaction_type": "ANY",
        "detection_logic": "Find customers with qualifying transactions.",
        "thresholds": [{
            "field": "transaction_amount",
            "operator": ">=",
            "value_from": 10000,
            "target_scope": "DETAIL",
            "transaction_type": "ANY",
            "explanation_codes": ["91"],
        }],
        "semantic_conditions": [],
        "explanation_codes": None,
        "explanation_codes_by_type": {"ANY": ["91"]},
    }

    class FakeLLM:
        def invoke(self, _messages):
            return SimpleNamespace(content=json.dumps(intent_json))

    def fail_if_code_search_runs(_transaction_types):
        raise AssertionError("ALL/ANY scope must skip explanation-code search")

    monkeypatch.setattr(aml_tools, "build_llm", lambda: FakeLLM())
    monkeypatch.setattr(
        aml_tools, "select_relevant_explanation_codes", fail_if_code_search_runs
    )

    result = json.loads(
        aml_tools.analyze_intent_and_discover_explanation_codes.invoke(
            {"user_prompt": "Customers with at least one transaction of 10K or more."},
            config={"configurable": {"user_id": "999"}},
        )
    )

    enriched = result["payload"]["enriched_intent"]
    assert result["payload"]["discovered_explanation_codes"] == []
    assert result["payload"]["explanation_code_checkpoint"] is None
    assert enriched["explanation_codes"] is None
    assert enriched["explanation_codes_by_type"] is None
    assert enriched["thresholds"][0]["explanation_codes"] is None
