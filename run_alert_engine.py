#!/usr/bin/env python
"""
Command Line Interface (CLI) for the Standalone AML Alert Execution Engine.

Usage:
  # Run all active scenarios for today's evaluation snapshot
  python run_alert_engine.py

  # Run a specific scenario by ID
  python run_alert_engine.py --scenario-id PRD_7EB9CBAD

  # Run for a specific historical simulation date
  python run_alert_engine.py --date 2026-08-17
"""

import argparse
import sys
from datetime import datetime

from services.aml_builder.web.services.alert_engine import run_scenario_alert_engine
from services.aml_builder.web.services.oracle import init_pool, init_shadow_pool
from services.aml_builder.web.services.logging_config import setup_logging


def main():
    parser = argparse.ArgumentParser(
        description="Standalone Daily AML Alert Execution Engine & Alert Populator."
    )
    parser.add_argument(
        "--scenario-id",
        type=str,
        default=None,
        help="Optional specific scenario ID to execute (e.g. PRD_7EB9CBAD).",
    )
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="Optional evaluation date in YYYY-MM-DD format (defaults to current date).",
    )

    args = parser.parse_args()

    setup_logging()
    init_pool()
    init_shadow_pool()

    eval_date = None
    if args.date:
        try:
            eval_date = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            print(f"Error: Invalid date format '{args.date}'. Expected YYYY-MM-DD.")
            sys.exit(1)

    print("=" * 70)
    print("[AML BATCH ALERT ENGINE RUNNER]")
    print(f"Target Evaluation Date: {eval_date or 'CURRENT_DATE (Today)'}")
    print(f"Filter Scenario ID:     {args.scenario_id or 'ALL ACTIVE SCENARIOS'}")
    print("=" * 70)

    summary = run_scenario_alert_engine(
        scenario_id=args.scenario_id, eval_date=eval_date
    )

    print("\n" + "=" * 70)
    print("[EXECUTION SUMMARY]")
    print("=" * 70)
    print(f"Scenarios Evaluated:         {summary['scenarios_evaluated']}")
    print(f"Scenarios Succeeded:         {summary['scenarios_succeeded']}")
    print(f"Scenarios Failed:            {summary['scenarios_failed']}")
    print(f"Header Alerts Generated:     {summary['total_customer_alerts_created']} (PIO_AML_CUSTOMERS)")
    print(f"Detail Alerts Generated:     {summary['total_detail_alerts_created']} (PIO_AML_CUSTOMERS_DET)")
    print(f"Elapsed Execution Time:      {summary['elapsed_seconds']}s")
    print("-" * 70)

    for sc in summary["scenarios"]:
        status_icon = "[SUCCESS]" if sc["status"] == "SUCCESS" else "[FAILED]"
        print(
            f"{status_icon} [{sc['scenario_id']}] {sc['scenario_name']}: "
            f"rows={sc.get('rows_matched', 0)}, "
            f"headers={sc.get('customer_alerts_created', 0)}, "
            f"details={sc.get('detail_alerts_created', 0)}"
        )
        if sc.get("error"):
            print(f"   [ERROR]: {sc['error']}")

    print("=" * 70)


if __name__ == "__main__":
    main()
