"""
Standalone integration check for scenario_registry_analytics.py.

Exercises all 4 query modes against a LIVE Oracle connection. This is NOT a
mocked unit test — it requires real ORACLE_DSN/ORACLE_USER/ORACLE_PASSWORD
(and OPENAI_API_KEY for the semantic search embedding call) configured in
.env, and is intended to be run manually against a real DWH once persisted
scenarios exist.

Usage:
    PYTHONPATH=services/aml_builder python services/aml_builder/tests/test_registry_analytics.py
"""

import json
import sys
from typing import Any, Dict, Optional

from services.aml_builder.web.services.oracle import init_pool, close_pool
from services.aml_builder.web.services.scenario_registry_analytics import (
    search_scenarios_semantic,
    get_registry_statistics,
    get_alert_telemetry,
    get_scenario_full_details,
)


def _print_result(label: str, result: Any) -> None:
    """Pretty-print one check's result under a labeled header.

    Args:
        label (str): Human-readable name of the check.
        result (Any): The JSON-serializable result to print.
    """
    print(f"\n{'=' * 70}\n{label}\n{'=' * 70}")
    print(json.dumps(result, indent=2, default=str, ensure_ascii=False))


def run_all_checks() -> bool:
    """Run all 4 query modes against the live database and report pass/fail.

    Each mode is exercised independently so one failure doesn't prevent the
    others from being checked. get_scenario_full_details is only exercised
    if get_registry_statistics reports at least one existing scenario.

    Returns:
        bool: True if every attempted check executed without raising, False otherwise.
    """
    all_passed = True
    stats: Dict[str, Any] = {}

    try:
        stats = get_registry_statistics()
        _print_result("get_registry_statistics()", stats)
    except Exception as exc:
        print(f"[FAIL] get_registry_statistics raised: {exc}")
        all_passed = False

    try:
        search_results = search_scenarios_semantic("cash deposits over a threshold", limit=5)
        _print_result("search_scenarios_semantic('cash deposits over a threshold')", search_results)
    except Exception as exc:
        print(f"[FAIL] search_scenarios_semantic raised: {exc}")
        all_passed = False
        search_results = {}

    try:
        telemetry = get_alert_telemetry(limit=5)
        _print_result("get_alert_telemetry() [no filters]", telemetry)
    except Exception as exc:
        print(f"[FAIL] get_alert_telemetry raised: {exc}")
        all_passed = False

    try:
        bad_date_telemetry = get_alert_telemetry(date_from="not-a-date")
        print(f"[FAIL] get_alert_telemetry did not raise on a malformed date: {bad_date_telemetry}")
        all_passed = False
    except ValueError as exc:
        print(f"\n[OK] get_alert_telemetry correctly rejected a malformed date: {exc}")

    sample_scenario_id: Optional[str] = None
    if stats.get("total_scenarios", 0) > 0:
        results = (search_results or {}).get("results") or []
        if results:
            sample_scenario_id = results[0]["scenario_id"]

    if sample_scenario_id:
        try:
            detail = get_scenario_full_details(sample_scenario_id)
            _print_result(f"get_scenario_full_details('{sample_scenario_id}')", detail)
            if detail is None:
                print(f"[FAIL] get_scenario_full_details returned None for a known scenario_id")
                all_passed = False
        except Exception as exc:
            print(f"[FAIL] get_scenario_full_details raised: {exc}")
            all_passed = False
    else:
        print("\n[SKIP] get_scenario_full_details — no persisted scenarios found to look up.")

    try:
        missing_detail = get_scenario_full_details("PRD_DOES_NOT_EXIST")
        if missing_detail is not None:
            print("[FAIL] get_scenario_full_details should return None for an unknown scenario_id")
            all_passed = False
        else:
            print("[OK] get_scenario_full_details correctly returned None for an unknown scenario_id")
    except Exception as exc:
        print(f"[FAIL] get_scenario_full_details raised on unknown id: {exc}")
        all_passed = False

    return all_passed


if __name__ == "__main__":
    init_pool()
    try:
        passed = run_all_checks()
    finally:
        close_pool()

    print(f"\n{'=' * 70}")
    print("ALL CHECKS PASSED" if passed else "SOME CHECKS FAILED — see [FAIL] lines above")
    print(f"{'=' * 70}")
    sys.exit(0 if passed else 1)
