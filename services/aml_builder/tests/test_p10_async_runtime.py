"""Bounded worker pool and per-call SQLite connection tests."""

import asyncio
import sqlite3
import threading
from pathlib import Path

import pytest

from services.aml_builder.web.services.io_executor import run_in_io_pool
from services.aml_builder.web.workflow.repo import WorkflowRepo


@pytest.mark.asyncio
async def test_blocking_work_runs_on_a_worker_thread() -> None:
    """The dedicated I/O executor keeps blocking work off the event-loop thread."""
    event_loop_thread = threading.get_ident()
    worker_thread = await run_in_io_pool(threading.get_ident)
    assert worker_thread != event_loop_thread


@pytest.mark.asyncio
async def test_concurrent_workflow_writes_use_independent_sqlite_connections(
    tmp_path: Path,
) -> None:
    """Concurrent writes are serialized safely by SQLite WAL and fresh connections."""
    repo = WorkflowRepo(tmp_path / "workflow.sqlite")
    repo.initialize()

    await asyncio.gather(
        *(
            run_in_io_pool(
                repo.get_or_create_workflow,
                f"p_chat-{index}_999",
                "p",
                f"chat-{index}",
                "999",
            )
            for index in range(12)
        )
    )

    with sqlite3.connect(repo.path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM scenario_workflow").fetchone()[0]
    assert count == 12

