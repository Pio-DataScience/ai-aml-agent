"""Workflow operations that prepare and resolve parameter cards."""

from __future__ import annotations

import json
import uuid
from typing import Any

from services.aml_builder.web.domain.hashing import stable_hash
from services.aml_builder.web.domain.signature import build_signature
from services.aml_builder.web.domain.param_gaps import (
    apply_confirmed_params,
    build_param_rows,
    ready_for_handoff,
)
from services.aml_builder.web.services.config_loader import load_catalogs
from services.aml_builder.web.services.plan_renderer import render_plan
from services.aml_builder.web.services.scenario_metadata import (
    SCENARIO_METADATA_FIELDS,
    seed_scenario_metadata,
    validate_scenario_metadata,
)
from services.aml_builder.web.services.settings import settings
from services.aml_builder.web.workflow.repo import WorkflowRepo, WorkflowConflict
from services.aml_builder.web.services.conflict_check import ConflictChecker
from services.aml_builder.web.services.confirmed_intent_completion import (
    complete_confirmed_intent,
)


def prepare_card(
    repo: WorkflowRepo,
    thread_id: str,
    payload: dict[str, Any],
    *,
    actor: str,
    conflict_checker: ConflictChecker | None = None,
) -> dict[str, Any]:
    """Prepare or auto-confirm the latest merged intent from a tool result.

    Args:
        repo: Workflow repository for this chat.
        thread_id: Deterministic workflow thread ID.
        payload: Successful intent-tool payload.
        actor: Authenticated user ID.

    Returns:
        Card data for a pending card, or an empty mapping after auto-confirm.
    """
    incoming = payload.get("enriched_intent")
    if not isinstance(incoming, dict):
        return {}
    catalogs = load_catalogs(settings.CATALOG_DIR)
    workflow = repo.get_workflow(thread_id)
    if workflow is None:
        return {}
    current = repo.current(thread_id)
    previous_card = workflow.pending_card or {}
    base_stage = previous_card.get("base_stage") or workflow.stage
    base_intent = _intent_from_revision(current)
    intent = _merge_intent(base_intent, incoming) if base_intent else incoming
    rows = build_param_rows(
        intent,
        catalogs.params,
        base_intent=base_intent,
        filters_catalog=catalogs.filters,
    )
    request_id = uuid.uuid4().hex
    card = {
        "request_id": request_id,
        "rows": rows,
        "intent": intent,
        "metadata": payload.get("scenario_metadata")
        or _metadata_from_revision(current),
        "metadata_from_analysis": isinstance(payload.get("scenario_metadata"), dict),
        "explanation_code_checkpoint": payload.get("explanation_code_checkpoint"),
        "base_revision_id": current.revision_id if current else None,
        "base_stage": base_stage,
        "created_by": actor,
        "created_at": None,
    }
    if (
        ready_for_handoff(rows, catalogs.params)
        and catalogs.params.skip_card_when_complete
    ):
        resolved_values: dict[str, Any] = {}
        for row in rows:
            if row.get("read_only"):
                continue
            value = row.get("value")
            choice = "provided"
            if row["status"] in {"defaulted", "inferred"}:
                value = row.get("recommended_value")
                choice = "accepted_default"
            if row.get("kind") == "filter" and row.get("status") == "resolved":
                resolved_values[row["key"]] = {
                    "value": value,
                    "choice": "provided",
                    "provenance": row.get("provenance"),
                }
            elif row["key"] in catalogs.params.params:
                resolved_values[row["key"]] = {
                    "value": value,
                    "choice": choice,
                    "provenance": row.get("provenance"),
                }
        for row in rows:
            if row.get("key", "").startswith("threshold#") and not row.get("read_only"):
                resolved_values[row["key"]] = {
                    "value": row.get("value"),
                    "choice": "provided",
                    "provenance": row.get("provenance"),
                }
        repo.set_pending_card(thread_id, card)
        confirmation = commit_confirmed_card(
            repo,
            thread_id,
            request_id,
            resolved_values,
            actor=actor,
            role="system",
            source="system",
            outcome="auto_confirmed",
            resolved_rows=resolved_card_rows(rows, resolved_values),
            conflict_checker=conflict_checker,
        )
        payload["conflict_report"] = confirmation.get("conflict_report")
        return {}
    repo.set_pending_card(thread_id, card)
    repo.set_stage(
        thread_id, "PARAMS_PENDING", actor=actor, action="parameter_card_pending"
    )
    return repo.get_workflow(thread_id).pending_card or card


