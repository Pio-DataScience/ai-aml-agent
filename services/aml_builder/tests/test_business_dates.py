"""Tests for business-date anchoring: no SYSDATE in scenario SQL, Sunday-Thursday windows.

No database, network or LLM is needed: the query helpers and the PioTech stream are mocked.

Usage:
    PYTHONPATH=services/aml_builder pytest services/aml_builder/tests/test_business_dates.py
"""

import json
import re
from datetime import date
from typing import Any, Dict, List

import httpx
import pytest

from services.aml_builder.web.services import alert_engine
from services.aml_builder.web.services import tools as aml_tools
from services.aml_builder.web.services.business_dates import (
    apply_fixed_period,
    clean_for_sql_service,
    find_fixed_period,
    is_business_day,
    latest_business_day,
    period_of,
    window_for_period,
    window_start,
)
from services.aml_builder.web.services.plan_renderer import build_plan_markdown
from services.aml_builder.web.services.settings import settings
from services.aml_builder.web.services.sql_dates import anchor_sql, bind_values, binds_used, pin_period

SUNDAY = date(2019, 6, 30)
THURSDAY = date(2019, 6, 27)
FRIDAY = date(2019, 6, 28)
SATURDAY = date(2019, 6, 29)
NO_CLOCK = re.compile(r"\b(SYSDATE|CURRENT_DATE|SYSTIMESTAMP)\b", re.IGNORECASE)


@pytest.fixture(autouse=True)
def _weekend(monkeypatch: pytest.MonkeyPatch) -> None:
    """Use the default Friday-Saturday weekend in every test.

    Args:
        monkeypatch (pytest.MonkeyPatch): The pytest monkeypatch fixture.
    """
    monkeypatch.setattr(settings, "BUSINESS_WEEKEND_DAYS", "FRI,SAT")


# ─────────────────────────── business-day arithmetic ───────────────────────────


def test_friday_and_saturday_are_not_business_days() -> None:
    """Sunday-Thursday are business days."""
    assert [is_business_day(date(2019, 6, d)) for d in range(23, 30)] == [True, True, True, True, True, False, False]


def test_window_counts_back_over_business_days_only() -> None:
    """7 business days ending Sunday 30 June: Sun 30, Thu 27, Wed 26, Tue 25, Mon 24, Sun 23, Thu 20."""
    assert window_start(SUNDAY, 1) == SUNDAY
    assert window_start(SUNDAY, 2) == THURSDAY
    assert window_start(SUNDAY, 7) == date(2019, 6, 20)


def test_weekend_anchor_counts_from_the_previous_business_day() -> None:
    """An anchor on Friday or Saturday starts counting from Thursday."""
    assert latest_business_day(SATURDAY) == THURSDAY
    assert window_start(FRIDAY, 1) == THURSDAY
    assert window_start(SATURDAY, 2) == date(2019, 6, 26)


def test_weekend_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    """BUSINESS_WEEKEND_DAYS changes which days are skipped."""
    monkeypatch.setattr(settings, "BUSINESS_WEEKEND_DAYS", "SAT,SUN")
    assert not is_business_day(SUNDAY) and is_business_day(FRIDAY)


# ─────────────────────────── SQL rewrite ───────────────────────────


