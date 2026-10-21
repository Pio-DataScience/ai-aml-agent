"""Tests for customer / account / transaction attribute filters and the demo lookup fill.

No database, network or LLM is needed: catalogs, the resolver LLM and the PioTech stream are mocked.

Usage:
    PYTHONPATH=services/aml_builder pytest services/aml_builder/tests/test_attribute_filters.py
"""

import json
from types import SimpleNamespace
from typing import Any, Dict, List

import httpx
import pytest

import fill_demo_lookups as fill
from services.aml_builder.web.services import attribute_filters as af
from services.aml_builder.web.services import tools as aml_tools
from services.aml_builder.web.services.plan_renderer import build_plan_markdown

CATALOGS: Dict[str, Dict[str, str]] = {
    "account_status": {"1": "OPEN", "2": "CLOSED", "3": "DORMANT", "DMT": "Dormant", "8": "BLACK LISTED"},
    "account_nature": {"1": "Business", "3": "Savings"},
    "account_product": {"11": "ATM", "1": "CURRENT"},
    "account_type": {"2": "NON RESIDENT"},
    "individual_corporate": dict(af.BY_KEY["individual_corporate"].fixed_values),
    "profession": {"1": "Accountant", "TEACHER": "TEACHER", "Teacher": "TEACHER"},
    "occupation": {"1": "DOCTOR"},
    "marital_status": {"2": "Married", "Married": "Married", "M": "Married", "1": "Single"},
    "customer_class": {"1": "Medium"},
    "city": {"123": "KHALDA"},
    "debit_credit": {"1": "Debit", "2": "Credit"},
}


