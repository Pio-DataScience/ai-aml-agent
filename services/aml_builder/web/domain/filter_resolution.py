"""Catalog-driven, deterministic scenario filter resolution."""

from __future__ import annotations

import difflib
import re
from typing import Any, Callable


CandidateProvider = Callable[[str, str, dict[str, Any]], list[dict[str, Any]]]


def resolve_filter_mentions(
    mentions: list[dict[str, Any]],
    catalog: Any,
    *,
    candidate_provider: CandidateProvider | None = None,
) -> list[dict[str, Any]]:
    """Resolve extracted filter mentions against enabled catalog dimensions.

    Args:
        mentions: LLM-extracted dimension, raw mention, and polarity mappings.
        catalog: FiltersCatalog model or its serialized mapping.
        candidate_provider: Offline-testable provider for lookup and semantic candidates.

    Returns:
        Filter rows with deterministic options, confidence, provenance and status.
    """
    dimensions = _mapping(catalog).get("dimensions", {})
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in mentions:
        if not isinstance(item, dict):
            continue
        name = str(item.get("dimension") or "").strip().casefold()
        dimension = dimensions.get(name) if isinstance(dimensions, dict) else None
        row = _base_row(name or "unknown", item, dimension)
        seen.add(name)
        if not isinstance(dimension, dict) or dimension.get("status") != "confirmed":
            row.update(
                status="disabled",
                options=["remove", "proceed_without_filter"],
                values=[],
            )
            rows.append(row)
            continue
        row["target"] = dimension.get("target", {})
        row["level"] = dimension.get("level", "unknown")
        row["polarity_allowed"] = bool(dimension.get("polarity_allowed", False))
        if row["polarity"] == "negative" and not row["polarity_allowed"]:
            row.update(status="unresolved", options=[], values=[])
            rows.append(row)
            continue
        explicit = item.get("values") or item.get("selected_values")
        if isinstance(explicit, list) and explicit:
            row.update(status="resolved", values=_stable_values(explicit), options=[])
            rows.append(row)
            continue
        resolver = str(dimension.get("resolver", ""))
        try:
            if resolver == "enum":
                candidates = _enum_candidates(dimension)
            elif candidate_provider is not None:
                candidates = candidate_provider(name, row["raw_mention"], dimension)
            else:
                candidates = []
        except Exception:
            row.update(status="lookup_failed", options=[], values=[])
            rows.append(row)
            continue
        normalized = _rank_candidates(candidates, row["raw_mention"], resolver)
        config = dimension.get("resolver_config") or {}
        max_options = max(1, int(config.get("max_options", 5)))
        threshold = float(config.get("match_threshold", 0.75))
        margin = float(config.get("ambiguity_margin", 0.05))
        row["options"] = normalized[:max_options]
        if not normalized or normalized[0]["confidence"] < threshold:
            row.update(status="unresolved", values=[])
        elif (
            len(normalized) > 1
            and normalized[0]["confidence"] - normalized[1]["confidence"] < margin
        ):
            row.update(status="ambiguous", values=[])
        else:
            row.update(status="resolved", values=[normalized[0]["value"]])
        rows.append(row)

    for name, dimension in (dimensions.items() if isinstance(dimensions, dict) else []):
        if (
            dimension.get("status") == "confirmed"
            and dimension.get("required")
            and name not in seen
        ):
            rows.append(
                _base_row(name, {"raw_mention": "", "polarity": "positive"}, dimension)
                | {
                    "target": dimension.get("target", {}),
                    "level": dimension.get("level", "unknown"),
                    "polarity_allowed": bool(dimension.get("polarity_allowed", False)),
                    "status": "unresolved",
                    "options": [],
                    "values": [],
                }
            )
    return rows


def build_filter_prompt_section(catalog: Any) -> str:
    """Render extractable filter labels without exposing physical column names."""
    dimensions = _mapping(catalog).get("dimensions", {})
    lines = ["Filter dimensions you may extract:"]
    for name, item in sorted(
        dimensions.items() if isinstance(dimensions, dict) else []
    ):
        label = str(item.get("label") or name)
        support = (
            "supported" if item.get("status") == "confirmed" else "not yet supported"
        )
        lines.append(f"- {name}: {label} ({support})")
    lines.append(
        "Return filters as objects with dimension, raw_mention, and polarity (positive or negative)."
    )
    lines.append(
        "Include unsupported or unfamiliar dimensions; the officer will be asked to resolve them."
    )
    return "\n".join(lines)


