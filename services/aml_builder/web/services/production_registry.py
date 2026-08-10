"""
Production Scenario Registry Module.

Manages creation and insertion of confirmed production-grade AML scenarios into the
PIO_AML_PRODUCTION_SCENARIOS table for consumption by the automated daily ETL runner.
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from services.aml_builder.web.services.settings import settings
from services.aml_builder.web.services.oracle import run_readonly, run_write

logger = logging.getLogger(__name__)


def init_production_scenarios_table() -> None:
    """Initialize the PIO_AML_PRODUCTION_SCENARIOS table in Oracle if it does not exist."""
    try:
        _, rows = run_readonly(
            """
            SELECT TABLE_NAME FROM ALL_TABLES
            WHERE UPPER(TABLE_NAME) = 'PIO_AML_PRODUCTION_SCENARIOS'
              AND OWNER = UPPER(:owner)
            """,
            {"owner": settings.ORACLE_USER},
        )
        if not rows:
            logger.info(
                "[PRODUCTION_REGISTRY] Creating table PIO_AML_PRODUCTION_SCENARIOS..."
            )
            create_sql = """
            CREATE TABLE PIO_AML_PRODUCTION_SCENARIOS (
                SCENARIO_ID          VARCHAR2(50) PRIMARY KEY,
                SCENARIO_NAME        VARCHAR2(250) NOT NULL,
                SCENARIO_TYPE        VARCHAR2(50) NOT NULL,
                DETECTION_LOGIC      CLOB,
                RAW_SQL              CLOB NOT NULL,
                TIME_WINDOW_DAYS     NUMBER,
                CREATED_BY           VARCHAR2(100),
                CREATED_AT           TIMESTAMP DEFAULT SYSTIMESTAMP,
                IS_ACTIVE            NUMBER(1) DEFAULT 1
            )
            """
            run_write(create_sql)
            logger.info(
                "[PRODUCTION_REGISTRY] Table PIO_AML_PRODUCTION_SCENARIOS created successfully."
            )
        else:
            logger.info(
                "[PRODUCTION_REGISTRY] Table PIO_AML_PRODUCTION_SCENARIOS verified."
            )
    except Exception as exc:
        logger.error(
            "[PRODUCTION_REGISTRY] Failed to initialize PIO_AML_PRODUCTION_SCENARIOS table: %s",
            exc,
            exc_info=True,
        )


def save_production_scenario(
    scenario_id: str,
    scenario_name: str,
    scenario_type: str,
    detection_logic: str,
    raw_sql: str,
    time_window_days: Optional[int] = None,
    created_by: str = "COMPLIANCE_OFFICER",
) -> bool:
    """Insert or update a confirmed scenario in PIO_AML_PRODUCTION_SCENARIOS.

    Args:
        scenario_id (str): Unique identifier for the scenario.
        scenario_name (str): Human readable scenario name.
        scenario_type (str): Category (e.g. CUSTOMER, ACCOUNT, TRANSACTION).
        detection_logic (str): Plain English logic summary.
        raw_sql (str): Production-grade ANSI Oracle SQL query from Service B.
        time_window_days (Optional[int]): Observation window size in days.
        created_by (str): User identifier who confirmed activation.

    Returns:
        bool: True if insert/update succeeded, False otherwise.
    """
    init_production_scenarios_table()
    try:
        # Check if scenario exists
        _, existing = run_readonly(
            "SELECT SCENARIO_ID FROM PIO_AML_PRODUCTION_SCENARIOS WHERE SCENARIO_ID = :sid",
            {"sid": scenario_id},
        )
        if existing:
            update_sql = """
            UPDATE PIO_AML_PRODUCTION_SCENARIOS
            SET SCENARIO_NAME = :sname,
                SCENARIO_TYPE = :stype,
                DETECTION_LOGIC = :logic,
                RAW_SQL = :sql,
                TIME_WINDOW_DAYS = :twd,
                CREATED_BY = :cby,
                CREATED_AT = SYSTIMESTAMP,
                IS_ACTIVE = 1
            WHERE SCENARIO_ID = :sid
            """
            run_write(
                update_sql,
                {
                    "sid": scenario_id,
                    "sname": scenario_name[:250],
                    "stype": scenario_type[:50],
                    "logic": detection_logic,
                    "sql": raw_sql,
                    "twd": time_window_days,
                    "cby": created_by[:100],
                },
            )
            logger.info(
                "[PRODUCTION_REGISTRY] Updated production scenario SCENARIO_ID=%s",
                scenario_id,
            )
        else:
            insert_sql = """
            INSERT INTO PIO_AML_PRODUCTION_SCENARIOS (
                SCENARIO_ID, SCENARIO_NAME, SCENARIO_TYPE, DETECTION_LOGIC,
                RAW_SQL, TIME_WINDOW_DAYS, CREATED_BY, CREATED_AT, IS_ACTIVE
            ) VALUES (
                :sid, :sname, :stype, :logic, :sql, :twd, :cby, SYSTIMESTAMP, 1
            )
            """
            run_write(
                insert_sql,
                {
                    "sid": scenario_id,
                    "sname": scenario_name[:250],
                    "stype": scenario_type[:50],
                    "logic": detection_logic,
                    "sql": raw_sql,
                    "twd": time_window_days,
                    "cby": created_by[:100],
                },
            )
            logger.info(
                "[PRODUCTION_REGISTRY] Inserted production scenario SCENARIO_ID=%s",
                scenario_id,
            )
        return True
    except Exception as exc:
        logger.error(
            "[PRODUCTION_REGISTRY] Error saving production scenario %s: %s",
            scenario_id,
            exc,
            exc_info=True,
        )
        return False


def get_production_scenarios(is_active_only: bool = True) -> List[Dict[str, Any]]:
    """Retrieve all stored production scenarios for ETL runner.

    Args:
        is_active_only (bool): If True, filter for IS_ACTIVE = 1.

    Returns:
        List[Dict[str, Any]]: List of production scenario records.
    """
    init_production_scenarios_table()
    try:
        where_clause = "WHERE IS_ACTIVE = 1" if is_active_only else ""
        sql = f"""
        SELECT SCENARIO_ID, SCENARIO_NAME, SCENARIO_TYPE, DETECTION_LOGIC,
               RAW_SQL, TIME_WINDOW_DAYS, CREATED_BY, CREATED_AT, IS_ACTIVE
        FROM PIO_AML_PRODUCTION_SCENARIOS
        {where_clause}
        ORDER BY CREATED_AT DESC
        """
        _, rows = run_readonly(sql)
        scenarios = []
        for r in rows:
            scenarios.append(
                {
                    "scenario_id": r[0],
                    "scenario_name": r[1],
                    "scenario_type": r[2],
                    "detection_logic": str(r[3]) if r[3] else "",
                    "raw_sql": str(r[4]) if r[4] else "",
                    "time_window_days": r[5],
                    "created_by": r[6],
                    "created_at": r[7].isoformat() if r[7] else None,
                    "is_active": bool(r[8]),
                }
            )
        return scenarios
    except Exception as exc:
        logger.error(
            "[PRODUCTION_REGISTRY] Error fetching production scenarios: %s", exc
        )
        return []
