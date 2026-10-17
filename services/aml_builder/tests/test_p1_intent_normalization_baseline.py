"""Golden characterization tests for the pre-refactor intent normalizer."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict

from services.aml_builder.web.services import tools as aml_tools
from services.aml_builder.web.services._intent_normalization_baseline import (
    normalize_intent_baseline,
)


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "p1_intent_normalization_golden.json"


def test_legacy_normalization_matches_frozen_golden_fixtures() -> None:
    """The extracted helper preserves outputs for ten legacy normalization cases."""
    fixtures = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    assert len(fixtures) >= 10
    for fixture in fixtures:
        assert normalize_intent_baseline(
            fixture["raw"], fixture["user_prompt"]
        ) == fixture["expected"], fixture["name"]


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
    monkeypatch.setattr(aml_tools, "normalize_intent_baseline", observe_helper)
    monkeypatch.setattr(aml_tools, "seed_scenario_metadata", lambda *_args: {})

    result = json.loads(
        aml_tools.analyze_intent_and_discover_explanation_codes.invoke(
            {"user_prompt": "Find large deposits"}
        )
    )

    assert len(helper_calls) == 1
    assert helper_calls[0][1] == "Find large deposits"
    assert result["enriched_intent"]["scenario_name"] == "Tool helper check"
