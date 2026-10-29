"""Offline tests for durable B9 live-run accounting and recovery."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.aml_builder.tests.eval.dwh_scenario.b9_execution_journal import (
    DurableCallJournal,
    UncertainDeliveryError,
    dispatch_with_journal,
)
from services.aml_builder.tests.eval.dwh_scenario.run_b9_live import (
    _prepare_runtime_directories,
    _write_results_atomically,
)


SCOPE = "a" * 64
PAYLOAD = "b" * 64


def _records(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _run_immediate(awaitable: object) -> object:
    """Drive a mock-only coroutine that has no suspension points."""
    coroutine = awaitable
    try:
        coroutine.send(None)  # type: ignore[attr-defined]
    except StopIteration as completed:
        return completed.value
    raise AssertionError("deterministic mock unexpectedly suspended")


def test_write_ahead_is_visible_before_dispatch_and_records_outcome(tmp_path: Path) -> None:
    path = tmp_path / "run.jsonl"
    journal = DurableCallJournal(path, scope_sha256=SCOPE)
    number = journal.begin_call(case_id="b9-01", service="Service A", payload_sha256=PAYLOAD)

    visible = _records(path)
    assert visible[-1]["event"] == "provider_call_write_ahead"
    assert visible[-1]["payload_sha256"] == PAYLOAD
    assert visible[-1]["dispatch_number"] == number

    journal.finish_call(
        dispatch_number=number,
        case_id="b9-01",
        service="Service A",
        payload_sha256=PAYLOAD,
        outcome="returned",
        request_id="req_mock_123",
        candidate_sql="SELECT CUS_NUM FROM BI_DWH.PIO_TRANSACTIONS",
    )
    journal.case_started(case_id="b9-01")
    journal.case_outcome(case_id="b9-01", outcome="completed", details={"semantic": "match"})
    journal.finish_run("completed")

    records = _records(path)
    call = records[2]
    assert call["request_id"] == "req_mock_123"
    assert call["candidate_sql_present"] is True
    assert len(str(call["candidate_sql_sha256"])) == 64
    assert call["timestamp"]
    assert records[-1]["event"] == "run_completed"
    assert DurableCallJournal.recover(path)["state"] == "completed"


def test_unresolved_write_ahead_recovers_as_unknown_without_retry(tmp_path: Path) -> None:
    path = tmp_path / "crashed.jsonl"
    journal = DurableCallJournal(path, scope_sha256=SCOPE)
    journal.case_started(case_id="b9-02")
    journal.begin_call(case_id="b9-02", service="Service B", payload_sha256=PAYLOAD)
    journal._stream.close()  # Simulate abrupt process termination; no terminal event.
    journal._closed = True

    recovered = DurableCallJournal.recover(path)
    assert recovered["state"] == "incomplete"
    assert recovered["dispatch_count"] == 1
    assert recovered["uncertain_calls"] == [{
        "dispatch_number": 1,
        "case_id": "b9-02",
        "service": "Service B",
        "payload_sha256": PAYLOAD,
        "delivery_state": "unknown",
    }]
    assert recovered["incomplete_cases"] == ["b9-02"]
    assert recovered["terminal_record_present"] is False


def test_dispatch_budget_blocks_call_before_the_25th_dispatch(tmp_path: Path) -> None:
    path = tmp_path / "budget.jsonl"
    journal = DurableCallJournal(path, scope_sha256=SCOPE, max_dispatches=24)
    for index in range(24):
        journal.begin_call(
            case_id=f"b9-{index // 2 + 1:02d}",
            service="Service A" if index % 2 == 0 else "Service B",
            payload_sha256=PAYLOAD,
        )
    before = len(_records(path))
    with pytest.raises(RuntimeError, match="budget exhausted before dispatch"):
        journal.begin_call(case_id="b9-13", service="Service A", payload_sha256=PAYLOAD)
    records = _records(path)
    assert len(records) == before + 1
    assert records[-1]["event"] == "dispatch_blocked_budget"
    assert len([item for item in records if item["event"] == "provider_call_write_ahead"]) == 24


def test_budget_configuration_cannot_exceed_hard_cap(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="between 1 and 24"):
        DurableCallJournal(tmp_path / "unsafe.jsonl", scope_sha256=SCOPE, max_dispatches=25)


def test_budget_refuses_the_25th_mock_dispatch_before_callable_runs(tmp_path: Path) -> None:
    path = tmp_path / "hard-budget.jsonl"
    journal = DurableCallJournal(path, scope_sha256=SCOPE)
    called: list[int] = []

    async def mock_call() -> SimpleNamespace:
        called.append(1)
        return SimpleNamespace(sql="SELECT CUS_NUM FROM BI_DWH.PIO_TRANSACTIONS")

    for index in range(24):
        _run_immediate(dispatch_with_journal(
            journal,
            case_id=f"b9-{index // 2 + 1:02d}",
            service="Service A" if index % 2 == 0 else "Service B",
            payload_sha256=PAYLOAD,
            call=mock_call,
        ))
    before = len(called)
    with pytest.raises(RuntimeError, match="budget exhausted before dispatch"):
        _run_immediate(dispatch_with_journal(
            journal,
            case_id="b9-13",
            service="Service A",
            payload_sha256=PAYLOAD,
            call=mock_call,
        ))
    assert len(called) == before == 24
    journal.finish_run("failed", details={"reason": "mock budget cap"})


def test_budget_refuses_the_25th_mock_dispatch_before_callable_runs(tmp_path: Path) -> None:
    path = tmp_path / "hard-budget.jsonl"
    journal = DurableCallJournal(path, scope_sha256=SCOPE)
    called: list[int] = []

    async def mock_call() -> SimpleNamespace:
        called.append(1)
        return SimpleNamespace(sql="SELECT CUS_NUM FROM BI_DWH.PIO_TRANSACTIONS")

    for index in range(24):
        _run_immediate(dispatch_with_journal(
            journal,
            case_id=f"b9-{index // 2 + 1:02d}",
            service="Service A" if index % 2 == 0 else "Service B",
            payload_sha256=PAYLOAD,
            call=mock_call,
        ))
    before = len(called)
    with pytest.raises(RuntimeError, match="budget exhausted before dispatch"):
        _run_immediate(dispatch_with_journal(
            journal,
            case_id="b9-13",
            service="Service A",
            payload_sha256=PAYLOAD,
            call=mock_call,
        ))
    assert len(called) == before == 24
    journal.finish_run("failed", details={"reason": "mock budget cap"})


def test_dispatch_helper_logs_before_call_and_records_mock_request_id(tmp_path: Path) -> None:
    path = tmp_path / "dispatch.jsonl"
    journal = DurableCallJournal(path, scope_sha256=SCOPE)

    async def mock_call() -> SimpleNamespace:
        assert _records(path)[-1]["event"] == "provider_call_write_ahead"
        return SimpleNamespace(
            sql="SELECT CUS_NUM FROM BI_DWH.PIO_TRANSACTIONS",
            generation_only_candidate_sql=None,
            provider_request_id="req_deterministic_mock",
            validation_findings=[],
            error=None,
        )

    result = _run_immediate(dispatch_with_journal(
        journal,
        case_id="b9-03",
        service="Service B",
        payload_sha256=PAYLOAD,
        call=mock_call,
    ))
    assert result.provider_request_id == "req_deterministic_mock"
    call_result = _records(path)[-1]
    assert call_result["event"] == "provider_call_outcome"
    assert call_result["request_id"] == "req_deterministic_mock"
    journal.finish_run("completed")


def test_failed_mock_call_is_unknown_and_interruption_is_explicit(tmp_path: Path) -> None:
    path = tmp_path / "interrupted.jsonl"
    journal = DurableCallJournal(path, scope_sha256=SCOPE)

    async def failing_call() -> object:
        raise TimeoutError("mock timeout")

    with pytest.raises(UncertainDeliveryError):
        _run_immediate(dispatch_with_journal(
            journal,
            case_id="b9-04",
            service="Service A",
            payload_sha256=PAYLOAD,
            call=failing_call,
        ))
    journal.finish_run("interrupted", details={"reason": "mock interruption"})
    recovered = DurableCallJournal.recover(path)
    assert recovered["state"] == "interrupted"
    assert recovered["uncertain_calls"][0]["delivery_state"] == "unknown"


def test_unknown_mock_delivery_stops_without_retry(tmp_path: Path) -> None:
    path = tmp_path / "unknown-delivery.jsonl"
    journal = DurableCallJournal(path, scope_sha256=SCOPE)
    calls: list[int] = []

    async def unavailable_call() -> SimpleNamespace:
        calls.append(1)
        return SimpleNamespace(
            sql=None,
            generation_only_candidate_sql=None,
            error="HTTP timeout",
            generation_only_sse_events=[],
        )

    with pytest.raises(UncertainDeliveryError, match="stopping without retry"):
        _run_immediate(dispatch_with_journal(
            journal,
            case_id="b9-06",
            service="Service B",
            payload_sha256=PAYLOAD,
            call=unavailable_call,
        ))
    assert calls == [1]
    assert len(DurableCallJournal.recover(path)["uncertain_calls"]) == 1
    journal.finish_run("failed", details={"reason": "unknown delivery"})


def test_recovery_tolerates_torn_final_record_and_keeps_prior_intent_unknown(
    tmp_path: Path,
) -> None:
    path = tmp_path / "torn.jsonl"
    journal = DurableCallJournal(path, scope_sha256=SCOPE)
    journal.begin_call(case_id="b9-05", service="Service B", payload_sha256=PAYLOAD)
    journal._stream.close()
    journal._closed = True
    with path.open("a", encoding="utf-8") as stream:
        stream.write('{"event":"provider_call_outcome"')

    recovered = DurableCallJournal.recover(path)
    assert recovered["trailing_record_incomplete"] is True
    assert recovered["uncertain_calls"][0]["case_id"] == "b9-05"


def test_completed_results_json_is_atomically_replaced(tmp_path: Path) -> None:
    path = tmp_path / "results.json"
    path.write_text("", encoding="utf-8")
    _write_results_atomically(path, {"cases": 12, "complete": True})
    assert json.loads(path.read_text(encoding="utf-8")) == {"cases": 12, "complete": True}
    assert not path.with_name("results.json.pending").exists()


def test_runtime_directories_create_missing_parent_roots(tmp_path: Path) -> None:
    runtime_root = tmp_path / "missing" / "run-root"
    directories = _prepare_runtime_directories(runtime_root)
    assert set(directories) == {"b9-static", "b9-artifacts", "b9-logs"}
    assert all(path.is_dir() for path in directories.values())