@pytest.mark.parametrize(
    "generated, expected",
    [
        ("WHERE t.TRA_DATE >= SYSDATE - 7",
         "WHERE t.TRA_DATE >= :bd_minus_7 AND t.TRA_DATE < :as_of_date + 1"),
        ("WHERE t.TRA_DATE >= TRUNC(SYSDATE) - 1",
         "WHERE t.TRA_DATE >= :bd_minus_1 AND t.TRA_DATE < :as_of_date + 1"),
        ("WHERE t.TRA_DATE >= ADD_MONTHS(SYSDATE, -1)",
         "WHERE t.TRA_DATE >= ADD_MONTHS(:as_of_date, -1) AND t.TRA_DATE < :as_of_date + 1"),
        ("WHERE (t.TRA_DATE >= TRUNC(SYSDATE, 'MM'))",
         "WHERE (t.TRA_DATE >= TRUNC(:as_of_date, 'MM') AND t.TRA_DATE < :as_of_date + 1)"),
        ("WHERE t.TRA_DATE >= SYSDATE - 210 AND t.TRA_DATE < SYSDATE - 30",
         "WHERE t.TRA_DATE >= :bd_minus_210 AND t.TRA_DATE < :bd_minus_30"),
        ("where t.tra_date >= sysdate - interval '15' day",
         "where t.tra_date >= :bd_minus_15 AND t.tra_date < :as_of_date + 1"),
        ("WHERE t.TRA_DATE >= ADD_MONTHS(TRUNC(SYSDATE, 'YYYY'), -24)",
         "WHERE t.TRA_DATE >= ADD_MONTHS(TRUNC(:as_of_date, 'YYYY'), -24) AND t.TRA_DATE < :as_of_date + 1"),
        ("WHERE (t.TRA_DATE >= SYSDATE - (7 * 2))",
         "WHERE (t.TRA_DATE >= :bd_minus_14 AND t.TRA_DATE < :as_of_date + 1)"),
    ],
)
def test_every_clock_form_becomes_a_business_date_bind(generated: str, expected: str) -> None:
    """Each SYSDATE form maps to the business-date binds, with an upper bound added."""
    out = anchor_sql(f"SELECT 1 FROM BI_DWH.PIO_TRANSACTIONS t {generated}")

    assert out.endswith(expected)
    assert not NO_CLOCK.search(out)
    assert anchor_sql(out) == out  # idempotent


def test_string_literals_are_never_edited() -> None:
    """A quoted 'SYSDATE' stays as written."""
    out = anchor_sql("SELECT 'SYSDATE - 5' n FROM x t WHERE t.TRA_DATE >= SYSDATE - 3 AND t.X = 'it''s'")
    assert "'SYSDATE - 5'" in out and "'it''s'" in out and ":bd_minus_3" in out


def test_weeks_count_five_business_days() -> None:
    """A 2-week window written as SYSDATE - 14 becomes 10 business days."""
    out = anchor_sql("SELECT 1 FROM x t WHERE t.TRA_DATE >= SYSDATE - 14",
                     {"time_window": {"unit": "WEEKS", "value": 2}})
    assert ":bd_minus_10" in out


def test_bind_values_cover_exactly_the_binds_used() -> None:
    """Oracle rejects unused binds, so only the ones in the SQL are returned."""
    sql = anchor_sql("SELECT 1 FROM x t WHERE t.TRA_DATE >= SYSDATE - 7")
    values = bind_values(sql, SUNDAY)

    assert binds_used(sql) == ["as_of_date", "bd_minus_7"]
    assert values == {"as_of_date": SUNDAY, "bd_minus_7": date(2019, 6, 20)}


# ─────────────────────────── shadow test and alert engine ───────────────────────────


def _sse(events: List[Dict[str, Any]]) -> bytes:
    """Encode events as an SSE body.

    Args:
        events (List[Dict[str, Any]]): Event payloads.

    Returns:
        bytes: The SSE stream body.
    """
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode("utf-8")


def test_shadow_test_runs_anchored_sql_with_business_date_binds(monkeypatch: pytest.MonkeyPatch) -> None:
    """PioTech's SYSDATE SQL is anchored; the shadow COUNT runs with binds for the scenario's anchor_date."""
    generated = "SELECT t.CUS_NUM FROM BI_DWH.PIO_TRANSACTIONS t WHERE t.TRA_DATE >= SYSDATE - 7 GROUP BY t.CUS_NUM"
    body = _sse([{"type": "final_answer", "text": generated}, {"type": "done"}])
    real_client = httpx.Client
    monkeypatch.setattr(aml_tools.httpx, "Client",
                        lambda **kw: real_client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=body)), **kw))
    executed: List[Any] = []

    def fake_run(sql: str, params: Any = None) -> Any:
        executed.append((sql, params))
        if sql.endswith("WHERE 1 = 0"):
            return ["cus_num"], []
        return ["count", "customers"], [(9, 4)]

    monkeypatch.setattr(aml_tools, "run_shadow_readonly", fake_run)
    intent = {"scenario_name": "x", "scenario_type": "CUSTOMER", "detection_logic": "x", "anchor_date": "2019-06-30"}

    result = json.loads(aml_tools.execute_oracle_dwh_shadow_test.invoke({"intent_json": json.dumps(intent)}))

    sql, params = executed[-1]
    assert not NO_CLOCK.search(sql) and not NO_CLOCK.search(result["raw_sql"])
    assert params == {"as_of_date": SUNDAY, "bd_minus_7": date(2019, 6, 20)}
    assert result["evaluation_date"] == "2019-06-30" and result["evaluation_date_source"] == "scenario anchor_date"
    assert result["window_dates"]["bd_minus_7"] == "2019-06-20"
    assert result["header_alert_count"] == 4