def commit_confirmed_card(
    repo: WorkflowRepo,
    thread_id: str,
    request_id: str,
    values: dict[str, Any],
    *,
    actor: str,
    role: str,
    source: str,
    outcome: str = "confirmed",
    resolved_rows: list[dict[str, Any]] | None = None,
    confirmation_hash: str | None = None,
    conflict_checker: ConflictChecker | None = None,
    message_position: int | None = None,
) -> dict[str, Any]:
    """Apply a confirmed card atomically and deterministically render its plan.

    Args:
        repo: Workflow repository for this chat.
        thread_id: Deterministic workflow thread ID.
        request_id: Current pending-card request ID.
        values: Validated, explicit values and choices.
        actor: Authenticated user ID.
        role: Resolved policy role.
        source: `ui`, `api`, or `system`.
        outcome: Final card outcome.

    Returns:
        Confirmation result including revision ID and metadata carry-over report.
    """
    confirmation_hash = confirmation_hash or stable_hash(values)
    prior = repo.get_card_confirmation(request_id)
    if prior is not None:
        if prior.get("confirmation_hash") != confirmation_hash:
            raise WorkflowConflict(
                "This card was already confirmed with different values", "CARD_STALE"
            )
        prior_revision = repo.get_revision(prior.get("revision_id"))
        if prior_revision is None:
            raise WorkflowConflict(
                "The confirmed revision is no longer available", "CARD_STALE"
            )
        carry_over = prior.get("carry_over") or {"kept": [], "dropped": []}
        return {
            "revision_id": prior_revision.revision_id,
            "revision": prior_revision,
            "carry_over": carry_over,
            "already_confirmed": True,
            "history_count": repo.history_count(thread_id),
        }
    workflow = repo.get_workflow(thread_id)
    card = workflow.pending_card if workflow else None
    if card is None or card.get("request_id") != request_id:
        raise WorkflowConflict("The parameter card request is stale", "CARD_STALE")
    current = repo.current(thread_id)
    if card.get("base_revision_id") != (current.revision_id if current else None):
        raise WorkflowConflict(
            "The card was shown for a revision that is no longer current", "CARD_STALE"
        )
    confirmed_intent = apply_confirmed_params(card.get("intent") or {}, values)
    confirmed_intent = complete_confirmed_intent(
        confirmed_intent,
        card.get("rows", []),
        values,
    )
    signature, signature_hash = build_signature(confirmed_intent)
    conflict = (
        conflict_checker.check_intent(
            confirmed_intent,
            thread_id=thread_id,
            scenario_key=workflow.scenario_key if workflow else None,
            exclude_scenario_ids={workflow.parent_scenario_id}
            if workflow and workflow.parent_scenario_id else None,
        )
        if conflict_checker is not None
        else {
            "revision_id": None,
            "provisional": False,
            "status": "clear",
            "conflict_hash": stable_hash([]),
            "blocking": False,
            "needs_ack": False,
            "matches": [],
        }
    )
    if conflict.get("blocking"):
        raise WorkflowConflict(
            "An identical active scenario blocks confirmation.",
            "CONFLICT_BLOCKED",
            details=conflict,
        )
    requires_ack = bool(conflict.get("needs_ack"))
    rendered = None if requires_ack else render_plan(
        confirmed_intent, card.get("explanation_code_checkpoint")
    )
    resulting_stage = "PARAMS_CONFIRMED" if requires_ack else "PLAN_PENDING"
    metadata, edited_keys, carry_over = _carry_forward_metadata(
        current, card, confirmed_intent
    )
    stored_card = {
        "intent": confirmed_intent,
        "metadata": metadata,
        "metadata_user_edited": edited_keys,
        "explanation_code_checkpoint": card.get("explanation_code_checkpoint"),
        "confirmed_request_id": request_id,
    }
    revision, already_confirmed = repo.confirm_card_revision(
        thread_id,
        request_id,
        base_revision_id=card.get("base_revision_id"),
        stored_card=stored_card,
        plan_md=rendered["plan_artifact"] if rendered else None,
        plan_hash=stable_hash(rendered["plan_artifact"]) if rendered else None,
        params_hash=stable_hash(confirmed_intent),
        metadata=metadata,
        metadata_hash=stable_hash(metadata),
        carry_over=carry_over,
        actor=actor,
        role=role,
        source=source,
        outcome=outcome,
        confirmation_hash=confirmation_hash,
        resolved_rows=resolved_rows or resolved_card_rows(card.get("rows", []), values),
        signature=signature,
        signature_hash=signature_hash,
        conflict=conflict,
        resulting_stage=resulting_stage,
        message_position=message_position,
    )
    return {
        "revision_id": revision.revision_id,
        "revision": revision,
        "carry_over": carry_over,
        "already_confirmed": already_confirmed,
        "history_count": repo.history_count(thread_id),
        "conflict_report": conflict,
    }


