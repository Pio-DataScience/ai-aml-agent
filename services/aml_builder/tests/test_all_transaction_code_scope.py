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

    # This test is about code discovery; the clarification flow has its own tests.
    monkeypatch.setattr(aml_tools.settings, "AML_CLARIFICATION_FLOW", False)
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
    """Transfers and an explicit fee request keep every code the search selected."""
    transfer = [{"code": "50", "description": "OUTWARD TRANSFER"}, {"code": "51", "description": "TRANSFER COMMISSION"}]
    fees = [{"code": "429", "description": "CASH WITHDRAWAL COMMISSION"}, {"code": "141", "description": "Cash withdrawal commission"}]
    deposit_fees = [{"code": "118", "description": "CASH DEPOSIT COMMISSION"},
                    {"code": "3019", "description": "CASHLESS CASH DEPOSIT COMMISSION"}]
    _fake_search(monkeypatch, {"OUTWARD TRANSFER": transfer, "cash withdrawal commission": fees,
                               "cash deposit commission": deposit_fees})

    assert [c["code"] for c in expl_search.select_relevant_explanation_codes("OUTWARD TRANSFER")] == ["50", "51"]
    assert [c["code"] for c in expl_search.select_relevant_explanation_codes("cash withdrawal commission")] == ["429", "141"]
    assert [c["code"] for c in expl_search.select_relevant_explanation_codes("cash deposit commission")] == ["118", "3019"]


def test_mixed_request_cleans_both_parts(monkeypatch):
    """'cash deposit, cash withdrawal': the deposit commissions and the withdrawal fees both go."""
    _fake_search(monkeypatch, {"CASH DEPOSIT": CASH_DEPOSIT_SEARCH, "CASH WITHDRAWAL": CASH_WITHDRAWAL_SEARCH})

    codes = [c["code"] for c in expl_search.select_relevant_explanation_codes(["CASH DEPOSIT", "CASH WITHDRAWAL"])]

    assert codes == [*GENUINE_DEPOSIT_CODES, *GENUINE_WITHDRAWAL_CODES]


# ─────────────────────────── cash-deposit commissions ───────────────────────────

# The 4 codes the catalog search returned for "CASH DEPOSIT" in the live run (OPENBI2, 2026-10-25).
CASH_DEPOSIT_SEARCH = [
    {"code": "1", "description": "CASH DEPOSIT"},
    {"code": "660", "description": "CASH DEPOSIT (E-BRANCH)"},
    {"code": "118", "description": "CASH DEPOSIT COMMISSION"},
    {"code": "3019", "description": "CASHLESS CASH DEPOSIT COMMISSION"},
]
GENUINE_DEPOSIT_CODES = ["1", "660"]


@pytest.mark.parametrize(
    "tx_type, expected",
    [
        ("CASH DEPOSIT", True),
        ("cash deposits", True),
        ("deposit", True),
        ("cash deposit commission", False),
        ("deposit fees", False),
        ("deposit transfer", False),
        ("CASH WITHDRAWAL", False),
        ("OUTWARD TRANSFER", False),
    ],
)
def test_cash_deposit_keywords(tx_type, expected):
    """Only deposit types that do not themselves name a fee or a transfer are cleaned."""
    assert expl_search.is_cash_deposit_type(tx_type) is expected


def test_commission_codes_are_removed_from_cash_deposits(monkeypatch):
    """Live case: 118 and 3019 were sent as cash deposits; only 1 and 660 stay."""
    _fake_search(monkeypatch, {"CASH DEPOSIT": CASH_DEPOSIT_SEARCH})

    codes = [c["code"] for c in expl_search.select_relevant_explanation_codes("CASH DEPOSIT")]

    assert codes == GENUINE_DEPOSIT_CODES
    assert not set(codes) & expl_search.CASH_DEPOSIT_FEE_CODES


def _flagged_deposit_totals(codes, rows):
    """Run the live rule's shape on in-memory rows: per-customer 2019 credit total above 10,000 JOD.

    Same scope, credit leg, date range, grouping, threshold and output column as the SQL PioTech wrote
    for "cash deposits total more than 10,000 JOD ... in 2019"; only the code list varies.

    Args:
        codes (list): The explanation codes of the rule.
        rows (list): (country, inst, customer, code, debit/credit, date, amount) tuples.

    Returns:
        dict: {CUS_NUM: summed amount} for the customers the rule flags.
    """
    import sqlite3

    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE PIO_TRANSACTIONS (COUNTRY_CODE INT, INST_CODE INT, CUS_NUM TEXT, EXPL_CODE TEXT, "
               "DEB_CRE_IND TEXT, TRA_DATE TEXT, EQU_TRA_AMT REAL)")
    db.executemany("INSERT INTO PIO_TRANSACTIONS VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    marks = ", ".join("?" for _ in codes)
    found = db.execute(
        f"SELECT CUS_NUM, SUM(EQU_TRA_AMT) FROM PIO_TRANSACTIONS "
        f"WHERE COUNTRY_CODE = 400 AND INST_CODE = 1 AND EXPL_CODE IN ({marks}) AND DEB_CRE_IND = '2' "
        f"AND TRA_DATE >= '2019-01-01' AND TRA_DATE < '2020-01-01' "
        f"GROUP BY COUNTRY_CODE, INST_CODE, CUS_NUM HAVING SUM(EQU_TRA_AMT) > 10000",
        list(codes),
    ).fetchall()
    db.close()
    return dict(found)


@pytest.mark.parametrize(
    "case, deposits, commission, flagged",
    [
        ("below", [6000, 3600], 500, False),    # 9,600 of deposits; with the commission 10,100 would be flagged
        ("equal", [7000, 3000], 100, False),    # exactly 10,000 is not "more than 10,000"
        ("above", [6000, 4200], 100, True),     # 10,200 of deposits is flagged on its own
    ],
)
def test_commissions_do_not_count_toward_the_deposit_total(monkeypatch, case, deposits, commission, flagged):
    """Below, at and above the threshold, the total is the deposits alone; commissions never tip it over."""
    _fake_search(monkeypatch, {"CASH DEPOSIT": CASH_DEPOSIT_SEARCH})
    codes = [c["code"] for c in expl_search.select_relevant_explanation_codes("CASH DEPOSIT")]
    rows = [(400, 1, "C1", "1", "2", "2019-06-02", deposits[0]),
            (400, 1, "C1", "660", "2", "2019-06-03", deposits[1]),
            (400, 1, "C1", "118", "2", "2019-06-02", commission),
            (400, 1, "C1", "3019", "2", "2019-06-03", commission),
            # outside the rule: another institution, 2018, and the debit leg
            (400, 2, "C1", "1", "2", "2019-06-04", 50000),
            (400, 1, "C1", "1", "2", "2018-12-31", 50000),
            (400, 1, "C1", "1", "1", "2019-06-04", 50000)]

    result = _flagged_deposit_totals(codes, rows)

    assert ("C1" in result) is flagged
    if flagged:
        assert result["C1"] == sum(deposits)
    # The codes as the search returned them (before the fix) flag every case: the commission inflated the total.
    assert "C1" in _flagged_deposit_totals([c["code"] for c in CASH_DEPOSIT_SEARCH], rows)