def test_shadow_anchor_falls_back_to_the_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without an anchor_date the SHADOW_ANCHOR_DATE setting is used."""
    monkeypatch.setattr(settings, "SHADOW_ANCHOR_DATE", "2019-04-30")
    assert aml_tools._shadow_anchor({}) == (date(2019, 4, 30), "SHADOW_ANCHOR_DATE setting")


def test_alert_engine_anchors_legacy_sysdate_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    """A rule saved with SYSDATE runs with binds for the evaluation date, never the clock."""
    executed: List[Any] = []
    monkeypatch.setattr(alert_engine, "run_shadow_readonly",
                        lambda sql, params=None: (executed.append((sql, params)) or (["CUS_NUM"], [("1",)])))

    cols, rows = alert_engine.execute_scenario_query(
        "SELECT t.CUS_NUM FROM BI_DWH.PIO_TRANSACTIONS t WHERE t.TRA_DATE >= TRUNC(SYSDATE) - 2", eval_date=SATURDAY
    )

    sql, params = executed[0]
    assert not NO_CLOCK.search(sql)
    assert params == {"as_of_date": SATURDAY, "bd_minus_2": date(2019, 6, 26)}
    assert cols == ["CUS_NUM"] and rows == [("1",)]


# ─────────────────────────── explicit fixed periods ───────────────────────────


@pytest.mark.parametrize(
    "text, first, last",
    [
        ("Flag any single cash withdrawal above 50,000 between 2016 and 2022", date(2016, 1, 1), date(2022, 12, 31)),
        ("cash deposits in 2019", date(2019, 1, 1), date(2019, 12, 31)),
        ("deposits over 10,000 JOD from 2020 into 2022", date(2020, 1, 1), date(2022, 12, 31)),
        ("withdrawals from March 2020 to June 2021", date(2020, 3, 1), date(2021, 6, 30)),
        ("transfers 2020-01-01 to 2020-03-31", date(2020, 1, 1), date(2020, 3, 31)),
        ("deposits from 1 Jan 2020 to 15 Feb 2020", date(2020, 1, 1), date(2020, 2, 15)),
        ("transactions 2016-2022", date(2016, 1, 1), date(2022, 12, 31)),
    ],
)
def test_explicit_periods_are_found(text: str, first: date, last: date) -> None:
    """Year, month and day ranges, and single years, give their first and last day."""
    found = find_fixed_period(text)
    assert found is not None and found[:2] == (first, last)


@pytest.mark.parametrize(
    "text",
    [
        "3 or more cash deposits between 8,000 and 9,999 within 2 days",
        "deposits between 1990 and 2000 JOD in 7 days",
        "more than 2020 transactions in 30 days",
        "withdrawals above $2000 within 7 days",
        "deposits totalling 2,019 JOD in 30 days",
    ],
)
def test_amounts_are_never_read_as_years(text: str) -> None:
    """Numbers with a currency, a separator or '$' are amounts, not a period."""
    assert find_fixed_period(text) is None


@pytest.mark.parametrize(
    "first, last, window",
    [
        (date(2016, 1, 1), date(2022, 12, 31), ("YEARS", 7)),
        (date(2020, 3, 1), date(2021, 6, 30), ("MONTHS", 16)),
        (date(2020, 1, 1), date(2020, 2, 15), ("DAYS", 46)),
    ],
)
def test_window_is_the_exact_length_of_the_period(first: date, last: date, window: Any) -> None:
    """The window is counted from the stated range, and period_of gives the range back."""
    tw = window_for_period(first, last)
    assert (tw["unit"], tw["value"], tw["is_rolling"]) == (*window, False)
    assert period_of({"anchor_date": last.isoformat(), "time_window": tw}) == (first, last)


def test_period_sets_the_evaluation_date_to_its_end() -> None:
    """A 7-year rolling window with no anchor becomes 2016-2022 ending 31 Dec 2022."""
    intent = {"detection_logic": "x", "time_window": {"unit": "YEARS", "value": 7, "is_rolling": True}}
    updated, period = apply_fixed_period(intent, "Flag any single cash withdrawal above 50,000 between 2016 and 2022")

    assert period == (date(2016, 1, 1), date(2022, 12, 31))
    assert updated["anchor_date"] == "2022-12-31"
    assert updated["time_window"] == {"unit": "YEARS", "value": 7, "is_rolling": False, "provenance": "stated"}
    assert "anchor_date" not in intent  # the input is not modified


def test_singular_units_from_the_llm_are_read_as_years() -> None:
    """Live case: the LLM wrote {'unit': 'YEAR', 'value': 6}; it is the period, not a 6-day inner window."""
    intent = {"time_window": {"unit": "YEAR", "value": 6, "is_rolling": False}}
    updated, period = apply_fixed_period(intent, "corporate customers, cash withdrawal above 50,000 between 2016 and 2022")

    assert updated["time_window"]["unit"] == "YEARS" and updated["time_window"]["value"] == 7
    assert period_of({"anchor_date": "2022-12-31", "time_window": {"unit": "YEAR", "value": 7, "is_rolling": False}}) == (
        date(2016, 1, 1), date(2022, 12, 31))


def test_a_short_stated_window_inside_the_period_is_kept() -> None:
    """'within 7 days, from 2020 into 2022': evaluated on 31 Dec 2022, still a 7-day window."""
    window = {"unit": "DAYS", "value": 7, "is_rolling": True, "provenance": "stated"}
    updated, period = apply_fixed_period({"time_window": window}, "more than 10,000 JOD within 7 days, from 2020 into 2022")

    assert period == (date(2020, 1, 1), date(2022, 12, 31))
    assert updated["anchor_date"] == "2022-12-31" and updated["time_window"] == window
    assert period_of(updated) is None  # so the SQL window is not widened to the period


def test_a_rolling_window_is_not_a_fixed_period() -> None:
    """Without a stated period nothing changes and no period is reported."""
    intent = {"anchor_date": "2019-06-30", "time_window": {"unit": "DAYS", "value": 7, "is_rolling": True}}
    assert apply_fixed_period(intent, "cash deposits over 10,000 JOD within 7 days") == (intent, None)


def test_period_wording_is_removed_from_the_text_sent_for_sql() -> None:
    """The years never reach PioTech as text; the threshold and other conditions stay."""
    intent = {
        "detection_logic": "Single cash withdrawal above 50,000 between 2016 and 2022.",
        "semantic_conditions": [
            {"raw_phrase": "between 2016 and 2022", "predicate": "date range"},
            {"raw_phrase": "single cash withdrawal", "predicate": "per transaction"},
        ],
        "thresholds": [{"field": "amount", "operator": ">", "value_from": 50000}],
    }
    cleaned = clean_for_sql_service(intent)

    assert cleaned["detection_logic"] == "Single cash withdrawal above 50,000."
    assert [c["raw_phrase"] for c in cleaned["semantic_conditions"]] == ["single cash withdrawal"]
    assert cleaned["thresholds"] == intent["thresholds"]
    assert len(intent["semantic_conditions"]) == 2  # the input is not modified


@pytest.mark.parametrize(
    "generated",
    [
        "WHERE t.TRA_DATE >= ADD_MONTHS(SYSDATE, -84)",
        "WHERE t.TRA_DATE >= SYSDATE - 2555",
        "WHERE t.TRA_DATE >= ADD_MONTHS(TRUNC(SYSDATE, 'YYYY'), -72)",
    ],
)
def test_sql_window_is_pinned_to_the_period(generated: str) -> None:
    """Whatever start PioTech writes, the SQL covers exactly 1 Jan 2016 to 31 Dec 2022."""
    sql = anchor_sql(f"SELECT t.CUS_NUM FROM BI_DWH.PIO_TRANSACTIONS t {generated} AND t.TRA_AMT > 50000")
    pinned, ok = pin_period(sql, date(2016, 1, 1), date(2022, 12, 31))

    assert ok
    assert pinned.endswith(
        "WHERE t.TRA_DATE >= DATE '2016-01-01' AND t.TRA_DATE < DATE '2023-01-01' AND t.TRA_AMT > 50000"
    )
    assert binds_used(pinned) == [] and not NO_CLOCK.search(pinned)


def test_two_different_window_starts_are_not_pinned() -> None:
    """A scenario with a baseline window keeps its binds."""
    sql = anchor_sql(
        "SELECT 1 FROM x t WHERE t.TRA_DATE >= SYSDATE - 7 "
        "AND EXISTS (SELECT 1 FROM y b WHERE b.TRA_DATE >= SYSDATE - 90)"
    )
    assert pin_period(sql, date(2016, 1, 1), date(2022, 12, 31)) == (sql, False)


def test_plan_shows_the_fixed_period() -> None:
    """The plan states the period and its evaluation date."""
    intent = {"scenario_name": "x", "detection_logic": "x", "anchor_date": "2022-12-31",
              "time_window": {"unit": "YEARS", "value": 7, "is_rolling": False}}
    plan = build_plan_markdown(intent, None)
    assert "Fixed period `2016-01-01` to `2022-12-31` (evaluation date `2022-12-31`)" in plan
    assert "Fixed Calendar Period (2016-01-01 to 2022-12-31)" in plan


def test_shadow_test_uses_the_stated_period(monkeypatch: pytest.MonkeyPatch) -> None:
    """End to end: clean text to PioTech, evaluation date 31 Dec 2022, SQL bounded to 2016-2022."""
    sent: List[Dict[str, Any]] = []
    generated = ("SELECT t.CUS_NUM FROM BI_DWH.PIO_TRANSACTIONS t WHERE t.TRA_DATE >= ADD_MONTHS(SYSDATE, -84) "
                 "AND t.TRA_AMT > 50000")
    body = _sse([{"type": "final_answer", "text": generated}, {"type": "done"}])

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, content=body)

    real_client = httpx.Client
    monkeypatch.setattr(aml_tools.httpx, "Client",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    executed: List[Any] = []
    monkeypatch.setattr(aml_tools, "run_shadow_readonly",
                        lambda sql, params=None: (executed.append((sql, params)) or (["COUNT(*)"], [(12,)])))
    intent = {"scenario_name": "Large cash withdrawal", "scenario_type": "CUSTOMER",
              "detection_logic": "Flag any single cash withdrawal above 50,000 between 2016 and 2022",
              "time_window": {"unit": "YEARS", "value": 7, "is_rolling": False}}

    result = json.loads(aml_tools.execute_oracle_dwh_shadow_test.invoke({"intent_json": json.dumps(intent)}))

    request_text = json.dumps(sent[0])
    assert "between 2016 and 2022" not in request_text and "2022-12-31" in request_text
    sql, params = executed[-1]
    assert "t.TRA_DATE >= DATE '2016-01-01' AND t.TRA_DATE < DATE '2023-01-01'" in sql
    assert not params
    assert result["evaluation_date"] == "2022-12-31"
    assert result["fixed_period"] == {"start": "2016-01-01", "end": "2022-12-31", "pinned_in_sql": True,
                                      "window_slides_over_period": False}
    assert result["header_alert_count"] == 12


# ─────────────────────────── measured shadow metrics ───────────────────────────


def _fake_db(columns: List[str], counts: tuple, executed: List[str]) -> Any:
    """A fake read-only runner: the 'WHERE 1 = 0' probe gets the columns, the metrics query the counts.

    Args:
        columns (List[str]): Result columns of the scenario SQL (lower case, as the pool returns them).
        counts (tuple): The row of the metrics query.
        executed (List[str]): Collects every SQL run.

    Returns:
        Any: A replacement for run_shadow_readonly.
    """
    def run(sql: str, params: Any = None) -> Any:
        executed.append(sql)
        if sql.endswith("WHERE 1 = 0"):
            return columns, []
        return ["count"] * len(counts), [counts]

    return run


def test_per_transaction_rule_reports_transactions_and_distinct_customers(monkeypatch: pytest.MonkeyPatch) -> None:
    """The user's 50,000 withdrawal rule: 120 transaction rows, 65 customers, no x4 estimate."""
    executed: List[str] = []
    monkeypatch.setattr(aml_tools, "run_shadow_readonly",
                        _fake_db(["cus_num", "tra_date", "tra_seq1", "equ_tra_amt"], (120, 65), executed))

    metrics = aml_tools._shadow_metrics("SELECT t.CUS_NUM, t.TRA_DATE, t.TRA_SEQ1, t.EQU_TRA_AMT FROM x t", {})

    assert "COUNT(DISTINCT shadow_query.CUS_NUM)" in executed[-1]
    assert metrics == {
        "matched_transaction_count": 120,
        "alerted_customer_count": 65,
        "result_row_count": 120,
        "header_alert_count": 65,
        "transaction_detail_count": 120,
        "alert_density_ratio": 1.85,
        "metrics_summary": "120 transactions across 65 distinct customers",
    }


