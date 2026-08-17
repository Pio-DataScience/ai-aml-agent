"""
PIO_AML_SCENARIO metadata field registry, seeding, live lookup fetching, and
the final validation guard-rail used before persistence.

Phase 2 of the Production Scenario Registry — see
Docs/PIO_AML_SCENARIO_schema_guide.md for the full business rationale.

Every PIO_AML_SCENARIO column falls into one of four population kinds:
  - system_default: filled from settings/config, the officer never sees it.
  - derived:        computed deterministically from AMLIntent (no LLM call).
  - static_enum:    a small fixed set of allowed values, no live query needed.
  - oracle_lookup:   values come from a live Oracle lookup table — the only
                     source of truth for compliance-governed codes.

Adding a new PIO_AML_SCENARIO column is a single new FieldSpec entry in
SCENARIO_METADATA_FIELDS below; nothing else in this module needs to change.
"""

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from services.aml_builder.web.services.oracle import run_readonly
from services.aml_builder.web.services.settings import settings

logger = logging.getLogger(__name__)


# =============================================================================
# LOOKUP TABLE CONFIGURATION (Verified against live Oracle schema)
# =============================================================================

@dataclass(frozen=True)
class LookupTableConfig:
    """Describes how to query a live Oracle lookup table for valid codes.

    Args:
        table (str): Oracle table name.
        code_column (str): Column holding the code value written to PIO_AML_SCENARIO.
        desc_column (str): Column holding the human-readable description.
        filter_by_country_inst (bool): Whether to filter WHERE COUNTRY_CODE=:cc AND INST_CODE=:ic.
    """

    table: str
    code_column: str
    desc_column: str
    filter_by_country_inst: bool = True


# Live Oracle lookup configurations verified against DWH
LOOKUP_CATEG_CODE = LookupTableConfig(
    table="PIO_AML_SCENARIO_CATEGORY",
    code_column="CATEGORY_CODE",
    desc_column="CATEGORY_ENG_NAME",
    filter_by_country_inst=True,
)
LOOKUP_RISK_DEGREE = LookupTableConfig(
    table="PIO_AML_DEGREE_RISK",
    code_column="DEGREE_CODE",
    desc_column="DESC_ENG",
    filter_by_country_inst=True,
)
LOOKUP_SCENARIO_TYPE = LookupTableConfig(
    table="PIO_AML_SCENARIO_TYPE",
    code_column="TYPE_CODE",
    desc_column="TYPE_ENG_NAME",
    filter_by_country_inst=False,
)
LOOKUP_SCENARIO_CLASS = LookupTableConfig(
    table="PIO_AML_SCENARIO_CLASSES",
    code_column="CLASS_CODE",
    desc_column="CLASS_ENG_NAME",
    filter_by_country_inst=False,
)


# =============================================================================
# FIELD SPEC REGISTRY (Mapped to actual PIO_AML_SCENARIO columns)
# =============================================================================


@dataclass(frozen=True)
class FieldSpec:
    """One PIO_AML_SCENARIO column's population rule.

    Args:
        name (str): Column name, matches the PIO_AML_SCENARIO DDL exactly.
        mandatory (bool): True if a null value fails validation at persist time.
        kind (str): 'system_default' | 'derived' | 'static_enum' | 'oracle_lookup'.
        allowed_values (Optional[List[str]]): Valid values for the 'static_enum' kind.
        lookup (Optional[LookupTableConfig]): Lookup source for the 'oracle_lookup' kind.
        description (str): Shown to the frontend side panel / relayed to the user in chat.
    """

    name: str
    mandatory: bool
    kind: str
    allowed_values: Optional[List[str]] = None
    lookup: Optional[LookupTableConfig] = None
    description: str = ""