@pytest.fixture(autouse=True)
def _catalogs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve the catalogs above instead of the lookup tables.

    Args:
        monkeypatch (pytest.MonkeyPatch): The pytest monkeypatch fixture.
    """
    monkeypatch.setattr(af, "load_catalog", lambda attribute: dict(CATALOGS[attribute.key]))


def _fake_llm(monkeypatch: pytest.MonkeyPatch, answer: Dict[str, Any], prompts: List[str]) -> None:
    """Make the resolver LLM return a fixed answer.

    Args:
        monkeypatch (pytest.MonkeyPatch): The pytest monkeypatch fixture.
        answer (Dict[str, Any]): The JSON the LLM returns.
        prompts (List[str]): Collects the prompts sent.
    """
    class FakeLLM:
        def invoke(self, prompt: str) -> Any:
            prompts.append(prompt)
            return SimpleNamespace(content=json.dumps(answer))

    monkeypatch.setattr(af, "build_llm", lambda fast=False: FakeLLM())


DORMANT = {"attribute": "account_status", "operator": "IN", "codes": ["3", "DMT"], "phrase": "dormant"}


# ─────────────────────────── resolution ───────────────────────────


def test_dormant_accounts_resolve_to_every_dormant_status_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """The resolver sees only the relevant catalogs; its codes are validated and described."""
    prompts: List[str] = []
    _fake_llm(monkeypatch, {"filters": [DORMANT]}, prompts)

    filters = af.resolve_attribute_filters("Cash deposits over 10,000 JOD into dormant accounts within 7 days")

    assert filters == [{
        "attribute": "account_status", "label": "Account status", "entity": "ACCOUNT", "column": "STA_CODE",
        "operator": "IN", "codes": ["3", "DMT"], "descriptions": ["DORMANT", "Dormant"], "phrase": "dormant",
    }]
    assert "account_status | Account status | account" in prompts[0]
    assert "marital_status" not in prompts[0]


def test_no_attribute_words_means_no_llm_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """A plain transaction-type request does not reach the resolver."""
    prompts: List[str] = []
    _fake_llm(monkeypatch, {"filters": [DORMANT]}, prompts)

    assert af.resolve_attribute_filters("Cash withdrawals above 50,000 within 30 days") == []
    assert prompts == []


def test_codes_outside_the_catalog_are_dropped() -> None:
    """Invented codes, unknown attributes and empty filters never reach the SQL."""
    filters = af.validate_filters([
        {"attribute": "account_status", "codes": ["3", "999"], "phrase": "dormant"},
        {"attribute": "favourite_colour", "codes": ["1"]},
        {"attribute": "marital_status", "codes": ["X"]},
        {"attribute": "individual_corporate", "operator": "not_in", "codes": ["1", "I"]},
    ])

    assert [(f["attribute"], f["operator"], f["codes"]) for f in filters] == [
        ("account_status", "IN", ["3"]),
        ("individual_corporate", "NOT IN", ["1", "I"]),
    ]


def test_resolver_failure_means_no_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    """An LLM error never fails intent parsing."""
    class Broken:
        def invoke(self, prompt: str) -> Any:
            raise RuntimeError("timeout")

    monkeypatch.setattr(af, "build_llm", lambda fast=False: Broken())
    assert af.resolve_attribute_filters("married customers") == []


# ─────────────────────────── SQL ───────────────────────────

PER_TRANSACTION = (
    "SELECT t.CUS_NUM, t.TRA_DATE, t.EQU_TRA_AMT\nFROM BI_DWH.PIO_TRANSACTIONS t\n"
    "WHERE t.COUNTRY_CODE = 400 AND t.EXPL_CODE IN ('1')\n  AND t.EQU_TRA_AMT > 10000"
)
GROUPED = (
    "SELECT t.CUS_NUM, SUM(t.EQU_TRA_AMT) TOTAL\nFROM BI_DWH.PIO_TRANSACTIONS t\n"
    "WHERE t.COUNTRY_CODE = 400 AND t.EXPL_CODE IN ('1')\nGROUP BY t.CUS_NUM\nHAVING SUM(t.EQU_TRA_AMT) > 10000"
)


def test_account_filter_goes_into_the_where_clause_on_the_latest_snapshot() -> None:
    """Transactions on dormant accounts only, judged by each account's latest snapshot."""
    sql, applied = af.apply_attribute_filters(PER_TRANSACTION, [DORMANT])

    assert sql.startswith(PER_TRANSACTION + "\n  AND t.ACCOUNT_NUMBER IN (SELECT e.ACCOUNT_NUMBER FROM BI_DWH.PIO_ACCOUNTS e")
    assert "e.STA_CODE IN ('3', 'DMT')" in sql
    assert "(e.ACCOUNT_NUMBER, e.DAY_DATE) IN (SELECT ACCOUNT_NUMBER, MAX(DAY_DATE) FROM BI_DWH.PIO_ACCOUNTS" in sql
    assert applied == [{"attribute": "account_status", "label": "Account status", "operator": "IN",
                        "descriptions": ["DORMANT", "Dormant"], "applied": "where"}]


def test_filters_go_before_group_by_so_aggregates_count_only_matching_rows() -> None:
    """Customer, account and transaction filters land in WHERE, ahead of GROUP BY / HAVING."""
    filters = [
        DORMANT,
        {"attribute": "individual_corporate", "codes": ["2", "C"], "phrase": "corporate"},
        {"attribute": "debit_credit", "codes": ["2"], "phrase": "credit"},
    ]
    sql, applied = af.apply_attribute_filters(GROUPED, filters)

    where, rest = sql.split("\nGROUP BY t.CUS_NUM\n")
    assert "t.CUS_NUM IN (SELECT e.CUS_NUM FROM BI_DWH.PIO_CUSTOMERS e" in where
    assert "e.INDV_CORP_IND IN ('2', 'C')" in where
    assert "t.ACCOUNT_NUMBER IN (SELECT e.ACCOUNT_NUMBER FROM BI_DWH.PIO_ACCOUNTS e" in where
    assert "t.DEB_CRE_IND IN ('2')" in where
    assert rest == "HAVING SUM(t.EQU_TRA_AMT) > 10000"
    assert {a["applied"] for a in applied} == {"where"}


