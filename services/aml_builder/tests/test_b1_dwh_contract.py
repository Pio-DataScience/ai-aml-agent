"""Offline tests for the B1 typed Service B handoff contract."""

from __future__ import annotations

import asyncio
import json
from datetime import date
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from services.aml_builder.web.domain.dwh_scenario.contracts import (
    ConfirmedFilter,
    ScenarioSqlRequest,
    ScenarioSqlResult,
)
from services.aml_builder.web.domain.dwh_scenario.sql_policy import (
    SqlPolicyError,
    validate_oracle_read,
)
from services.aml_builder.web.domain.dwh_scenario.validation import (
    derive_candidate_bind_values,
    validate_generated_sql,
)
from services.aml_builder.web.domain.filter_applied_check import (
    check_filter_application,
)
from services.aml_builder.web.domain.filter_resolution import build_service_b_intent
from services.aml_builder.web.services.schemas import AMLIntent
from services.aml_builder.web.services.dwh_scenario import http_adapter


def _install_fake_service_b(
    monkeypatch, lines, *, request_error=None, cancel_on_response=False
):
    """Install an offline SSE client and return the captured request."""
    observed = {}

    class FakeResponse:
        async def __aenter__(self):
            if cancel_on_response:
                raise asyncio.CancelledError
            return self

        async def __aexit__(self, *_args):
            return None

        def raise_for_status(self):
            if request_error:
                raise request_error

        async def aiter_lines(self):
            for line in lines:
                yield line

    class FakeClient:
        def __init__(self, timeout):
            observed["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def stream(self, method, url, *, json, headers):
            observed.update(method=method, url=url, payload=json, headers=headers)
            return FakeResponse()

    monkeypatch.setattr(http_adapter.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(
        http_adapter,
        "load_catalogs",
        lambda _path: SimpleNamespace(filters={"dimensions": {}}),
    )
    monkeypatch.setattr(http_adapter.settings, "PIOTECH_AI_TIMEOUT_SECONDS", 17)
    monkeypatch.setattr(
        http_adapter.settings, "PIOTECH_AI_URL", "http://offline.test/chat/stream"
    )
    monkeypatch.setattr(http_adapter.settings, "PIOTECH_AI_USER_ID", "tester")
    monkeypatch.setattr(http_adapter.settings, "PIOTECH_AI_PROJECT_ID", "project")
    return observed


def _intent(**updates):
    """Build a valid base intent, applying a requested contract variant."""
    payload = {
        "scenario_name": "Synthetic AML case",
        "scenario_type": "TRANSACTION",
        "detection_logic": "A synthetic detection rule",
        "aggregation": {
            "metric": "transaction amount",
            "function": "sum",
            "grain": "Per Customer",
        },
    }
    payload.update(updates)
    return AMLIntent.model_validate(payload)


@pytest.mark.parametrize(
    "updates",
    [
        {"transaction_type": "CASH DEPOSIT"},
        {"transaction_types": ["CASH DEPOSIT", "OUTWARD TRANSFER"]},
        {"transaction_type": ["CASH DEPOSIT", "OUTWARD TRANSFER"]},
        {"transaction_type": "ALL TRANSACTIONS"},
        {"transaction_type": None, "explanation_codes": None},
        {
            "transaction_type": "ANY",
            "explanation_codes_by_type": {"CASH DEPOSIT": ["110"]},
        },
        {"time_window": {"unit": "DAYS", "value": 30, "is_rolling": True}},
        {"time_window": {"unit": "MONTHS", "value": 6, "is_rolling": False}},
        {"baseline_window": {"unit": "DAYS", "duration": 180, "offset_days": 30}},
        {
            "baseline_window": {
                "unit": "MONTHS",
                "duration": 6,
                "offset_days": 1,
                "exclude_current_window": False,
            }
        },
        {"filters": [{"dimension": "country", "raw_mention": "Jordan"}]},
        {
            "resolved_filters": [
                {"dimension": "country", "status": "resolved", "values": ["JO"]}
            ]
        },
        {
            "thresholds": [
                {
                    "field": "amount",
                    "operator": ">",
                    "value_from": 1000,
                    "target_scope": "AGGREGATE",
                }
            ]
        },
        {
            "semantic_conditions": [
                {
                    "raw_phrase": "dormant then active",
                    "logical_type": "TRANSITION",
                    "subject": "Customer",
                    "predicate": "becomes active",
                }
            ]
        },
    ],
)
def test_aml_intent_variants_round_trip_to_typed_request(updates):
    intent = _intent(**updates)
    request = ScenarioSqlRequest(intent=intent)

    assert ScenarioSqlRequest.model_validate_json(request.model_dump_json()) == request


def test_request_forbids_unknown_contract_fields():
    with pytest.raises(ValidationError):
        ScenarioSqlRequest.model_validate({"intent": _intent(), "unexpected": True})
    with pytest.raises(ValidationError, match="Unknown intent field"):
        ScenarioSqlRequest.model_validate(
            {"intent": {**_intent().model_dump(), "unrecognized": "value"}}
        )
    malformed = _intent().model_dump()
    malformed["time_window"] = {"unit": "DAYS", "value": 3, "unrecognized": 1}
    with pytest.raises(ValidationError, match="Unknown intent.time_window field"):
        ScenarioSqlRequest.model_validate({"intent": malformed})


@pytest.mark.parametrize(
    "filter_row",
    [
        {"dimension": "country", "status": "unresolved", "values": []},
        {"dimension": "country", "status": "ambiguous", "values": []},
        {"dimension": "country", "status": "lookup_failed", "values": []},
    ],
)
def test_unresolved_filter_in_intent_rejects_request(filter_row):
    with pytest.raises(ValidationError, match="Unresolved required filters"):
        ScenarioSqlRequest(intent=_intent(resolved_filters=[filter_row]))


def test_unconfirmed_filter_is_not_forwarded_as_confirmed():
    intent = _intent(
        resolved_filters=[
            {"dimension": "country", "status": "disabled", "values": []},
            {"dimension": "channel", "status": "resolved", "values": ["WIRE"]},
        ]
    )
    request = ScenarioSqlRequest(
        intent=intent,
        confirmed_filters=[intent.resolved_filters[1]],
    )

    assert [item.dimension for item in request.confirmed_filters] == ["channel"]


def test_service_a_confirmed_filter_selection_and_all_scope_match_contract():
    """Keep A's resolved-only filter rules consistent with B's handoff."""
    catalog = {
        "dimensions": {
            "country": {
                "status": "confirmed",
                "target": {"table": "TX", "column": "COUNTRY_CODE"},
                "level": "transaction",
                "match": "any_of",
            },
            "explanation_code": {
                "status": "confirmed",
                "target": {"table": "TX", "column": "EXPL_CODE"},
                "level": "transaction",
                "match": "any_of",
            },
        }
    }
    intent = _intent(
        transaction_type="ALL TRANSACTIONS",
        resolved_filters=[
            {"dimension": "country", "status": "resolved", "values": ["JO"]},
            {"dimension": "country", "status": "disabled", "values": []},
            {"dimension": "explanation_code", "status": "resolved", "values": ["110"]},
        ],
    )
    service_intent = build_service_b_intent(intent.model_dump(mode="python"), catalog)
    assert [row["dimension"] for row in service_intent["resolved_filters"]] == [
        "country",
        "explanation_code",
    ]
    request = ScenarioSqlRequest(
        intent=intent,
        confirmed_filters=service_intent["resolved_filters"],
    )
    checked = check_filter_application(
        "SELECT CUS_NUM FROM TX WHERE COUNTRY_CODE = 'JO'",
        [item.model_dump(mode="python") for item in request.confirmed_filters],
        transaction_scope="ALL",
    )
    assert checked["validation_success"] is True
    assert [row["dimension"] for row in checked["filters"]] == ["country"]


def test_unready_intent_and_malformed_filter_reject():
    with pytest.raises(ValidationError, match="not ready"):
        ScenarioSqlRequest(intent=_intent(ready_for_handoff=False))
    with pytest.raises(ValidationError, match="Confirmed filters"):
        ScenarioSqlRequest(
            intent=_intent(),
            confirmed_filters=[
                {"dimension": "country", "status": "unresolved", "values": []}
            ],
        )


def test_contract_checks_projection_codes_and_rolling_baseline():
    intent = _intent(
        transaction_type="CASH DEPOSIT",
        explanation_codes=["110"],
        time_window={"unit": "DAYS", "value": 30, "is_rolling": True},
        baseline_window={"unit": "DAYS", "duration": 180, "offset_days": 30},
    )
    request = ScenarioSqlRequest(
        intent=intent,
        confirmed_filters=[
            {
                "dimension": "explanation_code",
                "target": {"table": "TX", "column": "EXPL_CODE"},
                "status": "resolved",
                "values": ["110"],
            }
        ],
    )
    sql = (
        "SELECT CUS_NUM, SUM(TRA_AMT) AS TOTAL_AMOUNT FROM TX "
        "WHERE EXPL_CODE IN (:cs_b1) "
        "AND TX_DATE >= SYSDATE - :cs_b2 AND TX_DATE < SYSDATE "
        "AND TX_DATE >= SYSDATE - (:cs_b2 + :cs_b3) "
        "AND TX_DATE < SYSDATE - :cs_b4 GROUP BY CUS_NUM"
    )

    assert validate_generated_sql(request, sql) == []
    findings = validate_generated_sql(request, "SELECT CUSTOMER_ID FROM TX")
    assert any("CUS_NUM" in finding for finding in findings)
    assert any("EXPL_CODE" in finding for finding in findings)
    assert any("Rolling" in finding for finding in findings)
    assert any("baseline" in finding.casefold() for finding in findings)


def _approved_cash_window_request() -> ScenarioSqlRequest:
    """Build the approved synthetic 14-day scenario with a fixed evaluation date."""
    intent = _intent(
        scenario_name="Two week cash",
        scenario_type="TRANSACTION",
        transaction_type="CASH DEPOSIT",
        explanation_codes=["4"],
        detection_logic="Cash over two weeks",
        time_window={"unit": "DAYS", "value": 14, "is_rolling": True},
        anchor_date="2026-10-28",
        aggregation=None,
    )
    return ScenarioSqlRequest(
        intent=intent,
        confirmed_filters=[
            {
                "dimension": "explanation_code",
                "target": {"table": "PIO_TRANSACTIONS", "column": "EXPL_CODE"},
                "status": "resolved",
                "values": ["4"],
            }
        ],
    )


def test_approved_b8_bind_sql_passes_b3_and_contract_validation():
    """Confirmed-code and fixed-date binds pass without literal/SYSDATE checks."""
    request = _approved_cash_window_request()
    sql = (
        "SELECT CUS_NUM FROM BI_DWH.PIO_TRANSACTIONS "
        "WHERE EXPL_CODE = :cs_b1 "
        "AND TRA_DATE >= :cs_b2 AND TRA_DATE < :cs_b3"
    )
    params = derive_candidate_bind_values(request, sql)

    assert params == {
        "cs_b1": "4",
        "cs_b2": date(2026, 10, 14),
        "cs_b3": date(2026, 10, 28),
    }
    assert validate_generated_sql(request, sql, params=params) == []
    assert validate_oracle_read(
        sql,
        params=params,
        allowed_schemas=("BI_DWH",),
        allowed_objects=("BI_DWH.PIO_TRANSACTIONS",),
        required_projection="CUS_NUM",
        confirmed_filters=[
            item.model_dump(mode="json") for item in request.confirmed_filters
        ],
    ).binds == ("cs_b1", "cs_b2", "cs_b3")


def test_bind_sql_fails_closed_for_missing_filter_and_bad_date_range():
    """A missing code or non-half-open date contract is rejected."""
    request = _approved_cash_window_request()
    missing_filter = (
        "SELECT CUS_NUM FROM BI_DWH.PIO_TRANSACTIONS "
        "WHERE TRA_DATE >= :cs_b2 AND TRA_DATE < :cs_b3"
    )
    assert any(
        "EXPL_CODE" in finding
        for finding in validate_generated_sql(request, missing_filter)
    )
    with pytest.raises(SqlPolicyError, match="half-open date bind pair"):
        derive_candidate_bind_values(
            request,
            "SELECT CUS_NUM FROM BI_DWH.PIO_TRANSACTIONS "
            "WHERE EXPL_CODE = :cs_b1 AND TRA_DATE >= :cs_b2 "
            "AND TRA_DATE <= :cs_b3",
        )


def test_bind_sql_rejects_wrong_confirmed_value_and_uncontracted_bind():
    """B3 refuses a wrong filter value and derivation refuses extra binds."""
    request = _approved_cash_window_request()
    sql = (
        "SELECT CUS_NUM FROM BI_DWH.PIO_TRANSACTIONS "
        "WHERE EXPL_CODE = :cs_b1 "
        "AND TRA_DATE >= :cs_b2 AND TRA_DATE < :cs_b3"
    )
    with pytest.raises(SqlPolicyError, match="confirmed filter"):
        validate_oracle_read(
            sql,
            params={
                "cs_b1": "5",
                "cs_b2": date(2026, 10, 14),
                "cs_b3": date(2026, 10, 28),
            },
            allowed_schemas=("BI_DWH",),
            allowed_objects=("BI_DWH.PIO_TRANSACTIONS",),
            required_projection="CUS_NUM",
            confirmed_filters=[
                item.model_dump(mode="json") for item in request.confirmed_filters
            ],
        )
    with pytest.raises(SqlPolicyError, match="outside confirmed filter and temporal"):
        derive_candidate_bind_values(
            request,
            sql.replace(
                "TRA_DATE < :cs_b3",
                "TRA_DATE < :cs_b3 AND CUS_NUM = :extra",
            ),
        )


def test_bind_sql_rejects_unsafe_object_and_write_sql():
    """The existing B3 policy still rejects unsafe SQL around bind contracts."""
    request = _approved_cash_window_request()
    params = {
        "cs_b1": "4",
        "cs_b2": date(2026, 10, 14),
        "cs_b3": date(2026, 10, 28),
    }
    for sql in (
        "SELECT CUS_NUM FROM BI_DWH.PIO_TRANSACTIONS "
        "JOIN BI_DWH.PIO_CUSTOMERS C ON 1=1 WHERE EXPL_CODE=:cs_b1 "
        "AND TRA_DATE >= :cs_b2 AND TRA_DATE < :cs_b3",
        "DELETE FROM BI_DWH.PIO_TRANSACTIONS WHERE EXPL_CODE=:cs_b1",
    ):
        with pytest.raises(SqlPolicyError):
            validate_oracle_read(
                sql,
                params=params,
                allowed_schemas=("BI_DWH",),
                allowed_objects=("BI_DWH.PIO_TRANSACTIONS",),
                required_projection="CUS_NUM",
                confirmed_filters=[
                    item.model_dump(mode="json")
                    for item in request.confirmed_filters
                ],
            )


def test_all_transactions_rejects_invented_explanation_code_filter():
    request = ScenarioSqlRequest(intent=_intent(transaction_type="ALL TRANSACTIONS"))

    findings = validate_generated_sql(
        request,
        "SELECT CUS_NUM, SUM(TRA_AMT) AS TOTAL_AMOUNT FROM TX "
        "WHERE EXPL_CODE = '110' GROUP BY CUS_NUM",
    )
    assert "SQL invents an EXPL_CODE restriction for an all/no-type intent" in findings
    assert all(
        finding == "SQL invents an EXPL_CODE restriction for an all/no-type intent"
        for finding in findings
    )


def test_result_contract_rejects_unknown_fields_and_models_evidence():
    result = ScenarioSqlResult(
        sql="SELECT CUS_NUM FROM TX",
        approved=True,
        execution={"columns": ["CUS_NUM"], "row_count": 1, "truncated": False},
        provider="http",
        trace_id="trace-1",
    )
    assert result.execution.row_count == 1
    with pytest.raises(ValidationError):
        ScenarioSqlResult.model_validate({"sql": "x", "unmodeled": 1})
    with pytest.raises(ValidationError):
        ConfirmedFilter.model_validate(
            {"dimension": "country", "values": ["JO"], "unmodeled": True}
        )
    with pytest.raises(ValidationError):
        ScenarioSqlRequest(
            intent=_intent(),
            version_context={"unknown_version_field": "value"},
        )


def test_http_adapter_preserves_existing_sse_payload_and_extracts_final_sql(
    monkeypatch,
):
    """Exercise the adapter offline with a fake SSE response."""
    observed = _install_fake_service_b(
        monkeypatch,
        [
            'data: {"type":"content","text":"ignored"}',
            "data: not-json",
            'data: {"type":"final_answer","text":"SELECT CUS_NUM FROM TX"}',
            "data: [DONE]",
        ],
    )

    confirmed_filter = {
        "dimension": "country",
        "raw_mention": "Jordan",
        "polarity": "positive",
        "level": "transaction",
        "status": "resolved",
        "options": [],
        "values": ["JO"],
        "confidence": None,
        "provenance": "catalog_resolver",
        "audit_choice": "user_input",
        "target": {"table": "TX", "column": "COUNTRY_CODE"},
        "match": "any_of",
        "polarity_allowed": False,
    }
    intent = _intent(resolved_filters=[confirmed_filter])
    request = ScenarioSqlRequest(
        intent=intent,
        confirmed_filters=[confirmed_filter],
        version_context={"request_id": "req-7", "correlation_id": "corr-9"},
    )
    result = asyncio.run(http_adapter.HttpScenarioSqlGenerator().generate(request))

    assert result.sql == "SELECT CUS_NUM FROM TX"
    assert result.approved is True
    assert result.provider == "http"
    assert observed["timeout"] == 17
    assert observed["method"] == "POST"
    body = observed["payload"]
    assert set(body) == {"messages", "metadata", "reasoning_mode"}
    assert body["metadata"]["user_id"] == "tester"
    assert body["metadata"]["project_id"] == "project"
    assert body["metadata"]["chat_id"].startswith("aml_tool_")
    assert body["reasoning_mode"] == "instant"
    assert observed["headers"] == {
        "Accept": "text/event-stream",
        "Content-Type": "application/json",
        "X-Request-ID": "req-7",
        "X-Correlation-ID": "corr-9",
    }
    content = json.loads(body["messages"][0]["content"])
    assert content["request_type"] == "AML_SCENARIO_GENERATION"
    assert content["intent"]["scenario_name"] == "Synthetic AML case"
    assert content["intent"]["resolved_filters"][0]["confidence"] is None
    baseline_intent = build_service_b_intent(
        intent.model_dump(mode="json", exclude_none=False), {"dimensions": {}}
    )
    assert content["intent"] == baseline_intent


def test_http_adapter_keeps_content_sse_fallback_and_sql_identity(monkeypatch):
    """Use content events when final_answer has no usable SQL, as in B0."""
    _install_fake_service_b(
        monkeypatch,
        [
            'data: {"type":"final_answer","text":"not a query"}',
            'data: {"type":"content","text":"SELECT CUS_NUM FROM TX;"}',
            "data: [DONE]",
            'data: {"type":"content","text":"SELECT WRONG FROM TX"}',
        ],
    )
    request = ScenarioSqlRequest(intent=_intent())

    result = asyncio.run(http_adapter.HttpScenarioSqlGenerator().generate(request))

    assert result.sql == "SELECT CUS_NUM FROM TX"
    assert result.approved is False


def test_http_adapter_generation_only_marks_exact_scope_and_pinned_schema(monkeypatch):
    """The opt-in HTTP method marks only its request for Service B's hard guard."""
    observed = _install_fake_service_b(
        monkeypatch,
        ['data: {"type":"generation_only_candidate","approved":false,"text":"SELECT CUS_NUM FROM TX"}'],
    )
    schema_records = [{"metadata": {
        "tenant_id": "tenant-a", "institution_id": "tenant-a",
        "source": "oracle_dictionary", "object_type": "TABLE",
        "table_name": "BI_DWH.PIO_TRANSACTIONS", "column_name": "CUS_NUM",
    }}]
    request = ScenarioSqlRequest(intent=_intent())
    result = asyncio.run(
        http_adapter.HttpScenarioSqlGenerator().generate_only_for_approved_evaluation(
            request,
            scope_sha256="a" * 64,
            schema_records=schema_records,
            case_id="b9-01",
            service_b_payload_sha256="b" * 64,
        )
    )
    metadata = observed["payload"]["metadata"]
    assert metadata["generation_only"] is True
    assert metadata["generation_only_scope_sha256"] == "a" * 64
    assert metadata["generation_only_schema_records"] == schema_records
    assert metadata["generation_only_case_id"] == "b9-01"
    assert metadata["generation_only_payload_sha256"] == "b" * 64
    assert len(metadata["generation_only_schema_sha256"]) == 64
    assert result.sql == "SELECT CUS_NUM FROM TX"
    assert result.approved is False
    assert result.error == "Generation-only candidate is not execution or deployment approved"


def test_http_sse_candidate_extraction_requires_explicit_generation_only_guard():
    event = {
        "type": "generation_only_candidate",
        "text": "SELECT CUS_NUM FROM TX",
        "approved": False,
    }
    assert http_adapter._sql_text_from_sse_event(event, generation_only=False) is None
    assert http_adapter._sql_text_from_sse_event(event, generation_only=True) == event["text"]
    assert http_adapter._sql_text_from_sse_event(
        {**event, "approved": True}, generation_only=True
    ) is None


def test_http_adapter_translates_http_error_to_existing_error_result(monkeypatch):
    """Preserve the B0 HTTP-error text without issuing a network request."""
    error = http_adapter.httpx.ConnectError("offline fixture")
    _install_fake_service_b(monkeypatch, [], request_error=error)
    request = ScenarioSqlRequest(intent=_intent())

    result = asyncio.run(http_adapter.HttpScenarioSqlGenerator().generate(request))

    assert result.error == "PioTech AI HTTP error: offline fixture"


def test_http_adapter_keeps_missing_sql_error_text(monkeypatch):
    """Preserve the B0 extraction failure when SSE contains no usable SQL."""
    _install_fake_service_b(
        monkeypatch,
        ['data: {"type":"content","text":"No query returned."}', "data: [DONE]"],
    )
    request = ScenarioSqlRequest(intent=_intent())

    result = asyncio.run(http_adapter.HttpScenarioSqlGenerator().generate(request))

    assert result.error == (
        "PioTech AI did not return a valid SQL query. "
        "Try refining the scenario intent."
    )


def test_http_adapter_does_not_swallow_cancellation(monkeypatch):
    """Propagate task cancellation rather than converting it to an HTTP error."""
    _install_fake_service_b(monkeypatch, [], cancel_on_response=True)
    request = ScenarioSqlRequest(intent=_intent())

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(http_adapter.HttpScenarioSqlGenerator().generate(request))
