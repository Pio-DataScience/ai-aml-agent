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
        No exceptions are propagated for model or parsing failures; deterministic
        descriptive fallbacks are used so confirmation can still complete.
    """
    completed = deepcopy(intent)
    fillable = _fillable_paths(completed)
    correctable = _correctable_paths(completed)
    if not fillable and not correctable:
        return completed

    fallback_fields = _descriptive_fallbacks(completed)
    request = {
        "confirmed_intent": completed,
        "confirmed_card_values": _confirmation_snapshot(card_rows, submitted_values),
        "eligible_generated_paths": sorted(fillable),
        "fallback_examples": {
            path: fallback_fields[path] for path in fillable if path in fallback_fields
        },
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
        logger.exception(
            "Confirmed intent completion failed; using descriptive fallbacks."
        )

    provenance = completed.get("field_provenance")
    provenance = provenance if isinstance(provenance, dict) else {}
    fallback_paths = set(fillable)
    for path in ("scenario_name", "detection_logic"):
        if provenance.get(path) != "stated":
            fallback_paths.add(path)
    for path in fallback_paths:
        current = _get_path(completed, path)
        machine_style_name = path == "scenario_name" and _is_machine_style_name(current)
        if (_is_generic_text(current) or machine_style_name) and path in fallback_fields:
            _set_path(completed, path, fallback_fields[path])
    return completed


def _descriptive_fallbacks(intent: dict[str, Any]) -> dict[str, str]:
    """Build plain-language descriptive text directly from confirmed values.

    Args:
        intent: Intent after applying the user's confirmed card values.

    Returns:
        Safe fallback text for scenario name and detection logic.
    """
    thresholds = intent.get("thresholds") or []
    first_threshold = (
        thresholds[0] if thresholds and isinstance(thresholds[0], dict) else {}
    )
    aggregation = intent.get("aggregation")
    aggregation = aggregation if isinstance(aggregation, dict) else {}
    field = str(
        first_threshold.get("field") or aggregation.get("metric") or "activity"
    )
    metric = _friendly_value(field)
    function = str(aggregation.get("function") or "").strip().casefold()
    if "transaction_count" in field.casefold():
        measure = "count transactions"
        if first_threshold.get("value_from") is not None:
            name_metric = (
                "High Transaction Frequency"
                if str(first_threshold.get("operator") or ">=") in {">", ">="}
                else "Low Transaction Frequency"
            )
        else:
            name_metric = "Transaction Frequency"
    elif "count" in field.casefold():
        measure = (
            "count transactions"
            if "transaction" in field.casefold()
            else f"count {metric}"
        )
        name_metric = metric
    elif function == "count":
        measure = f"count {metric}"
        name_metric = f"{metric} Count"
    elif function in {"sum", "total"}:
        measure = f"calculate the total {metric}"
        name_metric = f"Total {metric}"
    else:
        measure = f"monitor {metric}"
        name_metric = metric

    transaction_types = intent.get("transaction_types") or []
    transaction_type = intent.get("transaction_type")
    scope = ", ".join(str(item) for item in transaction_types if item)
    if not scope and transaction_type:
        scope = str(transaction_type)
    scope_phrase = f"{scope} transactions" if scope else "all transaction types"

    time_window = intent.get("time_window")
    window_phrase = ""
    window_label = ""
    if isinstance(time_window, dict) and time_window.get("value") is not None:
        amount = str(time_window.get("value"))
        unit = _friendly_value(str(time_window.get("unit") or "day"))
        if amount == "1":
            unit = unit.removesuffix("s")
        rolling = time_window.get("is_rolling", True)
        window_phrase = (
            f"over a {'rolling' if rolling else 'calendar'} {amount}-{unit} period"
        )
        window_label = f" ({amount} {unit.title()})"

    grain = aggregation.get("grain")
    grain_phrase = (
        f" at the {str(grain).replace('_', ' ').casefold()} level" if grain else ""
    )
    parts = [f"Monitor {scope_phrase}{grain_phrase} and {measure}"]
    if window_phrase:
        parts[0] += f" {window_phrase}"
    trigger = _trigger_phrase(first_threshold)
    if trigger:
        parts.append(trigger)
    elif not first_threshold or first_threshold.get("value_from") is None:
        parts.append("The trigger value has not been set")

    filters = [
        item
        for item in intent.get("resolved_filters") or []
        if isinstance(item, dict) and item.get("status") == "resolved"
    ]
    for item in filters:
        values = item.get("values") or []
        if values:
            label = _friendly_value(str(item.get("dimension") or "condition"))
            parts.append(
                f"limited to {label}: {', '.join(str(value) for value in values)}"
            )
    exclusions = intent.get("exclusions") or []
    if isinstance(exclusions, str):
        exclusions = [exclusions]
    if exclusions:
        parts.append(f"excluding {', '.join(str(item) for item in exclusions)}")

    scenario_type = _friendly_value(str(intent.get("scenario_type") or "AML")).title()
    if name_metric in {
        "High Transaction Frequency",
        "Low Transaction Frequency",
        "Transaction Frequency",
    }:
        name = f"{name_metric}{window_label}"
    elif scenario_type.casefold() in name_metric.casefold():
        name = f"{name_metric.title()}{window_label}"
    else:
        name = f"{scenario_type} {name_metric.title()} Monitoring{window_label}"
    return {
        "scenario_name": name[:_FILL_LIMITS["scenario_name"]],
        "detection_logic": ". ".join(part.rstrip(".") for part in parts) + ".",
    }


def _trigger_phrase(threshold: dict[str, Any]) -> str:
    """Describe a numeric threshold when the confirmed intent contains one."""
    value = threshold.get("value_from")
    if value is None:
        return ""
    operator = str(threshold.get("operator") or ">")
    field = _friendly_value(str(threshold.get("field") or "activity"))
    if operator.casefold() == "between" and threshold.get("value_to") is not None:
        condition = f"is between {value} and {threshold['value_to']}"
    else:
        operator_words = {
            ">": "exceeds",
            ">=": "is at least",
            "<": "is below",
            "<=": "does not exceed",
            "=": "equals",
        }
        condition = f"{operator_words.get(operator, operator)} {value}"
    return f"Flag activity when {field} {condition}"


def _friendly_value(value: str) -> str:
    """Convert a catalog value or enum into readable words."""
    return " ".join(value.replace("_", " ").split()).casefold()


def _is_generic_text(value: Any) -> bool:
    """Identify empty or non-descriptive completion output."""
    if not isinstance(value, str) or not value.strip():
        return True
    lowered = value.casefold()
    return any(
        phrase in lowered
        for phrase in (
            "unspecified",
            "without specifying",
            "no business rules",
            "no specific business rules",
            "no business scenario details",
            "no scenario details",
            "no aml detection logic",
            "aml detection scenario",
            "inferred aml scenario",
            "user requested one aml scenario",
            "new scenario",
            "new aml scenario",
            "newly requested aml scenario",
            "no additional business constraints",
            "no additional business details",
            "no additional constraints provided",
            "does not specify",
            "does not include",
            "no aml behavior",
            "no behavior",
            "no transaction pattern",
            "placeholder intent",
            "awaiting a concrete scenario",
        )
    )


def _is_machine_style_name(value: Any) -> bool:
    """Reject names that expose query syntax instead of describing a scenario."""
    if not isinstance(value, str):
        return True
    lowered = value.casefold()
    return any(
        marker in lowered
        for marker in (
            ":",
            ">=",
            "<=",
            "transaction_count",
            "transaction count monitoring",
            "customer transaction count",
            "count of",
            "rolling",
            "(all)",
        )
    )


def _fillable_paths(intent: dict[str, Any]) -> set[str]:
    """Return descriptive paths that are absent or explicitly model-derived."""
    provenance = intent.get("field_provenance")
    provenance = provenance if isinstance(provenance, dict) else {}
    paths: set[str] = set()
    for path in _FILL_LIMITS:
        existing = _get_path(intent, path)
        source = provenance.get(path)
        generic = _is_generic_text(existing) or (
            path == "scenario_name" and _is_machine_style_name(existing)
        )
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
