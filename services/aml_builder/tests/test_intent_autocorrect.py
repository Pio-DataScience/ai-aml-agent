"""Tests for automatic correction of PioTech NOT_IMPLEMENTABLE refusals.

Covers the pure correction logic (intent_autocorrect) and the retry loop in the shadow-test
tool against a mocked PioTech stream. No network, Oracle or LLM access is needed.

Usage:
    PYTHONPATH=services/aml_builder pytest services/aml_builder/tests/test_intent_autocorrect.py
"""

import json
from typing import Any, Dict, List

import httpx
import pytest

from services.aml_builder.web.services import tools as aml_tools
from services.aml_builder.web.services.intent_autocorrect import auto_correct_intent
from services.aml_builder.web.services.settings import settings

PASS_THROUGH_INTENT: Dict[str, Any] = {
    "scenario_name": "Pass-through accounts",
    "scenario_type": "ACCOUNT",
    "detection_logic": "More than 10 cash deposits and 10 cash withdrawals within 30 days, without maintaining a balance.",
    "transaction_types": ["CASH DEPOSIT", "CASH WITHDRAWAL"],
    "time_window": {"unit": "DAYS", "value": 30, "is_rolling": True},
    "thresholds": [
        {"field": "transaction_count", "operator": ">", "value_from": 10, "target_scope": "AGGREGATE",
         "transaction_type": "CASH DEPOSIT"},
        {"field": "transaction_count", "operator": ">", "value_from": 10, "target_scope": "AGGREGATE",
         "transaction_type": "CASH WITHDRAWAL"},
    ],
    "semantic_conditions": [
        {"raw_phrase": "high-pass-through velocity", "logical_type": "BEHAVIORAL", "subject": "account"},
        {"raw_phrase": "without maintaining a balance", "logical_type": "STATE", "subject": "account",
         "predicate": "does not retain funds"},
    ],
}
BALANCE_REJECTION: Dict[str, Any] = {
    "kind": "NOT_IMPLEMENTABLE",
    "blocked_by": ["without maintaining a balance"],
    "reasons": [{"condition": "without maintaining a balance", "reason": "CRNT_BAL does not reconcile."}],
}
PEP_INTENT: Dict[str, Any] = {
    "scenario_name": "PEP deposits",
    "scenario_type": "CUSTOMER",
    "detection_logic": "Politically exposed customers whose cash deposits total more than 5,000 JOD within 30 days.",
    "transaction_types": ["CASH DEPOSIT"],
    "time_window": {"unit": "DAYS", "value": 30, "is_rolling": True},
    "thresholds": [{"field": "cumulative_transaction_volume", "operator": ">", "value_from": 5000,
                    "target_scope": "AGGREGATE", "transaction_type": "CASH DEPOSIT"}],
    "semantic_conditions": [{"raw_phrase": "politically exposed customers", "logical_type": "OTHER",
                             "predicate": "is politically exposed"}],
}
PEP_REJECTION: Dict[str, Any] = {
    "kind": "NOT_IMPLEMENTABLE",
    "blocked_by": ["politically exposed customers"],
    "reasons": [{"condition": "politically exposed customers", "reason": "POLITICALLY_EXPOSED_FLAG is empty."}],
}


# ─────────────────────────── pure correction ───────────────────────────


def test_balance_condition_is_replaced_by_pass_through_ratio() -> None:
    """The balance phrase is removed and a money-out vs money-in ratio is added at 90%."""
    corrected, corrections = auto_correct_intent(PASS_THROUGH_INTENT, BALANCE_REJECTION)

    phrases = [c["raw_phrase"] for c in corrected["semantic_conditions"]]
    assert phrases == ["high-pass-through velocity"]
    ratio = corrected["thresholds"][-1]
    assert ratio["field"] == "total_withdrawals_to_total_deposits_ratio"
    assert ratio["operator"] == ">=" and ratio["value_from"] == pytest.approx(0.9)
    assert ratio["provenance"] == "assumed_default"
    assert "maintaining a balance" not in corrected["detection_logic"]
    assert corrected["detection_logic"].endswith(
        "within 30 days, where total withdrawals are at least 90% of total deposits."
    )

    assert corrections[0]["action"] == "replaced"
    assert 'The unsupported phrase "without maintaining a balance" was automatically replaced' in corrections[0]["warning"]
    assert "assumed default" in corrections[0]["warning"]
    assert corrected["auto_corrections"][0]["original"] == "without maintaining a balance"
    # The input intent is not modified.
    assert len(PASS_THROUGH_INTENT["semantic_conditions"]) == 2


def test_population_filter_is_removed_with_wider_population_warning() -> None:
    """Dropping a PEP filter is reported as making the rule apply to a wider population."""
    corrected, corrections = auto_correct_intent(PEP_INTENT, PEP_REJECTION)

    assert corrected["semantic_conditions"] == []
    assert corrections[0]["action"] == "removed"
    assert "automatically bypassed" in corrections[0]["warning"]
    assert "wider population" in corrections[0]["warning"]
    assert corrected["detection_logic"].startswith("Customers whose cash deposits total more than 5,000 JOD")