def test_negated_filter_and_missing_where() -> None:
    """'excluding closed accounts' becomes NOT IN; a statement without WHERE gets one."""
    sql, _ = af.apply_attribute_filters(
        "SELECT t.CUS_NUM FROM BI_DWH.PIO_TRANSACTIONS t GROUP BY t.CUS_NUM",
        [{"attribute": "account_status", "operator": "NOT IN", "codes": ["2"], "phrase": "closed"}],
    )
    assert sql.startswith("SELECT t.CUS_NUM FROM BI_DWH.PIO_TRANSACTIONS t\nWHERE t.ACCOUNT_NUMBER IN (")
    assert "e.STA_CODE NOT IN ('2')" in sql and sql.endswith("GROUP BY t.CUS_NUM")


def test_a_statement_that_cannot_be_split_is_filtered_on_its_customers() -> None:
    """A self-join is wrapped; the customer filter applies to its result, a transaction filter is skipped."""
    joined = ("SELECT a.CUS_NUM FROM BI_DWH.PIO_TRANSACTIONS a JOIN BI_DWH.PIO_TRANSACTIONS b "
              "ON a.ACCOUNT_NUMBER = b.ACCOUNT_NUMBER")
    sql, applied = af.apply_attribute_filters(joined, [
        {"attribute": "marital_status", "codes": ["2", "Married", "M"], "phrase": "married"},
        {"attribute": "debit_credit", "codes": ["1"], "phrase": "debit"},
    ])

    assert sql.startswith("SELECT r.* FROM (\n" + joined + "\n) r\nWHERE r.CUS_NUM IN (SELECT e.CUS_NUM FROM BI_DWH.PIO_CUSTOMERS e")
    assert [a["applied"] for a in applied] == ["result", "skipped"]


def test_codes_are_quoted_safely() -> None:
    """A quote inside a code cannot break the SQL."""
    CATALOGS["city"]["O'X"] = "ODD"
    try:
        sql, _ = af.apply_attribute_filters(PER_TRANSACTION, [{"attribute": "city", "codes": ["O'X"]}])
    finally:
        del CATALOGS["city"]["O'X"]
    assert "e.CITY_LOC_CODE IN ('O''X')" in sql


# ─────────────────────────── text sent to PioTech ───────────────────────────


def test_attribute_wording_is_removed_from_the_text_pioteck_reads() -> None:
    """Phrase, matching condition, matching segment and the filter list itself are removed."""
    intent = {
        "detection_logic": "Corporate customers depositing over 10,000 JOD into dormant accounts.",
        "semantic_conditions": [{"raw_phrase": "dormant accounts"}, {"raw_phrase": "within 7 days"}],
        "customer_segments": ["CORPORATE"],
        "attribute_filters": [DORMANT],
        "thresholds": [{"field": "amount", "value_from": 10000}],
    }
    filters = af.validate_filters([DORMANT, {"attribute": "individual_corporate", "codes": ["2"], "phrase": "corporate"}])

    cleaned = af.strip_attribute_phrases(intent, filters)

    assert cleaned["detection_logic"] == "customers depositing over 10,000 JOD into accounts."
    assert cleaned["semantic_conditions"] == [{"raw_phrase": "within 7 days"}]
    assert cleaned["customer_segments"] is None and "attribute_filters" not in cleaned
    assert cleaned["thresholds"] == intent["thresholds"] and intent["customer_segments"] == ["CORPORATE"]


def test_restriction_words_leave_every_free_text_field() -> None:
    """Live case: 'dormant' survived in the name and defaults, and PioTech added its own inactivity rule."""
    intent = {
        "scenario_name": "Dormant Account Cash Deposits",
        "detection_logic": "Cash deposits above 10,000 JOD into dormant accounts in 2019.",
        "applied_defaults": ["Dormancy is not derived; dormant accounts are taken as given."],
        "mapped_keywords": {"dormant": "STA_CODE 3", "above": "10000"},
        "semantic_conditions": [{"raw_phrase": "into dormant accounts", "predicate": "account is dormant"}],
        "transaction_types": ["CASH DEPOSIT"],
    }
    cleaned = af.strip_attribute_phrases(intent, af.validate_filters([{**DORMANT, "phrase": "dormant accounts"}]))

    assert "dormant" not in json.dumps(cleaned).lower()
    assert cleaned["scenario_name"] == "Account Cash Deposits"
    assert cleaned["mapped_keywords"] == {"above": "10000"} and cleaned["semantic_conditions"] == []
    assert cleaned["transaction_types"] == ["CASH DEPOSIT"]


