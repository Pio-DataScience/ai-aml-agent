"""Pure metadata defaulting, normalization and validation rules."""

from copy import deepcopy
from typing import Any, Dict, Iterable, List, Mapping, Tuple


def seed_metadata_defaults(
    intent: Mapping[str, Any],
    existing: Mapping[str, Any] | None,
    defaults: Mapping[str, Any],
) -> Dict[str, Any]:
    """Derive governance metadata defaults without overwriting officer values.

    Args:
        intent: Normalized scenario intent.
        existing: Previously collected metadata, if any.
        defaults: Runtime settings and fixed metadata default values.

    Returns:
        New metadata dictionary containing existing and derived values.
    """
    metadata = dict(existing or {})
    description = (
        intent.get("detection_logic") or intent.get("scenario_name") or ""
    ).strip()
    derived = dict(defaults)
    derived["SCENARIO_DES_ENG"] = description[:400] if description else None
    derived["SCENARIO_DES_NAT_LAN"] = (
        description[:400] if description else None
    )
    for key, value in derived.items():
        if value is not None and metadata.get(key) is None:
            metadata[key] = value
    return metadata


def normalize_and_validate_metadata(
    metadata: Mapping[str, Any],
    fields: Iterable[Any],
    defaults: Mapping[str, Any],
    lookup_values: Mapping[str, Iterable[str]],
) -> Tuple[Dict[str, Any], List[str]]:
    """Normalize metadata and validate its configured values.

    Args:
        metadata: Candidate governance metadata.
        fields: Specs with name, mandatory, kind and allowed_values attributes.
        defaults: System default values keyed by metadata field.
        lookup_values: Current field codes fetched by the adapter.

    Returns:
        A normalized metadata copy and its validation problem list.
    """
    normalized = deepcopy(dict(metadata))
    problems: List[str] = []
    for key, value in defaults.items():
        if not normalized.get(key):
            normalized[key] = value
    violation_map = {
        "HIGH": "H",
        "MEDIUM": "M",
        "LOW": "L",
        "H": "H",
        "M": "M",
        "L": "L",
    }
    raw_violation = (
        str(normalized.get("VIOLATION_LEVEL") or "").strip().upper()
    )
    if raw_violation in violation_map:
        normalized["VIOLATION_LEVEL"] = violation_map[raw_violation]
    risk_map = {
        "HIGH": "H",
        "MEDIUM": "M",
        "LOW": "L",
        "DIRECT": "D",
        "H": "H",
        "M": "M",
        "L": "L",
        "D": "D",
    }
    raw_risk = str(normalized.get("DEGREE_RISK_FLAG") or "").strip().upper()
    if raw_risk in risk_map:
        normalized["DEGREE_RISK_FLAG"] = risk_map[raw_risk]
    active_map = {"ACTIVE": "1", "INACTIVE": "0", "1": "1", "0": "0"}
    raw_active = str(normalized.get("ACTIVE_FLAG") or "1").strip().upper()
    if raw_active in active_map:
        normalized["ACTIVE_FLAG"] = active_map[raw_active]

    for spec in fields:
        value = normalized.get(spec.name)
        if spec.mandatory and (value is None or value == ""):
            problems.append(f"{spec.name} is mandatory but was not provided.")
            continue
        if value is None:
            continue
        if spec.kind == "static_enum" and str(value) not in (
            spec.allowed_values or []
        ):
            problems.append(
                f"{spec.name}='{value}' is not one of the allowed values "
                f"{spec.allowed_values}."
            )
        elif spec.kind == "oracle_lookup":
            valid_codes = set(lookup_values.get(spec.name, ()))
            table = spec.lookup.table if spec.lookup else "configured lookup"
            if not valid_codes:
                problems.append(
                    f"{spec.name}: could not verify '{value}' against {table} "
                    "(lookup query returned no rows — confirm table/column "
                    "configuration)."
                )
            elif str(value) not in valid_codes:
                problems.append(
                    f"{spec.name}='{value}' is not a valid code in {table}."
                )
    return normalized, problems
