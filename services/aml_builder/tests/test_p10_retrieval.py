"""Concurrency and per-type failure behavior for explanation-code retrieval."""

import threading

from services.aml_builder.web.services.explanation_code_retrieval import _run_per_type_searches


def test_per_type_searches_overlap_within_limit_and_preserve_input_order() -> None:
    """Concurrent searches keep stable order and respect the configured semaphore."""
    active = 0
    peak = 0
    lock = threading.Lock()
    overlap = threading.Event()

    def fake_search(value: str) -> list[dict[str, str]]:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            if active >= 2:
                overlap.set()
        assert overlap.wait(timeout=2)
        with lock:
            active -= 1
        return [{"code": value}]

    results = _run_per_type_searches(
        ["slow", "middle", "fast"], fake_search, concurrency=2
    )

    assert [item["transaction_type"] for item in results] == ["slow", "middle", "fast"]
    assert peak >= 2
    assert peak <= 2
    assert all(item["status"] == "ok" for item in results)


def test_one_type_failure_is_reported_without_losing_other_results() -> None:
    """A failed lookup is isolated to its transaction type."""
    def fake_search(value: str) -> list[dict[str, str]]:
        if value == "broken":
            raise RuntimeError("offline fake")
        return [{"code": value}]

    results = _run_per_type_searches(
        ["good", "broken", "also-good"], fake_search, concurrency=3
    )

    assert [item["status"] for item in results] == ["ok", "lookup_failed", "ok"]
    assert results[0]["codes"] == [{"code": "good"}]
    assert results[2]["codes"] == [{"code": "also-good"}]

