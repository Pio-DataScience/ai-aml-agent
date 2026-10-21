"""Constrained LLM completion for validated AML scenario parameters."""

import json
import logging
from copy import deepcopy
from difflib import SequenceMatcher
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from services.aml_builder.web.services.llm_client import build_llm, safe_parse_json
from services.aml_builder.web.services.prompts.loader import load_prompt

logger = logging.getLogger(__name__)

_FILL_LIMITS = {
    "scenario_name": 120,
    "detection_logic": 1200,
    "baseline_window.description": 400,
}
_MODEL_GENERATED_PROVENANCE = {"inferred", "default", "assumed_default"}


def complete_confirmed_intent(
    intent: dict[str, Any],
    card_rows: list[dict[str, Any]],
    submitted_values: dict[str, Any],
) -> dict[str, Any]:
    """Proofread free text and fill eligible descriptive fields after confirmation.

    Args:
        intent: Intent after applying validated values from the confirmed card.
        card_rows: Rows shown to the user for this confirmation request.
        submitted_values: Submitted choices and values, including provenance.

    Returns:
        A copied intent with only allowed descriptive paths updated.

    Raises:
        No exceptions are propagated for model or parsing failures; the original
        confirmed intent is returned so confirmation can still complete.
    """
    completed = deepcopy(intent)
    fillable = _fillable_paths(completed)
    correctable = _correctable_paths(completed)
    if not fillable and not correctable:
        return completed

    request = {
        "confirmed_intent": completed,
        "confirmed_card_values": _confirmation_snapshot(card_rows, submitted_values),
        "eligible_generated_paths": fillable,
        "correctable_user_text_paths": {
            path: _get_path(completed, path) for path in correctable
        },
    }
    try:
        response = build_llm(fast=True).invoke(
            [
                SystemMessage(content=load_prompt("confirmed_intent_completion.md")),
                HumanMessage(content=json.dumps(request, ensure_ascii=False)),
            ]
        )
        content = str(response.content or "").strip()
        result = safe_parse_json(content)
        generated = result.get("generated_fields")
        corrections = result.get("corrected_text")
        _apply_generated(completed, generated, fillable)
        _apply_corrections(completed, corrections, correctable)
    except Exception:
        logger.exception("Confirmed intent completion failed; preserving confirmed values.")
    return completed


def _fillable_paths(intent: dict[str, Any]) -> set[str]:
    """Return descriptive paths that are absent or explicitly model-derived."""
    provenance = intent.get("field_provenance")
    provenance = provenance if isinstance(provenance, dict) else {}
    paths: set[str] = set()
    for path in _FILL_LIMITS:
        existing = _get_path(intent, path)
        source = provenance.get(path)
        generic = path == "scenario_name" and existing == "AML Detection Scenario"
        if not isinstance(existing, str) or not existing.strip() or generic:
            paths.add(path)
        elif source in _MODEL_GENERATED_PROVENANCE:
            paths.add(path)
    if not isinstance(intent.get("baseline_window"), dict):
        paths.discard("baseline_window.description")
    return paths


def _correctable_paths(
    intent: dict[str, Any],
) -> set[str]:
    """Return descriptive text paths eligible for minimal proofreading."""
    paths = (
        {"detection_logic"}
        if isinstance(intent.get("detection_logic"), str)
        else set()
    )
    if isinstance(intent.get("scenario_name"), str):
        paths.add("scenario_name")
    # Parameter-card text can represent business rules (for example filter
    # values or exclusions), so only proofread descriptive intent fields.
    return {path for path in paths if isinstance(_get_path(intent, path), str)}


def _confirmation_snapshot(
    card_rows: list[dict[str, Any]], submitted_values: dict[str, Any]
) -> list[dict[str, Any]]:
    """Build a compact, provenance-aware view of the user's card choices."""
    snapshot: list[dict[str, Any]] = []
    for row in card_rows:
        key = str(row.get("key") or "")
        submitted = submitted_values.get(key)
        snapshot.append(
            {
                "key": key,
                "label": row.get("label"),
                "original_value": row.get("value"),
                "confirmed_value": submitted.get("value")
                if isinstance(submitted, dict)
                else submitted,
                "choice": submitted.get("choice")
                if isinstance(submitted, dict)
                else None,
                "provenance": submitted.get("provenance")
                if isinstance(submitted, dict)
                else row.get("provenance"),
            }
        )
    return snapshot


def _apply_generated(
    intent: dict[str, Any], generated: Any, allowed: set[str]
) -> None:
    """Apply validated generated strings to only explicitly eligible paths."""
    if not isinstance(generated, dict):
        return
    for path in allowed:
        value = generated.get(path)
        if isinstance(value, str) and value.strip():
            limit = _FILL_LIMITS[path]
            _set_path(intent, path, value.strip()[:limit])


def _apply_corrections(
    intent: dict[str, Any], corrections: Any, allowed: set[str]
) -> None:
    """Accept only close, minimal proofreading edits on approved text paths."""
    if not isinstance(corrections, dict):
        return
    for path in allowed:
        original = _get_path(intent, path)
        candidate = corrections.get(path)
        if (
            isinstance(original, str)
            and isinstance(candidate, str)
            and _is_trivial_correction(original, candidate)
        ):
            _set_path(intent, path, candidate.strip())


def _is_trivial_correction(original: str, candidate: str) -> bool:
    """Reject rewrites that materially change a text value."""
    original_clean = " ".join(original.split())
    candidate_clean = " ".join(candidate.split())
    if not candidate_clean or original_clean == candidate_clean:
        return False
    return (
        SequenceMatcher(
            None, original_clean.casefold(), candidate_clean.casefold()
        ).ratio()
        >= 0.86
    )


def _get_path(value: dict[str, Any], path: str) -> Any:
    """Read a dotted path through dicts and numeric list indexes."""
    current: Any = value
    for segment in path.split("."):
        if isinstance(current, dict):
            current = current.get(segment)
        elif isinstance(current, list) and segment.isdigit():
            index = int(segment)
            current = current[index] if index < len(current) else None
        else:
            return None
    return current


def _set_path(value: dict[str, Any], path: str, replacement: str) -> None:
    """Set a dotted path only when its parent structure already exists."""
    segments = path.split(".")
    current: Any = value
    for segment in segments[:-1]:
        if isinstance(current, dict):
            current = current.get(segment)
        elif isinstance(current, list) and segment.isdigit():
            index = int(segment)
            current = current[index] if index < len(current) else None
        else:
            return
        if current is None:
            return
    leaf = segments[-1]
    if isinstance(current, dict):
        current[leaf] = replacement
    elif isinstance(current, list) and leaf.isdigit():
        index = int(leaf)
        if index < len(current):
            current[index] = replacement
