"""Workflow operations that prepare and resolve parameter cards."""

from __future__ import annotations

import json
import uuid
from typing import Any

from services.aml_builder.web.domain.hashing import stable_hash
from services.aml_builder.web.domain.param_gaps import (
    apply_confirmed_params,
    build_param_rows,
    ready_for_handoff,
)
from services.aml_builder.web.services.config_loader import load_catalogs
from services.aml_builder.web.services.plan_renderer import render_plan
from services.aml_builder.web.services.settings import settings
from services.aml_builder.web.workflow.repo import WorkflowRepo, WorkflowConflict


def prepare_card(
    repo: WorkflowRepo,
    thread_id: str,
    payload: dict[str, Any],
    *,
    actor: str,
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
    rows = build_param_rows(intent, catalogs.params, base_intent=base_intent)
    request_id = uuid.uuid4().hex
    card = {
        "request_id": request_id,
        "rows": rows,
        "intent": intent,
        "metadata": payload.get("scenario_metadata") or _metadata_from_revision(current),
        "explanation_code_checkpoint": payload.get("explanation_code_checkpoint"),
        "base_revision_id": current.revision_id if current else None,
        "base_stage": base_stage,
        "created_by": actor,
        "created_at": None,
    }
    if ready_for_handoff(rows, catalogs.params) and catalogs.params.skip_card_when_complete:
        resolved_values: dict[str, Any] = {}
        for row in rows:
            if row.get("read_only") or row["key"] not in catalogs.params.params:
                continue
            value = row.get("value")
            choice = "provided"
            if row["status"] in {"defaulted", "inferred"}:
                value = row.get("recommended_value")
                choice = "accepted_default"
            resolved_values[row["key"]] = {
                "value": value,
                "choice": choice,
                "provenance": row.get("provenance"),
            }
        for row in rows:
            if row.get("key", "").startswith("threshold#") and not row.get("read_only"):
                resolved_values[row["key"]] = {
                    "value": row.get("value"), "choice": "provided",
                    "provenance": row.get("provenance"),
                }
        repo.set_pending_card(thread_id, card)
        commit_confirmed_card(
            repo, thread_id, request_id, resolved_values,
            actor=actor, role="system", source="system", outcome="auto_confirmed",
            resolved_rows=resolved_card_rows(rows, resolved_values),
        )
        return {}
    repo.set_pending_card(thread_id, card)
    repo.set_stage(thread_id, "PARAMS_PENDING", actor=actor, action="parameter_card_pending")
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
) -> str:
    """Apply confirmed values, render the plan, reset the current draft, and audit.

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
        Current revision ID.
    """
    workflow = repo.get_workflow(thread_id)
    card = workflow.pending_card if workflow else None
    if card is None or card.get("request_id") != request_id:
        raise WorkflowConflict("The parameter card request is stale", "CARD_STALE")
    current = repo.current(thread_id)
    if card.get("base_revision_id") != (current.revision_id if current else None):
        raise WorkflowConflict("The card was shown for a different revision", "REVISION_STALE")
    confirmed_intent = apply_confirmed_params(card.get("intent") or {}, values)
    rendered = render_plan(
        confirmed_intent, card.get("explanation_code_checkpoint")
    )
    stored_card = {
        "intent": confirmed_intent,
        "metadata": card.get("metadata") or {},
        "explanation_code_checkpoint": card.get("explanation_code_checkpoint"),
        "confirmed_request_id": request_id,
    }
    changes = {
        "card_json": stored_card,
        "params_hash": stable_hash(confirmed_intent),
        "plan_md": rendered["plan_artifact"],
        "plan_hash": stable_hash(rendered["plan_artifact"]),
        "plan_approved_at": None,
        "plan_approved_by": None,
        "shadow_json": None,
        "sql_hash": None,
        "result_approved_at": None,
        "result_approved_by": None,
        "metadata_json": None,
        "metadata_hash": None,
        "conflict_json": None,
        "signature_json": None,
        "signature_hash": None,
        "stage": "PLAN_PENDING",
    }
    if current is None:
        revision = repo.create_revision(
            thread_id, card=stored_card, stage="INTAKE", created_by=actor
        )
        current_workflow = repo.get_workflow(thread_id)
        revision = repo.update_revision(
            revision.revision_id,
            expected_version=current_workflow.version,
            **changes,
        )
    else:
        revision = repo.update_revision(
            current.revision_id,
            expected_version=workflow.version,
            **changes,
        )
    repo.resolve_pending_card(
        thread_id,
        request_id,
        outcome=outcome,
        actor=actor,
        role=role,
        source=source,
        revision_id=revision.revision_id,
        resolved_rows=resolved_rows,
    )
    return revision.revision_id


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
            snapshot["choice"] = submitted.get("choice", "user_input")
            snapshot["final_value"] = submitted.get("value")
            snapshot["provenance"] = submitted.get("provenance") or snapshot.get("provenance")
        elif submitted is not None:
            snapshot["choice"] = "user_input"
            snapshot["final_value"] = submitted
        snapshots.append(snapshot)
    return snapshots


def _merge_intent(base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """Keep the latest stored fields when a fresh extraction omits them."""
    merged = dict(base)
    for key, value in incoming.items():
        if value is None or value == [] or value == {}:
            continue
        if key in {"aggregation"} and isinstance(value, dict):
            merged[key] = {**(merged.get(key) or {}), **value}
        else:
            merged[key] = value
    return merged