def test_a_threshold_that_restates_the_attribute_is_dropped() -> None:
    """Live case: PioTech refused 'account_status = CLOSED'; the amount threshold stays."""
    amount = {"field": "transaction_amount", "operator": ">", "value_from": 10000, "target_scope": "DETAIL"}
    intent = {"detection_logic": "Cash deposits above 10,000 JOD into closed accounts.", "thresholds": [
        {"field": "account_status", "operator": "=", "value_from": "CLOSED", "target_scope": "DETAIL"},
        {"field": "status_flag", "operator": "IN", "value_from": "closed", "target_scope": "DETAIL"},
        amount,
    ]}
    cleaned = af.strip_attribute_phrases(intent, af.validate_filters([
        {"attribute": "account_status", "codes": ["2"], "phrase": "closed accounts"}]))

    assert cleaned["thresholds"] == [amount]


def test_transaction_types_that_share_a_word_are_kept() -> None:
    """'credit' is removed as a direction, never from 'credit card' or a structured type."""
    intent = {
        "detection_logic": "Credit transactions above 500,000 JOD, including credit card payments.",
        "transaction_types": ["CREDIT TRANSFER"],
        "thresholds": [{"field": "credit_amount", "transaction_type": "CREDIT TRANSFER"}],
    }
    cleaned = af.strip_attribute_phrases(intent, af.validate_filters([
        {"attribute": "debit_credit", "codes": ["2"], "phrase": "credit transactions"}]))

    assert cleaned["detection_logic"] == "transactions above 500,000 JOD, including credit card payments."
    assert cleaned["transaction_types"] == ["CREDIT TRANSFER"] and cleaned["thresholds"] == intent["thresholds"]


# ─────────────────────────── priority over explanation codes ───────────────────────────

DEBIT_CREDIT = {"attribute": "debit_credit", "codes": ["1", "2"], "phrase": "debit or credit transactions"}


@pytest.mark.parametrize(
    "tx_type, covered",
    [
        ("CREDIT", True),
        ("DEBIT", True),
        ("credit transactions", True),
        ("Debit or Credit", True),
        ("CREDIT CARD", False),
        ("CREDIT TRANSFER", False),
        ("CASH DEPOSIT", False),
        ("", False),
    ],
)
def test_only_pure_filter_values_count_as_covered_types(tx_type: str, covered: bool) -> None:
    """A type made only of the filter's words is covered; a real type sharing a word is not."""
    assert af.is_covered_type(tx_type, af.validate_filters([DEBIT_CREDIT])) is covered


def test_debit_credit_types_are_dropped_with_their_codes() -> None:
    """The user's case: 'Transaction Types CREDIT, DEBIT' plus 'Debit / credit' -> only the filter remains."""
    intent = {
        "transaction_type": "CREDIT, DEBIT",
        "transaction_types": ["CREDIT", "DEBIT"],
        "explanation_codes": ["2135", "2107"],
        "explanation_codes_by_type": {"CREDIT": ["2135"], "DEBIT": ["2107"]},
        "thresholds": [{"field": "transaction_amount", "value_from": 500000, "transaction_type": "CREDIT",
                        "explanation_codes": ["2135"]}],
    }
    updated, dropped = af.drop_covered_transaction_types(intent, af.validate_filters([DEBIT_CREDIT]))

    assert dropped == ["CREDIT", "DEBIT"]
    assert updated["transaction_types"] is None and updated["transaction_type"] is None
    assert updated["explanation_codes"] is None and updated["explanation_codes_by_type"] is None
    assert updated["thresholds"][0]["transaction_type"] is None and updated["thresholds"][0]["explanation_codes"] is None
    assert updated["thresholds"][0]["value_from"] == 500000 and intent["transaction_types"] == ["CREDIT", "DEBIT"]