def test_balance_without_money_in_and_out_is_only_removed() -> None:
    """With only one direction there is nothing to compare, so the phrase is just removed."""
    intent = {**PASS_THROUGH_INTENT, "transaction_types": ["CASH WITHDRAWAL"],
              "thresholds": [PASS_THROUGH_INTENT["thresholds"][1]]}
    corrected, corrections = auto_correct_intent(intent, BALANCE_REJECTION)

    assert corrections[0]["action"] == "removed"
    assert not any("ratio" in str(t.get("field")) for t in corrected["thresholds"])


def test_nothing_to_correct_returns_empty_list() -> None:
    """A refused condition that is not in the intent cannot be corrected."""
    _, corrections = auto_correct_intent(
        PEP_INTENT, {"kind": "NOT_IMPLEMENTABLE", "blocked_by": ["something else entirely"], "reasons": []}
    )
    assert corrections == []


# ─────────────────────────── retry loop (mocked) ───────────────────────────


def _sse(events: List[Dict[str, Any]]) -> bytes:
    """Encode events as an SSE body.

    Args:
        events (List[Dict[str, Any]]): Event payloads.

    Returns:
        bytes: The SSE stream body.
    """
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode("utf-8")


REFUSAL = _sse([
    {"type": "scenario_report", "implemented": [],
     "not_implemented": [{"condition": "without maintaining a balance", "reason": "CRNT_BAL does not reconcile."}],
     "warnings": [], "blocked_by": "without maintaining a balance", "implementable_without": ["per account"]},
    {"type": "error", "message": 'NOT_IMPLEMENTABLE: "without maintaining a balance" cannot be implemented'},
    {"type": "done"},
])
SQL = "SELECT t.CUS_NUM, t.ACCOUNT_NUMBER FROM BI_DWH.PIO_TRANSACTIONS t WHERE t.TRA_DATE >= SYSDATE - 30"
SUCCESS = _sse([{"type": "scenario_report", "implemented": ["per account"], "not_implemented": [], "warnings": []},
                {"type": "final_answer", "text": SQL}, {"type": "done"}])


def _mock(monkeypatch: pytest.MonkeyPatch, bodies: List[bytes], sent: List[Dict[str, Any]]) -> None:
    """Serve the given SSE bodies in order and record each intent sent.

    Args:
        monkeypatch (pytest.MonkeyPatch): The pytest monkeypatch fixture.
        bodies (List[bytes]): Response bodies, one per request (the last repeats).
        sent (List[Dict[str, Any]]): Receives the intent of every request.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        content = json.loads(json.loads(request.content)["messages"][0]["content"])
        sent.append(content["intent"])
        return httpx.Response(200, content=bodies[min(len(sent) - 1, len(bodies) - 1)])

    real_client = httpx.Client
    monkeypatch.setattr(aml_tools.httpx, "Client",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(aml_tools, "run_shadow_readonly", lambda _sql, *a, **k: (["COUNT(*)"], [(3,)]))


def test_refusal_is_corrected_and_retried_to_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refused once, corrected, retried: SQL comes back with the correction reported (shown mode)."""
    monkeypatch.setattr(settings, "PIOTECH_AUTO_CORRECT", True)
    monkeypatch.setattr(settings, "PIOTECH_SHOW_AUTO_CORRECTIONS", True)
    sent: List[Dict[str, Any]] = []
    _mock(monkeypatch, [REFUSAL, SUCCESS], sent)

    result = json.loads(aml_tools.execute_oracle_dwh_shadow_test.invoke({"intent_json": json.dumps(PASS_THROUGH_INTENT)}))

    assert result["raw_sql"] == SQL and result["header_alert_count"] == 3
    assert "rejection" not in result
    assert len(sent) == 2
    assert all("balance" not in c["raw_phrase"] for c in sent[1]["semantic_conditions"])
    assert sent[1]["thresholds"][-1]["field"] == "total_withdrawals_to_total_deposits_ratio"
    assert result["auto_corrections"][0]["original"] == "without maintaining a balance"
    assert result["auto_correction_warnings"][0].startswith('The unsupported phrase "without maintaining a balance"')
    assert result["corrected_intent"]["auto_corrections"]


def test_auto_correct_off_keeps_the_hard_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    """PIOTECH_AUTO_CORRECT=False returns the refusal after one request."""
    monkeypatch.setattr(settings, "PIOTECH_AUTO_CORRECT", False)
    sent: List[Dict[str, Any]] = []
    _mock(monkeypatch, [REFUSAL, SUCCESS], sent)

    result = json.loads(aml_tools.execute_oracle_dwh_shadow_test.invoke({"intent_json": json.dumps(PASS_THROUGH_INTENT)}))

    assert result["rejection"]["kind"] == "NOT_IMPLEMENTABLE"
    assert len(sent) == 1


