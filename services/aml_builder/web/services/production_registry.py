"""Read-only compatibility exports for the registry-v2 repository."""

from __future__ import annotations

from typing import Any

from services.aml_builder.web.adapters.oracle_repo import OracleRegistryRepo
from services.aml_builder.web.services.oracle import run_registry_readonly as run_readonly


def init_production_scenarios_table() -> None:
    """Ensure the versioned registry schema exists without altering old tables.

    Raises:
        RegistrySchemaError: When an agent-owned registry table has an incompatible shape.
    """
    OracleRegistryRepo().ensure_schema()


def init_scenario_metadata_table() -> None:
    """Retain the legacy initializer name without creating shared governance tables."""
    return None


def save_production_scenario(*args: Any, **kwargs: Any) -> bool:
    """Reject legacy in-place writes; all writes must pass through DeployService.

    Raises:
        RuntimeError: Always, to prevent bypassing versioning and audit.
    """
    del args, kwargs
    raise RuntimeError(
        "Legacy registry writes are disabled. Use the versioned DeployService."
    )


def get_production_scenarios(is_active_only: bool = True) -> list[dict[str, Any]]:
    """Read registry rows for the legacy alert-engine compatibility contract.

    Args:
        is_active_only: Whether to return only active versions.

    Returns:
        list[dict[str, Any]]: Active or all scenario records.
    """
    predicate = "WHERE IS_ACTIVE=1" if is_active_only else ""
    columns, rows = run_readonly(
        f"""SELECT SCENARIO_ID, SCENARIO_NAME, SCENARIO_TYPE, DETECTION_LOGIC,
                   RAW_SQL, TIME_WINDOW_DAYS, CREATED_BY, CREATED_AT, IS_ACTIVE
            FROM PIO_AML_AGENT_PRODUCTION_SCENARIOS {predicate}
            ORDER BY CREATED_AT DESC"""
    )
    result = []
    for row in rows:
        values = dict(zip(columns, row))
        raw_sql = values.get("raw_sql")
        if hasattr(raw_sql, "read"):
            raw_sql = raw_sql.read()
        logic = values.get("detection_logic")
        if hasattr(logic, "read"):
            logic = logic.read()
        result.append(
            {
                "scenario_id": values.get("scenario_id"),
                "scenario_name": values.get("scenario_name"),
                "scenario_type": values.get("scenario_type"),
                "detection_logic": str(logic or ""),
                "raw_sql": str(raw_sql or ""),
                "time_window_days": values.get("time_window_days"),
                "created_by": values.get("created_by"),
                "created_at": (
                    values["created_at"].isoformat()
                    if values.get("created_at")
                    else None
                ),
                "is_active": bool(values.get("is_active")),
            }
        )
    return result
