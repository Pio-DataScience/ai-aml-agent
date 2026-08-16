"""
Production Scenario Registry Module.

Manages creation and insertion of confirmed production-grade AML scenarios into
two tables, written atomically in a single transaction:
  - PIO_AML_PRODUCTION_SCENARIOS: the RAW_SQL consumed by the automated daily
    ETL runner.
  - PIO_AML_SCENARIO: business metadata (risk degree, category, period, etc.)
    consumed by downstream compliance UI/workflow modules — see
    Docs/PIO_AML_SCENARIO_schema_guide.md.

Both tables share the same SCENARIO_CODE/SCENARIO_ID as their key, so a
consumer can always join one to the other.
"""

import logging
from typing import Any, Dict, List, Optional

from services.aml_builder.web.services.settings import settings
from services.aml_builder.web.services.oracle import run_readonly, run_write, atomic_connection

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


def init_scenario_metadata_table() -> None:
    """Initialize the PIO_AML_SCENARIO table in Oracle if it does not exist.

    See Docs/PIO_AML_SCENARIO_schema_guide.md for the field-by-field
    rationale — this table is the business metadata registry consumed by
    downstream compliance UI/workflow modules, not by SQL generation.
    """
    try:
        _, rows = run_readonly(
            """
            SELECT TABLE_NAME FROM ALL_TABLES
            WHERE UPPER(TABLE_NAME) = 'PIO_AML_SCENARIO'
              AND OWNER = UPPER(:owner)
            """,
            {"owner": settings.ORACLE_USER},
        )
        if not rows:
            logger.info("[PRODUCTION_REGISTRY] Creating table PIO_AML_SCENARIO...")
            create_sql = """
            CREATE TABLE PIO_AML_SCENARIO (
                COUNTRY_CODE               NUMBER NOT NULL,
                INST_CODE                  NUMBER NOT NULL,
                SCENARIO_CODE              VARCHAR2(40) NOT NULL,
                SCENARIO_DES_ENG           VARCHAR2(400),
                SCENARIO_DES_NAT_LAN       VARCHAR2(400),
                ACTIVE_FLAG                VARCHAR2(40) DEFAULT '1',
                EXCLUDE_EXPL_FLAG          VARCHAR2(40) DEFAULT '0',
                USE_WATCHLIST_FLAG         VARCHAR2(40) DEFAULT '0',
                VIOLATION_LEVEL            VARCHAR2(40) DEFAULT 'H',
                DEGREE_RISK_FLAG           VARCHAR2(40) DEFAULT 'H',
                DEFAULT_SCENARIO_FLAG      VARCHAR2(40) DEFAULT '0',
                RUN_FLAG                   VARCHAR2(40) DEFAULT '1',
                APPROVAL_FLAG              VARCHAR2(40) DEFAULT '1',
                GROUP_BY_FLAG              VARCHAR2(40) DEFAULT '1',
                USE_WORLDCHECK_FLAG        VARCHAR2(200) DEFAULT '0',
                WORLDCHECK_GROUP_ID        VARCHAR2(200),
                CREATED_BY                 NUMBER,
                CREATED_DATE               DATE DEFAULT SYSDATE,
                UPDATED_BY                 NUMBER,
                UPDATED_DATE               DATE DEFAULT SYSDATE,
                TRANS_WITHOUTTRANS_FLAG    VARCHAR2(40) DEFAULT '1',
                CATEGORY_CODE              VARCHAR2(40) DEFAULT '999',
                ACTIVE_THRESHOLD_CURR_FLAG VARCHAR2(40) DEFAULT '0',
                SCE_TYPE_CODE              VARCHAR2(40) DEFAULT '1',
                CLASS_CODE                 VARCHAR2(40) DEFAULT '1',
                PRIMARY KEY (COUNTRY_CODE, INST_CODE, SCENARIO_CODE)
            )
            """
            run_write(create_sql)
            logger.info("[PRODUCTION_REGISTRY] Table PIO_AML_SCENARIO created successfully.")
        else:
            logger.info("[PRODUCTION_REGISTRY] Table PIO_AML_SCENARIO verified.")
    except Exception as exc:
        logger.error(
            "[PRODUCTION_REGISTRY] Failed to initialize PIO_AML_SCENARIO table: %s",
            exc,
            exc_info=True,
        )