def test_a_real_type_next_to_a_covered_one_keeps_its_codes() -> None:
    """'credit transactions and cash withdrawals': CREDIT goes, CASH WITHDRAWAL and its codes stay."""
    intent = {
        "transaction_types": ["CREDIT", "CASH WITHDRAWAL"],
        "explanation_codes": ["2135", "2", "466"],
        "explanation_codes_by_type": {"CREDIT": ["2135"], "CASH WITHDRAWAL": ["2", "466"]},
        "thresholds": [{"field": "amount", "transaction_type": "CASH WITHDRAWAL", "explanation_codes": ["2", "466"]}],
    }
    updated, dropped = af.drop_covered_transaction_types(intent, af.validate_filters([
        {"attribute": "debit_credit", "codes": ["2"], "phrase": "credit transactions"}]))

    assert dropped == ["CREDIT"]
    assert updated["transaction_types"] == ["CASH WITHDRAWAL"] and updated["transaction_type"] == "CASH WITHDRAWAL"
    assert updated["explanation_codes"] == ["2", "466"]
    assert updated["explanation_codes_by_type"] == {"CASH WITHDRAWAL": ["2", "466"]}
    assert updated["thresholds"] == intent["thresholds"]


def test_no_filter_or_no_covered_type_changes_nothing() -> None:
    """Without a matching filter the intent is returned as it is."""
    intent = {"transaction_types": ["CASH DEPOSIT"], "explanation_codes": ["1"]}
    assert af.drop_covered_transaction_types(intent, []) == (intent, [])
    assert af.drop_covered_transaction_types(intent, af.validate_filters([DORMANT])) == (intent, [])


def test_intent_tool_skips_code_search_for_covered_types(monkeypatch: pytest.MonkeyPatch) -> None:
    """End to end through the intent tool: no explanation-code search, the plan shows only the filter."""
    llm_intent = {
        "scenario_name": "Large debit or credit transactions", "scenario_type": "TRANSACTION",
        "detection_logic": "Debit or credit transactions above 500,000 JOD in 2019.",
        "transaction_type": "CREDIT, DEBIT", "transaction_types": ["CREDIT", "DEBIT"],
        "thresholds": [{"field": "transaction_amount", "operator": ">", "value_from": 500000,
                        "target_scope": "DETAIL", "transaction_type": "CREDIT"}],
        "semantic_conditions": [],
    }

    class IntentLLM:
        def invoke(self, _messages: Any) -> Any:
            return SimpleNamespace(content=json.dumps(llm_intent))

    searched: List[Any] = []
    monkeypatch.setattr(aml_tools, "build_llm", lambda fast=False: IntentLLM())
    monkeypatch.setattr(aml_tools, "select_relevant_explanation_codes", lambda types, *a, **k: searched.append(types) or [])
    monkeypatch.setattr(aml_tools, "seed_scenario_metadata", lambda intent, existing=None: {})
    _fake_llm(monkeypatch, {"filters": [DEBIT_CREDIT]}, [])

    result = json.loads(aml_tools.analyze_intent_and_discover_explanation_codes.invoke(
        {"user_prompt": "Flag debit or credit transactions above 500,000 JOD in 2019"}))
    enriched = result["enriched_intent"]

    assert searched == []
    assert enriched["transaction_types"] is None and enriched["explanation_codes"] is None
    assert [f["attribute"] for f in enriched["attribute_filters"]] == ["debit_credit"]
    assert "Transaction Type" not in result["plan_artifact"]
    assert "| **Debit / credit** | `Debit, Credit` |" in result["plan_artifact"]


def test_plan_lists_the_filters() -> None:
    """The plan overview shows each filter in business terms."""
    plan = build_plan_markdown({"scenario_name": "x", "detection_logic": "x",
                                "attribute_filters": af.validate_filters([DORMANT])}, None)
    assert "| **Account status** | `DORMANT, Dormant` |" in plan