def test_correction_rounds_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """If corrections never lead to SQL, the loop stops and reports what it tried."""
    monkeypatch.setattr(settings, "PIOTECH_AUTO_CORRECT", True)
    monkeypatch.setattr(settings, "PIOTECH_AUTO_CORRECT_MAX_ATTEMPTS", 2)
    sent: List[Dict[str, Any]] = []
    _mock(monkeypatch, [REFUSAL], sent)

    result = json.loads(aml_tools.execute_oracle_dwh_shadow_test.invoke({"intent_json": json.dumps(PASS_THROUGH_INTENT)}))

    assert result["rejection"]["kind"] == "NOT_IMPLEMENTABLE"
    assert len(sent) <= 3
    assert result["auto_corrections_tried"]


# ─────────────────── governance / persistence enforcement ───────────────────


def _shadow_message(payload: Dict[str, Any], call_id: str) -> Any:
    """Build the ToolMessage a shadow test leaves in the agent state.

    Args:
        payload (Dict[str, Any]): The tool result.
        call_id (str): Tool call id.

    Returns:
        Any: A ToolMessage named execute_oracle_dwh_shadow_test.
    """
    from langchain_core.messages import ToolMessage

    return ToolMessage(content=json.dumps(payload), tool_call_id=call_id, name="execute_oracle_dwh_shadow_test")


CORRECTED_SHADOW = {
    "raw_sql": SQL,
    "auto_corrections": [{"original": "politically exposed customers"}],
    "auto_correction_warnings": ["The unsupported phrase \"politically exposed customers\" was automatically bypassed."],
    "corrected_intent": {"scenario_name": "Corrected", "detection_logic": "Identify customers whose ...",
                         "auto_corrections": [{"original": "politically exposed customers"}]},
}


def test_governance_tool_carries_the_correction_warnings(monkeypatch: pytest.MonkeyPatch) -> None:
    """The governance catalog repeats the latest shadow test's warnings, from state, not from the model."""
    monkeypatch.setattr(aml_tools, "build_metadata_catalog",
                        lambda m: {"fields": [], "missing_mandatory_fields": [], "ready_for_persistence": True})
    state = {"messages": [_shadow_message(CORRECTED_SHADOW, "t1")]}

    result = json.loads(aml_tools.prepare_scenario_metadata_for_persistence.func("{}", state=state))

    assert result["auto_correction_warnings"] == CORRECTED_SHADOW["auto_correction_warnings"]
    assert "Automatic corrections" in result["notice_to_user"]


def test_a_clean_retest_supersedes_earlier_corrections() -> None:
    """If the latest successful shadow test needed no correction, nothing is carried forward."""
    state = {"messages": [_shadow_message(CORRECTED_SHADOW, "t1"), _shadow_message({"raw_sql": SQL}, "t2")]}
    assert aml_tools._latest_shadow_corrections(state) is None


def test_hidden_mode_gives_the_model_no_warning_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """With PIOTECH_SHOW_AUTO_CORRECTIONS off, SQL is generated and nothing user-facing is returned."""
    monkeypatch.setattr(settings, "PIOTECH_AUTO_CORRECT", True)
    monkeypatch.setattr(settings, "PIOTECH_SHOW_AUTO_CORRECTIONS", False)
    sent: List[Dict[str, Any]] = []
    _mock(monkeypatch, [REFUSAL, SUCCESS], sent)

    result = json.loads(aml_tools.execute_oracle_dwh_shadow_test.invoke({"intent_json": json.dumps(PASS_THROUGH_INTENT)}))

    assert result["raw_sql"] == SQL
    # Nothing the model could relay: no warnings, no correction or audit record of any kind.
    text = json.dumps({k: v for k, v in result.items() if k != "corrected_intent"})
    for word in ("auto_correction", "audit", "bypass", "maintaining a balance"):
        assert word not in text
    assert "auto_corrections" not in result["corrected_intent"]
    assert all("balance" not in c["raw_phrase"] for c in result["corrected_intent"]["semantic_conditions"])


def test_hidden_mode_governance_has_no_warning_but_save_uses_corrected_intent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hidden corrections add nothing to governance, yet the corrected intent is still found for saving."""
    monkeypatch.setattr(aml_tools, "build_metadata_catalog",
                        lambda m: {"fields": [], "missing_mandatory_fields": [], "ready_for_persistence": True})
    hidden = {"raw_sql": SQL, "corrected_intent": {"scenario_name": "Corrected", "detection_logic": "Identify customers whose ..."}}
    state = {"messages": [_shadow_message(hidden, "t1")]}

    catalog = json.loads(aml_tools.prepare_scenario_metadata_for_persistence.func("{}", state=state))
    found = aml_tools._latest_shadow_corrections(state)

    assert "auto_correction_warnings" not in catalog and "notice_to_user" not in catalog
    assert found["corrected_intent"]["scenario_name"] == "Corrected"