def test_per_customer_rule_sums_its_event_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """A rule grouped per customer: rows are customers, transactions come from EVENT_COUNT."""
    executed: List[str] = []
    monkeypatch.setattr(aml_tools, "run_shadow_readonly",
                        _fake_db(["cus_num", "event_count", "total_amount"], (40, 40, 310), executed))

    metrics = aml_tools._shadow_metrics("SELECT CUS_NUM, COUNT(*) EVENT_COUNT ... GROUP BY CUS_NUM", {})

    assert "SUM(shadow_query.EVENT_COUNT)" in executed[-1]
    assert (metrics["matched_transaction_count"], metrics["alerted_customer_count"]) == (310, 40)
    assert metrics["metrics_summary"] == "310 transactions across 40 distinct customers"
    assert metrics["alert_density_ratio"] == 7.75


def test_per_customer_rule_without_a_count_column_does_not_guess(monkeypatch: pytest.MonkeyPatch) -> None:
    """No count column: the transaction figure is null, never estimated."""
    monkeypatch.setattr(aml_tools, "run_shadow_readonly", _fake_db(["cus_num", "total_amount"], (9, 9), []))

    metrics = aml_tools._shadow_metrics("SELECT CUS_NUM, SUM(x) TOTAL_AMOUNT ... GROUP BY CUS_NUM", {})

    assert metrics["matched_transaction_count"] is None and metrics["alert_density_ratio"] is None
    assert metrics["header_alert_count"] == 9
    assert metrics["metrics_summary"].startswith("9 distinct customers")


