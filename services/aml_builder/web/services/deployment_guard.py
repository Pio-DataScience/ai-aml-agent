"""Server-side deployment authorization for AML scenario artifacts.

The guard reconstructs the latest SQL artifact from LangGraph messages, binds
approval to the exact intent, SQL, and governance metadata, and maintains a
small SQLite claim ledger so concurrent or replayed deployments cannot write
the same artifact twice.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Literal, Mapping, Optional, Tuple

from services.aml_builder.web.services.scenario_metadata import (
    validate_scenario_metadata,
)
from services.aml_builder.web.services.settings import settings


ApprovalSource = Literal["chat", "direct_endpoint"]


class DeploymentGuardError(ValueError):
    """Raised when deployment evidence is missing, stale, or already consumed."""

    def __init__(self, code: str, message: str) -> None:
        """Create an actionable deployment rejection.

        Args:
            code: Stable machine-readable rejection code.
            message: Human-readable remediation guidance.
        """
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class DeploymentEvidence:
    """Immutable evidence authorizing one exact deployment artifact."""

    artifact_id: str
    thread_id: str
    intent: Dict[str, Any]
    raw_sql: str
    metadata: Dict[str, Any]
    approved_by: str
    approval_source: ApprovalSource
    approved_at: str
    validation_success: bool = True
    metadata_valid: bool = True


@dataclass(frozen=True)
class _ToolResult:
    """Parsed tool result and its originating tool-call arguments."""

    index: int
    name: str
    payload: Dict[str, Any]
    arguments: Dict[str, Any]


@dataclass(frozen=True)
class _CheckpointArtifact:
    """Latest lifecycle evidence recovered from one checkpoint message list."""

    shadow: _ToolResult
    intent: Dict[str, Any]
    metadata_result: Optional[_ToolResult]
    latest_intent_index: int


_APPROVAL_PATTERNS = (
    re.compile(r"\bdeploy(?:\s+(?:it|this|the scenario))?(?:\s+to production)?\b", re.I),
    re.compile(r"\bpersist(?:\s+(?:it|this|the scenario))?\b", re.I),
    re.compile(r"\bsave\s+(?:it|this|the scenario)(?:\s+to production)?\b", re.I),
    re.compile(r"\bfinali[sz]e(?:\s+(?:it|this|the scenario))?\b", re.I),
    re.compile(r"\bconfirm\s+and\s+save\b", re.I),
)
_REJECTION_PATTERN = re.compile(
    r"\b(?:do\s+not|don't|dont|not|cancel|stop|reject)\b.{0,30}"
    r"\b(?:deploy|persist|save|finali[sz]e)\b",
    re.I,
)
_MODIFICATION_PATTERN = re.compile(
    r"\b(?:change|modify|update|adjust|revise|replace|edit|set|increase|decrease)\b",
    re.I,
)


def _canonical_json(value: Any) -> str:
    """Return deterministic JSON for artifact identity and equality checks."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _normalize_sql(raw_sql: str) -> str:
    """Normalize only harmless outer whitespace while preserving SQL identity."""
    return str(raw_sql or "").strip()


def _unwrap_intent(value: Any) -> Dict[str, Any]:
    """Parse and unwrap supported AML intent envelope shapes."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise DeploymentGuardError(
                "invalid_intent", f"Intent JSON is invalid: {exc}"
            ) from exc
    if not isinstance(value, dict):
        raise DeploymentGuardError(
            "invalid_intent", "A structured scenario intent is required."
        )
    for key in ("enriched_intent", "AMLIntent", "aml_intent", "intent"):
        nested = value.get(key)
        if isinstance(nested, dict):
            return deepcopy(nested)
    return deepcopy(value)


def _parse_json_object(value: Any, label: str) -> Dict[str, Any]:
    """Parse a dictionary or serialized JSON dictionary."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise DeploymentGuardError(
                f"invalid_{label}", f"{label.replace('_', ' ').title()} JSON is invalid: {exc}"
            ) from exc
    if not isinstance(value, dict):
        raise DeploymentGuardError(
            f"invalid_{label}", f"{label.replace('_', ' ').title()} must be a JSON object."
        )
    return deepcopy(value)