# ─────────────────────────── shadow test end to end ───────────────────────────


def test_shadow_test_applies_the_filter_and_hides_it_from_piotech(monkeypatch: pytest.MonkeyPatch) -> None:
    """PioTech gets no 'dormant' wording; the SQL that runs (and is saved) carries the account filter."""
    sent: List[Dict[str, Any]] = []
    generated = PER_TRANSACTION.replace("AND t.EQU_TRA_AMT > 10000", "AND t.EQU_TRA_AMT > 10000 AND t.TRA_DATE >= SYSDATE - 7")
    body = "".join(f"data: {json.dumps(e)}\n\n" for e in [{"type": "final_answer", "text": generated}, {"type": "done"}])

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, content=body.encode("utf-8"))

    real_client = httpx.Client
    monkeypatch.setattr(aml_tools.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    executed: List[str] = []

    def fake_db(sql: str, params: Any = None) -> Any:
        executed.append(sql)
        return (["cus_num", "tra_date"], []) if sql.endswith("WHERE 1 = 0") else (["n", "c"], [(5, 2)])

    monkeypatch.setattr(aml_tools, "run_shadow_readonly", fake_db)
    intent = {"scenario_name": "Deposits into dormant accounts", "scenario_type": "ACCOUNT",
              "detection_logic": "Cash deposits over 10,000 JOD into dormant accounts within 7 days",
              "anchor_date": "2019-06-30", "attribute_filters": [DORMANT]}

    result = json.loads(aml_tools.execute_oracle_dwh_shadow_test.invoke({"intent_json": json.dumps(intent)}))

    request_text = json.dumps(sent[0]).lower()
    assert "dormant" not in request_text and "attribute_filters" not in request_text
    assert "t.ACCOUNT_NUMBER IN (SELECT e.ACCOUNT_NUMBER FROM BI_DWH.PIO_ACCOUNTS e" in result["raw_sql"]
    assert "e.STA_CODE IN ('3', 'DMT')" in executed[-1]
    assert result["attribute_filters_applied"][0]["applied"] == "where"
    assert result["metrics_summary"] == "5 transactions across 2 distinct customers"


# ─────────────────────────── demo lookup fill ───────────────────────────


@pytest.mark.parametrize(
    "key, code, expected",
    [
        ("marital_status", "Unmarried", "Single"),
        ("marital_status", "MERED", "Married"),
        ("marital_status", "5", "MARITAL STATUS 5 (MEANING UNKNOWN - DEMO FILL)"),
        ("account_status", "8", "BLACK LISTED"),
        ("debit_credit", "1", "Debit"),
        ("debit_credit", "2", "Credit"),
        ("profession", "  Sinior  HR Analyst", "SINIOR HR ANALYST"),
        ("customer_class", "EMPL_GOVT", "EMPL GOVT"),
        ("customer_class", "HH", "CUSTOMER CLASSIFICATION HH (MEANING UNKNOWN - DEMO FILL)"),
        ("city", "R101", "CITY CODE R101 (NAME UNKNOWN - DEMO FILL)"),
    ],
)
def test_fill_descriptions_are_grounded_or_marked_unknown(key: str, code: str, expected: str) -> None:
    """Known meanings come from the database; anything else is labelled as unknown, never invented."""
    assert fill.describe(af.BY_KEY[key], code)[0] == expected


def test_rollback_script_targets_exactly_the_inserted_keys() -> None:
    """The rollback deletes by full key, escapes quotes, and is never run by the script."""
    rows = [{"table": "BI_DWH.PIO_CITIES", "key": {"COUNTRY_CODE": 400, "INST_CODE": 1, "CITY_CODE": "O'X"}}]
    script = fill.rollback_sql(rows, "lookup_fills_x.json")

    assert "DELETE FROM BI_DWH.PIO_CITIES WHERE COUNTRY_CODE = 400 AND INST_CODE = 1 AND CITY_CODE = 'O''X';" in script
    assert "NOT executed automatically" in script
