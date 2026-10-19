"""Offline tests for P1 tool envelopes and pure domain functions."""

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from services.aml_builder.web.api.codec import (
    ToolErrorCode,
    ToolStatus,
    decode_tool_result,
    encode_tool_result,
)
from services.aml_builder.web.api import sse_mapper
from services.aml_builder.web.api.dependencies import Identity
from services.aml_builder.web.api.routes import sessions
from services.aml_builder.web.domain.hashing import normalize_sql, stable_hash
from services.aml_builder.web.services import tools as aml_tools
from services.aml_builder.web.domain.metadata_rules import (
    normalize_and_validate_metadata,
    seed_metadata_defaults,
)
from services.aml_builder.web.domain.sql_extract import extract_sql
from services.aml_builder.web.domain.window import map_period, time_window_days
from services.aml_builder.web.services.sql_extraction import (
    extract_sql as legacy_extract_sql,
)


def test_codec_round_trip_and_legacy_decode() -> None:
    """Typed results round trip and legacy results retain their old keys."""
    encoded = encode_tool_result(
        "generate_scenario_execution_plan",
        {
            "plan_artifact": "# Plan",
            "plan_conditions": [{"condition": "Window"}],
        },
    )
    result = decode_tool_result(encoded, "generate_scenario_execution_plan")

    assert result.schema_version == 1
    assert result.status == ToolStatus.OK
    assert result.payload == {
        "plan_artifact": "# Plan",
        "plan_conditions": [{"condition": "Window"}],
    }

    legacy = decode_tool_result(
        json.dumps({"plan_artifact": "# Legacy", "plan_conditions": []}),
        "generate_scenario_execution_plan",
    )
    assert legacy.payload["plan_artifact"] == "# Legacy"
    assert legacy.schema_version == 1


def test_tool_names_remain_unchanged() -> None:
    """P1 keeps the existing six LangChain tool names."""
    assert {
        aml_tools.analyze_intent_and_discover_explanation_codes.name,
        aml_tools.generate_scenario_execution_plan.name,
        aml_tools.execute_oracle_dwh_shadow_test.name,
        aml_tools.prepare_scenario_metadata_for_persistence.name,
        aml_tools.persist_and_validate_scenario_in_dwh.name,
        aml_tools.query_production_scenario_registry.name,
    } == {
        "analyze_intent_and_discover_explanation_codes",
        "generate_scenario_execution_plan",
        "execute_oracle_dwh_shadow_test",
        "prepare_scenario_metadata_for_persistence",
        "persist_and_validate_scenario_in_dwh",
        "query_production_scenario_registry",
    }


def test_failed_shadow_result_keeps_payload_and_emits_scenario_result() -> (
    None
):
    """A failed shadow run remains visible through the existing SSE event."""
    encoded = encode_tool_result(
        "execute_oracle_dwh_shadow_test",
        {
            "raw_sql": "SELECT 1 FROM dual",
            "header_alert_count": None,
            "transaction_detail_count": None,
            "alert_density_ratio": None,
            "validation_success": False,
            "error": "ORA-00942: table or view does not exist",
        },
    )
    decoded = decode_tool_result(encoded, "execute_oracle_dwh_shadow_test")
    events = sse_mapper.map_tool_result_to_sse(
        "execute_oracle_dwh_shadow_test", encoded
    )

    assert decoded.status == ToolStatus.ERROR
    assert decoded.error_code == ToolErrorCode.SHADOW_FAILED
    assert decoded.payload["raw_sql"] == "SELECT 1 FROM dual"
    assert decoded.payload["validation_success"] is False
    assert (
        decoded.payload["error"] == "ORA-00942: table or view does not exist"
    )
    assert len(events) == 1
    assert events[0].type == "scenario_result"
    assert events[0].data == decoded.payload


def test_sse_mapper_plan_and_metadata_snapshots() -> None:
    """Plan and metadata tool results keep their existing event snapshots."""
    plan = encode_tool_result(
        "generate_scenario_execution_plan",
        {"plan_artifact": "# Scenario Plan"},
    )
    catalog = encode_tool_result(
        "prepare_scenario_metadata_for_persistence",
        {"fields": [{"name": "ACTIVE_FLAG"}], "missing_mandatory_fields": []},
    )

    plan_event = sse_mapper.map_tool_result_to_sse(
        "generate_scenario_execution_plan", plan
    )[0]
    catalog_event = sse_mapper.map_tool_result_to_sse(
        "prepare_scenario_metadata_for_persistence", catalog
    )[0]
    assert plan_event.model_dump(exclude_none=True) == {
        "type": "plan_artifact",
        "text": "# Scenario Plan",
    }
    assert catalog_event.model_dump(exclude_none=True) == {
        "type": "scenario_metadata_catalog",
        "data": {
            "fields": [{"name": "ACTIVE_FLAG"}],
            "missing_mandatory_fields": [],
        },
    }


