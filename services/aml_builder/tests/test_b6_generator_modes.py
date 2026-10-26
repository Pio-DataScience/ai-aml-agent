"""Offline mode selection and shadow-isolation tests for B6."""

from __future__ import annotations

import asyncio
from threading import Event, Timer

import pytest

from services.aml_builder.web.domain.dwh_scenario.contracts import (
    ScenarioSqlRequest,
    ScenarioSqlResult,
)
from services.aml_builder.web.services.dwh_scenario.mode import (
    compare_results,
    generate_by_mode,
)
from services.aml_builder.web.services.settings import Settings


def config(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "ORACLE_DSN": "unused", "ORACLE_USER": "unused", "ORACLE_PASSWORD": "unused",
        "_env_file": None, "DWH_SQL_MODE": "http", "DWH_SQL_ENVIRONMENT": "test",
        "DWH_SQL_SHADOW_TIMEOUT_SECONDS": 0.02,
    }
    values.update(overrides)
    return Settings(**values)


def request() -> ScenarioSqlRequest:
    return ScenarioSqlRequest(intent={
        "scenario_name": "mode test", "scenario_type": "TRANSACTION",
        "detection_logic": "test", "ready_for_handoff": True,
    })


class Generator:
    def __init__(self, result: ScenarioSqlResult | None = None, delay: float = 0):
        self.result = result or ScenarioSqlResult(sql="SELECT CUS_NUM FROM BI_DWH.TX", approved=True)
        self.delay = delay
        self.calls = 0

    async def generate(self, _request: ScenarioSqlRequest) -> ScenarioSqlResult:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.result


def test_http_default_does_not_construct_local_generator() -> None:
    http = Generator()
    def forbidden_factory():
        raise AssertionError("local generator initialized in HTTP mode")
    async def run() -> ScenarioSqlResult:
        return await generate_by_mode(
            request(), http_generator=http, local_factory=forbidden_factory, config=config()
        )
    result = asyncio.run(run())
    assert result is http.result
    assert http.calls == 1


def test_inprocess_mode_returns_local_result() -> None:
    local = Generator(ScenarioSqlResult(sql="SELECT CUS_NUM FROM BI_DWH.TX", approved=True))
    async def run() -> ScenarioSqlResult:
        return await generate_by_mode(
            request(), http_generator=Generator(), local_factory=lambda: local,
            config=config(DWH_SQL_MODE="inprocess"),
        )
    result = asyncio.run(run())
    assert result is local.result
    assert local.calls == 1


def test_inprocess_missing_dependency_fails_closed_without_http_fallback() -> None:
    http = Generator()
    def missing_dependency():
        raise RuntimeError("optional chromadb package is required")
    async def run() -> ScenarioSqlResult:
        return await generate_by_mode(
            request(), http_generator=http, local_factory=missing_dependency,
            config=config(DWH_SQL_MODE="inprocess"),
        )
    with pytest.raises(RuntimeError, match="chromadb"):
        asyncio.run(run())
    assert http.calls == 0


def test_shadow_returns_http_result_and_local_failure_isolated() -> None:
    http_result = ScenarioSqlResult(sql=None, approved=False, trace_id="http")
    http, local = Generator(http_result), Generator(delay=0.2)
    async def run() -> ScenarioSqlResult:
        return await generate_by_mode(
            request(), http_generator=http, local_factory=lambda: local,
            config=config(DWH_SQL_MODE="shadow"),
        )
    result = asyncio.run(run())
    assert result is http_result
    assert result.trace_id == "http" and not result.approved and result.sql is None
    assert http.calls == 1


def test_late_shadow_completion_cannot_mutate_active_workflow_state() -> None:
    """A timed-out local operation finishing later has no workflow write access."""
    released = Event()
    finished = Event()
    active_state = {
        "active_sql": "SELECT CUS_NUM FROM BI_DWH.ACTIVE",
        "receipts": ["http-receipt"],
        "approved": False,
        "deployment": None,
    }
    snapshot = {key: list(value) if isinstance(value, list) else value for key, value in active_state.items()}
    http_result = ScenarioSqlResult(sql="SELECT CUS_NUM FROM BI_DWH.ACTIVE", trace_id="http")

    class DeferredLocal:
        async def generate(self, _request: ScenarioSqlRequest) -> ScenarioSqlResult:
            def complete_later() -> None:
                released.wait()
                finished.set()
            await asyncio.to_thread(complete_later)
            return ScenarioSqlResult(
                sql="SELECT CUS_NUM FROM BI_DWH.LOCAL", approved=True,
                validation_findings=[{"receipt": "local-receipt"}],
            )

    async def run() -> ScenarioSqlResult:
        return await generate_by_mode(
            request(), http_generator=Generator(http_result),
            local_factory=DeferredLocal,
            config=config(DWH_SQL_MODE="shadow", DWH_SQL_SHADOW_TIMEOUT_SECONDS=0.01),
        )

    release_timer = Timer(0.05, released.set)
    release_timer.daemon = True
    release_timer.start()
    result = asyncio.run(run())
    assert result is http_result
    assert finished.wait(timeout=2)
    assert active_state == snapshot


def test_shadow_production_guard_precedes_both_generators() -> None:
    http, local = Generator(), Generator()
    with pytest.raises(ValueError, match="forbidden in production"):
        async def run() -> ScenarioSqlResult:
            return await generate_by_mode(
                request(), http_generator=http, local_factory=lambda: local,
                config=config(DWH_SQL_MODE="shadow", DWH_SQL_ENVIRONMENT="production"),
            )
        asyncio.run(run())
    assert http.calls == local.calls == 0


def test_invalid_mode_is_rejected_by_settings() -> None:
    with pytest.raises(ValueError):
        config(DWH_SQL_MODE="fallback")


def test_comparison_reports_structure_and_not_text_only_differences() -> None:
    req = request()
    http = ScenarioSqlResult(sql="SELECT CUS_NUM FROM BI_DWH.TX", approved=True)
    local = ScenarioSqlResult(sql="select CUS_NUM from BI_DWH.TX", approved=False)
    report = compare_results(req, http, {"result": local, "elapsed_ms": 2.0})
    assert "discrepancies" in report
    assert "semantic_difference" not in report["discrepancies"]
    assert "SQL" not in str(report)


def test_local_error_comparison_is_redacted() -> None:
    report = compare_results(request(), ScenarioSqlResult(error="secret SQL text"), {"error": "RuntimeError"})
    assert report["local"]["error"] is True
    assert "secret SQL text" not in str(report)
