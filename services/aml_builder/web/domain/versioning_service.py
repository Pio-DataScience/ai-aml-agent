"""Deterministic version change-management rules over repository ports."""

from __future__ import annotations

import json
import uuid
from typing import Any, Callable

from services.aml_builder.web.domain.param_gaps import build_param_rows
from services.aml_builder.web.services.config_loader import load_catalogs
from services.aml_builder.web.services.settings import settings


class VersioningError(RuntimeError):
    """A typed refusal from a scenario versioning action."""

    def __init__(self, code: str, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def _json_object(value: Any, field: str) -> dict[str, Any]:
    """Decode an Oracle JSON column into a mapping."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise VersioningError("VERSION_SOURCE_INCOMPLETE", f"Stored {field} is not valid JSON.", details=[field]) from exc
    return value if isinstance(value, dict) else {}


def intent_from_registry_row(row: dict[str, Any]) -> dict[str, Any]:
    """Rebuild a complete intent from the deployed registry snapshot.

    Args:
        row: Registry version row with stored JSON columns.

    Returns:
        The intent with resolved filters restored from the separate filter column
        when that column is populated.

    Raises:
        VersioningError: If a field required by card rendering or SQL generation
            is missing from the stored registry snapshot.
    """
    params = _json_object(row.get("params_json"), "PARAMS_JSON")
    intent = params.get("intent") if isinstance(params.get("intent"), dict) else params
    intent = dict(intent) if isinstance(intent, dict) else {}
    filters = row.get("filters_json")
    if filters:
        if isinstance(filters, str):
            try:
                filters = json.loads(filters)
            except json.JSONDecodeError as exc:
                raise VersioningError("VERSION_SOURCE_INCOMPLETE", "Stored FILTERS_JSON is not valid JSON.", details=["FILTERS_JSON"]) from exc
        if isinstance(filters, list):
            intent["resolved_filters"] = filters
        elif isinstance(filters, dict):
            intent["resolved_filters"] = filters.get("resolved_filters", filters.get("filters", []))
    required = [key for key in ("scenario_name", "scenario_type", "detection_logic", "time_window", "thresholds", "filters") if intent.get(key) is None]
    if "resolved_filters" not in intent and not row.get("filters_json"):
        required.append("resolved_filters")
    signature = _json_object(row.get("signature_json"), "SIGNATURE_JSON")
    if not signature:
        required.append("SIGNATURE_JSON")
    metadata = _json_object(row.get("metadata_json"), "METADATA_JSON")
    if not metadata:
        required.append("METADATA_JSON")
    if required:
        raise VersioningError(
            "VERSION_SOURCE_INCOMPLETE",
            "The stored version is missing fields required to create a new version: " + ", ".join(required),
            details=required,
        )
    intent.setdefault("filters", [])
    intent.setdefault("resolved_filters", [])
    intent.setdefault("thresholds", [])
    return intent


def compare_version_payloads(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """Return a deterministic path-based diff for stored version JSON.

    Args:
        left: Earlier version JSON mapping.
        right: Later version JSON mapping.

    Returns:
        A stable list of changed JSON paths and their values.
    """
    changes: list[dict[str, Any]] = []

    def visit(path: str, old: Any, new: Any) -> None:
        if isinstance(old, dict) and isinstance(new, dict):
            for key in sorted(set(old) | set(new)):
                visit(f"{path}.{key}" if path else key, old.get(key), new.get(key))
        elif old != new:
            changes.append({"path": path, "old": old, "new": new})

    visit("", left, right)
    return {"changes": changes}


class VersioningService:
    """Coordinate permission checks and scenario lifecycle operations."""

    def __init__(
        self,
        *,
        registry_repo: Any,
        workflow_repo: Any,
        authorizer: Callable[[str, str], bool],
        conflict_checker: Callable[[dict[str, Any], str], dict[str, Any]],
    ) -> None:
        """Configure registry/workflow ports and policy callbacks."""
        self.registry_repo = registry_repo
        self.workflow_repo = workflow_repo
        self.authorizer = authorizer
        self.conflict_checker = conflict_checker

    def create_new_version(
        self,
        scenario_id: str,
        *,
        project_id: str,
        user_id: str,
        actor_id: str,
        actor_role: str,
    ) -> dict[str, Any]:
        """Create or return the caller's open child chat from the latest version."""
        self._require(actor_id, "scenario.revise")
        parent = self.registry_repo.get_version(scenario_id)
        if parent is None:
            raise VersioningError("VERSION_NOT_FOUND", "Scenario version was not found.")
        key = str(parent["scenario_key"])
        existing = self.workflow_repo.find_open_child_chat(scenario_id, int(parent["version_no"]), user_id)
        if existing:
            return {
                "new_chat_id": existing["chat_id"],
                "parent_scenario_id": scenario_id,
                "parent_version": int(parent["version_no"]),
                "scenario_key": key,
                "reserved_version": int(parent["version_no"]) + 1,
            }
        latest = self.registry_repo.latest_version(key)
        if latest is None or latest.get("scenario_id") != scenario_id or str(latest.get("status", "")).upper() not in {"ACTIVE", "RETIRED"}:
            raise VersioningError("REVISE_STALE", "This is no longer the latest version. Start again from the latest version.")
        intent = intent_from_registry_row(parent)
        metadata = _json_object(parent.get("metadata_json"), "METADATA_JSON")
        catalogs = load_catalogs(settings.CATALOG_DIR)
        rows = build_param_rows(intent, catalogs.params, base_intent=intent, filters_catalog=catalogs.filters)
        chat_id = uuid.uuid4().hex
        title = f"New version of {parent.get('scenario_name') or intent['scenario_name']} (v{int(parent['version_no']) + 1})"
        card = {
            "request_id": uuid.uuid4().hex,
            "rows": rows,
            "intent": intent,
            "metadata": metadata,
            "metadata_from_analysis": False,
            "unchanged_from_current_version": True,
            "base_revision_id": None,
            "base_stage": "PARAMS_PENDING",
            "created_by": actor_id,
            "created_at": None,
        }
        created = self.workflow_repo.create_version_chat(
            project_id=project_id,
            chat_id=chat_id,
            user_id=user_id,
            scenario_key=key,
            scenario_id=f"{key}_V{int(parent['version_no']) + 1}",
            parent_scenario_id=scenario_id,
            parent_version=int(parent["version_no"]),
            stage="PARAMS_PENDING",
            pending_card=card,
            title=title,
            actor=actor_id,
        )
        return {
            "new_chat_id": created.get("chat_id", chat_id),
            "parent_scenario_id": scenario_id,
            "parent_version": int(parent["version_no"]),
            "scenario_key": key,
            "reserved_version": int(parent["version_no"]) + 1,
        }

    def rollback(
        self,
        scenario_id: str,
        *,
        expected_active_version: int,
        reason: str,
        actor_id: str,
        actor_role: str,
        acknowledge_warnings: bool = False,
    ) -> dict[str, Any]:
        """Reactivate one superseded version and supersede the current active row."""
        self._require(actor_id, "scenario.rollback")
        _validate_reason(reason)
        reason = reason.strip()
        target = self.registry_repo.get_version(scenario_id)
        if target is None:
            raise VersioningError("VERSION_NOT_FOUND", "Scenario version was not found.")
        key = str(target["scenario_key"])
        active = self.registry_repo.latest_active_version(key)
        if str(target.get("status", "")).upper() != "SUPERSEDED" or active is None:
            raise VersioningError("VERSION_STALE", "Rollback requires a superseded version and a current active version.")
        if int(active["version_no"]) != int(expected_active_version):
            raise VersioningError("VERSION_STALE", "The active version changed. Refresh version history and try again.")
        report = self.conflict_checker(_json_object(target.get("signature_json"), "SIGNATURE_JSON"), scenario_id)
        if report.get("status") in {"unavailable", "not_run"}:
            raise VersioningError("CONFLICT_CHECK_UNAVAILABLE", "The active scenario registry could not be checked.")
        if report.get("blocking") or report.get("status") == "blocked":
            raise VersioningError("CONFLICT_BLOCKED", "Rollback is blocked by an identical active scenario.", details=report.get("matches", []))
        if (report.get("needs_ack") or report.get("status") == "needs_ack") and not acknowledge_warnings:
            raise VersioningError("CONFLICT_ACK_REQUIRED", "Review the similar active scenarios and confirm rollback.", details=report.get("matches", []))
        changes = [(active["scenario_id"], "ACTIVE", "SUPERSEDED"), (scenario_id, "SUPERSEDED", "ACTIVE")]
        self.registry_repo.set_statuses(
            changes, actor=actor_id, actor_role=actor_role, reason=reason,
            audit_action="ROLLBACK",
            audit_detail={"reactivated_scenario_id": scenario_id, "superseded_scenario_id": active["scenario_id"], "conflicts": report.get("matches", [])},
        )
        return {"scenario_id": scenario_id, "scenario_key": key, "version_no": target["version_no"], "status": "ACTIVE"}

    def retire(self, scenario_id: str, *, reason: str, actor_id: str, actor_role: str) -> None:
        """Soft-retire one active version and append its audit row atomically."""
        self._require(actor_id, "scenario.delete")
        _validate_reason(reason)
        reason = reason.strip()
        row = self.registry_repo.get_version(scenario_id)
        if row is None:
            raise VersioningError("VERSION_NOT_FOUND", "Scenario version was not found.")
        if str(row.get("status", "")).upper() != "ACTIVE":
            raise VersioningError("VERSION_STALE", "Only an active version can be retired.")
        self.registry_repo.set_statuses(
            [(scenario_id, "ACTIVE", "RETIRED")], actor=actor_id,
            actor_role=actor_role, reason=reason, audit_action="RETIRE",
        )

    def _require(self, actor_id: str, action: str) -> None:
        if not self.authorizer(actor_id, action):
            raise VersioningError("PERMISSION_DENIED", "Permission denied.")


def _validate_reason(reason: str) -> None:
    """Require a reason between five and five hundred characters."""
    if not isinstance(reason, str) or not 5 <= len(reason.strip()) <= 500:
        raise VersioningError("REASON_REQUIRED", "Enter a reason between 5 and 500 characters.")
