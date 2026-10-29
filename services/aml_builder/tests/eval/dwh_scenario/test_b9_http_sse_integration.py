"""One loopback HTTP/SSE exchange with a deterministic Service B mock."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from services.aml_builder.web.domain.dwh_scenario.contracts import ScenarioSqlRequest
from services.aml_builder.web.services.dwh_scenario.http_adapter import (
    HttpScenarioSqlGenerator,
)
from services.aml_builder.web.services.settings import settings


ROOT = Path(__file__).resolve().parents[5]
FIXTURE_PATH = Path(__file__).with_name("b9_cases.jsonl")
PAYLOAD_PATH = ROOT / "Docs" / "B9_model_payloads.json"
SCOPE_PATH = ROOT / "Docs" / "B9_generation_only_scope.json"


@pytest.mark.skipif(
    os.environ.get("RUN_B9_HTTP_SSE") != "1",
    reason="set RUN_B9_HTTP_SSE=1 to run the deterministic loopback SSE transport test",
)
def test_adapter_round_trips_one_fragmented_generation_only_sse_response(monkeypatch) -> None:
    """Prove HTTP request, fragmented SSE delivery, and adapter parsing without Service B execution."""
    assert settings.DWH_SQL_MODE == "http"
    fixtures = [
        json.loads(line)
        for line in FIXTURE_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    fixture = fixtures[0]
    case_id = "b9-01"
    scope = json.loads(SCOPE_PATH.read_text(encoding="utf-8"))
    payloads = json.loads(PAYLOAD_PATH.read_text(encoding="utf-8"))
    payload_case = next(item for item in payloads["cases"] if item["case_id"] == case_id)
    candidate = fixture["sql_b"]
    request = ScenarioSqlRequest(
        intent=fixture["intent"], confirmed_filters=fixture["confirmed_filters"]
    )
    received: dict[str, Any] = {}

    async def serve_sse(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Read one HTTP POST and respond with fragmented SSE frames."""
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=3)
            headers: dict[str, str] = {}
            while line := await asyncio.wait_for(reader.readline(), timeout=3):
                if line in (b"\r\n", b"\n"):
                    break
                name, value = line.decode("latin-1").split(":", 1)
                headers[name.casefold()] = value.strip()
            content_length = int(headers.get("content-length", "0"))
            raw_body = await asyncio.wait_for(
                reader.readexactly(content_length), timeout=3
            )
            received["request_line"] = request_line.decode("ascii").strip()
            received["body"] = json.loads(raw_body)
            frames = [
                "data: "
                + json.dumps(
                    {
                        "type": "generation_only_candidate",
                        "approved": False,
                        "text": candidate,
                        "normalizations": ["mocked temporal-bound disclosure"],
                        "validation_findings": [{"message": "synthetic finding"}],
                    }
                )
                + "\n\n",
                "data: {\"type\": \"done\"}\n\n",
                "data: [DONE]\n\n",
            ]
            body = "".join(frames).encode("utf-8")
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"content-type: text/event-stream; charset=utf-8\r\n"
                b"transfer-encoding: chunked\r\n"
                b"connection: close\r\n\r\n"
            )
            # Split inside JSON/event content to exercise incremental line buffering.
            split_points = (1, 7, 19, 43, 81, len(body))
            start = 0
            for end in split_points:
                if end <= start:
                    continue
                chunk = body[start:end]
                writer.write(f"{len(chunk):X}\r\n".encode("ascii") + chunk + b"\r\n")
                await writer.drain()
                await asyncio.sleep(0.005)
                start = end
            writer.write(b"0\r\n\r\n")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def exchange() -> Any:
        """Run a bounded client/server exchange on the same event loop."""
        server = await asyncio.start_server(serve_sse, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        monkeypatch.setattr(settings, "PIOTECH_AI_URL", f"http://127.0.0.1:{port}/chat/stream")
        monkeypatch.setattr(settings, "PIOTECH_AI_TIMEOUT_SECONDS", 5)
        async with server:
            async with asyncio.timeout(8):
                result = await HttpScenarioSqlGenerator(filter_catalog={}).generate_only_for_approved_evaluation(
                    request,
                    scope_sha256=scope["scope_sha256"],
                    schema_records=payloads["schema_records"],
                    case_id=case_id,
                    service_b_payload_sha256=payload_case[
                        "service_b_payload_sha256"
                    ],
                )
        return result

    if sys.platform == "win32":
        result = asyncio.run(exchange(), loop_factory=asyncio.SelectorEventLoop)
    else:
        result = asyncio.run(exchange())

    assert received["request_line"].startswith("POST /chat/stream HTTP/")
    metadata = received["body"]["metadata"]
    assert metadata["generation_only"] is True
    assert metadata["generation_only_scope_sha256"] == scope["scope_sha256"]
    assert metadata["generation_only_case_id"] == case_id
    assert result.sql == candidate
    assert result.generation_only_candidate_sql == candidate
    assert result.approved is False
    assert result.generation_only_normalizations == ["mocked temporal-bound disclosure"]
    assert result.generation_only_provider_validation_findings == [
        {"message": "synthetic finding"}
    ]
    assert [event["type"] for event in result.generation_only_sse_events] == [
        "generation_only_candidate", "done", "transport_done"
    ]
