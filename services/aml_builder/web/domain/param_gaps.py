"""Pure parameter-card gap detection and confirmed intent application."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


_EDIT_HINT = "Change via chat."
_READ_ONLY_ROWS = ("baseline_window", "semantic_conditions")


def condition_matches(context: dict[str, Any], condition: Any) -> bool:
    """Evaluate a catalog `required_if` condition without executing code.

    Args:
        context: Flat parameter values available to the condition.
        condition: A mapping with `param` and `equals` keys.

    Returns:
        Whether the named parameter exists and equals the requested value.
    """
    if not isinstance(condition, dict) or set(condition) != {"param", "equals"}:
        return False
    param = condition.get("param")
    return (
        isinstance(param, str)
        and param in context
        and context[param] == condition.get("equals")
    )


def build_param_rows(
    intent: dict[str, Any],
    catalog: Any,
    *,
    base_intent: dict[str, Any] | None = None,
    filters_catalog: Any = None,
) -> list[dict[str, Any]]:
    """Create display rows from normalized intent and catalog rules.

    Args:
        intent: Normalized AMLIntent mapping from extraction.
        catalog: Validated ParamsCatalog or its mapping representation.
        base_intent: Prior merged intent used to flag changed rows.

    Returns:
        Ordered rows for editable parameters and read-only understood structures.
    """
    catalog_data = _catalog_dict(catalog)
    params = catalog_data.get("params", {})
    source_values = _intent_values(intent)
    base_values = _intent_values(base_intent or {})
    clarifications = intent.get("clarifications") or []
    rows: list[dict[str, Any]] = []

    for key, spec in params.items():
        if key in {"threshold", "comparator"}:
            if key == "comparator":
                continue
            thresholds = intent.get("thresholds") or []
            if not thresholds:
                row = _row("threshold#1", spec, None, "missing", "stated", base_values)
                _apply_ambiguity(row, clarifications, "threshold", 1)
                rows.append(row)
            else:
                for index, threshold in enumerate(thresholds, start=1):
                    value = _threshold_value(threshold)
                    status, provenance = _status_for_value(
                        value.get("value"), threshold.get("provenance", "stated"), spec
                    )
                    row = _row(
                        f"threshold#{index}",
                        spec,
                        value,
                        status,
                        provenance,
                        base_values,
                    )
                    _apply_ambiguity(row, clarifications, "threshold", index)
                    rows.append(row)
            continue

        value = source_values.get(key)
        provenance = _provenance_for(intent, key)
        status, provenance = _status_for_value(value, provenance, spec)
        row = _row(key, spec, value, status, provenance, base_values)
        _apply_ambiguity(row, clarifications, key)
        if key == "window" and _is_all_time(value):
            row["status"] = "missing"
            row["validation_errors"] = ["All-time windows are not allowed."]
        elif key == "window" and value is not None:
            errors = _validate_duration(value, spec)
            if errors:
                row["validation_errors"] = errors
        elif key == "threshold" and value is not None:
            row["validation_errors"] = _validate_number(value, spec)
        required_if = spec.get("required_if")
        if required_if:
            context = dict(source_values)
            context["relative"] = bool(intent.get("baseline_window")) or any(
                isinstance(threshold, dict) and threshold.get("relative") is True
                for threshold in (intent.get("thresholds") or [])
            )
            row["required"] = bool(spec.get("required", False)) or condition_matches(
                context, required_if
            )
        rows.append(row)

    for key in _READ_ONLY_ROWS:
        value = intent.get(key)
        if value:
            rows.append(
                {
                    "key": key,
                    "label": (
                        "Baseline window"
                        if key == "baseline_window"
                        else "Semantic conditions"
                    ),
                    "status": "provided",
                    "value": value,
                    "recommended_value": value,
                    "input_type": "readonly",
                    "prompt": "Understood from your request",
                    "provenance": _provenance_for(intent, key),
                    "required": False,
                    "read_only": True,
                    "hint": _EDIT_HINT,
                    "validation_errors": [],
                }
            )
    for index, item in enumerate(intent.get("resolved_filters") or [], start=1):
        if not isinstance(item, dict):
            continue
        dimension = str(item.get("dimension") or "unknown")
        selected_values = list(item.get("values") or [])
        options = list(item.get("options") or [])
        option_values = {
            str(option.get("value")) if isinstance(option, dict) else str(option)
            for option in options
        }
        options.extend(
            {"value": value, "label": str(value), "confidence": item.get("confidence")}
            for value in selected_values
            if str(value) not in option_values
        )
        rows.append(
            {
                "kind": "filter",
                "key": f"filter#{index}",
                "dimension": dimension,
                "raw_mention": item.get("raw_mention", ""),
                "polarity": item.get("polarity", "positive"),
                "level": item.get("level", "unknown"),
                "label": _filter_label(dimension, filters_catalog),
                "status": item.get("status", "unresolved"),
                "value": selected_values,
                "selected_values": selected_values,
                "recommended_value": selected_values,
                "options": options,
                "confidence": item.get("confidence"),
                "input_type": "filter",
                "required": item.get("status") != "resolved",
                "read_only": False,
                "provenance": item.get("provenance", "catalog_resolver"),
                "hint": "Choose values, remove this filter, or acknowledge proceeding without it.",
                "validation_errors": [],
            }
        )
    return rows


def ready_for_handoff(rows: list[dict[str, Any]], catalog: Any) -> bool:
    """Return whether all required editable rows are resolved and valid."""
    catalog_data = _catalog_dict(catalog)
    block_optional = bool(
        catalog_data.get("ambiguity", {}).get("block_on_optional", False)
    )
    for row in rows:
        if row.get("read_only"):
            continue
        if row.get("kind") == "filter" and row.get("status") != "resolved":
            return False
        if row.get("validation_errors"):
            return False
        if row.get("status") in {"missing", "ambiguous"} and (
            row.get("required", False)
            or (row.get("status") == "ambiguous" and block_optional)
        ):
            return False
    return True


def validate_confirmed_values(
    rows: list[dict[str, Any]], values: dict[str, Any], catalog: Any
) -> tuple[list[str], list[str], list[dict[str, str]]]:
    """Validate action values and return missing, unresolved, and invalid details."""
    catalog_data = _catalog_dict(catalog)
    specs = catalog_data.get("params", {})
    missing: list[str] = []
    unresolved: list[str] = []
    invalid: list[dict[str, str]] = []
    for row in rows:
        if row.get("read_only"):
            continue
        key = str(row["key"])
        if row.get("kind") == "filter":
            submitted = values.get(key)
            if submitted is None:
                if row.get("status") != "resolved":
                    unresolved.append(key)
                continue
            value = submitted.get("value") if isinstance(submitted, dict) else submitted
            choice = (
                submitted.get("choice") if isinstance(submitted, dict) else "user_input"
            )
            if choice == "removed" and value is None:
                continue
            if (
                choice == "acknowledged"
                and value == "proceed_without_filter"
                and row.get("status") == "disabled"
            ):
                continue
            allowed = {
                str(option.get("value")) if isinstance(option, dict) else str(option)
                for option in row.get("options", [])
            }
            selected = value if isinstance(value, list) else [value]
            original = {str(item) for item in row.get("selected_values", [])}
            unchanged_resolved = (
                row.get("status") == "resolved"
                and {str(item) for item in selected} == original
            )
            if not selected or (
                not unchanged_resolved
                and any(str(item) not in allowed for item in selected)
            ):
                unresolved.append(key)
            continue
        spec_key = key.split("#", 1)[0]
        spec = specs.get(spec_key, {})
        submitted = values.get(key)
        if isinstance(submitted, dict) and "value" in submitted:
            submitted_value = submitted.get("value")
        else:
            submitted_value = submitted
        chosen = submitted_value if submitted is not None else None
        if row.get("required") and chosen is None:
            missing.append(key)
            continue
        if (
            row.get("required")
            and row.get("status") == "ambiguous"
            and submitted_value is None
        ):
            unresolved.append(key)
        if chosen is not None:
            errors = _validate_value(key, chosen, spec)
            for reason in errors:
                invalid.append({"key": key, "reason": reason})
    return missing, unresolved, invalid


def apply_confirmed_params(
    intent: dict[str, Any], values: dict[str, Any]
) -> dict[str, Any]:
    """Apply confirmed card values to a copied AMLIntent and add provenance.

    Args:
        intent: Current merged AMLIntent mapping.
        values: Per-row values, optionally wrapped with choice and provenance.

    Returns:
        A new intent mapping with resolved values and cleared clarification state.
    """
    result = deepcopy(intent)
    result.setdefault("field_provenance", {})
    result.setdefault("applied_defaults", [])
    defaults = list(result.get("applied_defaults") or [])
    thresholds = result.setdefault("thresholds", [])
    for key, submitted in values.items():
        row = (
            submitted
            if isinstance(submitted, dict) and "value" in submitted
            else {"value": submitted}
        )
        value = row.get("value")
        choice = row.get("choice") or "user_input"
        provenance = row.get("provenance") or (
            "inferred"
            if choice == "accepted_default" and row.get("inferred")
            else "assumed_default" if choice == "accepted_default" else "user_input"
        )
        if key.startswith("filter#"):
            try:
                index = int(key.split("#", 1)[1]) - 1
            except ValueError:
                continue
            filters = result.setdefault("resolved_filters", [])
            if index < 0 or index >= len(filters):
                continue
            current = filters[index]
            if choice == "removed":
                filters[index] = None
            elif choice == "acknowledged" and value == "proceed_without_filter":
                current["acknowledged_without_filter"] = True
                current["status"] = "disabled"
                current["values"] = []
                current["audit_choice"] = "acknowledged"
            else:
                current["values"] = list(value) if isinstance(value, list) else [value]
                current["status"] = "resolved"
                current["provenance"] = provenance
                current["audit_choice"] = choice
        elif key.startswith("threshold#"):
            try:
                index = int(key.split("#", 1)[1]) - 1
            except ValueError:
                continue
            while len(thresholds) <= index:
                thresholds.append(
                    {
                        "field": "transaction_amount",
                        "operator": ">",
                        "value_from": None,
                        "target_scope": "AGGREGATE",
                    }
                )
            threshold = thresholds[index]
            if isinstance(value, dict):
                threshold["operator"] = value.get(
                    "comparator", threshold.get("operator")
                )
                threshold["value_from"] = value.get("value")
                threshold["value_to"] = value.get("value_to")
                threshold["target_scope"] = value.get(
                    "scope", threshold.get("target_scope", "AGGREGATE")
                )
                threshold["transaction_type"] = value.get(
                    "transaction_type", threshold.get("transaction_type")
                )
            else:
                threshold["value_from"] = value
            threshold["provenance"] = provenance
        else:
            _set_intent_value(result, key, value)
            result["field_provenance"][key] = provenance
            if key == "window" and isinstance(result.get("time_window"), dict):
                result["time_window"]["provenance"] = provenance
        if choice == "accepted_default":
            defaults.append(f"{key}: {value}")
    result["applied_defaults"] = list(dict.fromkeys(defaults))
    result["resolved_filters"] = [
        item for item in result.get("resolved_filters", []) if isinstance(item, dict)
    ]
    result["filters"] = [
        {
            "dimension": item.get("dimension"),
            "raw_mention": item.get("raw_mention", ""),
            "polarity": item.get("polarity", "positive"),
        }
        for item in result["resolved_filters"]
        if item.get("status") == "resolved"
    ]
    code_filter = next(
        (
            item
            for item in result["resolved_filters"]
            if item.get("dimension") == "explanation_code"
            and item.get("status") == "resolved"
        ),
        None,
    )
    if code_filter:
        selected_codes = [str(value) for value in code_filter.get("values", [])]
        result["explanation_codes"] = selected_codes or None
        previous_by_type = result.get("explanation_codes_by_type")
        if isinstance(previous_by_type, dict):
            filtered = {
                str(tx_type): [
                    str(code) for code in codes if str(code) in selected_codes
                ]
                for tx_type, codes in previous_by_type.items()
            }
            filtered = {tx_type: codes for tx_type, codes in filtered.items() if codes}
            mapped_codes = {code for codes in filtered.values() for code in codes}
            if mapped_codes == set(selected_codes):
                result["explanation_codes_by_type"] = filtered
            else:
                result["explanation_codes_by_type"] = None
        else:
            result["explanation_codes_by_type"] = None
    elif any(
        isinstance(item, dict) and item.get("dimension") == "explanation_code"
        for item in intent.get("resolved_filters", [])
    ):
        result["explanation_codes"] = None
        result["explanation_codes_by_type"] = None
    result["clarifications"] = []
    result["clarification_questions"] = []
    result["clarification_needed"] = False
    result["ready_for_handoff"] = True
    return result


def _set_intent_value(intent: dict[str, Any], key: str, value: Any) -> None:
    """Map one catalog key into the existing AMLIntent shape."""
    if key == "scenario_type":
        intent[key] = value
    elif key == "grain":
        intent.setdefault("aggregation", {})["grain"] = value
    elif key == "aggregation":
        intent.setdefault("aggregation", {})["function"] = value
    elif key == "metric":
        thresholds = intent.setdefault("thresholds", [])
        if thresholds:
            thresholds[0]["field"] = value
    elif key == "comparator":
        thresholds = intent.setdefault("thresholds", [])
        if thresholds:
            thresholds[0]["operator"] = value
    elif key == "window":
        intent["time_window"] = deepcopy(value)
        if isinstance(intent["time_window"], dict):
            intent["time_window"]["provenance"] = intent.get(
                "field_provenance", {}
            ).get(key, "user_input")
    elif key == "window_kind" and isinstance(intent.get("time_window"), dict):
        intent["time_window"]["is_rolling"] = value == "rolling"
    elif key == "transaction_scope":
        if value == "all":
            intent["transaction_type"] = None
            intent["transaction_types"] = None
        else:
            intent.setdefault("transaction_type", None)
    elif key == "baseline":
        intent["baseline_window"] = deepcopy(value)
    elif key == "exclusions":
        intent["exclusions"] = (
            value if isinstance(value, list) else ([value] if value else [])
        )
    elif key == "expected_alert_range" and isinstance(value, dict):
        intent["expected_alert_range_min"] = value.get("min")
        intent["expected_alert_range_max"] = value.get("max")
    elif key == "threshold":
        thresholds = intent.setdefault("thresholds", [])
        if thresholds:
            thresholds[0]["value_from"] = value


def _catalog_dict(catalog: Any) -> dict[str, Any]:
    if hasattr(catalog, "model_dump"):
        return catalog.model_dump(mode="python")
    return catalog if isinstance(catalog, dict) else {}


def _filter_label(dimension: str, catalog: Any) -> str:
    dimensions = _catalog_dict(catalog).get("dimensions", {})
    item = dimensions.get(dimension, {}) if isinstance(dimensions, dict) else {}
    return str(item.get("label") or dimension.replace("_", " ").title())


def _intent_values(intent: dict[str, Any]) -> dict[str, Any]:
    aggregation = intent.get("aggregation") or {}
    thresholds = intent.get("thresholds") or []
    first = thresholds[0] if thresholds and isinstance(thresholds[0], dict) else {}
    time_window = intent.get("time_window")
    scope = (
        "specific"
        if intent.get("transaction_type") or intent.get("transaction_types")
        else "all"
    )
    value = {
        "scenario_type": intent.get("scenario_type"),
        "grain": aggregation.get("grain") if isinstance(aggregation, dict) else None,
        "metric": first.get("field"),
        "aggregation": (
            aggregation.get("function") if isinstance(aggregation, dict) else None
        ),
        "comparator": first.get("operator"),
        "window": time_window,
        "window_kind": (
            "rolling"
            if isinstance(time_window, dict) and time_window.get("is_rolling", True)
            else "calendar"
        ),
        "transaction_scope": scope,
        "baseline": intent.get("baseline_window"),
        "exclusions": intent.get("exclusions"),
        "expected_alert_range": _alert_range(intent),
    }
    for index, threshold in enumerate(thresholds, start=1):
        if isinstance(threshold, dict):
            value[f"threshold#{index}"] = _threshold_value(threshold)
    return value


def _threshold_value(threshold: dict[str, Any]) -> dict[str, Any]:
    return {
        "comparator": threshold.get("operator"),
        "value": threshold.get("value_from"),
        "value_to": threshold.get("value_to"),
        "scope": threshold.get("target_scope"),
        "transaction_type": threshold.get("transaction_type"),
    }


def _alert_range(intent: dict[str, Any]) -> dict[str, Any] | None:
    minimum = intent.get("expected_alert_range_min")
    maximum = intent.get("expected_alert_range_max")
    return (
        {"min": minimum, "max": maximum}
        if minimum is not None or maximum is not None
        else None
    )


def _provenance_for(intent: dict[str, Any], key: str) -> str:
    provenance = intent.get("field_provenance") or {}
    return (
        str(provenance.get(key) or "stated")
        if isinstance(provenance, dict)
        else "stated"
    )


def _status_for_value(
    value: Any, provenance: str, spec: dict[str, Any]
) -> tuple[str, str]:
    if isinstance(value, dict) and value.get("value") is None:
        value = None
    if value is None or value == "":
        default = spec.get("default")
        if default is not None:
            return "defaulted", "assumed_default"
        return "missing", provenance
    if provenance == "inferred":
        return "inferred", provenance
    if provenance in {"assumed_default", "default"}:
        return "defaulted", provenance
    return "provided", provenance


def _row(
    key: str,
    spec: dict[str, Any],
    value: Any,
    status: str,
    provenance: str,
    base_values: dict[str, Any],
) -> dict[str, Any]:
    base_key = key if key in base_values else key.split("#", 1)[0]
    old_value = base_values.get(base_key)
    return {
        "key": key,
        "label": spec.get("label", key.replace("_", " ").title()),
        "status": status,
        "value": value,
        "recommended_value": value if value is not None else spec.get("default"),
        "input_type": spec.get("input", "text"),
        "options": spec.get("options", []),
        "prompt": spec.get("prompt", ""),
        "provenance": provenance,
        "required": bool(spec.get("required", False)),
        "read_only": False,
        "changed": base_key in base_values and old_value != value,
        "validation_errors": [],
    }


def _apply_ambiguity(
    row: dict[str, Any], clarifications: list[Any], key: str, index: int | None = None
) -> None:
    aliases = {key.casefold(), key.replace("_", " ").casefold()}
    if key == "threshold":
        aliases.update({"amount", "value", "high value", "threshold"})
    for clarification in clarifications:
        if not isinstance(clarification, dict):
            continue
        dimension = str(clarification.get("dimension", "")).casefold()
        if any(alias in dimension or dimension in alias for alias in aliases):
            row["status"] = "ambiguous"
            row["prompt"] = clarification.get("question") or row.get("prompt")
            row["why_it_matters"] = clarification.get("why_it_matters")
            row["options"] = clarification.get("options") or row.get("options", [])
            if row.get("value") is None:
                row["recommended_value"] = None
            return


def _is_all_time(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().casefold() in {"all", "all time", "all-time", "forever"}
    return isinstance(value, dict) and (
        value.get("all_time") is True
        or value.get("unit", "").casefold() in {"all", "all_time", "all-time"}
    )


def _validate_value(key: str, value: Any, spec: dict[str, Any]) -> list[str]:
    if key == "window":
        return (
            ["All-time windows are not allowed."]
            if _is_all_time(value)
            else _validate_duration(value, spec)
        )
    if key.startswith("threshold#"):
        scalar = value.get("value") if isinstance(value, dict) else value
        return _validate_number(scalar, spec)
    if spec.get("input") == "enum" and value not in spec.get("options", []):
        return [
            f"Value must be one of: {', '.join(map(str, spec.get('options', [])))}."
        ]
    if spec.get("input") == "number":
        return _validate_number(value, spec)
    return []


def _validate_number(value: Any, spec: dict[str, Any]) -> list[str]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ["A numeric value is required."]
    validation = spec.get("validation") or {}
    errors: list[str] = []
    minimum = validation.get("min")
    maximum = validation.get("max")
    if minimum is not None and number < minimum:
        errors.append(f"Value must be at least {minimum}.")
    if maximum is not None and number > maximum:
        errors.append(f"Value must be at most {maximum}.")
    return errors


def _validate_duration(value: Any, spec: dict[str, Any]) -> list[str]:
    if not isinstance(value, dict):
        return ["A duration with unit and value is required."]
    try:
        amount = float(value.get("value"))
    except (TypeError, ValueError):
        return ["A numeric duration is required."]
    unit = str(value.get("unit", "")).upper()
    days_per_unit = {
        "DAYS": 1,
        "DAY": 1,
        "WEEKS": 7,
        "WEEK": 7,
        "MONTHS": 30,
        "MONTH": 30,
        "YEARS": 365,
        "YEAR": 365,
    }
    if unit not in days_per_unit:
        return ["Choose a supported duration unit."]
    days = amount * days_per_unit[unit]
    errors: list[str] = []
    validation = spec.get("validation") or {}
    minimum = validation.get("min_days", spec.get("min_days", 1))
    maximum = validation.get("max_days", spec.get("max_days", 365))
    if days < minimum:
        errors.append(f"Window must be at least {minimum} day.")
    if days > maximum:
        errors.append(f"Window cannot exceed {maximum} days.")
    return errors