def build_service_b_intent(intent: dict[str, Any], catalog: Any) -> dict[str, Any]:
    """Add resolved filters and deterministic temporal semantics to the handoff."""
    result = dict(intent)
    result.pop("filters", None)
    result["temporal_contract"] = _service_b_temporal_contract(intent)
    dimensions = _mapping(catalog).get("dimensions", {})
    resolved = [
        dict(item)
        for item in (intent.get("resolved_filters") or [])
        if isinstance(item, dict)
        and item.get("status") == "resolved"
        and item.get("values")
    ]
    instructions = []
    for item in resolved:
        dimension = (
            dimensions.get(item.get("dimension"), {})
            if isinstance(dimensions, dict)
            else {}
        )
        target = dimension.get("target", {})
        table, column = target.get("table"), target.get("column")
        item.setdefault("target", target)
        item.setdefault("level", dimension.get("level", "unknown"))
        item.setdefault("match", dimension.get("match", "any_of"))
        item.setdefault("polarity_allowed", dimension.get("polarity_allowed", False))
        join = dimension.get("join_hint")
        values_text = ", ".join(str(value) for value in item.get("values", []))
        instructions.append(
            f"Apply {item.get('polarity', 'positive')} {dimension.get('match', 'any_of')} filter "
            f"{item.get('dimension')} with values [{values_text}] "
            f"to {table}.{column}" + (f" using {join}" if join else "") + "."
        )
    result["resolved_filters"] = resolved
    result["filter_instructions"] = "\n".join(instructions)
    return result


def _service_b_temporal_contract(intent: dict[str, Any]) -> dict[str, str]:
    """Express supported temporal boundaries and relationships as typed values.

    The numeric durations remain in the validated time-window fields. This
    contract preserves their meaning when Service B receives the reduced AML
    intent, without carrying free-text descriptions into its model context.
    """
    window = _mapping(intent.get("time_window"))
    baseline = _mapping(intent.get("baseline_window"))
    none = {
        "current_lower_boundary": "NONE",
        "current_upper_boundary": "NONE",
        "baseline_lower_boundary": "NONE",
        "baseline_upper_boundary": "NONE",
        "baseline_relationship": "NONE",
    }
    if not window and not baseline:
        return {"kind": "NONE", **none}
    if (
        window.get("unit") == "MONTHS"
        and window.get("value") == 1
        and window.get("is_rolling") is False
        and not baseline
    ):
        return {
            "kind": "CALENDAR",
            **none,
            "current_lower_boundary": "CALENDAR_MONTH_START",
            "current_upper_boundary": "NEXT_CALENDAR_MONTH_START",
        }
    if window.get("unit") == "DAYS" and window.get("is_rolling") is True:
        if not baseline:
            return {
                "kind": "ROLLING",
                **none,
                "current_lower_boundary": "SYSDATE_MINUS_DURATION",
                "current_upper_boundary": "SYSDATE_EXCLUSIVE",
            }
        if (
            baseline.get("unit", "DAYS") == "DAYS"
            and baseline.get("exclude_current_window") is True
            and isinstance(baseline.get("duration"), int)
            and isinstance(baseline.get("offset_days"), int)
        ):
            return {
                "kind": "COMPARISON",
                "current_lower_boundary": "SYSDATE_MINUS_CURRENT_DURATION",
                "current_upper_boundary": "SYSDATE_EXCLUSIVE",
                "baseline_lower_boundary": (
                    "SYSDATE_MINUS_CURRENT_AND_BASELINE_DURATIONS"
                ),
                "baseline_upper_boundary": "SYSDATE_MINUS_CURRENT_OFFSET",
                "baseline_relationship": "IMMEDIATELY_PRECEDING_NON_OVERLAPPING",
            }
    return {"kind": "UNSUPPORTED", **none}


def _base_row(name: str, item: dict[str, Any], dimension: Any) -> dict[str, Any]:
    return {
        "dimension": name,
        "raw_mention": str(item.get("raw_mention") or ""),
        "polarity": str(item.get("polarity") or "positive").casefold(),
        "level": (
            dimension.get("level", "unknown")
            if isinstance(dimension, dict)
            else "unknown"
        ),
        "status": "unresolved",
        "options": [],
        "values": [],
        "confidence": None,
        "provenance": "catalog_resolver",
    }


def _rank_candidates(candidates: Any, raw: str, resolver: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for candidate in candidates if isinstance(candidates, list) else []:
        if not isinstance(candidate, dict) or candidate.get("value") is None:
            continue
        value = str(candidate["value"])
        label = str(candidate.get("label") or candidate.get("desc_eng") or value)
        confidence = candidate.get("confidence", candidate.get("score"))
        if resolver in {"lookup", "enum"}:
            confidence = 1.0 if _normal(raw) == _normal(label) else 0.0
        elif confidence is None:
            confidence = difflib.SequenceMatcher(
                None, _normal(raw), _normal(label)
            ).ratio()
        rows.append(
            {"value": value, "label": label, "confidence": round(float(confidence), 6)}
        )
    return sorted(
        rows,
        key=lambda row: (
            -row["confidence"],
            row["value"].casefold(),
            row["label"].casefold(),
        ),
    )


def _enum_candidates(dimension: dict[str, Any]) -> list[dict[str, Any]]:
    config = dimension.get("resolver_config") or {}
    options = config.get("options", dimension.get("options", []))
    return [{"value": option, "label": str(option)} for option in options]


def _stable_values(values: list[Any]) -> list[str]:
    return list(
        dict.fromkeys(str(value).strip() for value in values if str(value).strip())
    )


def _normal(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().casefold())


def _mapping(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="python")
    return value if isinstance(value, dict) else {}
