"""Durable write-ahead logging for the one-shot B9 evaluation runner."""

from __future__ import annotations

import hashlib
import argparse
import json
import os
import signal
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable


MAX_PROVIDER_DISPATCHES = 24


class UncertainDeliveryError(RuntimeError):
    """Raised after a dispatched call has no conclusive provider outcome."""


def utc_now() -> str:
    """Return an ISO-8601 UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


def _digest(value: str) -> str:
    """Return the SHA-256 digest of UTF-8 text."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class DurableCallJournal:
    """Append-only, fsynced journal for one B9 comparison run.

    The journal must use a new path for every run. An intent record is written
    and fsynced before the caller is allowed to dispatch a provider request.
    An intent without a terminal call record is always considered uncertain.
    """

    def __init__(
        self,
        path: Path,
        *,
        scope_sha256: str,
        max_dispatches: int = MAX_PROVIDER_DISPATCHES,
    ) -> None:
        """Create a new journal and record run start.

        Args:
            path: New JSONL journal path.
            scope_sha256: Approved scope digest for this run.
            max_dispatches: Maximum provider dispatch intents allowed.

        Raises:
            FileExistsError: If the journal path already exists.
            ValueError: If the configured call budget is outside the safe cap.
        """
        if max_dispatches < 1 or max_dispatches > MAX_PROVIDER_DISPATCHES:
            raise ValueError(f"B9 provider dispatch budget must be between 1 and {MAX_PROVIDER_DISPATCHES}")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.max_dispatches = max_dispatches
        self._stream = path.open("x", encoding="utf-8", newline="\n")
        self._dispatch_count = 0
        self._closed = False
        self._write({
            "event": "run_started",
            "timestamp": utc_now(),
            "scope_sha256": scope_sha256,
            "max_provider_dispatches": max_dispatches,
            "dispatch_count": 0,
        })

    def _write(self, record: dict[str, Any]) -> None:
        """Append one JSON record and force it to the filesystem."""
        if self._closed:
            raise RuntimeError("B9 execution journal is closed")
        self._stream.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
        self._stream.flush()
        os.fsync(self._stream.fileno())

    def begin_call(self, *, case_id: str, service: str, payload_sha256: str) -> int:
        """Write a durable write-ahead record before provider dispatch.

        Args:
            case_id: Synthetic B9 case identifier.
            service: Service A or Service B.
            payload_sha256: Hash of the exact disclosed model-bound payload.

        Returns:
            The one-based local dispatch number.

        Raises:
            RuntimeError: If the strict dispatch budget is exhausted.
        """
        if self._dispatch_count >= self.max_dispatches:
            self._write({
                "event": "dispatch_blocked_budget",
                "timestamp": utc_now(),
                "case_id": case_id,
                "service": service,
                "payload_sha256": payload_sha256,
                "dispatch_count": self._dispatch_count,
                "max_provider_dispatches": self.max_dispatches,
            })
            raise RuntimeError("B9 provider dispatch budget exhausted before dispatch")
        self._dispatch_count += 1
        dispatch_number = self._dispatch_count
        self._write({
            "event": "provider_call_write_ahead",
            "timestamp": utc_now(),
            "dispatch_number": dispatch_number,
            "case_id": case_id,
            "service": service,
            "payload_sha256": payload_sha256,
            "delivery_state": "uncertain_until_outcome_recorded",
        })
        return dispatch_number

    def finish_call(
        self,
        *,
        dispatch_number: int,
        case_id: str,
        service: str,
        payload_sha256: str,
        outcome: str,
        request_id: str | None = None,
        candidate_sql: str | None = None,
        sse_events: list[Any] | None = None,
        validation_findings: list[Any] | None = None,
        error: str | None = None,
        delivery_state: str | None = None,
    ) -> None:
        """Write a durable provider-call outcome.

        Args:
            dispatch_number: Number returned by ``begin_call``.
            case_id: Synthetic B9 case identifier.
            service: Service A or Service B.
            payload_sha256: Exact payload hash used for the request.
            outcome: ``returned`` or ``raised``.
            request_id: Provider request ID, if the service exposed one.
            candidate_sql: Generated SQL, if returned.
            validation_findings: Deterministic validator findings, if available.
            error: Failure detail for raised calls.
        """
        delivery_state = delivery_state or (
            "response_observed" if outcome == "returned" else "unknown"
        )
        self._write({
            "event": "provider_call_outcome",
            "timestamp": utc_now(),
            "dispatch_number": dispatch_number,
            "case_id": case_id,
            "service": service,
            "payload_sha256": payload_sha256,
            "outcome": outcome,
            "delivery_state": delivery_state,
            "request_id": request_id,
            "candidate_sql": candidate_sql,
            "candidate_sql_sha256": _digest(candidate_sql) if candidate_sql is not None else None,
            "candidate_sql_present": candidate_sql is not None,
            "sse_events": sse_events or [],
            "validation_findings": validation_findings or [],
            "error": error,
        })

    def case_outcome(self, *, case_id: str, outcome: str, details: dict[str, Any]) -> None:
        """Write a durable paired per-case result summary."""
        self._write({
            "event": "case_outcome",
            "timestamp": utc_now(),
            "case_id": case_id,
            "outcome": outcome,
            "details": details,
        })

    def case_started(self, *, case_id: str) -> None:
        """Write a durable marker before beginning a synthetic case."""
        self._write({
            "event": "case_started",
            "timestamp": utc_now(),
            "case_id": case_id,
            "dispatch_count": self._dispatch_count,
        })

    def finish_run(self, state: str, *, details: dict[str, Any] | None = None) -> None:
        """Record explicit run completion, failure, or interruption and close."""
        if state not in {"completed", "failed", "interrupted"}:
            raise ValueError(f"Invalid B9 terminal state: {state}")
        self._write({
            "event": f"run_{state}",
            "timestamp": utc_now(),
            "dispatch_count": self._dispatch_count,
            "max_provider_dispatches": self.max_dispatches,
            "details": details or {},
        })
        self._stream.close()
        self._closed = True

    @staticmethod
    def recover(path: Path) -> dict[str, Any]:
        """Summarize a journal without retrying or modifying it.

        Args:
            path: Existing JSONL journal.

        Returns:
            Run state, dispatch counts, uncertain call identities, and incomplete cases.

        Raises:
            ValueError: If a complete JSONL record is malformed.
        """
        records: list[dict[str, Any]] = []
        trailing_record_incomplete = False
        with path.open("r", encoding="utf-8") as stream:
            lines = stream.readlines()
            for line_number, line in enumerate(lines, start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    if line_number == len(lines) and not line.endswith("\n"):
                        trailing_record_incomplete = True
                        break
                    raise ValueError(f"Malformed B9 journal record at line {line_number}") from exc
                if not isinstance(record, dict):
                    raise ValueError(f"B9 journal record at line {line_number} is not an object")
                records.append(record)
        intents = {
            record["dispatch_number"]: record
            for record in records
            if record.get("event") == "provider_call_write_ahead"
        }
        resolved = {
            record["dispatch_number"]
            for record in records
            if record.get("event") == "provider_call_outcome"
        }
        unknown_outcomes = {
            record["dispatch_number"]: record
            for record in records
            if record.get("event") == "provider_call_outcome"
            and record.get("delivery_state") == "unknown"
        }
        terminal = next(
            (record for record in reversed(records) if record.get("event") in {
                "run_completed", "run_failed", "run_interrupted"
            }),
            None,
        )
        case_starts = {
            record["case_id"] for record in records if record.get("event") == "case_started"
        }
        case_finishes = {
            record["case_id"] for record in records if record.get("event") == "case_outcome"
        }
        return {
            "state": terminal["event"][4:] if terminal else "incomplete",
            "dispatch_count": len(intents),
            "max_provider_dispatches": next(
                (r.get("max_provider_dispatches") for r in records if r.get("event") == "run_started"),
                MAX_PROVIDER_DISPATCHES,
            ),
            "uncertain_calls": [
                {
                    "dispatch_number": number,
                    "case_id": record.get("case_id"),
                    "service": record.get("service"),
                    "payload_sha256": record.get("payload_sha256"),
                    "delivery_state": "unknown",
                }
                for number, record in sorted(intents.items())
                if number not in resolved or number in unknown_outcomes
            ],
            "incomplete_cases": sorted(case_starts - case_finishes),
            "terminal_record_present": terminal is not None,
            "trailing_record_incomplete": trailing_record_incomplete,
        }


async def dispatch_with_journal(
    journal: DurableCallJournal,
    *,
    case_id: str,
    service: str,
    payload_sha256: str,
    call: Callable[[], Awaitable[Any]],
) -> Any:
    """Journal one call before dispatch and its observed result after.

    Args:
        journal: Active run journal.
        case_id: Synthetic B9 case identifier.
        service: Service A or Service B.
        payload_sha256: Exact approved payload hash.
        call: Awaitable provider call wrapper.

    Returns:
        The provider wrapper result.

    Raises:
        BaseException: Re-raises provider failures after recording uncertainty.
    """
    number = journal.begin_call(
        case_id=case_id, service=service, payload_sha256=payload_sha256
    )
    try:
        result = await call()
    except BaseException as exc:
        journal.finish_call(
            dispatch_number=number,
            case_id=case_id,
            service=service,
            payload_sha256=payload_sha256,
            outcome="raised",
            error=f"{type(exc).__name__}: {exc}",
        )
        if isinstance(exc, (KeyboardInterrupt, SystemExit, GeneratorExit)):
            raise
        raise UncertainDeliveryError(
            f"{service} delivery is unknown for {case_id}; stopping without retry"
        ) from exc
    candidate = getattr(result, "generation_only_candidate_sql", None) or getattr(result, "sql", None)
    findings = getattr(result, "validation_findings", None)
    sse_events = getattr(result, "generation_only_sse_events", None)
    if service == "Service B":
        findings = getattr(result, "generation_only_provider_validation_findings", findings)
    request_id = _extract_request_id(result)
    result_error = getattr(result, "error", None)
    delivery_state = "unknown" if result_error and candidate is None else "response_observed"
    journal.finish_call(
        dispatch_number=number,
        case_id=case_id,
        service=service,
        payload_sha256=payload_sha256,
        outcome="returned",
        request_id=request_id,
        candidate_sql=candidate,
        sse_events=sse_events if isinstance(sse_events, list) else [],
        validation_findings=findings if isinstance(findings, list) else [],
        error=str(result_error) if result_error else None,
        delivery_state=delivery_state,
    )
    if delivery_state == "unknown":
        raise UncertainDeliveryError(
            f"{service} delivery is unknown for {case_id}; stopping without retry"
        )
    return result


def _extract_request_id(result: Any) -> str | None:
    """Extract a provider request identifier when the adapter exposes one."""
    for attr in ("provider_request_id", "request_id"):
        value = getattr(result, attr, None)
        if isinstance(value, str) and value:
            return value
    response = getattr(result, "response_metadata", None)
    if isinstance(response, dict):
        for key in ("request_id", "x-request-id"):
            value = response.get(key)
            if isinstance(value, str) and value:
                return value
        headers = response.get("headers")
        if isinstance(headers, dict):
            value = headers.get("x-request-id") or headers.get("X-Request-ID")
            if isinstance(value, str) and value:
                return value
    return None


def main() -> None:
    """Print a read-only recovery summary for a B9 execution journal."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("journal", type=Path, help="Existing B9 JSONL execution journal")
    args = parser.parse_args()
    print(json.dumps(DurableCallJournal.recover(args.journal), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
