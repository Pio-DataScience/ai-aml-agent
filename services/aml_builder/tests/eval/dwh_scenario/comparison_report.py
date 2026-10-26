"""Offline side-by-side report for the B0 synthetic scenario evaluation set."""

from __future__ import annotations

from pathlib import Path

from services.aml_builder.tests.eval.dwh_scenario.runner import (
    evaluate,
    load_cases,
    stub_generator,
)


def main() -> None:
    """Print aggregate HTTP/local structural scores using deterministic fakes."""
    cases = load_cases(Path(__file__).with_name("cases.jsonl"))
    # Independent invocations model the two adapters while keeping this command
    # entirely offline; the same cases and scorer are used for each side.
    results = {
        "HTTP fake": evaluate(cases, stub_generator),
        "Local fake": evaluate(cases, stub_generator),
    }
    metrics = (
        "sql_parses", "select_only", "projection_present",
        "confirmed_filters_present", "execution_success",
    )
    print(f"{'Metric':34} {'HTTP fake':>12} {'Local fake':>12}")
    for metric in metrics:
        columns = [
            sum(bool(item[metric]) for item in results[label])
            for label in results
        ]
        print(f"{metric:34} {columns[0]:>8}/{len(cases):<3} {columns[1]:>8}/{len(cases):<3}")


if __name__ == "__main__":
    main()
