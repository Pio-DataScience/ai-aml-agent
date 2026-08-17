"""
Standalone Daily Alert Execution Engine & Alert Populator.

Phase 3 of the Production Scenario Registry:
1. Discovers active scenarios in PIO_AML_PRODUCTION_SCENARIOS joined with PIO_AML_SCENARIO.
2. Executes their production RAW_SQL queries against BI_DWH.
3. Automatically classifies output (Transactional Activity vs Profile/Account).
4. Atomically populates PIO_AML_CUSTOMERS (Header Alerts) and PIO_AML_CUSTOMERS_DET (Evidence Details).
5. Guarantees sequence PK safety and handles non-null DDL constraints.
"""

import logging
import time
from datetime import datetime, date
from typing import Any, Dict, List, Optional, Tuple

from services.aml_builder.web.services.oracle import (
    atomic_connection,
    run_readonly,
    run_shadow_readonly,
)
from services.aml_builder.web.services.settings import settings

logger = logging.getLogger(__name__)


def get_active_scenarios(scenario_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Retrieve active scenarios from PIO_AML_PRODUCTION_SCENARIOS joined with PIO_AML_SCENARIO.

    Args:
        scenario_id (Optional[str]): If provided, only fetch this specific scenario.

    Returns:
        List[Dict[str, Any]]: List of scenario definition dictionaries.
    """
    sql = """
    SELECT 
        p.SCENARIO_ID,
        p.SCENARIO_NAME,
        p.SCENARIO_TYPE,
        p.RAW_SQL,
        p.TIME_WINDOW_DAYS,
        s.COUNTRY_CODE,
        s.INST_CODE,
        s.SCENARIO_DES_ENG,
        s.DEGREE_RISK_FLAG,
        s.CATEGORY_CODE,
        s.VIOLATION_LEVEL,
        s.ACTIVE_FLAG
    FROM PIO_AML_PRODUCTION_SCENARIOS p
    LEFT JOIN PIO_AML_SCENARIO s 
        ON p.SCENARIO_ID = s.SCENARIO_CODE
    WHERE p.IS_ACTIVE = 1
    """
    params: Dict[str, Any] = {}
    if scenario_id:
        sql += " AND p.SCENARIO_ID = :sid"
        params["sid"] = scenario_id.strip()
    else:
        sql += " AND NVL(s.ACTIVE_FLAG, '1') = '1'"

    sql += " ORDER BY p.CREATED_AT DESC"

    try:
        cols, rows = run_readonly(sql, params)
        scenarios: List[Dict[str, Any]] = []
        for row in rows:
            raw_sql_val = row[3]
            if hasattr(raw_sql_val, "read"):
                raw_sql_str = raw_sql_val.read()
            else:
                raw_sql_str = str(raw_sql_val) if raw_sql_val else ""

            scenarios.append(
                {
                    "scenario_id": str(row[0]),
                    "scenario_name": str(row[1] or ""),
                    "scenario_type": str(row[2] or "CUSTOMER"),
                    "raw_sql": raw_sql_str,
                    "time_window_days": row[4],
                    "country_code": int(row[5] or settings.AML_COUNTRY_CODE),
                    "inst_code": int(row[6] or settings.AML_INST_CODE),
                    "scenario_desc": str(row[7] or row[1] or ""),
                    "degree_risk": str(row[8] or "H"),
                    "category_code": str(row[9] or "999"),
                    "violation_level": str(row[10] or "H"),
                    "active_flag": str(row[11] or "1"),
                }
            )
        return scenarios
    except Exception as exc:
        logger.error("[ALERT_ENGINE] Failed to fetch active scenarios: %s", exc, exc_info=True)
        return []


def execute_scenario_query(
    raw_sql: str, eval_date: Optional[date] = None
) -> Tuple[List[str], List[Tuple[Any, ...]]]:
    """Execute scenario RAW_SQL against BI_DWH shadow database.

    Args:
        raw_sql (str): SQL statement to execute.
        eval_date (Optional[date]): Target evaluation date. If supplied and different
            from current date, replaces TRUNC(SYSDATE) with target date literal.

    Returns:
        Tuple[List[str], List[Tuple[Any, ...]]]: (column_names, rows).
    """
    executable_sql = raw_sql.strip()
    if eval_date:
        date_str = eval_date.strftime("%Y-%m-%d")
        # Replace TRUNC(SYSDATE) with date literal for historical simulation
        executable_sql = executable_sql.replace(
            "TRUNC(SYSDATE)", f"DATE '{date_str}'"
        ).replace("SYSDATE", f"DATE '{date_str}'")

    logger.info("[ALERT_ENGINE] Executing scenario query on BI_DWH (length=%d chars)...", len(executable_sql))
    try:
        cols, rows = run_shadow_readonly(executable_sql)
        logger.info("[ALERT_ENGINE] Query returned %d rows, %d columns.", len(rows), len(cols))
        return [c.upper() for c in cols], rows
    except Exception as exc:
        logger.error("[ALERT_ENGINE] Error executing query against shadow pool: %s", exc)
        # Fallback to primary readonly pool if shadow pool fails
        cols, rows = run_readonly(executable_sql)
        return [c.upper() for c in cols], rows


def _get_next_seq(cursor, table_name: str, key_conditions: Dict[str, Any]) -> int:
    """Calculate the next SEQ integer to prevent ORA-00001 primary key violation.

    Args:
        cursor: Active database cursor.
        table_name (str): PIO_AML_CUSTOMERS or PIO_AML_CUSTOMERS_DET.
        key_conditions (Dict[str, Any]): Dictionary of column: value pairs for grouping.

    Returns:
        int: Next available sequence number (starts at 1).
    """
    where_parts = [f"{col} = :{col}" for col in key_conditions.keys()]
    query = f"SELECT NVL(MAX(SEQ), 0) FROM {table_name} WHERE {' AND '.join(where_parts)}"
    cursor.execute(query, key_conditions)
    res = cursor.fetchone()
    return int(res[0] or 0) + 1 if res else 1


def populate_scenario_alerts(
    scenario: Dict[str, Any],
    columns: List[str],
    rows: List[Tuple[Any, ...]],
    eval_date: Optional[date] = None,
) -> Dict[str, int]:
    """Populate PIO_AML_CUSTOMERS and PIO_AML_CUSTOMERS_DET from query results.

    Args:
        scenario (Dict[str, Any]): Scenario definition and metadata.
        columns (List[str]): Column names returned by query.
        rows (List[Tuple[Any, ...]]): Data rows returned by query.
        eval_date (Optional[date]): Evaluation snapshot date (defaults to today).

    Returns:
        Dict[str, int]: Summary counts {'customer_alerts': int, 'detail_alerts': int}.
    """
    if not rows:
        logger.info(
            "[ALERT_ENGINE] Scenario %s returned 0 rows. No alerts generated.",
            scenario["scenario_id"],
        )
        return {"customer_alerts": 0, "detail_alerts": 0}

    col_map = {col.upper(): idx for idx, col in enumerate(columns)}
    target_date = eval_date or date.today()
    target_datetime = datetime.combine(target_date, datetime.min.time())

    scenario_id = scenario["scenario_id"]
    country_code = scenario["country_code"]
    inst_code = scenario["inst_code"]
    degree_risk = scenario["degree_risk"]
    scenario_desc = scenario["scenario_desc"][:400]

    # Check if transaction details exist in output
    has_transaction_details = "TRA_SEQ1" in col_map or "TRA_DATE" in col_map

    customer_alerts_inserted = 0
    detail_alerts_inserted = 0

    with atomic_connection() as conn:
        cursor = conn.cursor()

        # Group rows by Customer
        cus_idx = col_map.get("CUS_NUM")
        if cus_idx is None:
            logger.error("[ALERT_ENGINE] Query output missing CUS_NUM column! Cannot populate alerts.")
            return {"customer_alerts": 0, "detail_alerts": 0}

        # Unique customers for header alerts
        customer_rows_map: Dict[str, List[Tuple[Any, ...]]] = {}
        for row in rows:
            cus_num = str(row[cus_idx]).strip() if row[cus_idx] is not None else ""
            if cus_num:
                if cus_num not in customer_rows_map:
                    customer_rows_map[cus_num] = []
                customer_rows_map[cus_num].append(row)

        for cus_num, cus_transactions in customer_rows_map.items():
            sample_row = cus_transactions[0]
            day_date_val = sample_row[col_map["DAY_DATE"]] if "DAY_DATE" in col_map and sample_row[col_map["DAY_DATE"]] else target_datetime
            if isinstance(day_date_val, str):
                try:
                    day_date_val = datetime.fromisoformat(day_date_val)
                except Exception:
                    day_date_val = target_datetime

            bra_code_val = str(sample_row[col_map["BRA_CODE"]]) if "BRA_CODE" in col_map and sample_row[col_map["BRA_CODE"]] is not None else None

            # 1. Insert Header Alert into PIO_AML_CUSTOMERS
            next_cus_seq = _get_next_seq(
                cursor,
                "PIO_AML_CUSTOMERS",
                {
                    "COUNTRY_CODE": country_code,
                    "INST_CODE": inst_code,
                    "CUS_NUM": cus_num,
                    "AML_SCENARIO_CODE": scenario_id,
                },
            )

            header_insert_sql = """
            INSERT INTO PIO_AML_CUSTOMERS (
                DAY_DATE, COUNTRY_CODE, INST_CODE, CUS_NUM, AML_SCENARIO_CODE,
                SEQ, RISK_DEGREE, INITIAL_STATUS, FINAL_STATUS, CURRENT_STATUS,
                CURRENT_STEP, WORKCASE, MANUAL_ALERT_DESC, CIF, BRA_CODE,
                CREATION_DATE, EVIDENCE_FLAG
            ) VALUES (
                :day_date, :cc, :ic, :cus_num, :sid,
                :seq, :risk, 'NEW', 'OPEN', 'PENDING',
                'INVESTIGATION', '0', :scen_desc, :cif, :bra,
                SYSDATE, :ev_flag
            )
            """
            cursor.execute(
                header_insert_sql,
                {
                    "day_date": day_date_val,
                    "cc": country_code,
                    "ic": inst_code,
                    "cus_num": cus_num,
                    "sid": scenario_id,
                    "seq": next_cus_seq,
                    "risk": degree_risk,
                    "scen_desc": scenario_desc,
                    "cif": cus_num,
                    "bra": bra_code_val,
                    "ev_flag": "1" if has_transaction_details else "0",
                },
            )
            customer_alerts_inserted += 1

            # 2. Insert Transaction Evidence into PIO_AML_CUSTOMERS_DET (if available)
            if has_transaction_details:
                for t_row in cus_transactions:
                    tra_date_val = t_row[col_map["TRA_DATE"]] if "TRA_DATE" in col_map and t_row[col_map["TRA_DATE"]] else day_date_val
                    tra_day_date_val = t_row[col_map["TRA_DAY_DATE"]] if "TRA_DAY_DATE" in col_map and t_row[col_map["TRA_DAY_DATE"]] else tra_date_val
                    tra_seq1_val = str(t_row[col_map["TRA_SEQ1"]]) if "TRA_SEQ1" in col_map and t_row[col_map["TRA_SEQ1"]] is not None else "1"
                    tra_seq2_val = str(t_row[col_map["TRA_SEQ2"]]) if "TRA_SEQ2" in col_map and t_row[col_map["TRA_SEQ2"]] is not None else "1"
                    bra_code = str(t_row[col_map["BRA_CODE"]]) if "BRA_CODE" in col_map and t_row[col_map["BRA_CODE"]] is not None else "0"
                    cur_code = str(t_row[col_map["CUR_CODE"]]) if "CUR_CODE" in col_map and t_row[col_map["CUR_CODE"]] is not None else "JOD"
                    led_code = str(t_row[col_map["LED_CODE"]]) if "LED_CODE" in col_map and t_row[col_map["LED_CODE"]] is not None else "0"
                    sub_acct = str(t_row[col_map["SUB_ACCT_CODE"]]) if "SUB_ACCT_CODE" in col_map and t_row[col_map["SUB_ACCT_CODE"]] is not None else "0"
                    account_no = str(t_row[col_map["ACCOUNT_NUMBER"]]) if "ACCOUNT_NUMBER" in col_map and t_row[col_map["ACCOUNT_NUMBER"]] is not None else None
                    tra_amt = float(t_row[col_map["TRA_AMT"]]) if "TRA_AMT" in col_map and t_row[col_map["TRA_AMT"]] is not None else None
                    equ_tra_amt = float(t_row[col_map["EQU_TRA_AMT"]]) if "EQU_TRA_AMT" in col_map and t_row[col_map["EQU_TRA_AMT"]] is not None else tra_amt
                    expl_code = str(t_row[col_map["EXPL_CODE"]]) if "EXPL_CODE" in col_map and t_row[col_map["EXPL_CODE"]] is not None else None

                    next_det_seq = _get_next_seq(
                        cursor,
                        "PIO_AML_CUSTOMERS_DET",
                        {
                            "COUNTRY_CODE": country_code,
                            "INST_CODE": inst_code,
                            "CUS_NUM": cus_num,
                            "AML_SCENARIO_CODE": scenario_id,
                        },
                    )

                    det_insert_sql = """
                    INSERT INTO PIO_AML_CUSTOMERS_DET (
                        DAY_DATE, TRA_DAY_DATE, COUNTRY_CODE, INST_CODE,
                        TRA_DATE, TRA_SEQ1, TRA_SEQ2, BRA_CODE, CUS_NUM,
                        CUR_CODE, LED_CODE, SUB_ACCT_CODE, ACCOUNT_NUMBER,
                        AML_RULE_CODE, AML_SCENARIO_CODE, TRA_AMT, EQU_TRA_AMT,
                        EXPL_CODE, SEQ, LINK_CUS_NUM, REL_TYPE, ENTITIY_SIGNATORY, CIF
                    ) VALUES (
                        :day_date, :tra_day_date, :cc, :ic,
                        :tra_date, :tra_seq1, :tra_seq2, :bra, :cus_num,
                        :cur, :led, :sub_acct, :acct,
                        :rule_code, :sid, :amt, :equ_amt,
                        :expl, :seq, :link_cus, 'SELF', 'N/A', :cif
                    )
                    """
                    cursor.execute(
                        det_insert_sql,
                        {
                            "day_date": day_date_val,
                            "tra_day_date": tra_day_date_val,
                            "cc": country_code,
                            "ic": inst_code,
                            "tra_date": tra_date_val,
                            "tra_seq1": tra_seq1_val,
                            "tra_seq2": tra_seq2_val,
                            "bra": bra_code,
                            "cus_num": cus_num,
                            "cur": cur_code,
                            "led": led_code,
                            "sub_acct": sub_acct,
                            "acct": account_no,
                            "rule_code": scenario_id,
                            "sid": scenario_id,
                            "amt": tra_amt,
                            "equ_amt": equ_tra_amt,
                            "expl": expl_code,
                            "seq": next_det_seq,
                            "link_cus": cus_num,
                            "cif": cus_num,
                        },
                    )
                    detail_alerts_inserted += 1

    logger.info(
        "[ALERT_ENGINE] Scenario %s completed: %d customer alerts, %d transaction detail alerts.",
        scenario_id,
        customer_alerts_inserted,
        detail_alerts_inserted,
    )
    return {
        "customer_alerts": customer_alerts_inserted,
        "detail_alerts": detail_alerts_inserted,
    }


def run_scenario_alert_engine(
    scenario_id: Optional[str] = None, eval_date: Optional[date] = None
) -> Dict[str, Any]:
    """Execute all (or specific) active scenarios and populate the alert tables.

    Args:
        scenario_id (Optional[str]): Optional single scenario to run.
        eval_date (Optional[date]): Target evaluation date (defaults to today).

    Returns:
        Dict[str, Any]: Execution summary metrics.
    """
    start_time = time.time()
    active_scenarios = get_active_scenarios(scenario_id=scenario_id)
    target_date = eval_date or date.today()

    logger.info(
        "[ALERT_ENGINE] Starting alert engine run. Target date: %s, Active scenarios found: %d",
        target_date.isoformat(),
        len(active_scenarios),
    )

    results: List[Dict[str, Any]] = []
    total_customers = 0
    total_details = 0
    failures = 0

    for sc in active_scenarios:
        sc_id = sc["scenario_id"]
        sc_name = sc["scenario_name"]
        logger.info("[ALERT_ENGINE] Processing scenario [%s] '%s'...", sc_id, sc_name)

        try:
            cols, rows = execute_scenario_query(sc["raw_sql"], eval_date=target_date)
            pop_counts = populate_scenario_alerts(
                scenario=sc, columns=cols, rows=rows, eval_date=target_date
            )
            c_count = pop_counts["customer_alerts"]
            d_count = pop_counts["detail_alerts"]
            total_customers += c_count
            total_details += d_count
            results.append(
                {
                    "scenario_id": sc_id,
                    "scenario_name": sc_name,
                    "status": "SUCCESS",
                    "rows_matched": len(rows),
                    "customer_alerts_created": c_count,
                    "detail_alerts_created": d_count,
                }
            )
        except Exception as exc:
            failures += 1
            logger.error("[ALERT_ENGINE] Scenario %s failed during execution: %s", sc_id, exc, exc_info=True)
            results.append(
                {
                    "scenario_id": sc_id,
                    "scenario_name": sc_name,
                    "status": "FAILED",
                    "error": str(exc),
                    "customer_alerts_created": 0,
                    "detail_alerts_created": 0,
                }
            )

    elapsed = round(time.time() - start_time, 2)
    summary = {
        "execution_date": target_date.isoformat(),
        "scenarios_evaluated": len(active_scenarios),
        "scenarios_succeeded": len(active_scenarios) - failures,
        "scenarios_failed": failures,
        "total_customer_alerts_created": total_customers,
        "total_detail_alerts_created": total_details,
        "elapsed_seconds": elapsed,
        "scenarios": results,
    }
    logger.info(
        "[ALERT_ENGINE] Run complete in %ss. Total customer alerts: %d, Total detail alerts: %d",
        elapsed,
        total_customers,
        total_details,
    )
    return summary