def compute_artifact_id(
    intent: Mapping[str, Any], raw_sql: str, metadata: Mapping[str, Any]
) -> str:
    """Compute the immutable identity of a deployable scenario artifact.

    Args:
        intent: Current structured scenario intent.
        raw_sql: Successfully shadow-tested SQL.
        metadata: Validated governance metadata.

    Returns:
        A SHA-256 hexadecimal artifact identifier.
    """
    payload = {
        "intent": dict(intent),
        "raw_sql": _normalize_sql(raw_sql),
        "metadata": dict(metadata),
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def is_explicit_deployment_approval(message: str) -> bool:
    """Return whether a user message explicitly authorizes persistence."""
    user_text = str(message or "").split(
        "[Active Scenario Governance Metadata from Sidebar]", 1
    )[0]
    if _REJECTION_PATTERN.search(user_text):
        return False
    if _MODIFICATION_PATTERN.search(user_text):
        return False
    return any(pattern.search(user_text) for pattern in _APPROVAL_PATTERNS)


def _message_type(message: Any) -> str:
    """Return a stable LangChain message type without importing message classes."""
    return str(getattr(message, "type", "") or message.__class__.__name__).lower()


def _tool_calls(message: Any) -> Iterable[Dict[str, Any]]:
    """Yield normalized tool calls from an AI message."""
    for call in getattr(message, "tool_calls", None) or []:
        if isinstance(call, dict):
            yield call
        else:
            yield {
                "id": getattr(call, "id", None),
                "name": getattr(call, "name", None),
                "args": getattr(call, "args", {}) or {},
            }


def _parse_tool_payload(message: Any) -> Optional[Dict[str, Any]]:
    """Parse a ToolMessage JSON object, returning ``None`` for non-JSON output."""
    content = getattr(message, "content", None)
    if isinstance(content, dict):
        return content
    if not isinstance(content, str):
        return None
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _recover_checkpoint_artifact(messages: Iterable[Any]) -> _CheckpointArtifact:
    """Recover the latest SQL, intent, and metadata evidence from graph messages."""
    calls: Dict[str, Tuple[str, Dict[str, Any]]] = {}
    results: List[_ToolResult] = []
    latest_intent_index = -1
    latest_shadow_call_index = -1

    for index, message in enumerate(messages):
        msg_type = _message_type(message)
        if msg_type in ("ai", "aimessage"):
            for call in _tool_calls(message):
                call_id = call.get("id")
                name = str(call.get("name") or "")
                arguments = call.get("args") or {}
                if call_id and isinstance(arguments, dict):
                    calls[str(call_id)] = (name, arguments)
                if name == "analyze_intent_and_discover_explanation_codes":
                    latest_intent_index = index
                elif name == "execute_oracle_dwh_shadow_test":
                    latest_shadow_call_index = index

        if msg_type not in ("tool", "toolmessage"):
            continue
        payload = _parse_tool_payload(message)
        if payload is None:
            continue
        tool_call_id = str(getattr(message, "tool_call_id", "") or "")
        mapped_name, arguments = calls.get(tool_call_id, ("", {}))
        name = str(getattr(message, "name", "") or mapped_name)
        result = _ToolResult(index, name, payload, arguments)
        results.append(result)
        if name == "analyze_intent_and_discover_explanation_codes":
            latest_intent_index = index

    shadow_results = [
        result
        for result in results
        if result.name == "execute_oracle_dwh_shadow_test"
    ]
    if not shadow_results:
        raise DeploymentGuardError(
            "missing_shadow_test",
            "No shadow-test evidence exists for this session. Run the shadow test first.",
        )
    shadow = shadow_results[-1]
    if latest_shadow_call_index > shadow.index:
        raise DeploymentGuardError(
            "shadow_test_in_progress",
            "A newer shadow test has not completed. Wait for its result before deployment.",
        )
    if shadow.payload.get("validation_success") is not True:
        raise DeploymentGuardError(
            "shadow_validation_failed",
            "The latest SQL shadow test did not pass. Fix the SQL and run it again.",
        )
    if not _normalize_sql(str(shadow.payload.get("raw_sql") or "")):
        raise DeploymentGuardError(
            "missing_validated_sql", "The successful shadow test contains no SQL artifact."
        )
    if latest_intent_index > shadow.index:
        raise DeploymentGuardError(
            "stale_shadow_test",
            "The scenario intent changed after the latest shadow test. Shadow-test the current intent again.",
        )

    intent_argument = shadow.arguments.get("intent_json")
    if intent_argument is None:
        raise DeploymentGuardError(
            "missing_intent_evidence",
            "The shadow test is not bound to a recoverable intent. Run the shadow test again.",
        )
    intent = _unwrap_intent(intent_argument)

    metadata_results = [
        result
        for result in results
        if result.name == "prepare_scenario_metadata_for_persistence"
        and result.index > shadow.index
    ]
    metadata_result = metadata_results[-1] if metadata_results else None
    return _CheckpointArtifact(
        shadow=shadow,
        intent=intent,
        metadata_result=metadata_result,
        latest_intent_index=latest_intent_index,
    )


def _validated_metadata(metadata_value: Any) -> Dict[str, Any]:
    """Parse and perform definitive live governance metadata validation."""
    metadata = _parse_json_object(metadata_value, "metadata")
    is_valid, problems = validate_scenario_metadata(metadata)
    if not is_valid:
        raise DeploymentGuardError(
            "invalid_metadata",
            "Governance metadata is not deployable: " + "; ".join(problems),
        )
    return metadata


def _latest_explicit_approval_after(messages: List[Any], index: int) -> bool:
    """Check for explicit user deployment approval after an evidence boundary."""
    for message in reversed(messages[index + 1 :]):
        if _message_type(message) not in ("human", "humanmessage"):
            continue
        return is_explicit_deployment_approval(str(getattr(message, "content", "")))
    return False


def _build_evidence(
    *,
    artifact: _CheckpointArtifact,
    metadata: Dict[str, Any],
    thread_id: str,
    approved_by: str,
    approval_source: ApprovalSource,
) -> DeploymentEvidence:
    """Build immutable evidence from already verified lifecycle inputs."""
    raw_sql = _normalize_sql(str(artifact.shadow.payload.get("raw_sql") or ""))
    return DeploymentEvidence(
        artifact_id=compute_artifact_id(artifact.intent, raw_sql, metadata),
        thread_id=thread_id,
        intent=deepcopy(artifact.intent),
        raw_sql=raw_sql,
        metadata=deepcopy(metadata),
        approved_by=approved_by,
        approval_source=approval_source,
        approved_at=datetime.now(timezone.utc).isoformat(),
    )


def authorize_chat_deployment(
    *,
    messages: Iterable[Any],
    intent_json: Any,
    raw_sql: str,
    metadata_json: Any,
    thread_id: str,
    approved_by: str,
) -> DeploymentEvidence:
    """Authorize the agent-tool deployment route from current graph state."""
    message_list = list(messages)
    artifact = _recover_checkpoint_artifact(message_list)
    supplied_intent = _unwrap_intent(intent_json)
    if _canonical_json(supplied_intent) != _canonical_json(artifact.intent):
        raise DeploymentGuardError(
            "stale_intent",
            "The supplied intent is not the intent used by the latest shadow test.",
        )
    validated_sql = _normalize_sql(str(artifact.shadow.payload.get("raw_sql") or ""))
    if _normalize_sql(raw_sql) != validated_sql:
        raise DeploymentGuardError(
            "stale_sql", "The supplied SQL is not the latest successfully validated SQL."
        )

    metadata = _validated_metadata(metadata_json)
    metadata_result = artifact.metadata_result
    if metadata_result is None:
        raise DeploymentGuardError(
            "missing_metadata_review",
            "Prepare and review governance metadata after the shadow test before deployment.",
        )
    if metadata_result.payload.get("ready_for_persistence") is not True:
        raise DeploymentGuardError(
            "metadata_not_ready",
            "The latest governance metadata review is not ready for persistence.",
        )
    reviewed_metadata = metadata_result.payload.get("metadata")
    if not isinstance(reviewed_metadata, dict):
        raise DeploymentGuardError(
            "missing_metadata_evidence",
            "The metadata review has no recoverable metadata snapshot. Prepare it again.",
        )
    normalized_reviewed_metadata = _validated_metadata(reviewed_metadata)
    if _canonical_json(normalized_reviewed_metadata) != _canonical_json(metadata):
        raise DeploymentGuardError(
            "stale_metadata",
            "Governance metadata changed after review. Prepare and approve the current values again.",
        )
    if not _latest_explicit_approval_after(message_list, metadata_result.index):
        raise DeploymentGuardError(
            "missing_approval",
            "Explicitly ask to deploy, persist, save, or finalize the reviewed scenario.",
        )
    return _build_evidence(
        artifact=artifact,
        metadata=metadata,
        thread_id=thread_id,
        approved_by=approved_by,
        approval_source="chat",
    )


def authorize_direct_deployment(
    *,
    messages: Iterable[Any],
    metadata: Mapping[str, Any],
    thread_id: str,
    approved_by: str,
) -> DeploymentEvidence:
    """Authorize an explicit direct-deploy request using checkpoint evidence."""
    artifact = _recover_checkpoint_artifact(messages)
    validated_metadata = _validated_metadata(dict(metadata))
    return _build_evidence(
        artifact=artifact,
        metadata=validated_metadata,
        thread_id=thread_id,
        approved_by=approved_by,
        approval_source="direct_endpoint",
    )


def assert_evidence_matches(
    evidence: DeploymentEvidence,
    *,
    scenario_name: str,
    scenario_type: str,
    detection_logic: str,
    raw_sql: str,
    scenario_metadata: Mapping[str, Any],
    time_window_days: Optional[int],
) -> None:
    """Ensure writer inputs still match the authorized immutable artifact."""
    if not evidence.validation_success or not evidence.metadata_valid:
        raise DeploymentGuardError(
            "invalid_evidence", "Deployment evidence is not valid for persistence."
        )
    expected_id = compute_artifact_id(
        evidence.intent, evidence.raw_sql, evidence.metadata
    )
    supplied_id = compute_artifact_id(evidence.intent, raw_sql, scenario_metadata)
    if evidence.artifact_id != expected_id or supplied_id != expected_id:
        raise DeploymentGuardError(
            "artifact_mismatch",
            "Deployment inputs do not match the approved SQL artifact.",
        )
    expected_name = str(evidence.intent.get("scenario_name") or "AML Detection Scenario")
    expected_type = str(evidence.intent.get("scenario_type") or "CUSTOMER")
    expected_logic = str(evidence.intent.get("detection_logic") or expected_name)
    time_window = evidence.intent.get("time_window") or {}
    value = int(time_window.get("value", 0) or 0)
    unit = str(time_window.get("unit", "DAYS")).upper()
    multipliers = {"DAYS": 1, "WEEKS": 7, "MONTHS": 30, "YEARS": 365}
    expected_days = value * multipliers.get(unit, 1) if value else None
    if (
        scenario_name != expected_name
        or scenario_type != expected_type
        or detection_logic != expected_logic
        or time_window_days != expected_days
    ):
        raise DeploymentGuardError(
            "artifact_mismatch",
            "Scenario fields do not match the approved intent artifact.",
        )


def init_deployment_ledger(db_path: Optional[str] = None) -> None:
    """Create the local single-use deployment claim ledger if needed."""
    path = Path(db_path or settings.CHECKPOINT_DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    try:
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS deployment_artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    approved_by TEXT NOT NULL,
                    approval_source TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('IN_PROGRESS', 'DEPLOYED')),
                    scenario_id TEXT,
                    claimed_at TEXT NOT NULL,
                    deployed_at TEXT
                )
                """
            )
    finally:
        conn.close()


def claim_deployment(
    evidence: DeploymentEvidence, db_path: Optional[str] = None
) -> None:
    """Atomically claim an artifact before opening the Oracle write transaction."""
    path = str(db_path or settings.CHECKPOINT_DB_PATH)
    conn: Optional[sqlite3.Connection] = None
    try:
        init_deployment_ledger(path)
        conn = sqlite3.connect(path, timeout=10, isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO deployment_artifacts (
                    artifact_id, thread_id, approved_by, approval_source,
                    status, claimed_at
                ) VALUES (?, ?, ?, ?, 'IN_PROGRESS', ?)
                """,
                (
                    evidence.artifact_id,
                    evidence.thread_id,
                    evidence.approved_by,
                    evidence.approval_source,
                    evidence.approved_at,
                ),
            )
            conn.commit()
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            raise DeploymentGuardError(
                "artifact_already_claimed",
                "This exact approved artifact is already deploying or has been deployed.",
            ) from exc
    except DeploymentGuardError:
        raise
    except sqlite3.Error as exc:
        raise DeploymentGuardError(
            "deployment_ledger_unavailable",
            "Deployment safety state is unavailable. No production write was attempted.",
        ) from exc
    finally:
        if conn is not None:
            conn.close()


def release_deployment_claim(
    artifact_id: str, db_path: Optional[str] = None
) -> None:
    """Release a failed pre-commit claim so the same artifact can be retried."""
    path = str(db_path or settings.CHECKPOINT_DB_PATH)
    conn = sqlite3.connect(path, timeout=10)
    try:
        with conn:
            conn.execute(
                "DELETE FROM deployment_artifacts "
                "WHERE artifact_id = ? AND status = 'IN_PROGRESS'",
                (artifact_id,),
            )
    finally:
        conn.close()


def mark_deployment_succeeded(
    artifact_id: str, scenario_id: str, db_path: Optional[str] = None
) -> None:
    """Mark a claimed artifact deployed; failures intentionally leave it claimed."""
    path = str(db_path or settings.CHECKPOINT_DB_PATH)
    conn = sqlite3.connect(path, timeout=10)
    try:
        with conn:
            conn.execute(
                """
                UPDATE deployment_artifacts
                SET status = 'DEPLOYED', scenario_id = ?, deployed_at = ?
                WHERE artifact_id = ? AND status = 'IN_PROGRESS'
                """,
                (scenario_id, datetime.now(timezone.utc).isoformat(), artifact_id),
            )
    finally:
        conn.close()