GROUPED_RULE = (
    "SELECT t.CUS_NUM\nFROM BI_DWH.PIO_TRANSACTIONS t\nWHERE t.COUNTRY_CODE = 400\n  AND t.EXPL_CODE IN ('2', '466')\n"
    "  AND t.DEB_CRE_IND = '1'\n  AND t.EQU_TRA_AMT > 50000\n"
    "  AND t.TRA_DATE >= DATE '2016-01-01' AND t.TRA_DATE < DATE '2023-01-01'\n"
    "GROUP BY t.COUNTRY_CODE, t.INST_CODE, t.CUS_NUM"
)


def test_grouped_rule_counts_the_transactions_behind_its_customers(monkeypatch: pytest.MonkeyPatch) -> None:
    """The live PioTech shape (GROUP BY CUS_NUM, no count column): its own rows are counted."""
    executed: List[str] = []
    monkeypatch.setattr(aml_tools, "run_shadow_readonly", _fake_db(["cus_num"], (65, 65), executed))

    metrics = aml_tools._shadow_metrics(GROUPED_RULE, {})

    count_sql = executed[-1]
    assert count_sql.startswith("SELECT COUNT(*) FROM BI_DWH.PIO_TRANSACTIONS t\nWHERE t.COUNTRY_CODE = 400")
    assert "GROUP BY" not in count_sql.split(" IN (SELECT shadow_query.CUS_NUM")[0]
    assert "AND t.CUS_NUM IN (SELECT shadow_query.CUS_NUM FROM (" + GROUPED_RULE + ") shadow_query)" in count_sql
    assert metrics["matched_transaction_count"] == 65 and metrics["alerted_customer_count"] == 65


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT a.CUS_NUM FROM x a JOIN y b ON a.k = b.k GROUP BY a.CUS_NUM",
        "SELECT a.CUS_NUM FROM x a, y b WHERE a.k = b.k GROUP BY a.CUS_NUM",
        "WITH s AS (SELECT * FROM x) SELECT s.CUS_NUM FROM s GROUP BY s.CUS_NUM",
        "SELECT CUS_NUM FROM x GROUP BY CUS_NUM UNION SELECT CUS_NUM FROM y GROUP BY CUS_NUM",
        "SELECT t.ACCOUNT_NUMBER FROM x t GROUP BY t.ACCOUNT_NUMBER",
    ],
)
def test_transaction_count_is_not_built_for_rules_it_cannot_split(sql: str) -> None:
    """Joins, CTEs, unions and non-customer groupings are left uncounted rather than miscounted."""
    assert aml_tools._transactions_behind_customers_sql(sql) is None


