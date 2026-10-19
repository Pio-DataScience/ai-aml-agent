"""Golden characterization tests for the pre-refactor intent normalizer."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict

from services.aml_builder.web.services import tools as aml_tools
from services.aml_builder.web.services._intent_normalization_baseline import (
    normalize_intent_baseline,
)
from services.aml_builder.web.domain.intent_normalize import normalize_intent


FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "p1_intent_normalization_golden.json"
)


def _without_provenance(value: Any) -> Any:
    """Remove provenance additions before comparing with legacy fixtures."""
    if isinstance(value, dict):
        return {
            key: _without_provenance(child)
            for key, child in value.items()
            if key
            not in {"provenance", "field_provenance", "applied_defaults"}
        }
    if isinstance(value, list):
        return [_without_provenance(child) for child in value]
    return value


def test_legacy_normalization_matches_frozen_golden_fixtures() -> None:
    """The domain normalizer preserves legacy outputs apart from provenance."""
    fixtures = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    assert len(fixtures) >= 10
    for fixture in fixtures:
        normalized = normalize_intent(fixture["raw"], fixture["user_prompt"])
        legacy_normalized = normalize_intent_baseline(
            fixture["raw"], fixture["user_prompt"]
        )
        assert (
            _without_provenance(normalized)
            == legacy_normalized
            == fixture["expected"]
        ), fixture["name"]


def test_intent_tool_calls_extracted_baseline_helper(
    monkeypatch: Any,
) -> None:
    """The intent tool delegates parsed output to the extracted helper."""
    parsed_intent: Dict[str, Any] = {"scenario_name": "Tool helper check"}
    helper_calls = []

    class FakeLLM:
        """Return a fixed intent without accessing an external model."""

        def invoke(self, _messages: Any) -> SimpleNamespace:
            return SimpleNamespace(content=json.dumps(parsed_intent))

    def observe_helper(
        raw_intent: Dict[str, Any], user_prompt: str
    ) -> Dict[str, Any]:
        helper_calls.append((raw_intent, user_prompt))
        return normalize_intent_baseline(raw_intent, user_prompt)

    monkeypatch.setattr(aml_tools, "build_llm", lambda: FakeLLM())
    monkeypatch.setattr(aml_tools, "normalize_intent", observe_helper)
    monkeypatch.setattr(aml_tools, "seed_scenario_metadata", lambda *_args: {})

    result = json.loads(
        aml_tools.analyze_intent_and_discover_explanation_codes.invoke(
            {"user_prompt": "Find large deposits"},
            config={"configurable": {"user_id": "999"}},
        )
    )

    assert len(helper_calls) == 1
    assert helper_calls[0][1] == "Find large deposits"
    assert (
        result["payload"]["enriched_intent"]["scenario_name"]
        == "Tool helper check"
    )
    assert "plan_artifact" not in result["payload"]


def test_injected_defaults_have_provenance_and_list_entries() -> None:
    """Defaulted intent fields carry provenance and readable paths."""
    normalized = normalize_intent(
        {
            "time_window": {},
            "aggregation": {},
            "baseline_window": {"duration": 6, "offset_days": 30},
            "thresholds": [{"target_scope": "DETAIL"}],
        },
        "Find large activity",
    )

    assert normalized["time_window"]["unit"] == "DAYS"
    assert normalized["time_window"]["provenance"] == "default"
    assert normalized["baseline_window"]["provenance"] == "default"
    assert normalized["thresholds"][0]["provenance"] == "default"
    assert normalized["field_provenance"]["time_window.unit"] == "default"
    assert "time_window.unit: DAYS" in normalized["applied_defaults"]
    assert (
        "aggregation.metric: transaction_amount"
        in normalized["applied_defaults"]
    )
