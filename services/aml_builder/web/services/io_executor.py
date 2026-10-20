"""Dedicated bounded executor for blocking database and filesystem adapters."""

from __future__ import annotations

import asyncio
import contextvars
import functools
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, TypeVar

from services.aml_builder.web.services.settings import settings


T = TypeVar("T")
_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()


def initialize_io_executor() -> None:
    """Create the dedicated worker pool if it has not been initialized.

    The pool is separate from asyncio's default executor and bounded by
    ``IO_WORKERS``. Each repository adapter call owns its connection; the
    executor never shares SQLite connections between workers.
    """
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(
                max_workers=settings.IO_WORKERS,
                thread_name_prefix="aml-io",
            )


def _get_executor() -> ThreadPoolExecutor:
    """Return the process-local bounded worker pool."""
    initialize_io_executor()
    assert _executor is not None
    return _executor


async def run_in_io_pool(
    function: Callable[..., T], *args: Any, **kwargs: Any
) -> T:
    """Run one blocking adapter operation in the dedicated worker pool.

    Args:
        function: Synchronous callable to execute.
        *args: Positional callable arguments.
        **kwargs: Keyword callable arguments.

    Returns:
        The callable's result.
    """
    loop = asyncio.get_running_loop()
    call = functools.partial(function, *args, **kwargs)
    context = contextvars.copy_context()
    return await loop.run_in_executor(_get_executor(), context.run, call)


def close_io_executor() -> None:
    """Drain and close the dedicated worker pool at application shutdown."""
    global _executor
    with _executor_lock:
        executor = _executor
        _executor = None
    if executor is not None:
        executor.shutdown(wait=True, cancel_futures=False)

