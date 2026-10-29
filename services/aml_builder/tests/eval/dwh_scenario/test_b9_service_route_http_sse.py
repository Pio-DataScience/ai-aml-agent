"""Linux-only real Service A-to-B route integration with a mocked graph output."""

from __future__ import annotations

import asyncio
import contextvars
import importlib
import json
import os
import socket
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from services.aml_builder.web.domain.dwh_scenario.contracts import ScenarioSqlRequest
from services.aml_builder.web.services.dwh_scenario import http_adapter
from services.aml_builder.web.services.settings import settings as a_settings


ROOT = Path(__file__).resolve().parents[5]
SERVICE_B_ROOT = ROOT.parent / "PioTech-AI" / "services" / "dwh_scenario_agent"
SERVICE_B_ENV = ROOT.parent / "PioTech-AI" / "Docs" / "B9_service_b_development.env.example"
FIXTURE_PATH = Path(__file__).with_name("b9_cases.jsonl")
PAYLOAD_PATH = ROOT / "Docs" / "B9_model_payloads.json"
SCOPE_PATH = ROOT / "Docs" / "B9_generation_only_scope.json"


def _read_env_example(path: Path) -> dict[str, str]:
    """Read development settings from the reviewed example file."""
    result = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            result[key] = value
    return result


def _port() -> int:
    """Return an available loopback TCP port."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@pytest.mark.skipif(sys.platform == "win32", reason="Run under Linux/WSL for HTTP SSE integration")
def test_service_a_adapter_to_service_b_chat_stream_route(monkeypatch, tmp_path: Path) -> None:
    """Verify the guarded real route and SSE handoff while replacing only model output."""
    assert a_settings.DWH_SQL_MODE == "http"
    for key, value in _read_env_example(SERVICE_B_ENV).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("OPENAI_API_KEY", "mocked-graph-must-not-call-provider")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    runtime_root = tmp_path / "b9-service-b-smoke"
    runtime_paths = {
        "STATIC_DIR": runtime_root / "static",
        "ARTIFACTS_DIR": runtime_root / "artifacts",
        "LOGS_DIR": runtime_root / "logs",
    }
    for name, path in runtime_paths.items():
        path.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv(name, str(path))
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    sys.path.insert(0, str(SERVICE_B_ROOT))

    # The route's lifespan is disabled and this stub keeps imports independent
    # of Torch; no production code path or dependency is altered.
    torch_stub = types.ModuleType("torch")
    torch_stub.cuda = types.SimpleNamespace(is_available=lambda: False)
    torch_stub.__spec__ = __import__("importlib.machinery").machinery.ModuleSpec(
        "torch", loader=None
    )
    monkeypatch.setitem(sys.modules, "torch", torch_stub)

    scope = json.loads(SCOPE_PATH.read_text(encoding="utf-8"))
    manifest = json.loads(PAYLOAD_PATH.read_text(encoding="utf-8"))
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8").splitlines()[0])
    case_id = "b9-01"
    payload_case = next(item for item in manifest["cases"] if item["case_id"] == case_id)
    candidate = fixture["sql_b"]
    disclosure = ["mocked generation normalization disclosure"]
    graph_calls: list[dict[str, Any]] = []

    fake_agent = types.ModuleType("web.services.agent")
    fake_agent.MAX_ITERATIONS = 1

    class DeterministicGraph:
        """Return exactly one already-generated synthetic candidate."""

        def stream(self, state: dict[str, Any], *, config: dict[str, Any]):
            graph_calls.append({"state": state, "config": config})
            yield {
                "sql_generation": {
                    "verdict": "GENERATION_ONLY",
                    "generation_only_candidate_sql": candidate,
                    "generation_only_normalizations": disclosure,
                }
            }

    fake_agent.app = DeterministicGraph()
    fake_agent._register_thread_sse = lambda *_args: None
    fake_agent._unregister_thread_sse = lambda: None
    fake_agent.register_sse_session = lambda *_args: None
    fake_agent.unregister_sse_session = lambda *_args: None
    fake_agent.sse_queue_var = contextvars.ContextVar("route_test_queue")
    fake_agent.sse_loop_var = contextvars.ContextVar("route_test_loop")
    monkeypatch.setitem(sys.modules, "web.services.agent", fake_agent)

    b_settings_module = importlib.import_module("web.services.settings")
    b_settings = importlib.reload(b_settings_module).settings
    b_main = importlib.import_module("web.api.main")
    assert b_settings.DWH_SCENARIO_GENERATION_ONLY_SCOPE_SHA256 == scope["scope_sha256"]
    assert "web.services.oracle" not in sys.modules
    monkeypatch.setattr(b_main, "upsert_chat_session", lambda *_args: None)

    port = _port()
    server = __import__("uvicorn").Server(
        __import__("uvicorn").Config(
            b_main.app,
            host="127.0.0.1",
            port=port,
            lifespan="off",
            timeout_graceful_shutdown=3,
            log_level="critical",
            access_log=False,
        )
    )
    monkeypatch.setattr(a_settings, "PIOTECH_AI_URL", f"http://127.0.0.1:{port}/chat/stream")
    monkeypatch.setattr(a_settings, "PIOTECH_AI_TIMEOUT_SECONDS", 10)
    request = ScenarioSqlRequest(
        intent=fixture["intent"], confirmed_filters=fixture["confirmed_filters"]
    )
    results = []

    async def exchange() -> None:
        """Serve the actual route and let Service A consume its streamed response."""
        server_task = asyncio.create_task(server.serve())
        try:
            async with asyncio.timeout(10):
                while not server.started:
                    await asyncio.sleep(0.01)
                results.append(
                    await http_adapter.HttpScenarioSqlGenerator().generate_only_for_approved_evaluation(
                        request,
                        scope_sha256=scope["scope_sha256"],
                        schema_records=manifest["schema_records"],
                        case_id=case_id,
                        service_b_payload_sha256=payload_case[
                            "service_b_payload_sha256"
                        ],
                    )
                )
        finally:
            server.should_exit = True
            await asyncio.wait_for(server_task, timeout=4)

    if sys.platform == "win32":
        asyncio.run(exchange(), loop_factory=asyncio.SelectorEventLoop)
    else:
        asyncio.run(exchange())

    assert len(graph_calls) == 1
    assert graph_calls[0]["state"]["generation_only"] is True
    assert results[0].sql == candidate
    assert results[0].approved is False
    assert results[0].generation_only_normalizations == disclosure
    assert "web.services.oracle" not in sys.modules
