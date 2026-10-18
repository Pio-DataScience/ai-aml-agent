"""Regression checks for ALL/ANY transaction explanation-code discovery."""

import json
from types import SimpleNamespace

import pytest

from services.aml_builder.web.services import explanation_code_search as expl_search
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
            {"user_prompt": "Customers with at least one transaction of 10K or more."}
        )
    )

    enriched = result["enriched_intent"]
    assert result["discovered_explanation_codes"] == []
    assert result["explanation_code_checkpoint"] is None
    assert enriched["explanation_codes"] is None
    assert enriched["explanation_codes_by_type"] is None
    assert enriched["thresholds"][0]["explanation_codes"] is None


# The 18 codes the catalog search returned for "CASH WITHDRAWAL" in the user's chat (OPENBI2, 2026-10-17).
CASH_WITHDRAWAL_SEARCH = [
    {"code": "2", "description": "CASH WITHDRAWAL"},
    {"code": "466", "description": "CASH WITHDRAWAL FROM OUR ATM."},
    {"code": "2114", "description": "OUTWARD CASH WITHDRAWAL"},
    {"code": "467", "description": "CASH WITHDRAWAL FROM OTHER'S ATM."},
    {"code": "2164", "description": "ATM/POS CASH WITHDRAWAL"},
    {"code": "2108", "description": "CREDIT CARDS CASH WITHDRAWAL"},
    {"code": "3", "description": "CASH WITHDRAWAL CERTIFIED CHECK"},
    {"code": "429", "description": "CASH WITHDRAWAL COMMISSION"},
    {"code": "141", "description": "Cash withdrawal commission"},
    {"code": "3020", "description": "CASHLESS CASH WITHDRAWAL COMMISSION"},
    {"code": "2116", "description": "OUTWARD CASH WITHDRAWAL COMM."},
    {"code": "340", "description": "ATM COMMISSION-WITHDRAWAL"},
    {"code": "119", "description": "cash with comm"},
    {"code": "2143", "description": "OUTWARD WITHDRAWAL COMM.\\CREDIT CARDS"},
    {"code": "2119", "description": "LOCAL WITHDRAWAL COMM./OTHER CARDS"},
    {"code": "2110", "description": "LOCAL WITHDRAWAL COMM./REVOLVING CARDS"},
    {"code": "2109", "description": "LOCAL WITHDRAWAL COMM./VISA ELECTRON"},
    {"code": "2146", "description": "LOCAL WITHDRAWAL COMMISSION\\OTHERS"},
]
GENUINE_WITHDRAWAL_CODES = ["2", "466", "2114", "467", "2164", "2108", "3"]


def _fake_search(monkeypatch, results):
    """Replace the vector search + sub-LLM selection with fixed results per type.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
        results (dict): {transaction type: selected codes}.
    """
    monkeypatch.setattr(
        expl_search, "_select_codes_for_single_type",
        lambda tx, min_similarity=0.55: [dict(c) for c in results.get(tx, [])],
    )


@pytest.mark.parametrize(
    "tx_type, expected",
    [
        ("CASH WITHDRAWAL", True),
        ("ATM withdrawals", True),
        ("withdrawal", True),
        ("cash withdrawal commission", False),
        ("withdrawal fees", False),
        ("withdrawal transfer", False),
        ("CASH DEPOSIT", False),
        ("OUTWARD TRANSFER", False),
    ],
)
def test_cash_withdrawal_keywords(tx_type, expected):
    """Only withdrawal types that do not themselves name a fee or a transfer are cleaned."""
    assert expl_search.is_cash_withdrawal_type(tx_type) is expected


def test_commission_codes_are_removed_from_cash_withdrawals(monkeypatch):
    """The 11 commission/fee codes go; the withdrawal, ATM, card and certified-cheque codes stay."""
    _fake_search(monkeypatch, {"CASH WITHDRAWAL": CASH_WITHDRAWAL_SEARCH})

    codes = [c["code"] for c in expl_search.select_relevant_explanation_codes("CASH WITHDRAWAL")]

    assert codes == GENUINE_WITHDRAWAL_CODES
    assert not set(codes) & expl_search.CASH_WITHDRAWAL_FEE_CODES


def test_other_transaction_types_are_unaffected(monkeypatch):
    """Deposits, transfers and an explicit fee request keep every code the search selected."""
    deposit = [{"code": "1", "description": "CASH DEPOSIT"}, {"code": "128", "description": "CASH DEPOSIT COMMISSION"}]
    fees = [{"code": "429", "description": "CASH WITHDRAWAL COMMISSION"}, {"code": "141", "description": "Cash withdrawal commission"}]
    _fake_search(monkeypatch, {"CASH DEPOSIT": deposit, "cash withdrawal commission": fees})

    assert [c["code"] for c in expl_search.select_relevant_explanation_codes("CASH DEPOSIT")] == ["1", "128"]
    assert [c["code"] for c in expl_search.select_relevant_explanation_codes("cash withdrawal commission")] == ["429", "141"]


def test_mixed_request_cleans_only_the_withdrawal_part(monkeypatch):
    """'cash deposit, cash withdrawal': the deposit codes stay whole, the withdrawal fees go."""
    deposit = [{"code": "1", "description": "CASH DEPOSIT"}, {"code": "128", "description": "CASH DEPOSIT COMMISSION"}]
    _fake_search(monkeypatch, {"CASH DEPOSIT": deposit, "CASH WITHDRAWAL": CASH_WITHDRAWAL_SEARCH})

    codes = [c["code"] for c in expl_search.select_relevant_explanation_codes(["CASH DEPOSIT", "CASH WITHDRAWAL"])]

    assert codes == ["1", "128", *GENUINE_WITHDRAWAL_CODES]
