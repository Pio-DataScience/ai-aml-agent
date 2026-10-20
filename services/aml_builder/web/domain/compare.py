"""Deterministic scenario signature comparison and conflict descriptions."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


@dataclass(frozen=True)
class Comparison:
    """One deterministic relation result and its explainable dimensions."""

    relation: str | None
    diff: list[dict[str, Any]]
    similarity_pct: int


@dataclass(frozen=True)
class _SetRelation:
    new_subset: bool
    existing_subset: bool
    intersects: bool
    same: bool


def compare_signatures(
    new: dict[str, Any],
    existing: dict[str, Any],
    *,
    dimension_weights: dict[str, int] | None = None,
    window_mismatch_relation: str = "RELATED",
) -> Comparison:
    """Compare two canonical signatures without probabilistic or LLM decisions.

    Args:
        new: Signature for the draft scenario.
        existing: Signature for an active registry scenario.
        dimension_weights: Policy weights used for the displayed similarity.
        window_mismatch_relation: Policy relation for different window lengths/kinds.

    Returns:
        Relation, per-dimension diff, and weighted matching percentage. A ``None``
        relation means the scenarios have different entity types and should not be compared.
    """
    weights = dimension_weights or {}
    if _norm(new.get("scenario_type")) != _norm(existing.get("scenario_type")):
        return Comparison(None, [], 0)
    dimensions = (
        "scenario_type", "grain", "metric", "aggregation", "comparator",
        "window", "threshold", "baseline", "transaction_scope", "filters",
    )
    diff = [
        {
            "dimension": dimension,
            "new": new.get(dimension),
            "existing": existing.get(dimension),
            "same": _same_dimension(dimension, new.get(dimension), existing.get(dimension)),
        }
        for dimension in dimensions
    ]
    total = sum(max(0, int(weights.get(name, 1))) for name in dimensions)
    matched = sum(
        max(0, int(weights.get(item["dimension"], 1)))
        for item in diff if item["same"]
    )
    similarity = round(100 * matched / total) if total else 0

    new_window = new.get("window") or {}
    existing_window = existing.get("window") or {}
    if new_window.get("kind") != existing_window.get("kind"):
        return Comparison(window_mismatch_relation, diff, similarity)
    core = ("scenario_type", "grain", "metric", "aggregation", "comparator")
    if any(not _same_dimension(key, new.get(key), existing.get(key)) for key in core):
        return Comparison("RELATED", diff, similarity)
    if new.get("baseline") != existing.get("baseline") and (
        new.get("baseline") is not None or existing.get("baseline") is not None
    ):
        return Comparison("RELATED", diff, similarity)
    if new_window.get("unit_days") != existing_window.get("unit_days"):
        return Comparison(window_mismatch_relation, diff, similarity)

    relations = [
        _threshold_relation(new.get("threshold"), existing.get("threshold")),
        _scope_relation(new.get("transaction_scope") or {}, existing.get("transaction_scope") or {}),
        _filters_relation(new.get("filters") or {}, existing.get("filters") or {}),
    ]
    if any(relation is None for relation in relations):
        return Comparison("RELATED", diff, similarity)
    typed_relations = [relation for relation in relations if relation is not None]
    new_subset = all(item.new_subset for item in typed_relations)
    existing_subset = all(item.existing_subset for item in typed_relations)
    intersects = all(item.intersects for item in typed_relations)
    if new_subset and existing_subset:
        relation = "IDENTICAL"
    elif new_subset:
        relation = "COVERED"
    elif existing_subset:
        relation = "COVERS"
    elif intersects:
        relation = "OVERLAP"
    else:
        relation = "RELATED"
    return Comparison(relation, diff, similarity)


def _threshold_relation(new: Any, existing: Any) -> _SetRelation | None:
    if new == existing:
        return _SetRelation(True, True, True, True)
    if not isinstance(new, dict) or not isinstance(existing, dict):
        return None
    if new.get("currency") != existing.get("currency"):
        return None
    first = _interval(new)
    second = _interval(existing)
    if first is None or second is None:
        return None
    new_lower, new_lower_inclusive, new_upper, new_upper_inclusive = first
    old_lower, old_lower_inclusive, old_upper, old_upper_inclusive = second
    return _interval_relation(
        new_lower, new_lower_inclusive, new_upper, new_upper_inclusive,
        old_lower, old_lower_inclusive, old_upper, old_upper_inclusive,
    )


def _interval(value: dict[str, Any]) -> tuple[Decimal | None, bool, Decimal | None, bool] | None:
    operator = str(value.get("operator") or "").upper()
    try:
        start = _decimal(value.get("from", value.get("value")))
        end = _decimal(value.get("to"))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if operator == ">":
        return start, False, None, False
    if operator == ">=":
        return start, True, None, False
    if operator == "<":
        return None, False, start, False
    if operator == "<=":
        return None, False, start, True
    if operator in {"=", "IN"}:
        return start, True, start, True
    if operator == "BETWEEN" and start is not None and end is not None:
        return start, True, end, True
    return None


def _interval_relation(
    nl: Decimal | None, nli: bool, nu: Decimal | None, nui: bool,
    el: Decimal | None, eli: bool, eu: Decimal | None, eui: bool,
) -> _SetRelation:
    new_subset = _lower_at_least(nl, nli, el, eli) and _upper_at_most(nu, nui, eu, eui)
    existing_subset = _lower_at_least(el, eli, nl, nli) and _upper_at_most(eu, eui, nu, nui)
    if nu is None or el is None:
        lower_overlap = True
    elif nu > el:
        lower_overlap = True
    else:
        lower_overlap = nu == el and nui and eli
    if eu is None or nl is None:
        upper_overlap = True
    elif eu > nl:
        upper_overlap = True
    else:
        upper_overlap = eu == nl and eui and nli
    return _SetRelation(new_subset, existing_subset, lower_overlap and upper_overlap, new_subset and existing_subset)


def _lower_at_least(a: Decimal | None, ai: bool, b: Decimal | None, bi: bool) -> bool:
    if b is None:
        return True
    if a is None:
        return False
    return a > b or (a == b and (not ai or bi))


def _upper_at_most(a: Decimal | None, ai: bool, b: Decimal | None, bi: bool) -> bool:
    if b is None:
        return True
    if a is None:
        return False
    return a < b or (a == b and (not ai or bi))


def _scope_relation(new: dict[str, Any], existing: dict[str, Any]) -> _SetRelation:
    if new == existing:
        return _SetRelation(True, True, True, True)
    if new.get("mode") == "all":
        return _SetRelation(existing.get("mode") == "all", True, True, existing.get("mode") == "all")
    if existing.get("mode") == "all":
        return _SetRelation(True, new.get("mode") == "all", True, False)
    new_values = _scope_values(new)
    existing_values = _scope_values(existing)
    return _finite_relation(new_values, existing_values)


def _filters_relation(new: dict[str, Any], existing: dict[str, Any]) -> _SetRelation | None:
    result = _SetRelation(True, True, True, True)
    for dimension in sorted(set(new) | set(existing)):
        new_filter = new.get(dimension)
        existing_filter = existing.get(dimension)
        if new_filter is None:
            new_filter = {"values": [], "match": "any_of", "polarity": "positive"}
        if existing_filter is None:
            existing_filter = {"values": [], "match": "any_of", "polarity": "positive"}
        if new_filter.get("match") != existing_filter.get("match"):
            return None
        match = new_filter.get("match", "any_of")
        if match != "any_of":
            if new_filter == existing_filter:
                continue
            return None
        new_values = set(new_filter.get("values") or [])
        old_values = set(existing_filter.get("values") or [])
        if new_filter.get("polarity", "positive") == "negative":
            if existing_filter.get("polarity", "positive") != "negative":
                return None
            relation = _excluded_set_relation(new_values, old_values)
        elif existing_filter.get("polarity", "positive") == "negative":
            relation = _positive_exclusion_relation(new_values, old_values)
        else:
            relation = _finite_relation(new_values, old_values)
        result = _combine_relations(result, relation)
    return result


def _excluded_set_relation(new: set[str], existing: set[str]) -> _SetRelation:
    # A larger excluded set is a smaller allowed set.
    return _SetRelation(existing <= new, new <= existing, True, new == existing)


def _positive_exclusion_relation(positive: set[str], excluded: set[str]) -> _SetRelation:
    intersects = bool(positive - excluded)
    new_subset = positive.isdisjoint(excluded)
    return _SetRelation(new_subset, False, intersects, False)


def _finite_relation(new: set[str], existing: set[str]) -> _SetRelation:
    return _SetRelation(new <= existing, existing <= new, bool(new & existing), new == existing)


def _combine_relations(left: _SetRelation, right: _SetRelation) -> _SetRelation:
    return _SetRelation(
        left.new_subset and right.new_subset,
        left.existing_subset and right.existing_subset,
        left.intersects and right.intersects,
        left.same and right.same,
    )


def _scope_values(value: dict[str, Any]) -> set[str]:
    return {"type:" + item for item in value.get("types", [])} | {"code:" + item for item in value.get("codes", [])}


def _same_dimension(name: str, left: Any, right: Any) -> bool:
    if name == "scenario_type":
        return _norm(left) == _norm(right)
    return left == right


def _norm(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def _decimal(value: Any) -> Decimal | None:
    return Decimal(str(value)) if value is not None else None