def test_subqueries_and_literals_do_not_confuse_the_split() -> None:
    """FROM inside EXTRACT(), a nested SELECT and a quoted 'GROUP BY' stay inside the WHERE clause."""
    sql = ("SELECT t.CUS_NUM FROM PIO_TRANSACTIONS t WHERE EXTRACT(YEAR FROM t.TRA_DATE) = 2019 "
           "AND t.NOTE <> 'GROUP BY x' AND t.EXPL_CODE IN (SELECT c.CODE FROM codes c) "
           "GROUP BY t.CUS_NUM HAVING SUM(t.EQU_TRA_AMT) > 100")
    count_sql = aml_tools._transactions_behind_customers_sql(sql)
    assert count_sql.startswith(
        "SELECT COUNT(*) FROM PIO_TRANSACTIONS t WHERE EXTRACT(YEAR FROM t.TRA_DATE) = 2019 "
        "AND t.NOTE <> 'GROUP BY x' AND t.EXPL_CODE IN (SELECT c.CODE FROM codes c) AND t.CUS_NUM IN ("
    )


def test_shadow_result_carries_the_measured_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    """End to end through the tool: the summary and counts reach the agent."""
    body = _sse([{"type": "final_answer", "text": "SELECT t.CUS_NUM, t.TRA_DATE FROM x t WHERE t.TRA_DATE >= SYSDATE - 7"},
                 {"type": "done"}])
    real_client = httpx.Client
    monkeypatch.setattr(aml_tools.httpx, "Client",
                        lambda **kw: real_client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=body)), **kw))
    monkeypatch.setattr(aml_tools, "run_shadow_readonly", _fake_db(["cus_num", "tra_date"], (120, 65), []))
    intent = {"scenario_name": "x", "scenario_type": "CUSTOMER", "detection_logic": "x", "anchor_date": "2019-06-30"}

    result = json.loads(aml_tools.execute_oracle_dwh_shadow_test.invoke({"intent_json": json.dumps(intent)}))

    assert result["metrics_summary"] == "120 transactions across 65 distinct customers"
    assert (result["header_alert_count"], result["transaction_detail_count"]) == (65, 120)


