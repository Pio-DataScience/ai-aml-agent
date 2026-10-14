"""Tests for the PioTech rejection passthrough (piotech_report + the shadow-test tool).

The report/error shapes below are copied from real responses of the PioTech DWH Scenario
Agent on OPENBI2 (October 2026). No network, Oracle or LLM access is needed: the PioTech
stream is served by an httpx.MockTransport and the shadow-test executor is replaced.

Usage:
    PYTHONPATH=services/aml_builder pytest services/aml_builder/tests/test_piotech_rejection.py
"""

import json
from typing import Any, Dict, List

import httpx
import pytest

from services.aml_builder.web.services import tools as aml_tools
from services.aml_builder.web.services.piotech_report import (
    build_rejection,
    build_unavailable_rejection,
    normalize_scenario_report,
    render_rejection_markdown,
)

BALANCE_REASON = (
    "account balance: CRNT_BAL is the balance after a posting, in the account's own currency. "
    "It still cannot be vouched for as a condition: 31% of credit steps and 44% of debit steps do "
    "not reconcile with the posting before them. To describe money that does not stay in the "
    "account, compare money out with money in instead (for example: total withdrawals at least "
    "90% of total deposits)."
)
BALANCE_REPORT: Dict[str, Any] = {
    "type": "scenario_report",
    "implemented": [],
    "not_implemented": [{"condition": "without maintaining a balance", "reason": BALANCE_REASON}],
    "warnings": [
        'Only "without maintaining a balance" blocks this scenario. Without it the scenario can be '
        "generated, with the conditions listed in implementable_without. No reduced SQL was generated: "
        "leaving a condition out is the officer's decision - send the intent again without it.",
        "Per account: postings without an account number are left out.",
    ],
    "blocked_by": "without maintaining a balance",
    "implementable_without": [
        "transaction types CASH DEPOSIT, CASH WITHDRAWAL",
        "transaction_count > 10 (CASH DEPOSIT)",
        "per account",
        "within 30 days",
    ],
}
BALANCE_ERROR = 'NOT_IMPLEMENTABLE: "without maintaining a balance" cannot be implemented: ' + BALANCE_REASON
INTENT = {"scenario_name": "Pass-through", "scenario_type": "ACCOUNT", "detection_logic": "x"}


# ─────────────────────────── pure parsing ───────────────────────────


def test_single_blocker_gives_blocked_by_reason_and_suggestion() -> None:
    """A one-condition refusal yields the condition, PioTech's reason and its advice."""
    rejection = build_rejection(BALANCE_ERROR, normalize_scenario_report(BALANCE_REPORT))

    assert rejection["kind"] == "NOT_IMPLEMENTABLE"
    assert rejection["blocked_by"] == ["without maintaining a balance"]
    assert rejection["reasons"][0]["reason"] == BALANCE_REASON
    assert rejection["implementable_without"][-1] == "within 30 days"
    joined = " ".join(rejection["suggestions"])
    assert "compare money out with money in instead" in joined
    assert "send the scenario again without it" in joined
    # PioTech's own "send the intent again without it" sentence is not repeated as a second suggestion.
    assert not any("send the intent again" in s for s in rejection["suggestions"])
    # The PioTech single-blocker warning becomes a suggestion, not a duplicate warning.
    assert rejection["warnings"] == ["Per account: postings without an account number are left out."]


def test_unresolved_message_is_split_into_open_items() -> None:
    """UNRESOLVED: a | b lists each open point as a blocker and asks the user to answer."""
    rejection = build_rejection(
        'UNRESOLVED: "average" could mean the total per period or the amount per transaction | threshold amount',
        normalize_scenario_report({"implemented": [], "not_implemented": [], "warnings": []}),
    )

    assert rejection["kind"] == "UNRESOLVED"
    assert rejection["blocked_by"] == [
        '"average" could mean the total per period or the amount per transaction',
        "threshold amount",
    ]
    assert any("Answer the open point" in s for s in rejection["suggestions"])


def test_several_conditions_without_single_blocker() -> None:
    """Two failing conditions are both listed; a generic fallback suggestion is given."""
    report = normalize_scenario_report(
        {
            "not_implemented": [
                {"condition": "no activity for > 180 days", "reason": "matches no structural check"},
                {"condition": "suddenly receive funds", "reason": "matches no structural check"},
            ],
            "warnings": [],
        }
    )
    rejection = build_rejection('NOT_IMPLEMENTABLE: "no activity for > 180 days" cannot be implemented', report)

    assert rejection["blocked_by"] == ["no activity for > 180 days", "suddenly receive funds"]
    assert rejection["suggestions"] and "Remove or rephrase it" in rejection["suggestions"][0]


def test_error_without_report_still_explains() -> None:
    """A bare error message (no report) becomes the reason, never an empty rejection."""
    rejection = build_rejection("No approved SQL candidate", None)

    assert rejection["kind"] == "VALIDATION_FAILED"
    assert rejection["reasons"] == [{"condition": "the scenario as a whole", "reason": "No approved SQL candidate"}]
    assert rejection["suggestions"]


def test_malformed_report_members_are_dropped() -> None:
    """Non-list fields and junk members never raise."""
    report = normalize_scenario_report({"implemented": "x", "not_implemented": ["plain text", 5, None], "warnings": None})

    assert report["implemented"] == []
    assert report["not_implemented"][0] == {"condition": "plain text", "reason": ""}
    assert report["warnings"] == []