def _carry_forward_metadata(
    current: Any, card: dict[str, Any], intent: dict[str, Any]
) -> tuple[dict[str, Any], list[str], dict[str, list[str]]]:
    """Re-seed metadata and carry forward only edited values that still validate."""
    previous_metadata: dict[str, Any] = {}
    previous_intent: dict[str, Any] = {}
    previous_user_edited: list[str] | None = None
    if current is not None:
        if current.metadata_json:
            try:
                previous_metadata = json.loads(current.metadata_json)
            except (json.JSONDecodeError, TypeError):
                previous_metadata = {}
            if not isinstance(previous_metadata, dict):
                previous_metadata = {}
        if current.card_json:
            try:
                previous_card = json.loads(current.card_json)
            except (json.JSONDecodeError, TypeError):
                previous_card = {}
            if not previous_metadata and isinstance(previous_card, dict):
                stored_metadata = previous_card.get("metadata")
                previous_metadata = (
                    stored_metadata if isinstance(stored_metadata, dict) else {}
                )
            if isinstance(previous_card, dict):
                stored_intent = previous_card.get("intent")
                previous_intent = (
                    stored_intent if isinstance(stored_intent, dict) else {}
                )
                edited = previous_card.get("metadata_user_edited")
                if isinstance(edited, list):
                    previous_user_edited = [str(key) for key in edited]
    else:
        card_metadata = card.get("metadata")
        previous_metadata = card_metadata if isinstance(card_metadata, dict) else {}
        card_intent = card.get("intent")
        previous_intent = card_intent if isinstance(card_intent, dict) else {}

    previous_seeded = seed_scenario_metadata(previous_intent, {})
    inferred_edits = [
        key
        for key, value in previous_metadata.items()
        if key in previous_seeded and value != previous_seeded[key]
    ]
    previous_user_edited = sorted(set(previous_user_edited or []) | set(inferred_edits))
    valid_fields = {spec.name for spec in SCENARIO_METADATA_FIELDS}
    keys = [
        key
        for key in previous_user_edited
        if key in previous_metadata and key in valid_fields
    ]
    seeded = seed_scenario_metadata(intent, {})
    if card.get("metadata_from_analysis") and isinstance(card.get("metadata"), dict):
        generated_description_fields = {"SCENARIO_DES_ENG", "SCENARIO_DES_NAT_LAN"}
        seeded.update(
            {
                key: value
                for key, value in card["metadata"].items()
                if value is not None and key not in generated_description_fields
            }
        )
    carried = {key: previous_metadata[key] for key in keys}
    candidate = {**seeded, **carried}
    kept = list(keys)
    dropped: list[str] = []
    while kept:
        _, problems = validate_scenario_metadata(candidate)
        invalid = [key for key in kept if any(key in problem for problem in problems)]
        if not invalid:
            break
        for key in invalid:
            if key in seeded:
                candidate[key] = seeded[key]
            else:
                candidate.pop(key, None)
            carried.pop(key, None)
            kept.remove(key)
            dropped.append(key)
    return candidate, sorted(kept), {"kept": sorted(kept), "dropped": sorted(dropped)}