# ─────────────────────────── a short window anywhere inside a period ───────────────────────────

WEEKLY_RULE = (
    "SELECT t.CUS_NUM, COUNT(*) AS TRANSACTION_COUNT\nFROM BI_DWH.PIO_TRANSACTIONS t\nWHERE t.COUNTRY_CODE = 400\n"
    "  AND t.EXPL_CODE IN ('1', '660')\n  AND t.DEB_CRE_IND = '2'\n  AND t.EQU_TRA_AMT >= 10000\n"
    "  AND t.TRA_DATE >= SYSDATE - 7\nGROUP BY t.COUNTRY_CODE, t.INST_CODE, t.CUS_NUM\nHAVING COUNT(*) >= 3"
)
WEEK = {"unit": "WEEKS", "value": 1}


def test_one_week_slides_over_the_whole_year() -> None:
    """Live case: '3 high cash deposits in one week in 2019' checks every week of 2019, not only the last one."""
    from services.aml_builder.web.services.sql_dates import slide_window_over_period

    sql, ok = slide_window_over_period(anchor_sql(WEEKLY_RULE, {"time_window": WEEK}), date(2019, 1, 1), date(2019, 12, 31), WEEK)

    assert ok
    assert ("COUNT(*) OVER (PARTITION BY t.COUNTRY_CODE, t.INST_CODE, t.CUS_NUM ORDER BY t.TRA_DATE "
            "RANGE BETWEEN 6 PRECEDING AND CURRENT ROW) AS W1") in sql
    assert "t.TRA_DATE >= DATE '2019-01-01' AND t.TRA_DATE < DATE '2020-01-01'" in sql
    assert sql.startswith("SELECT CUS_NUM, MAX(W1) AS TRANSACTION_COUNT\nFROM (\nSELECT ")
    assert sql.endswith("GROUP BY COUNTRY_CODE, INST_CODE, CUS_NUM\nHAVING MAX(W1) >= 3")
    assert binds_used(sql) == [] and not NO_CLOCK.search(sql)