SCENARIO_METADATA_FIELDS: List[FieldSpec] = [
    FieldSpec("COUNTRY_CODE", True, "system_default", description="Country identifier code."),
    FieldSpec("INST_CODE", True, "system_default", description="Institution identifier code."),
    FieldSpec("SCENARIO_DES_ENG", False, "derived", description="Executive scenario description (English)."),
    FieldSpec("SCENARIO_DES_NAT_LAN", False, "derived", description="Executive scenario description (Native)."),
    FieldSpec(
        "CATEGORY_CODE", True, "oracle_lookup", lookup=LOOKUP_CATEG_CODE,
        description="Typology classification category code (from PIO_AML_SCENARIO_CATEGORY).",
    ),
    FieldSpec(
        "ACTIVE_FLAG", True, "static_enum", allowed_values=["1", "0"],
        description="Enables/disables scenario execution in daily batch runs ('1'=Active, '0'=Inactive).",
    ),
    FieldSpec(
        "DEGREE_RISK_FLAG", True, "oracle_lookup", lookup=LOOKUP_RISK_DEGREE,
        description="Risk degree assigned to alerts fired by this scenario (D/H/M/L from PIO_AML_DEGREE_RISK).",
    ),
    FieldSpec(
        "VIOLATION_LEVEL", True, "static_enum", allowed_values=["H", "M", "L", "HIGH", "MEDIUM", "LOW"],
        description="Breach severity level (H=High, M=Medium, L=Low). Used for automated escalation workflows.",
    ),
    FieldSpec(
        "CLASS_CODE", False, "oracle_lookup", lookup=LOOKUP_SCENARIO_CLASS,
        description="Scenario monitoring class (from PIO_AML_SCENARIO_CLASSES, e.g. 1=Transaction Monitoring).",
    ),
    FieldSpec(
        "SCE_TYPE_CODE", False, "oracle_lookup", lookup=LOOKUP_SCENARIO_TYPE,
        description="Scenario type code (from PIO_AML_SCENARIO_TYPE).",
    ),
    FieldSpec("RUN_FLAG", True, "system_default", description="Execution trigger flag ('1'=Run, '0'=Skip)."),
    FieldSpec("APPROVAL_FLAG", True, "system_default", description="Approval flag ('1'=Approved)."),
    FieldSpec("GROUP_BY_FLAG", True, "system_default", description="Group by entity flag ('1'=True, '0'=False)."),
    FieldSpec("EXCLUDE_EXPL_FLAG", True, "system_default", description="Exclude explanation code flag ('0'=No, '1'=Yes)."),
    FieldSpec("USE_WATCHLIST_FLAG", True, "system_default", description="Watchlist integration flag ('0'=No, '1'=Yes)."),
    FieldSpec("TRANS_WITHOUTTRANS_FLAG", True, "system_default", description="Transactions without transfer flag ('1'=Yes)."),
    FieldSpec("ACTIVE_THRESHOLD_CURR_FLAG", True, "system_default", description="Active threshold currency flag ('0'=No)."),
    FieldSpec("DEFAULT_SCENARIO_FLAG", True, "system_default", description="Default system scenario flag ('0'=Custom)."),
    FieldSpec("USE_WORLDCHECK_FLAG", False, "system_default", description="WorldCheck screening flag ('0'=No)."),
    FieldSpec("CREATED_BY", True, "system_default", description="Author user ID."),
    FieldSpec("UPDATED_BY", True, "system_default", description="Updater user ID."),
]


_UNIT_TO_PERIOD_TYPE: Dict[str, str] = {"DAYS": "D", "MONTHS": "M", "YEARS": "Y"}


def _map_period(time_window: Optional[Dict[str, Any]]) -> Tuple[Optional[str], Optional[int]]:
    """Map AMLIntent.time_window to (PERIOD_TYPE, PERIOD_NUM).

    WEEKS has no direct PIO_AML_SCENARIO.PERIOD_TYPE equivalent (only D/M/Y
    are valid), so it is normalized to a day count instead.

    Args:
        time_window (Optional[Dict[str, Any]]): The AMLIntent.time_window dict.

    Returns:
        Tuple[Optional[str], Optional[int]]: (PERIOD_TYPE, PERIOD_NUM), both
            None if time_window is absent or has no numeric value.
    """
    if not time_window:
        return None, None
    unit = str(time_window.get("unit", "")).upper()
    value = time_window.get("value")
    if value is None:
        return None, None
    if unit == "WEEKS":
        return "D", int(value) * 7
    return _UNIT_TO_PERIOD_TYPE.get(unit), int(value)