def _intent_from_revision(revision: Any) -> dict[str, Any] | None:
    if revision is None or not revision.card_json:
        return None
    try:
        decoded = json.loads(revision.card_json)
    except (json.JSONDecodeError, TypeError):
        return None
    intent = decoded.get("intent") if isinstance(decoded, dict) else None
    return intent if isinstance(intent, dict) else None


def _metadata_from_revision(revision: Any) -> dict[str, Any] | None:
    if revision is None or not revision.card_json:
        return None
    try:
        decoded = json.loads(revision.card_json)
    except (json.JSONDecodeError, TypeError):
        return None
    metadata = decoded.get("metadata") if isinstance(decoded, dict) else None
    return metadata if isinstance(metadata, dict) else None


def resolved_card_rows(
    rows: list[dict[str, Any]], values: dict[str, Any]
) -> list[dict[str, Any]]:
    """Snapshot recommendations, choices, final values, and provenance for audit."""
    snapshots: list[dict[str, Any]] = []
    for row in rows:
        snapshot = dict(row)
        submitted = values.get(str(row.get("key")))
        if isinstance(submitted, dict) and "value" in submitted:
            snapshot["original_value"] = row.get("value")
            snapshot["value"] = submitted.get("value")
            snapshot["choice"] = submitted.get("choice", "user_input")
            snapshot["final_value"] = submitted.get("value")
            snapshot["provenance"] = submitted.get("provenance") or snapshot.get(
                "provenance"
            )
        elif submitted is not None:
            snapshot["original_value"] = row.get("value")
            snapshot["value"] = submitted
            snapshot["choice"] = "user_input"
            snapshot["final_value"] = submitted
        snapshots.append(snapshot)
    return snapshots


def create_revision_card(repo: WorkflowRepo, thread_id: str, *, actor: str) -> dict[str, Any]:
    """Create an editable parameter card from the exact current revision intent.

    Args:
        repo: Workflow repository for the chat.
        thread_id: Deterministic workflow thread ID.
        actor: Authenticated user who requested the change.

    Returns:
        New pending card populated from the current confirmed values.

    Raises:
        WorkflowConflict: The chat has no current intent or is already locked.
    """
    workflow = repo.get_workflow(thread_id)
    current = repo.current(thread_id)
    if workflow is None or current is None or workflow.locked:
        raise WorkflowConflict("A current editable scenario is required.", "STAGE_NOT_ALLOWED")
    intent = _intent_from_revision(current)
    if intent is None:
        raise WorkflowConflict("The current revision intent is unavailable.", "PARAMS_UNCONFIRMED")
    catalogs = load_catalogs(settings.CATALOG_DIR)
    rows = build_param_rows(intent, catalogs.params, base_intent=intent, filters_catalog=catalogs.filters)
    stored = {}
    try:
        stored = json.loads(current.card_json or "{}")
    except (json.JSONDecodeError, TypeError):
        stored = {}
    card = {
        "request_id": uuid.uuid4().hex,
        "rows": rows,
        "intent": intent,
        "metadata": _metadata_from_revision(current) or {},
        "metadata_from_analysis": False,
        "explanation_code_checkpoint": stored.get("explanation_code_checkpoint"),
        "base_revision_id": current.revision_id,
        "base_stage": workflow.stage,
        "created_by": actor,
        "created_at": None,
    }
    repo.set_pending_card(thread_id, card)
    repo.set_stage(thread_id, "PARAMS_PENDING", actor=actor, action="conflict_change_params")
    return repo.get_workflow(thread_id).pending_card or card


def _merge_intent(base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """Keep the latest stored fields when a fresh extraction omits them."""
    merged = dict(base)
    for key, value in incoming.items():
        if key in {"filters", "resolved_filters"}:
            merged[key] = value
            continue
        if value is None or value == [] or value == {}:
            continue
        if key in {"aggregation"} and isinstance(value, dict):
            merged[key] = {**(merged.get(key) or {}), **value}
        else:
            merged[key] = value
    return merged
