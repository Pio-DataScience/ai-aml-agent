"""Canonical AML scenario signatures for duplicate and overlap checks."""

from __future__ import annotations

import json
from typing import Any

from services.aml_builder.web.domain.hashing import stable_hash

_UNIT_DAYS = {"DAYS": 1, "WEEKS": 7, "MONTHS": 30, "YEARS": 365}


def build_signature(intent: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Build the normalized canonical signature and its SHA-256 hash.

    Args:
        intent: Confirmed AML intent with resolved parameters and filters.

    Returns:
        A pair containing the canonical signature and its stable hash.
    """
    aggregation = intent.get("aggregation")
    aggregation = aggregation if isinstance(aggregation, dict) else {}
    thresholds = intent.get("thresholds")
    thresholds = [item for item in thresholds if isinstance(item, dict)] if isinstance(thresholds, list) else []
    threshold = thresholds[0] if thresholds else {}
    window = _window(intent.get("time_window"))
    normalized = {
        "scenario_type": _text(intent.get("scenario_type")),
        "grain": _text(aggregation.get("grain")),
        "metric": _text(threshold.get("field")),
        "aggregation": _text(aggregation.get("function")),
        "window": window,
        "comparator": _comparator_direction(threshold.get("operator")),
        "threshold": _threshold(threshold),
        "baseline": _baseline(intent.get("baseline_window")),
        "transaction_scope": _transaction_scope(intent),
        "filters": _filters(intent.get("resolved_filters")),
    }
    canonical = json.loads(json.dumps(normalized, sort_keys=True, separators=(",", ":")))
    return canonical, stable_hash(canonical)


def canonical_json(signature: dict[str, Any]) -> str:
    """Serialize a signature into deterministic compact JSON.

    Args:
        signature: Canonical scenario signature.

    Returns:
        Compact, key-sorted JSON text.
    """
    return json.dumps(signature, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _window(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    unit = str(value.get("unit") or "DAYS").upper()
    days = value.get("unit_days")
    if days is None:
        try:
            days = int(value.get("value")) * _UNIT_DAYS[unit]
        except (KeyError, TypeError, ValueError):
            return None
    return {
        "value": int(days),
        "unit_days": int(days),
        "kind": "rolling" if value.get("is_rolling", value.get("kind") == "rolling") else "calendar",
    }


def _threshold(value: dict[str, Any]) -> dict[str, Any] | None:
    if not value:
        return None
    operator = str(value.get("operator") or "").strip().upper()
    start = value.get("value_from", value.get("value"))
    end = value.get("value_to")
    result: dict[str, Any] = {"operator": operator}
    if operator == "BETWEEN" or end is not None:
        result["from"] = _scalar(start)
        result["to"] = _scalar(end)
    else:
        result["value"] = _scalar(start)
    currency = value.get("currency")
    if currency:
        result["currency"] = _text(currency)
    return result


def _baseline(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    unit = str(value.get("unit") or "DAYS").upper()
    duration = value.get("unit_days")
    if duration is None:
        try:
            duration = int(value.get("duration", value.get("value"))) * _UNIT_DAYS[unit]
        except (KeyError, TypeError, ValueError):
            duration = None
    offset = value.get("offset_days")
    return {
        "unit_days": int(duration) if duration is not None else None,
        "offset_days": int(offset) if offset is not None else None,
        "exclude_current_window": bool(value.get("exclude_current_window", True)),
    }


def _transaction_scope(intent: dict[str, Any]) -> dict[str, Any]:
    types = intent.get("transaction_types")
    if not isinstance(types, list):
        types = []
    single = intent.get("transaction_type")
    if single and not types:
        types = [part.strip() for part in str(single).replace(";", ",").split(",") if part.strip()]
    codes = intent.get("explanation_codes")
    codes = codes if isinstance(codes, list) else []
    for item in intent.get("resolved_filters", []) if isinstance(intent.get("resolved_filters"), list) else []:
        if isinstance(item, dict) and item.get("dimension") == "explanation_code":
            codes.extend(item.get("values") if isinstance(item.get("values"), list) else [])
    raw_scope_values = [str(item).strip().casefold() for item in types]
    explicit_scope = str(intent.get("transaction_scope") or "").strip().casefold()
    all_tokens = {"all", "any", "all transaction types", "any transaction type", "all types"}
    if explicit_scope in {"all", "any"} or raw_scope_values and all(item in all_tokens for item in raw_scope_values):
        types = []
        codes = []
        mode = "all"
    else:
        types = _sorted_values(types)
        codes = _sorted_values(codes)
        mode = explicit_scope or ("specific" if types or codes else "all")
    if mode not in {"specific", "all"}:
        mode = "specific" if types or codes else "all"
    return {"mode": mode, "types": types, "codes": codes}


def _filters(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, list):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for item in value:
        if not isinstance(item, dict) or item.get("status") not in (None, "resolved"):
            continue
        dimension = str(item.get("dimension") or "").strip().casefold()
        if not dimension:
            continue
        values = item.get("values", item.get("selected_values", []))
        result[dimension] = {
            "values": _sorted_values(values if isinstance(values, list) else [values]),
            "match": str(item.get("match") or "any_of").casefold(),
            "polarity": str(item.get("polarity") or "positive").casefold(),
        }
    return dict(sorted(result.items()))


def _comparator_direction(value: Any) -> str | None:
    operator = str(value or "").strip().upper()
    if operator in {">", ">="}:
        return "greater_than"
    if operator in {"<", "<="}:
        return "less_than"
    if operator in {"=", "IN"}:
        return "equal"
    if operator == "BETWEEN":
        return "between"
    return operator.casefold() or None


def _sorted_values(values: list[Any]) -> list[str]:
    return sorted({_text(value) or "" for value in values if value is not None})


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).strip().split())
    return text.casefold() if text else None


def _scalar(value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        try:
            return float(text) if "." in text else int(text)
        except ValueError:
            return text.casefold()
    return value
