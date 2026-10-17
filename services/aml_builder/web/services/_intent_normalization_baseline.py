"""Pure characterization helper for legacy intent normalization."""

import re
from copy import deepcopy
from typing import Any, Dict


def normalize_intent_baseline(
    raw_intent: Dict[str, Any], user_prompt: str
) -> Dict[str, Any]:
    """Apply the current inline normalization rules to a copied intent.

    Args:
        raw_intent: Parsed model output before the legacy inline normalization.
        user_prompt: Original request for the legacy detection-logic fallback.

    Returns:
        A new dictionary with the legacy normalization rules applied.
    """
    intent_dict = deepcopy(raw_intent)

    # Sanitize optional dict/model fields that might be serialized as "" or
    # invalid empty types.
    for dict_field in ("baseline_window", "aggregation", "time_window"):
        if dict_field in intent_dict and not isinstance(
            intent_dict[dict_field], dict
        ):
            intent_dict[dict_field] = None

    for map_field in ("mapped_keywords",):
        if map_field in intent_dict and not isinstance(
            intent_dict[map_field], dict
        ):
            intent_dict[map_field] = None

    # Sensible fallbacks for required fields if omitted by LLM
    if not intent_dict.get("scenario_name"):
        intent_dict["scenario_name"] = "AML Detection Scenario"
    if not intent_dict.get("scenario_type"):
        intent_dict["scenario_type"] = "CUSTOMER"
    if not intent_dict.get("detection_logic"):
        intent_dict["detection_logic"] = intent_dict.get(
            "scenario_name", user_prompt
        )

    # Normalize threshold keys if the LLM used aliases (type -> field,
    # comparison -> operator, value -> value_from).
    thresholds = intent_dict.get("thresholds")
    if isinstance(thresholds, list):
        for threshold in thresholds:
            if isinstance(threshold, dict):
                if "type" in threshold and "field" not in threshold:
                    threshold["field"] = str(threshold.pop("type"))
                if "field" not in threshold or not threshold.get("field"):
                    threshold["field"] = "transaction_amount"
                if "comparison" in threshold and "operator" not in threshold:
                    threshold["operator"] = str(threshold.pop("comparison"))
                if "operator" not in threshold or not threshold.get(
                    "operator"
                ):
                    threshold["operator"] = ">"
                if "value" in threshold and "value_from" not in threshold:
                    value = threshold.pop("value")
                    try:
                        threshold["value_from"] = float(value)
                    except (ValueError, TypeError):
                        threshold["value_from"] = (
                            str(value) if value is not None else None
                        )

    # Normalize time_window keys / case
    time_window = intent_dict.get("time_window")
    if isinstance(time_window, dict):
        if "unit" in time_window and isinstance(time_window["unit"], str):
            time_window["unit"] = time_window["unit"].upper()
        if "value" not in time_window and "duration" in time_window:
            time_window["value"] = time_window.pop("duration")
        if "value" not in time_window:
            time_window["value"] = 1

    # Normalize aggregation keys / defaults
    aggregation = intent_dict.get("aggregation")
    if isinstance(aggregation, dict):
        if not aggregation.get("metric"):
            aggregation["metric"] = "transaction_amount"
        if not aggregation.get("function"):
            aggregation["function"] = "SUM"
        if not aggregation.get("grain"):
            aggregation["grain"] = "PER CUSTOMER PER DAY"

    transaction_type = intent_dict.get("transaction_type")
    transaction_types = intent_dict.get("transaction_types")
    if not transaction_type and isinstance(intent_dict.get("filters"), dict):
        transaction_type = intent_dict["filters"].get("transaction_type")
        transaction_types = intent_dict["filters"].get("transaction_types")

    # Scan semantic conditions when transaction types are absent.
    if (
        not transaction_type
        and not transaction_types
        and isinstance(intent_dict.get("semantic_conditions"), list)
    ):
        discovered_from_conditions = []
        for condition in intent_dict["semantic_conditions"]:
            if isinstance(condition, dict):
                predicate = str(condition.get("predicate", ""))
                raw_phrase = str(condition.get("raw_phrase", ""))
                match = re.search(
                    r"transaction_?type\s+is\s+([^,;.]+)",
                    predicate,
                    re.IGNORECASE,
                )
                if match:
                    value = match.group(1).strip()
                    if value and value.lower() not in [
                        item.lower() for item in discovered_from_conditions
                    ]:
                        discovered_from_conditions.append(value)
                elif (
                    condition.get("logical_type") == "BEHAVIORAL"
                    and str(condition.get("subject", "")).lower()
                    == "transaction"
                ):
                    if (
                        raw_phrase
                        and raw_phrase.lower()
                        not in [
                            item.lower() for item in discovered_from_conditions
                        ]
                        and raw_phrase.lower() not in ("or", "and")
                    ):
                        discovered_from_conditions.append(raw_phrase)
        if discovered_from_conditions:
            transaction_types = discovered_from_conditions
            transaction_type = ", ".join(discovered_from_conditions)
            intent_dict["transaction_types"] = transaction_types
            intent_dict["transaction_type"] = transaction_type

    if transaction_types and isinstance(transaction_types, list):
        intent_dict["transaction_types"] = [
            str(value).strip()
            for value in transaction_types
            if str(value).strip()
        ]
        if not transaction_type:
            intent_dict["transaction_type"] = ", ".join(
                intent_dict["transaction_types"]
            )
    elif transaction_type:
        intent_dict["transaction_type"] = transaction_type

    return intent_dict
