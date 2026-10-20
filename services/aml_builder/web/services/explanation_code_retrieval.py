"""Concurrent per-type explanation-code retrieval helpers."""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from services.aml_builder.web.services.settings import settings

logger = logging.getLogger(__name__)


def _run_per_type_searches(
    transaction_types: list[str],
    worker: Callable[[str], list[dict[str, Any]]],
    *,
    concurrency: int | None = None,
) -> list[dict[str, Any]]:
    """Run independent type searches concurrently, preserving input order.

    A failure is returned as ``lookup_failed`` for its own type and does not
    discard successful results for other types.

    Args:
        transaction_types: Search terms in caller order.
        worker: Synchronous one-type retrieval and reranking function.
        concurrency: Optional test override for the bounded worker count.

    Returns:
        Ordered records containing each type's status and result codes.
    """
    configured_limit = concurrency or settings.RETRIEVAL_CONCURRENCY
    limit = max(1, min(configured_limit, settings.IO_WORKERS))
    semaphore = threading.BoundedSemaphore(limit)

    def run_one(transaction_type: str) -> dict[str, Any]:
        with semaphore:
            try:
                return {
                    "transaction_type": transaction_type,
                    "status": "ok",
                    "codes": worker(transaction_type),
                }
            except Exception:
                logger.warning(
                    "[EXPL_SEARCH] Lookup failed for one transaction type; continuing other searches."
                )
                return {
                    "transaction_type": transaction_type,
                    "status": "lookup_failed",
                    "codes": [],
                }

    with ThreadPoolExecutor(
        max_workers=limit, thread_name_prefix="aml-retrieval"
    ) as executor:
        futures = [executor.submit(run_one, item) for item in transaction_types]
        return [future.result() for future in futures]
