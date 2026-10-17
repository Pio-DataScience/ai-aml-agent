"""Pure normalization rules for AML intent payloads."""

import re
from copy import deepcopy
from typing import Any, Dict, List


def normalize_intent(
    raw_intent: Dict[str, Any], user_prompt: str
) -> Dict[str, Any]:
    """Normalize legacy AML intent fields and record every injected default.

    Args:
        raw_intent: Parsed intent from the language model.
        user_prompt: Original request used for the detection logic fallback.

    Returns:
        Copied and normalized intent with default provenance recorded.
    """
    intent = deepcopy(raw_intent)
    applied_defaults: List[str] = []
    field_provenance: Dict[str, str] = {}

    def mark_default(path: str) -> None:
        applied_defaults.append(f"{path}: {intent_path_value(intent, path)}")
        field_provenance[path] = "default"

    for dict_field in ("baseline_window", "aggregation", "time_window"):
        if dict_field in intent and not isinstance(intent[dict_field], dict):
            intent[dict_field] = None
    for map_field in ("mapped_keywords",):
        if map_field in intent and not isinstance(intent[map_field], dict):
            intent[map_field] = None

    if not intent.get("scenario_name"):
        intent["scenario_name"] = "AML Detection Scenario"
        mark_default("scenario_name")
    if not intent.get("scenario_type"):
        intent["scenario_type"] = "CUSTOMER"
        mark_default("scenario_type")
    if not intent.get("detection_logic"):
        intent["detection_logic"] = intent.get("scenario_name", user_prompt)
        mark_default("detection_logic")

    thresholds = intent.get("thresholds")
    if isinstance(thresholds, list):
        for index, threshold in enumerate(thresholds):
            if not isinstance(threshold, dict):
                continue
            if "type" in threshold and "field" not in threshold:
                threshold["field"] = str(threshold.pop("type"))
            threshold_defaulted = False
            if "field" not in threshold or not threshold.get("field"):
                threshold["field"] = "transaction_amount"
                mark_default(f"thresholds[{index}].field")
                threshold_defaulted = True
            if "comparison" in threshold and "operator" not in threshold:
                threshold["operator"] = str(threshold.pop("comparison"))
            if "operator" not in threshold or not threshold.get("operator"):
                threshold["operator"] = ">"
                mark_default(f"thresholds[{index}].operator")
                threshold_defaulted = True
            if "value" in threshold and "value_from" not in threshold:
                value = threshold.pop("value")
                try:
                    threshold["value_from"] = float(value)
                except (ValueError, TypeError):
                    threshold["value_from"] = (
                        str(value) if value is not None else None
                    )
            if threshold_defaulted:
                threshold["provenance"] = "default"

    time_window = intent.get("time_window")
    if isinstance(time_window, dict):
        if "unit" in time_window and isinstance(time_window["unit"], str):
            time_window["unit"] = time_window["unit"].upper()
        elif "unit" not in time_window:
            time_window["unit"] = "DAYS"
            time_window["provenance"] = "default"
            mark_default("time_window.unit")
        if "value" not in time_window and "duration" in time_window:
            time_window["value"] = time_window.pop("duration")
        if "value" not in time_window:
            time_window["value"] = 1
            time_window["provenance"] = "default"
            mark_default("time_window.value")

    baseline_window = intent.get("baseline_window")
    if isinstance(baseline_window, dict) and "unit" not in baseline_window:
        baseline_window["unit"] = "DAYS"
        baseline_window["provenance"] = "default"
        mark_default("baseline_window.unit")

    aggregation = intent.get("aggregation")
    if isinstance(aggregation, dict):
        for key, value in (
            ("metric", "transaction_amount"),
            ("function", "SUM"),
            ("grain", "PER CUSTOMER PER DAY"),
        ):
            if not aggregation.get(key):
                aggregation[key] = value
                aggregation["provenance"] = "default"
                mark_default(f"aggregation.{key}")

    transaction_type = intent.get("transaction_type")
    transaction_types = intent.get("transaction_types")
    if not transaction_type and isinstance(intent.get("filters"), dict):
        transaction_type = intent["filters"].get("transaction_type")
        transaction_types = intent["filters"].get("transaction_types")

    if (
        not transaction_type
        and not transaction_types
        and isinstance(intent.get("semantic_conditions"), list)
    ):
        discovered = []
        for condition in intent["semantic_conditions"]:
            if not isinstance(condition, dict):
                continue
            predicate = str(condition.get("predicate", ""))
            raw_phrase = str(condition.get("raw_phrase", ""))
            match = re.search(
                r"transaction_?type\s+is\s+([^,;.]+)", predicate, re.IGNORECASE
            )
            if match:
                value = match.group(1).strip()
                if value and value.lower() not in [
                    item.lower() for item in discovered
                ]:
                    discovered.append(value)
            elif (
                condition.get("logical_type") == "BEHAVIORAL"
                and str(condition.get("subject", "")).lower() == "transaction"
                and raw_phrase
                and raw_phrase.lower()
                not in [item.lower() for item in discovered]
                and raw_phrase.lower() not in ("or", "and")
            ):
                discovered.append(raw_phrase)
        if discovered:
            transaction_types = discovered
            transaction_type = ", ".join(discovered)
            intent["transaction_types"] = discovered
            intent["transaction_type"] = transaction_type

    if transaction_types and isinstance(transaction_types, list):
        intent["transaction_types"] = [
            str(value).strip()
            for value in transaction_types
            if str(value).strip()
        ]
        if not transaction_type:
            intent["transaction_type"] = ", ".join(intent["transaction_types"])
    elif transaction_type:
        intent["transaction_type"] = transaction_type

    intent["field_provenance"] = field_provenance
    intent["applied_defaults"] = applied_defaults
    return intent


def intent_path_value(intent: Dict[str, Any], path: str) -> Any:
    """Read a dotted intent path for human-readable applied-default entries.

    Args:
        intent: Normalized intent dictionary.
        path: Dotted field path, with optional list indexes.

    Returns:
        Value currently stored at the path, or None if it cannot be resolved.
    """
    value: Any = intent
    segments = [
        match.group(0).strip("[]")
        for match in re.finditer(r"[^.\[\]]+|\[\d+\]", path)
    ]
    if not segments:
        return None
    for segment in segments:
        if isinstance(value, dict):
            value = value.get(segment)
        elif isinstance(value, list):
            try:
                value = value[int(segment)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return value