def save_production_scenario(
    scenario_id: str,
    scenario_name: str,
    scenario_type: str,
    detection_logic: str,
    raw_sql: str,
    scenario_metadata: Dict[str, Any],
    time_window_days: Optional[int] = None,
    created_by: str = "COMPLIANCE_OFFICER",
) -> bool:
    """Insert or update a confirmed scenario across both registry tables atomically.

    Writes PIO_AML_PRODUCTION_SCENARIOS (the RAW_SQL) and PIO_AML_SCENARIO
    (business metadata) in a single Oracle transaction — either both succeed
    and commit, or both roll back. `scenario_metadata` must already have
    passed `scenario_metadata.validate_scenario_metadata()`; this function
    does not re-validate it.

    Args:
        scenario_id (str): Unique identifier, shared as SCENARIO_ID on
            PIO_AML_PRODUCTION_SCENARIOS and SCENARIO_CODE on PIO_AML_SCENARIO.
        scenario_name (str): Human readable scenario name.
        scenario_type (str): Category (e.g. CUSTOMER, ACCOUNT, TRANSACTION).
        detection_logic (str): Plain English logic summary.
        raw_sql (str): Production-grade ANSI Oracle SQL query from Service B.
        scenario_metadata (Dict[str, Any]): Validated PIO_AML_SCENARIO field
            values — see scenario_metadata.SCENARIO_METADATA_FIELDS for the
            expected keys (COUNTRY_CODE, INST_CODE, CATEG_CODE, RISK_DEGREE, etc.).
        time_window_days (Optional[int]): Observation window size in days.
        created_by (str): User identifier written to PIO_AML_PRODUCTION_SCENARIOS.CREATED_BY.

    Returns:
        bool: True if both writes succeeded and were committed, False if the
            transaction failed and was rolled back on both tables.
    """
    init_production_scenarios_table()
    init_scenario_metadata_table()

    try:
        with atomic_connection() as conn:
            cursor = conn.cursor()

            # ---- 1. PIO_AML_PRODUCTION_SCENARIOS (RAW_SQL) ----
            cursor.execute(
                "SELECT SCENARIO_ID FROM PIO_AML_PRODUCTION_SCENARIOS WHERE SCENARIO_ID = :sid",
                {"sid": scenario_id},
            )
            existing_prod = cursor.fetchone()

            prod_params = {
                "sid": scenario_id,
                "sname": scenario_name[:250],
                "stype": scenario_type[:50],
                "logic": detection_logic,
                "sql": raw_sql,
                "twd": time_window_days,
                "cby": created_by[:100],
            }
            if existing_prod:
                cursor.execute(
                    """
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
                    """,
                    prod_params,
                )
            else:
                cursor.execute(
                    """
                    INSERT INTO PIO_AML_PRODUCTION_SCENARIOS (
                        SCENARIO_ID, SCENARIO_NAME, SCENARIO_TYPE, DETECTION_LOGIC,
                        RAW_SQL, TIME_WINDOW_DAYS, CREATED_BY, CREATED_AT, IS_ACTIVE
                    ) VALUES (
                        :sid, :sname, :stype, :logic, :sql, :twd, :cby, SYSTIMESTAMP, 1
                    )
                    """,
                    prod_params,
                )

            # ---- 2. PIO_AML_SCENARIO (business metadata) ----
            country_code = int(scenario_metadata.get("COUNTRY_CODE") or settings.AML_COUNTRY_CODE)
            inst_code = int(scenario_metadata.get("INST_CODE") or settings.AML_INST_CODE)

            cursor.execute(
                """
                SELECT SCENARIO_CODE FROM PIO_AML_SCENARIO
                WHERE COUNTRY_CODE = :cc AND INST_CODE = :ic AND SCENARIO_CODE = :sid
                """,
                {"cc": country_code, "ic": inst_code, "sid": scenario_id},
            )
            existing_scenario = cursor.fetchone()

            des_eng = (
                scenario_metadata.get("SCENARIO_DES_ENG")
                or scenario_metadata.get("SCENARIO_DESC")
                or scenario_name
            )[:400]
            des_nat = (
                scenario_metadata.get("SCENARIO_DES_NAT_LAN")
                or des_eng
            )[:400]

            viol_raw = str(scenario_metadata.get("VIOLATION_LEVEL") or "H").upper()
            viol_map = {"HIGH": "H", "MEDIUM": "M", "LOW": "L"}
            viol = viol_map.get(viol_raw, viol_raw[:1] if viol_raw else "H")

            degree_risk = str(
                scenario_metadata.get("DEGREE_RISK_FLAG")
                or scenario_metadata.get("RISK_DEGREE")
                or "H"
            )[:40]
            category_code = str(
                scenario_metadata.get("CATEGORY_CODE")
                or scenario_metadata.get("CATEG_CODE")
                or "999"
            )[:40]
            class_code = str(scenario_metadata.get("CLASS_CODE") or "1")[:40]
            sce_type = str(scenario_metadata.get("SCE_TYPE_CODE") or "1")[:40]
            active_flag = str(scenario_metadata.get("ACTIVE_FLAG") or "1")[:40]
            cby = int(scenario_metadata.get("CREATED_BY") or settings.AML_CREATED_BY or 999)

            scenario_params = {
                "cc": country_code,
                "ic": inst_code,
                "sid": scenario_id[:40],
                "des_eng": des_eng,
                "des_nat": des_nat,
                "active": active_flag,
                "viol": viol,
                "degree_risk": degree_risk,
                "category_code": category_code,
                "class_code": class_code,
                "sce_type": sce_type,
                "cby": cby,
            }

            if existing_scenario:
                cursor.execute(
                    """
                    UPDATE PIO_AML_SCENARIO
                    SET SCENARIO_DES_ENG = :des_eng,
                        SCENARIO_DES_NAT_LAN = :des_nat,
                        ACTIVE_FLAG = :active,
                        RUN_FLAG = :active,
                        VIOLATION_LEVEL = :viol,
                        DEGREE_RISK_FLAG = :degree_risk,
                        CATEGORY_CODE = :category_code,
                        CLASS_CODE = :class_code,
                        SCE_TYPE_CODE = :sce_type,
                        UPDATED_BY = :cby,
                        UPDATED_DATE = SYSDATE
                    WHERE COUNTRY_CODE = :cc AND INST_CODE = :ic AND SCENARIO_CODE = :sid
                    """,
                    scenario_params,
                )
            else:
                cursor.execute(
                    """
                    INSERT INTO PIO_AML_SCENARIO (
                        COUNTRY_CODE, INST_CODE, SCENARIO_CODE,
                        SCENARIO_DES_ENG, SCENARIO_DES_NAT_LAN,
                        ACTIVE_FLAG, EXCLUDE_EXPL_FLAG, USE_WATCHLIST_FLAG,
                        VIOLATION_LEVEL, DEGREE_RISK_FLAG,
                        DEFAULT_SCENARIO_FLAG, RUN_FLAG, APPROVAL_FLAG, GROUP_BY_FLAG,
                        USE_WORLDCHECK_FLAG, WORLDCHECK_GROUP_ID,
                        CREATED_BY, CREATED_DATE, UPDATED_BY, UPDATED_DATE,
                        TRANS_WITHOUTTRANS_FLAG, CATEGORY_CODE,
                        ACTIVE_THRESHOLD_CURR_FLAG, SCE_TYPE_CODE, CLASS_CODE
                    ) VALUES (
                        :cc, :ic, :sid,
                        :des_eng, :des_nat,
                        :active, '0', '0',
                        :viol, :degree_risk,
                        '0', :active, '1', '1',
                        '0', NULL,
                        :cby, SYSDATE, :cby, SYSDATE,
                        '1', :category_code,
                        '0', :sce_type, :class_code
                    )
                    """,
                    scenario_params,
                )

        logger.info(
            "[PRODUCTION_REGISTRY] Persisted scenario SCENARIO_CODE=%s atomically across "
            "PIO_AML_PRODUCTION_SCENARIOS and PIO_AML_SCENARIO.",
            scenario_id,
        )
        return True
    except Exception as exc:
        logger.error(
            "[PRODUCTION_REGISTRY] Atomic write failed for scenario %s — both tables rolled back: %s",
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