def seed_scenario_metadata(
    intent: Dict[str, Any], existing: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Deterministically seed/merge PIO_AML_SCENARIO metadata defaults from AMLIntent.

    Never overwrites a value already present in `existing` — side-panel clicks
    and values filled in prior turns always win over re-derived defaults.

    Args:
        intent (Dict[str, Any]): The current AMLIntent payload as a dict.
        existing (Optional[Dict[str, Any]]): Metadata accumulated so far (from
            side panel clicks and/or prior chat turns), if any.

    Returns:
        Dict[str, Any]: Merged metadata dict, one key per PIO_AML_SCENARIO
            column. Fields that couldn't be derived are simply absent.
    """
    metadata: Dict[str, Any] = dict(existing or {})

    scenario_desc = (intent.get("detection_logic") or intent.get("scenario_name") or "").strip()

    derived_defaults: Dict[str, Any] = {
        "COUNTRY_CODE": settings.AML_COUNTRY_CODE,
        "INST_CODE": settings.AML_INST_CODE,
        "CREATED_BY": settings.AML_CREATED_BY,
        "UPDATED_BY": settings.AML_CREATED_BY,
        "ACTIVE_FLAG": "1",
        "RUN_FLAG": "1",
        "APPROVAL_FLAG": "1",
        "GROUP_BY_FLAG": "1",
        "EXCLUDE_EXPL_FLAG": "0",
        "USE_WATCHLIST_FLAG": "0",
        "TRANS_WITHOUTTRANS_FLAG": "1",
        "ACTIVE_THRESHOLD_CURR_FLAG": "0",
        "DEFAULT_SCENARIO_FLAG": "0",
        "USE_WORLDCHECK_FLAG": "0",
        "CATEGORY_CODE": "999",
        "CLASS_CODE": "1",
        "SCE_TYPE_CODE": "1",
        "DEGREE_RISK_FLAG": "H",
        "VIOLATION_LEVEL": "H",
        "SCENARIO_DES_ENG": scenario_desc[:400] if scenario_desc else None,
        "SCENARIO_DES_NAT_LAN": scenario_desc[:400] if scenario_desc else None,
    }

    for key, value in derived_defaults.items():
        if value is not None and metadata.get(key) is None:
            metadata[key] = value

    return metadata


def fetch_lookup_options(lookup: LookupTableConfig) -> List[Dict[str, str]]:
    """Query a live Oracle lookup table for valid (code, description) pairs.

    Args:
        lookup (LookupTableConfig): Table/column configuration to query.

    Returns:
        List[Dict[str, str]]: [{'code': ..., 'description': ...}, ...]. Empty
            on query failure (e.g. table/columns not yet confirmed) — callers
            must treat an empty list as "cannot verify yet", not "no valid values".
    """
    where_clause = ""
    params: Dict[str, Any] = {}
    if lookup.filter_by_country_inst:
        where_clause = "WHERE COUNTRY_CODE = :cc AND INST_CODE = :ic"
        params = {"cc": settings.AML_COUNTRY_CODE, "ic": settings.AML_INST_CODE}

    sql = f"SELECT DISTINCT {lookup.code_column}, {lookup.desc_column} FROM {lookup.table} {where_clause}"
    try:
        _, rows = run_readonly(sql, params)
        if not rows and lookup.filter_by_country_inst:
            fallback_sql = f"SELECT DISTINCT {lookup.code_column}, {lookup.desc_column} FROM {lookup.table}"
            _, rows = run_readonly(fallback_sql)

        return [
            {"code": str(r[0]).strip(), "description": str(r[1]).strip() if r[1] else str(r[0]).strip()}
            for r in rows
            if r[0] is not None
        ]
    except Exception as exc:
        logger.error(
            "[SCENARIO_METADATA] Failed to fetch lookup options from %s: %s.",
            lookup.table,
            exc,
        )
        return []


def build_metadata_catalog(metadata: Dict[str, Any]) -> Dict[str, Any]:
    """Build the full field-by-field catalog for the side panel / chat fallback.

    For every static_enum or oracle_lookup field, the currently valid options
    are attached so the frontend can render pickers or the agent can present
    them in chat.

    Args:
        metadata (Dict[str, Any]): Current accumulated metadata state.

    Returns:
        Dict[str, Any]: {
            'metadata': <current merged values>,
            'fields': [{name, mandatory, kind, description, value, options?}, ...],
            'missing_mandatory_fields': [...],
            'ready_for_persistence': bool,
        }
    """
    fields: List[Dict[str, Any]] = []
    missing: List[str] = []

    for spec in SCENARIO_METADATA_FIELDS:
        value = metadata.get(spec.name)
        entry: Dict[str, Any] = {
            "name": spec.name,
            "mandatory": spec.mandatory,
            "kind": spec.kind,
            "description": spec.description,
            "value": value,
        }
        if spec.kind == "static_enum":
            entry["options"] = [{"code": v, "description": v} for v in (spec.allowed_values or [])]
        elif spec.kind == "oracle_lookup" and spec.lookup:
            entry["options"] = fetch_lookup_options(spec.lookup)

        fields.append(entry)
        if spec.mandatory and (value is None or value == ""):
            missing.append(spec.name)

    return {
        "metadata": metadata,
        "fields": fields,
        "missing_mandatory_fields": missing,
        "ready_for_persistence": not missing,
    }


def validate_scenario_metadata(metadata: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """Final validation guard-rail — the only gate before writing PIO_AML_SCENARIO.

    Every mandatory field must be non-null, and every static_enum/oracle_lookup
    field's value must exactly match one of its currently valid values.

    Args:
        metadata (Dict[str, Any]): The fully merged metadata dict.

    Returns:
        Tuple[bool, List[str]]: (True, []) if valid, else (False, [problem descriptions]).
    """
    problems: List[str] = []

    # Ensure system defaults exist
    if not metadata.get("COUNTRY_CODE"):
        metadata["COUNTRY_CODE"] = settings.AML_COUNTRY_CODE
    if not metadata.get("INST_CODE"):
        metadata["INST_CODE"] = settings.AML_INST_CODE
    if not metadata.get("CREATED_BY"):
        metadata["CREATED_BY"] = settings.AML_CREATED_BY
    if not metadata.get("UPDATED_BY"):
        metadata["UPDATED_BY"] = settings.AML_CREATED_BY

    # Normalize friendly names for VIOLATION_LEVEL
    viol_map = {"HIGH": "H", "MEDIUM": "M", "LOW": "L", "H": "H", "M": "M", "L": "L"}
    viol_raw = str(metadata.get("VIOLATION_LEVEL") or "").strip().upper()
    if viol_raw in viol_map:
        metadata["VIOLATION_LEVEL"] = viol_map[viol_raw]

    # Normalize friendly names for DEGREE_RISK_FLAG
    risk_map = {"HIGH": "H", "MEDIUM": "M", "LOW": "L", "DIRECT": "D", "H": "H", "M": "M", "L": "L", "D": "D"}
    risk_raw = str(metadata.get("DEGREE_RISK_FLAG") or "").strip().upper()
    if risk_raw in risk_map:
        metadata["DEGREE_RISK_FLAG"] = risk_map[risk_raw]

    # Normalize ACTIVE_FLAG
    active_map = {"ACTIVE": "1", "INACTIVE": "0", "1": "1", "0": "0"}
    active_raw = str(metadata.get("ACTIVE_FLAG") or "1").strip().upper()
    if active_raw in active_map:
        metadata["ACTIVE_FLAG"] = active_map[active_raw]

    for spec in SCENARIO_METADATA_FIELDS:
        value = metadata.get(spec.name)

        if spec.mandatory and (value is None or value == ""):
            problems.append(f"{spec.name} is mandatory but was not provided.")
            continue

        if value is None:
            continue

        if spec.kind == "static_enum":
            if str(value) not in (spec.allowed_values or []):
                problems.append(
                    f"{spec.name}='{value}' is not one of the allowed values {spec.allowed_values}."
                )
        elif spec.kind == "oracle_lookup" and spec.lookup:
            valid_codes = {opt["code"] for opt in fetch_lookup_options(spec.lookup)}
            if not valid_codes:
                problems.append(
                    f"{spec.name}: could not verify '{value}' against {spec.lookup.table} "
                    f"(lookup query returned no rows — confirm table/column configuration)."
                )
            elif str(value) not in valid_codes:
                problems.append(f"{spec.name}='{value}' is not a valid code in {spec.lookup.table}.")

    return (len(problems) == 0, problems)
