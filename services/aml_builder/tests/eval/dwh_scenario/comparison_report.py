"""Offline side-by-side report for the B0 synthetic scenario evaluation set."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from services.aml_builder.tests.eval.dwh_scenario.runner import (
    evaluate,
    load_cases,
    semantic_signature,
    stub_generator,
)


def main() -> None:
    """Print structural scores and semantic parity from distinct offline fakes."""
    cases = load_cases(Path(__file__).with_name("cases.jsonl"))

    def local_fake(case: dict[str, Any]) -> dict[str, Any]:
        """Produce the stub SQL with predicate order and formatting varied."""
        result = stub_generator(case)
        sql = result["sql"]
        if " AND " in sql:
            head, where = sql.split(" WHERE ", 1)
            result["sql"] = (
                head + " WHERE " + " AND ".join(reversed(where.split(" AND ")))
            )
        else:
            result["sql"] = sql.replace("SELECT CUS_NUM", "SELECT  CUS_NUM")
        return result

    results = {
        "HTTP fake": evaluate(cases, stub_generator),
        "Local fake": evaluate(cases, local_fake),
    }
    metrics = (
        "sql_parses",
        "select_only",
        "projection_present",
        "confirmed_filters_present",
        "window_semantics",
        "baseline_semantics",
        "forbidden_constructs_absent",
        "execution_success",
    )
    print(f"{'Metric':34} {'HTTP fake':>12} {'Local fake':>12}")
    for metric in metrics:
        columns = [
            sum(bool(item[metric]) for item in results[label]) for label in results
        ]
        print(
            f"{metric:34} {columns[0]:>8}/{len(cases):<3} {columns[1]:>8}/{len(cases):<3}"
        )
    parity = [
        semantic_signature(stub_generator(case)["sql"])
        == semantic_signature(local_fake(case)["sql"])
        for case in cases
    ]
    print(f"{'semantic parity':34} {sum(parity):>8}/{len(cases):<3}")


if __name__ == "__main__":
    main()