def test_hashing_canonicalizes_comments_whitespace_and_keyword_case() -> None:
    """SQL hashes ignore comments and whitespace outside literals."""
    first = (
        "SELECT /* review */ Name FROM Customers -- trailing\n"
        " WHERE Kind = 'Cash';"
    )
    equivalent = " select Name\n from Customers where Kind='Cash' ; "
    changed_literal = "SELECT Name FROM Customers WHERE Kind='cash';"

    assert normalize_sql(first) == normalize_sql(equivalent)
    assert stable_hash(normalize_sql(first)) == stable_hash(
        normalize_sql(equivalent)
    )
    assert stable_hash(normalize_sql(first)) != stable_hash(
        normalize_sql(changed_literal)
    )


def test_stable_hash_uses_canonical_json() -> None:
    """Object key order does not affect the UTF-8 canonical JSON digest."""
    assert stable_hash({"b": 2, "a": "é"}) == stable_hash({"a": "é", "b": 2})


@pytest.mark.parametrize(
    ("window", "tool_legacy_days", "deploy_legacy_days"),
    [
        ({"value": 30, "unit": "DAYS"}, 30, 30),
        ({"value": 2, "unit": "weeks"}, 14, 14),
        ({"value": 2, "unit": "MONTHS"}, 60, 60),
        ({"value": 1, "unit": "YEARS"}, 365, 365),
        ({"value": 2, "unit": "FORTNIGHTS"}, 2, 2),
        ({"value": 0, "unit": "DAYS"}, None, None),
        (None, None, None),
    ],
)
def test_window_days_matches_both_legacy_implementations(
    window: Any,
    tool_legacy_days: int | None,
    deploy_legacy_days: int | None,
) -> None:
    """One shared conversion matches frozen tool and deploy legacy outputs."""
    assert time_window_days(window) == tool_legacy_days
    assert time_window_days(window) == deploy_legacy_days


def test_metadata_rules_preserve_existing_values_and_validate_purely() -> None:
    """Metadata defaults and enum checks run without Oracle lookups."""
    seeded = seed_metadata_defaults(
        {"scenario_name": "New", "detection_logic": "Find activity"},
        {"COUNTRY_CODE": "OFFICER"},
        {"COUNTRY_CODE": "DEFAULT", "ACTIVE_FLAG": "1"},
    )
    assert seeded == {
        "COUNTRY_CODE": "OFFICER",
        "ACTIVE_FLAG": "1",
        "SCENARIO_DES_ENG": "Find activity",
        "SCENARIO_DES_NAT_LAN": "Find activity",
    }
    field = SimpleNamespace(
        name="ACTIVE_FLAG",
        mandatory=True,
        kind="static_enum",
        allowed_values=["1", "0"],
        lookup=None,
    )
    normalized, problems = normalize_and_validate_metadata(
        {"ACTIVE_FLAG": "ACTIVE"}, [field], {}, {}
    )
    assert normalized["ACTIVE_FLAG"] == "1"
    assert problems == []
    _, invalid_problems = normalize_and_validate_metadata(
        {"ACTIVE_FLAG": "yes"}, [field], {}, {}
    )
    assert invalid_problems == [
        "ACTIVE_FLAG='yes' is not one of the allowed values ['1', '0']."
    ]


def test_window_period_mapping_preserves_legacy_period_rules() -> None:
    """Period mapping retains the old Oracle metadata representation."""
    assert map_period({"unit": "WEEKS", "value": 3}) == ("D", 21)
    assert map_period({"unit": "MONTHS", "value": 3}) == ("M", 3)
    assert map_period(None) == (None, None)


def test_sql_extraction_compatibility_export() -> None:
    """The old service import path delegates to the domain function."""
    response = "```sql\nSELECT * FROM accounts;\n```"
    assert extract_sql(response) == "SELECT * FROM accounts"
    assert legacy_extract_sql(response) == extract_sql(response)


def test_history_endpoint_decodes_synthetic_tool_envelope(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """History reconstruction reads tool artifacts through the shared codec."""
    checkpoint_path = tmp_path / "checkpoint.sqlite"
    checkpoint_path.touch()
    monkeypatch.setattr(
        sessions,
        "settings",
        SimpleNamespace(CHECKPOINT_DB_PATH=checkpoint_path),
    )

    class FakeGraph:
        async def aget_state(self, _config: Any) -> Any:
            return SimpleNamespace(
                values={
                    "messages": [
                        HumanMessage(content="Build a scenario"),
                        AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "id": "plan-call",
                                    "name": "generate_scenario_execution_plan",
                                    "args": {},
                                }
                            ],
                        ),
                        ToolMessage(
                            content=encode_tool_result(
                                "generate_scenario_execution_plan",
                                {"plan_artifact": "# Recovered plan"},
                            ),
                            tool_call_id="plan-call",
                        ),
                        AIMessage(content="Here is the plan."),
                    ]
                }
            )

    async def fake_get_graph() -> FakeGraph:
        return FakeGraph()

    monkeypatch.setattr(sessions, "get_tool_driven_graph", fake_get_graph)
    response = asyncio.run(
        sessions.get_chat_history("proj", "chat", "user", Identity("user", "", "proj"))
    )

    assert response.messages[-1].plan_artifact == "# Recovered plan"