def test_markdown_has_the_three_sections() -> None:
    """The side-panel markdown carries Blocked by, Reason and Suggestion."""
    md = render_rejection_markdown(build_rejection(BALANCE_ERROR, normalize_scenario_report(BALANCE_REPORT)))

    for heading in ("### Blocked by", "### Reason", "### Suggestion", "### What can still be generated without it"):
        assert heading in md
    assert "without maintaining a balance" in md


def test_unavailable_rejection_points_at_the_service() -> None:
    """A connection failure says the scenario was not evaluated."""
    rejection = build_unavailable_rejection("http://127.0.0.1:8006/chat/stream", "[WinError 10061] refused")

    assert rejection["kind"] == "SERVICE_UNAVAILABLE"
    assert "not evaluated" in rejection["reasons"][0]["reason"]


# ─────────────────────── tool end to end (mocked) ───────────────────────


def _sse(events: List[Dict[str, Any]]) -> bytes:
    """Encode events as an SSE body.

    Args:
        events (List[Dict[str, Any]]): Event payloads.

    Returns:
        bytes: The SSE stream body.
    """
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode("utf-8")


def _mock_piotech(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    """Route every httpx.Client created by the tool through a MockTransport.

    Args:
        monkeypatch (pytest.MonkeyPatch): The pytest monkeypatch fixture.
        handler (Any): The MockTransport request handler.
    """
    real_client = httpx.Client
    monkeypatch.setattr(
        aml_tools.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw)
    )


def test_tool_returns_rejection_and_never_runs_shadow_sql(monkeypatch: pytest.MonkeyPatch) -> None:
    """Report + error from PioTech become a rejection; no SQL is executed on Oracle."""
    body = _sse([BALANCE_REPORT, {"type": "error", "message": BALANCE_ERROR}, {"type": "done"}])
    _mock_piotech(monkeypatch, lambda request: httpx.Response(200, content=body))

    def no_oracle(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("shadow SQL must not run when no SQL was generated")

    monkeypatch.setattr(aml_tools, "run_shadow_readonly", no_oracle)

    result = json.loads(aml_tools.execute_oracle_dwh_shadow_test.invoke({"intent_json": json.dumps(INTENT)}))

    assert result["rejection"]["kind"] == "NOT_IMPLEMENTABLE"
    assert result["rejection"]["blocked_by"] == ["without maintaining a balance"]
    assert "### Suggestion" in result["rejection_report"]
    assert "without maintaining a balance" in result["error"]
    assert result["scenario_report"]["blocked_by"] == "without maintaining a balance"


def test_tool_reports_unreachable_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """A refused connection is a SERVICE_UNAVAILABLE rejection, not a scenario problem."""

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("[WinError 10061] connection refused", request=request)

    _mock_piotech(monkeypatch, refuse)

    result = json.loads(aml_tools.execute_oracle_dwh_shadow_test.invoke({"intent_json": json.dumps(INTENT)}))

    assert result["rejection"]["kind"] == "SERVICE_UNAVAILABLE"
    assert "10061" in result["rejection"]["reasons"][0]["reason"]


def test_tool_reports_422_as_rejected_request_not_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 422 from a running service is REQUEST_REJECTED with its own detail, not SERVICE_UNAVAILABLE."""
    detail = {
        "detail": "Invalid AML_SCENARIO_GENERATION intent: 1 validation error for ServiceAIntent\n"
        "time_window.value\n  Input should be greater than 0 [type=greater_than, input_value=0, input_type=int]"
    }
    _mock_piotech(monkeypatch, lambda request: httpx.Response(422, json=detail))

    result = json.loads(aml_tools.execute_oracle_dwh_shadow_test.invoke({"intent_json": json.dumps(INTENT)}))
    rejection = result["rejection"]

    assert rejection["kind"] == "REQUEST_REJECTED"
    assert rejection["blocked_by"] == ["the time window of the scenario"]
    assert "Input should be greater than 0" in rejection["reasons"][0]["reason"]
    assert any("within 30 days" in s for s in rejection["suggestions"])
    assert not any("Start the PioTech" in s for s in rejection["suggestions"])


def test_tool_reports_500_as_service_fault(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 5xx says the fault is in the SQL service, not in the scenario."""
    _mock_piotech(monkeypatch, lambda request: httpx.Response(500, text="Internal Server Error"))

    rejection = json.loads(
        aml_tools.execute_oracle_dwh_shadow_test.invoke({"intent_json": json.dumps(INTENT)})
    )["rejection"]

    assert rejection["kind"] == "VALIDATION_FAILED"
    assert "not in the scenario" in rejection["suggestions"][0]


def test_tool_success_path_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """When SQL comes back, the result has metrics and no rejection."""
    sql = "SELECT t.CUS_NUM FROM BI_DWH.PIO_TRANSACTIONS t WHERE t.TRA_DATE >= SYSDATE - 7"
    body = _sse([{"type": "scenario_report", "implemented": ["within 7 days"], "not_implemented": [], "warnings": []},
                 {"type": "final_answer", "text": sql}, {"type": "done"}])
    _mock_piotech(monkeypatch, lambda request: httpx.Response(200, content=body))
    monkeypatch.setattr(aml_tools, "run_shadow_readonly", lambda _sql, *a, **k: (["COUNT(*)"], [(0,)]))

    result = json.loads(aml_tools.execute_oracle_dwh_shadow_test.invoke({"intent_json": json.dumps(INTENT)}))

    assert result["raw_sql"].startswith("SELECT t.CUS_NUM")
    assert result["validation_success"] is True
    assert "rejection" not in result
