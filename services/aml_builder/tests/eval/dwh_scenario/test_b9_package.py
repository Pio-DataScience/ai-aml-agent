"""Deterministic consistency checks for the approved B9 synthetic scope."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from services.aml_builder.web.domain.dwh_scenario.contracts import ScenarioSqlRequest
from services.aml_builder.web.domain.dwh_scenario.sql_policy import (
    SqlPolicyError,
    validate_oracle_read,
)
from services.aml_builder.web.domain.dwh_scenario.validation import (
    derive_candidate_bind_values,
    validate_generated_sql,
)
from services.aml_builder.web.domain.filter_resolution import build_service_b_intent
from services.aml_builder.web.services.dwh_scenario.generator import _generation_payload_hash
from services.aml_builder.tests.eval.dwh_scenario.runner import (
    semantic_pair_matches,
    semantic_signature,
)
from services.aml_builder.tests.eval.dwh_scenario.build_b9_payload_manifest import (
    build_service_a_model_messages,
    service_a_schema_context,
)
from services.aml_builder.web.services.settings import settings as a_settings


ROOT = Path(__file__).resolve().parents[5]
FIXTURES = ROOT / "services/aml_builder/tests/eval/dwh_scenario/b9_cases.jsonl"
SCOPE_PATH = ROOT / "Docs/B9_generation_only_scope.json"
PAYLOAD_PATH = ROOT / "Docs/B9_model_payloads.json"
SCHEMA_COLUMNS = {"CUS_NUM", "TRA_DATE", "EXPL_CODE", "TRA_AMT"}
A_PROMPT = ROOT / "services/aml_builder/web/services/dwh_scenario/prompts/generator_v1.md"
B_PROMPT = ROOT.parent / "PioTech-AI/services/dwh_scenario_agent/web/services/prompts/generation_only_system.md"


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_all_twelve_fixtures_match_intent_binds_and_semantics() -> None:
    cases = [
        json.loads(line)
        for line in FIXTURES.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(cases) == 12
    assert "cash" not in cases[1]["scenario"].casefold()
    assert cases[11]["intent"]["aggregation"]["function"] == "AVG"
    assert "AVG(TRA_AMT)" in cases[11]["sql_a"].upper()
    assert cases[11]["params"] == {}
    for case in cases:
        request = ScenarioSqlRequest(
            intent=case["intent"], confirmed_filters=case["confirmed_filters"]
        )
        for sql_key in ("sql_a", "sql_b"):
            sql = case[sql_key]
            binds = derive_candidate_bind_values(request, sql)
            assert binds == case["params"], case["scenario"]
            validate_oracle_read(
                sql,
                params=binds,
                allowed_schemas=("BI_DWH",),
                allowed_objects=("BI_DWH.PIO_TRANSACTIONS",),
                required_projection="CUS_NUM",
                confirmed_filters=[
                    item.model_dump(mode="json") for item in request.confirmed_filters
                ],
            )
        a_binds = derive_candidate_bind_values(request, case["sql_a"])
        b_binds = derive_candidate_bind_values(request, case["sql_b"])
        assert semantic_pair_matches(case["sql_a"], a_binds, case["sql_b"], b_binds)
        assert set(case["params"]) == set(derive_candidate_bind_values(request, case["sql_a"]))


def test_case9_requires_sum_evidence_even_when_sum_is_in_having() -> None:
    case = json.loads(FIXTURES.read_text(encoding="utf-8").splitlines()[8])
    request = ScenarioSqlRequest(
        intent=case["intent"], confirmed_filters=case["confirmed_filters"]
    )
    candidate = (
        "SELECT CUS_NUM FROM BI_DWH.PIO_TRANSACTIONS GROUP BY CUS_NUM "
        "HAVING SUM(TRA_AMT) > :cs_b1"
    )
    findings = validate_generated_sql(request, candidate, params=case["params"])
    assert "Aggregate intent requires an aliased SUM(TRA_AMT) projection" in findings
    assert not validate_generated_sql(request, case["sql_a"], params=case["params"])


def test_reviewed_generation_prompts_cover_b9_corrections() -> None:
    a_prompt = A_PROMPT.read_text(encoding="utf-8").casefold()
    b_prompt = B_PROMPT.read_text(encoding="utf-8").casefold()
    assert "calendar boundaries are expressions, not date binds" in a_prompt
    assert "sum(tra_amt) as <evidence_alias>" in a_prompt
    assert "count(*) as <evidence_alias>" in a_prompt
    assert "count(distinct tra_date) as <evidence_alias>" in a_prompt
    assert "a filter-only" in a_prompt
    assert "do not substitute opaque date binds" in b_prompt
    assert "project `cus_num` and exactly" in b_prompt
    assert "distinct opaque bind token at each temporal occurrence" in b_prompt


def test_stored_live_pairs_use_the_shared_bind_aware_comparator() -> None:
    """Post-run reporting uses the same semantic path as direct comparisons."""
    live_path = ROOT / "Docs/B9_live_results_bd9bf5e7_20261029.json"
    live = _read_json(live_path)
    cases = [
        json.loads(line)
        for line in FIXTURES.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    matches: list[str] = []
    for fixture, result in zip(cases, live["results"], strict=True):
        assert result["case_id"] == f"b9-{cases.index(fixture) + 1:02d}"
        request = ScenarioSqlRequest(
            intent=fixture["intent"], confirmed_filters=fixture["confirmed_filters"]
        )
        sql_a = result["service_a"]["candidate_sql"]
        sql_b = result["service_b"]["candidate_sql"]
        try:
            binds_a = derive_candidate_bind_values(request, sql_a)
            binds_b = derive_candidate_bind_values(request, sql_b)
        except (SqlPolicyError, ValueError):
            continue
        if semantic_pair_matches(sql_a, binds_a, sql_b, binds_b):
            matches.append(result["case_id"])
    assert matches == ["b9-01", "b9-03", "b9-07", "b9-09", "b9-10", "b9-11", "b9-12"]


def test_frozen_baseline_and_snapshot_hash_provenance() -> None:
    """Verify hash algorithms without rewriting either frozen artifact."""
    baseline_path = ROOT / "Docs/B9_frozen_baseline_20261029.json"
    raw_digest = _sha256(baseline_path.read_bytes())
    baseline = _read_json(baseline_path)
    embedded_digest = baseline.pop("baseline_sha256")
    canonical_digest = _sha256(
        json.dumps(
            baseline, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    )
    assert raw_digest == "8243cc92268c78560f27b09adf8031918eb0b7ad962f1797d589eb61735cc7ad"
    assert embedded_digest == canonical_digest
    assert canonical_digest == "9772b9b5f632114356b2d64877b3ba757d3b81f20d0510f76f945bfcf5ff0ca6"

    snapshot = _read_json(ROOT / "Docs/B9_live_snapshot_bd9bf5e7_20261029.json")
    snapshot_digest = snapshot.pop("snapshot_sha256")
    assert _sha256(
        json.dumps(
            snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    ) == snapshot_digest
    assert snapshot["captured_candidate_count"] == 24
    assert len(snapshot["captured_journal_records"]) == 74


@pytest.mark.parametrize(
    ("case_index", "sql", "message"),
    [
        (
            0,
            "SELECT CUS_NUM FROM BI_DWH.PIO_TRANSACTIONS "
            "WHERE EXPL_CODE = '4' AND TRA_DATE >= SYSDATE - :cs_b2",
            "Confirmed filter values must use approved bind placeholders",
        ),
        (
            1,
            "SELECT CUS_NUM FROM BI_DWH.PIO_TRANSACTIONS WHERE TRA_AMT >= 10000",
            "Threshold values must use one approved bind placeholder",
        ),
        (
            3,
            "SELECT CUS_NUM FROM BI_DWH.PIO_TRANSACTIONS GROUP BY CUS_NUM "
            "HAVING COUNT(*) >= 5",
            "Threshold values must use one approved bind placeholder",
        ),
        (
            1,
            "SELECT CUS_NUM FROM BI_DWH.PIO_TRANSACTIONS",
            "A threshold predicate is missing or differs from intent",
        ),
    ],
)
def test_intent_filters_and_thresholds_reject_literals_or_missing_predicates(
    case_index: int, sql: str, message: str
) -> None:
    cases = [
        json.loads(line)
        for line in FIXTURES.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    case = cases[case_index]
    request = ScenarioSqlRequest(
        intent=case["intent"], confirmed_filters=case["confirmed_filters"]
    )
    with pytest.raises(SqlPolicyError, match=message):
        derive_candidate_bind_values(request, sql)


@pytest.mark.parametrize("case_index", [2, 3, 4, 8])
def test_aggregate_intent_requires_aliased_measure_projection(case_index: int) -> None:
    cases = [
        json.loads(line)
        for line in FIXTURES.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    case = cases[case_index]
    request = ScenarioSqlRequest(
        intent=case["intent"], confirmed_filters=case["confirmed_filters"]
    )
    finding = validate_generated_sql(
        request,
        "SELECT CUS_NUM FROM BI_DWH.PIO_TRANSACTIONS GROUP BY CUS_NUM "
        "HAVING COUNT(*) >= :cs_b1",
    )
    assert any("requires an aliased" in message for message in finding)
    assert not validate_generated_sql(request, case["sql_a"], params=case["params"])
    extra_measure = (
        "SELECT CUS_NUM, SUM(TRA_AMT) AS TOTAL_AMOUNT, COUNT(*) AS TXN_COUNT "
        "FROM BI_DWH.PIO_TRANSACTIONS GROUP BY CUS_NUM "
        "HAVING SUM(TRA_AMT) >= :cs_b1"
    )
    if case_index == 2:
        assert "SQL projects an aggregate measure not requested by intent" in (
            validate_generated_sql(request, extra_measure, params=case["params"])
        )
    if case_index == 8:
        ungrouped = validate_generated_sql(
            request,
            "SELECT CUS_NUM, SUM(TRA_AMT) AS total_transaction_amount "
            "FROM BI_DWH.PIO_TRANSACTIONS HAVING SUM(TRA_AMT) > :cs_b1",
            params=case["params"],
        )
        assert "Aggregate intent requires grouping at CUS_NUM granularity" in ungrouped
        finer_grain = validate_generated_sql(
            request,
            "SELECT CUS_NUM, TRA_DATE, SUM(TRA_AMT) AS total_transaction_amount "
            "FROM BI_DWH.PIO_TRANSACTIONS GROUP BY CUS_NUM, TRA_DATE "
            "HAVING SUM(TRA_AMT) > :cs_b1",
            params=case["params"],
        )
        assert "Aggregate intent requires grouping at CUS_NUM granularity" in finer_grain


def test_rolling_window_requires_strict_half_open_upper_bound() -> None:
    case = json.loads(FIXTURES.read_text(encoding="utf-8").splitlines()[5])
    request = ScenarioSqlRequest(
        intent=case["intent"], confirmed_filters=case["confirmed_filters"]
    )
    incomplete = "SELECT CUS_NUM FROM BI_DWH.PIO_TRANSACTIONS WHERE TRA_DATE >= SYSDATE - :cs_b1"
    binds = derive_candidate_bind_values(request, incomplete)
    assert "half-open upper bound" in " ".join(
        validate_generated_sql(request, incomplete, params=binds)
    )
    complete = incomplete + " AND TRA_DATE < SYSDATE"
    complete_binds = derive_candidate_bind_values(request, complete)
    assert not validate_generated_sql(request, complete, params=complete_binds)


def test_existential_detail_threshold_case2_is_a_valid_threshold_contract() -> None:
    case = json.loads(FIXTURES.read_text(encoding="utf-8").splitlines()[1])
    request = ScenarioSqlRequest(intent=case["intent"])
    valid = (
        "SELECT CUS_NUM FROM BI_DWH.PIO_TRANSACTIONS GROUP BY CUS_NUM "
        "HAVING MAX(CASE WHEN TRA_AMT >= :cs_b1 THEN 1 ELSE 0 END) = 1"
    )
    assert derive_candidate_bind_values(request, valid) == {"cs_b1": 10000.0}
    assert not validate_generated_sql(request, valid, params={"cs_b1": 10000.0})
    for invalid in (
        valid.replace("MAX(", "MIN("),
        valid.replace("TRA_AMT >=", "TRA_AMT <"),
        valid.replace("ELSE 0", "ELSE 1"),
        valid.replace(
            "TRA_AMT >= :cs_b1",
            "TRA_AMT >= :cs_b1 AND TRA_DATE > DATE '2026-01-01'",
        ),
    ):
        with pytest.raises(SqlPolicyError):
            derive_candidate_bind_values(request, invalid)


def test_parenthesized_rolling_bounds_and_comparison_arithmetic_are_contractual() -> None:
    cases = [
        json.loads(line)
        for line in FIXTURES.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    rolling_request = ScenarioSqlRequest(
        intent=cases[0]["intent"], confirmed_filters=cases[0]["confirmed_filters"]
    )
    rolling_sql = (
        "SELECT CUS_NUM FROM BI_DWH.PIO_TRANSACTIONS "
        "WHERE EXPL_CODE = :cs_b1 AND TRA_DATE >= (SYSDATE - :cs_b2) "
        "AND TRA_DATE < SYSDATE"
    )
    assert derive_candidate_bind_values(rolling_request, rolling_sql) == cases[0]["params"]
    assert not validate_generated_sql(rolling_request, rolling_sql, params=cases[0]["params"])

    comparison_request = ScenarioSqlRequest(
        intent=cases[7]["intent"], confirmed_filters=cases[7]["confirmed_filters"]
    )
    comparison_sql = (
        "SELECT CUS_NUM, COUNT(CASE WHEN TRA_DATE >= SYSDATE - :cs_b1 "
        "AND TRA_DATE < SYSDATE THEN 1 END) AS CURRENT_COUNT, "
        "COUNT(CASE WHEN TRA_DATE >= SYSDATE - (:cs_b2 + :cs_b3) "
        "AND TRA_DATE < SYSDATE - :cs_b4 THEN 1 END) AS PRIOR_COUNT "
        "FROM BI_DWH.PIO_TRANSACTIONS GROUP BY CUS_NUM"
    )
    expected = {"cs_b1": 30, "cs_b2": 30, "cs_b3": 30, "cs_b4": 30}
    assert derive_candidate_bind_values(comparison_request, comparison_sql) == expected
    assert not validate_generated_sql(comparison_request, comparison_sql, params=expected)
    invalid = comparison_sql.replace(
        "TRA_DATE < SYSDATE - :cs_b4",
        "TRA_DATE <= SYSDATE - :cs_b4",
    )
    with pytest.raises(SqlPolicyError):
        derive_candidate_bind_values(comparison_request, invalid)


def test_comparison_bind_roles_remain_distinct_when_period_lengths_differ() -> None:
    case = json.loads(FIXTURES.read_text(encoding="utf-8").splitlines()[7])
    intent = json.loads(json.dumps(case["intent"]))
    intent["time_window"]["value"] = 14
    intent["baseline_window"]["duration"] = 45
    intent["baseline_window"]["offset_days"] = 14
    request = ScenarioSqlRequest(intent=intent)
    sql = (
        "SELECT CUS_NUM, "
        "COUNT(CASE WHEN TRA_DATE >= SYSDATE - :cs_b1 AND TRA_DATE < SYSDATE "
        "THEN 1 END) AS CURRENT_COUNT, "
        "COUNT(CASE WHEN TRA_DATE >= SYSDATE - (:cs_b2 + :cs_b3) "
        "AND TRA_DATE < SYSDATE - :cs_b4 THEN 1 END) AS PRIOR_COUNT "
        "FROM BI_DWH.PIO_TRANSACTIONS GROUP BY CUS_NUM"
    )
    expected = {"cs_b1": 14, "cs_b2": 14, "cs_b3": 45, "cs_b4": 14}
    assert derive_candidate_bind_values(request, sql) == expected
    assert not validate_generated_sql(request, sql, params=expected)
    wrong_offset = sql.replace("SYSDATE - :cs_b4", "SYSDATE - :cs_b3")
    with pytest.raises(SqlPolicyError):
        derive_candidate_bind_values(request, wrong_offset)


@pytest.mark.parametrize(
    ("case_index", "expected_kind", "expected_relationship"),
    [
        (1, "NONE", "NONE"),
        (0, "ROLLING", "NONE"),
        (6, "CALENDAR", "NONE"),
        (7, "COMPARISON", "IMMEDIATELY_PRECEDING_NON_OVERLAPPING"),
        (10, "ROLLING", "NONE"),
    ],
)
def test_service_b_handoff_preserves_typed_temporal_contract(
    case_index: int, expected_kind: str, expected_relationship: str
) -> None:
    cases = [
        json.loads(line)
        for line in FIXTURES.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    service_intent = build_service_b_intent(cases[case_index]["intent"], {})
    contract = service_intent["temporal_contract"]
    assert contract["kind"] == expected_kind
    assert contract["baseline_relationship"] == expected_relationship
    if expected_kind == "CALENDAR":
        assert contract["current_lower_boundary"] == "CALENDAR_MONTH_START"
        assert contract["current_upper_boundary"] == "NEXT_CALENDAR_MONTH_START"
    if expected_kind == "COMPARISON":
        assert contract["current_lower_boundary"] == "SYSDATE_MINUS_CURRENT_DURATION"
        assert contract["current_upper_boundary"] == "SYSDATE_EXCLUSIVE"
        assert contract["baseline_lower_boundary"] == (
            "SYSDATE_MINUS_CURRENT_AND_BASELINE_DURATIONS"
        )
        assert contract["baseline_upper_boundary"] == "SYSDATE_MINUS_CURRENT_OFFSET"


def test_comparison_counts_accept_aliased_conditional_sum_but_keep_temporal_checks() -> None:
    case = json.loads(FIXTURES.read_text(encoding="utf-8").splitlines()[7])
    request = ScenarioSqlRequest(
        intent=case["intent"], confirmed_filters=case["confirmed_filters"]
    )
    candidate = case["sql_a"]
    assert not validate_generated_sql(request, candidate, params=case["params"])

    conditional_count = candidate.replace(
        "SUM(CASE WHEN TRA_DATE >= SYSDATE - :cs_b1 AND TRA_DATE < SYSDATE "
        "THEN 1 ELSE 0 END)",
        "COUNT(CASE WHEN TRA_DATE >= SYSDATE - :cs_b1 AND TRA_DATE < SYSDATE THEN 1 END)",
    ).replace(
        "SUM(CASE WHEN TRA_DATE >= SYSDATE - :cs_b2 "
        "AND TRA_DATE < SYSDATE - :cs_b3 THEN 1 ELSE 0 END)",
        "COUNT(CASE WHEN TRA_DATE >= SYSDATE - :cs_b2 "
        "AND TRA_DATE < SYSDATE - :cs_b3 THEN 1 END)",
    )
    assert not validate_generated_sql(request, conditional_count, params=case["params"])

    one_measure = candidate.replace(
        ", SUM(CASE WHEN TRA_DATE >= SYSDATE - :cs_b2 "
        "AND TRA_DATE < SYSDATE - :cs_b3 THEN 1 ELSE 0 END) AS PRIOR_COUNT",
        "",
    )
    assert any(
        "current and baseline count measures" in message
        for message in validate_generated_sql(request, one_measure, params=case["params"])
    )

    invalid_temporal = candidate.replace(
        "TRA_DATE >= SYSDATE - :cs_b1", "TRA_DATE >= TRUNC(SYSDATE) - 30"
    )
    findings = validate_generated_sql(request, invalid_temporal, params=case["params"])
    assert any("half-open" in message or "baseline" in message for message in findings)


def test_customer_intent_rejects_transaction_grain_projection() -> None:
    case = json.loads(FIXTURES.read_text(encoding="utf-8").splitlines()[9])
    request = ScenarioSqlRequest(
        intent=case["intent"], confirmed_filters=case["confirmed_filters"]
    )
    transaction_grain_sql = (
        "SELECT CUS_NUM, TRA_DATE, EXPL_CODE, TRA_AMT "
        "FROM BI_DWH.PIO_TRANSACTIONS WHERE TRA_AMT > :cs_b1 "
        "AND EXPL_CODE IN (:cs_b2, :cs_b3)"
    )
    findings = validate_generated_sql(request, transaction_grain_sql)
    assert "Customer-grain results cannot project transaction-level columns" in findings
    assert not validate_generated_sql(request, case["sql_a"], params=case["params"])


def test_approval_discloses_exact_hashed_payloads_for_both_services() -> None:
    scope = _read_json(SCOPE_PATH)
    payload_bytes = PAYLOAD_PATH.read_bytes()
    payloads = json.loads(payload_bytes)
    scope_digest = scope.pop("scope_sha256")
    assert _sha256(json.dumps(
        scope, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()) == scope_digest
    assert _sha256(payload_bytes) == scope["payload_manifest_sha256"]
    assert _sha256(FIXTURES.read_bytes()) == scope["fixtures_sha256"]
    assert len(payloads["cases"]) == 12
    assert payloads["evaluator_sql_sent"] is False
    assert payloads["synthetic_only"] is True
    assert payloads["schema_records_sha256"] == scope["schema_records_sha256"]
    assert _sha256(A_PROMPT.read_bytes()) == scope["service_a"]["prompt_sha256"]
    assert _sha256(B_PROMPT.read_bytes()) == scope["service_b"]["prompt_sha256"]
    assert payloads["service_a_system_prompt"] == A_PROMPT.read_text(encoding="utf-8")
    assert payloads["service_b_generation_prompt"] == B_PROMPT.read_text(encoding="utf-8")
    assert {
        item["metadata"]["column_name"] for item in payloads["schema_records"]
    } == SCHEMA_COLUMNS
    for case in payloads["cases"]:
        assert case["service_a_provider_identity"] == scope["service_a"]["provider_identity"]
        assert case["service_b_provider_identity"] == scope["service_b"]["provider_identity"]
        a_messages = case["service_a_messages"]
        b_messages = case["service_b_model_messages"]
        assert [item["role"] for item in a_messages] == ["system", "user"]
        assert [item["role"] for item in b_messages] == ["system", "user"]
        assert all(isinstance(item["content"], str) for item in a_messages + b_messages)
        assert _generation_payload_hash(
            "openai", "gpt-5.4-mini", a_messages, "https://api.openai.com/v1"
        ) == case["service_a_payload_sha256"]
        b_digest = _sha256(json.dumps(
            {
                "provider_identity": case["service_b_provider_identity"],
                "messages": b_messages,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode())
        assert b_digest == case["service_b_payload_sha256"]
        assert scope["service_a"]["payload_hashes"][case["case_id"]] == case["service_a_payload_sha256"]
        assert scope["service_b"]["payload_hashes"][case["case_id"]] == case["service_b_payload_sha256"]
        assert "99887766" not in json.dumps(case)
        assert "evaluator_sql" not in case


def test_service_a_runtime_message_builder_matches_every_approved_payload() -> None:
    """Use the same typed schema/message builder at approval and dispatch time."""
    scope = _read_json(SCOPE_PATH)
    payloads = _read_json(PAYLOAD_PATH)
    fixtures = [
        json.loads(line)
        for line in FIXTURES.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    tables, columns = service_a_schema_context(scope["schema_scope"])
    assert all(set(column) == {"schema", "table", "column", "data_type"} for column in columns)
    for fixture, disclosed in zip(fixtures, payloads["cases"], strict=True):
        request = ScenarioSqlRequest(
            intent=fixture["intent"], confirmed_filters=fixture["confirmed_filters"]
        )
        actual = build_service_a_model_messages(
            request,
            payloads["service_a_system_prompt"],
            tables=tables,
            columns=columns,
            budget=a_settings.DWH_SQL_CONTEXT_BUDGET,
        )
        assert actual == disclosed["service_a_messages"], disclosed["case_id"]
        assert _generation_payload_hash(
            "openai", "gpt-5.4-mini", actual, "https://api.openai.com/v1"
        ) == disclosed["service_a_payload_sha256"]
