"""Measure a scripted fake-adapter scenario before and after P10 optimizations."""

from __future__ import annotations

import argparse
import asyncio
import logging
import statistics
import time
from typing import Any

from langchain_core.messages import HumanMessage, ToolMessage

from services.aml_builder.web.services.observability import emit_tool_metric
from services.aml_builder.web.services.io_executor import run_in_io_pool
from services.aml_builder.web.workflow.context import build_model_messages


class _DiscardMetrics(logging.Handler):
    """Keep benchmark metrics out of the console while measuring fake stages."""

    def emit(self, _record: logging.LogRecord) -> None:
        return


async def _one_run(mode: str, run_number: int) -> dict[str, float]:
    """Run one fake scenario pipeline and return latency/context measurements."""
    metric_logger = logging.getLogger("aml.metrics")
    discard = _DiscardMetrics()
    metric_logger.addHandler(discard)
    try:
        stage_ms: list[float] = []
        total_start = time.perf_counter()
        for stage in ("intent", "plan", "shadow", "metadata", "deploy_check"):
            started = time.perf_counter()
            await asyncio.sleep(0.002)
            elapsed = (time.perf_counter() - started) * 1000
            stage_ms.append(elapsed)
            emit_tool_metric(
                correlation_id=f"fake-thread:req-{run_number}",
                thread_id="fake-thread",
                revision_id="fake-revision",
                tool=stage,
                duration_ms=elapsed,
                status="ok",
                error_code=None,
                token_usage={"input_tokens": 120, "output_tokens": 18},
            )

        def fake_search(search_type: str) -> list[dict[str, str]]:
            time.sleep(0.02)
            return [{"code": search_type}]

        retrieval_start = time.perf_counter()
        if mode in {"baseline", "context", "io"}:
            if mode == "io":
                retrieval_result = [
                    await run_in_io_pool(fake_search, item)
                    for item in ("cash", "wire", "card")
                ]
            else:
                retrieval_result = [fake_search(item) for item in ("cash", "wire", "card")]
        else:
            from services.aml_builder.web.services.explanation_code_search import (
                _run_per_type_searches,
            )
            retrieval_result = await run_in_io_pool(
                _run_per_type_searches,
                ["cash", "wire", "card"],
                fake_search,
                concurrency=3,
            )
        retrieval_ms = (time.perf_counter() - retrieval_start) * 1000

        probe_scheduled = time.perf_counter()

        async def probe_loop() -> float:
            """Return when the event loop gets a turn after blocking work."""
            await asyncio.sleep(0)
            return time.perf_counter()

        loop_probe = asyncio.create_task(probe_loop())
        if mode in {"baseline", "context", "retrieval"}:
            time.sleep(0.02)
        else:
            await run_in_io_pool(time.sleep, 0.02)
        probe_started = await loop_probe
        event_loop_lag_ms = max(0.0, (probe_started - probe_scheduled) * 1000)

        messages: list[Any] = []
        for index in range(3):
            messages.append(HumanMessage(content=f"old user turn {index}"))
            messages.append(
                ToolMessage(
                    content=(
                        '{"schema_version":1,"status":"ok","revision_id":"old",'
                        '"payload":{"plan_hash":"fake-hash-' + str(index) + '","data":"'
                        + "x" * 1800
                        + '"}}'
                    ),
                    name="planner",
                    tool_call_id=f"planner-{index}",
                )
            )
        messages.append(HumanMessage(content="latest user turn"))
        if mode in {"baseline", "io", "retrieval"}:
            context_messages = messages
        else:
            context_messages = build_model_messages(
                messages,
                current_revision={"revision_id": "current", "rev_no": 2, "stage": "PLAN_PENDING"},
                superseded={},
                context_policy={
                    "trimming": True,
                    "keep_latest_per_tool": True,
                    "max_tool_payload_chars": 400,
                    "keep_latest_turns": 1,
                },
            )
        context_chars = float(sum(len(str(message.content)) for message in context_messages))
        total_ms = (time.perf_counter() - total_start) * 1000
        return {
            "total_ms": total_ms,
            "stage_mean_ms": statistics.mean(stage_ms),
            "retrieval_ms": retrieval_ms,
            "event_loop_lag_ms": event_loop_lag_ms,
            "context_chars": context_chars,
            "retrieval_results": float(len(retrieval_result)),
        }
    finally:
        metric_logger.removeHandler(discard)


async def _run(mode: str, runs: int) -> list[dict[str, float]]:
    """Execute repeated fake scenarios in the requested comparison mode."""
    return [await _one_run(mode, number) for number in range(runs)]


def main() -> None:
    """Parse CLI options and print median fake-scenario measurements."""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=("baseline", "context", "io", "retrieval", "final"),
        required=True,
    )
    parser.add_argument("--runs", type=int, default=5)
    args = parser.parse_args()
    rows = asyncio.run(_run(args.mode, max(args.runs, 1)))
    keys = rows[0].keys()
    result: dict[str, Any] = {
        key: round(statistics.median(row[key] for row in rows), 3) for key in keys
    }
    result.update({"mode": args.mode, "runs": len(rows)})
    print(result)


if __name__ == "__main__":
    main()