@pytest.mark.parametrize("window, frame", [
    ({"unit": "WEEKS", "value": 2}, "13 PRECEDING"),
    ({"unit": "DAYS", "value": 1}, "0 PRECEDING"),
    ({"unit": "DAYS", "value": 5}, "6 PRECEDING"),
    ({"unit": "MONTHS", "value": 1}, "INTERVAL '1' MONTH PRECEDING"),
])
def test_window_frames(window: Dict[str, Any], frame: str) -> None:
    """Weeks and months exactly; N working days as about N * 7 / 5 calendar days."""
    from services.aml_builder.web.services.sql_dates import _window_range

    assert _window_range(window) == frame


@pytest.mark.parametrize("rule", [
    WEEKLY_RULE.replace("HAVING COUNT(*) >= 3", "HAVING COUNT(*) < 3"),
    WEEKLY_RULE.replace("COUNT(*) AS", "COUNT(DISTINCT t.BRA_CODE) AS").replace("HAVING COUNT(*)", "HAVING COUNT(DISTINCT t.BRA_CODE)"),
    WEEKLY_RULE.replace("\nGROUP BY t.COUNTRY_CODE, t.INST_CODE, t.CUS_NUM\nHAVING COUNT(*) >= 3", ""),
    WEEKLY_RULE + "\nORDER BY 2 DESC",
])
def test_rules_that_cannot_slide_are_left_as_they_are(rule: str) -> None:
    """'Fewer than', DISTINCT counts, per-transaction rules and ORDER BY keep the last-window behaviour."""
    from services.aml_builder.web.services.sql_dates import slide_window_over_period

    anchored = anchor_sql(rule, {"time_window": WEEK})
    assert slide_window_over_period(anchored, date(2019, 1, 1), date(2019, 12, 31), WEEK) == (anchored, False)


def test_plan_says_any_window_within_the_period() -> None:
    """The plan states that the week can be any week of the period."""
    plan = build_plan_markdown({"scenario_name": "x", "detection_logic": "x", "anchor_date": "2019-12-31",
                                "time_window": {"unit": "WEEKS", "value": 1, "is_rolling": True},
                                "evaluation_period": {"start": "2019-01-01", "end": "2019-12-31"}}, None)
    assert "Any `1 week` window within `2019-01-01` to `2019-12-31`" in plan
