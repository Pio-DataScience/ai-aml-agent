"""Owner-run, read-only extraction and local-state archive helper.

The script never mutates Oracle. Local sqlite files move only when the owner
explicitly passes ``--move-local-state``.
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from services.aml_builder.web.services.oracle import run_readonly


ReadQuery = Callable[[str, dict[str, Any] | None], tuple[list[str], list[tuple[Any, ...]]]]


def _select_rows(
    query: ReadQuery, sql: str, ids: list[str] | None = None
) -> list[dict[str, Any]]:
    """Run a SELECT and map its rows to dictionaries.

    Args:
        query (ReadQuery): SELECT-only query function.
        sql (str): SELECT statement.
        ids (list[str] | None): Optional scenario IDs bound to the statement.

    Returns:
        list[dict[str, Any]]: Result rows keyed by lower-case column names.
    """
    params: dict[str, Any] = {}
    if ids is not None:
        binds = ", ".join(f":id{index}" for index in range(len(ids)))
        sql = sql.replace("{id_binds}", binds or "NULL")
        params = {f"id{index}": value for index, value in enumerate(ids)}
    columns, rows = query(sql, params)
    return [dict(zip(columns, row)) for row in rows]


def collect_legacy_records(query: ReadQuery = run_readonly) -> dict[str, Any]:
    """Read legacy PRD scenarios, matching metadata, and alert counts.

    Args:
        query (ReadQuery): SELECT-only Oracle query function.

    Returns:
        dict[str, Any]: Read-only extracts and counts scoped to exact PRD IDs.
    """
    production = _select_rows(
        query,
        "SELECT * FROM PIO_AML_PRODUCTION_SCENARIOS "
        "WHERE SUBSTR(SCENARIO_ID, 1, 4) = 'PRD_' ORDER BY SCENARIO_ID",
    )
    scenario_ids = [str(row["scenario_id"]) for row in production]
    metadata = _select_rows(
        query,
        "SELECT * FROM PIO_AML_SCENARIO WHERE SCENARIO_CODE IN ({id_binds})",
        scenario_ids,
    ) if scenario_ids else []
    alert_counts: dict[str, int] = {}
    if scenario_ids:
        for table in ("PIO_AML_CUSTOMERS", "PIO_AML_CUSTOMERS_DET"):
            _columns, rows = query(
                f"SELECT COUNT(*) AS row_count FROM {table} "
                "WHERE AML_SCENARIO_CODE IN ({id_binds})".format(
                    id_binds=", ".join(f":id{i}" for i in range(len(scenario_ids)))
                ),
                {f"id{i}": value for i, value in enumerate(scenario_ids)},
            )
            alert_counts[table] = int(rows[0][0]) if rows else 0
    else:
        alert_counts = {"PIO_AML_CUSTOMERS": 0, "PIO_AML_CUSTOMERS_DET": 0}
    return {
        "production_rows": production,
        "scenario_metadata_rows": metadata,
        "scenario_ids": scenario_ids,
        "alert_counts": alert_counts,
    }


def local_state_files(sqlite_path: Path) -> list[Path]:
    """List the sqlite database and its SQLite sidecar files if present.

    Args:
        sqlite_path (Path): Legacy checkpoint/sidebar database path.

    Returns:
        list[Path]: Existing database, WAL, and SHM paths.
    """
    candidates = [
        sqlite_path,
        Path(f"{sqlite_path}-wal"),
        Path(f"{sqlite_path}-shm"),
    ]
    return [path for path in candidates if path.exists()]


def write_exports(destination: Path, records: dict[str, Any]) -> None:
    """Write the matching Oracle extracts as JSON and CSV files.

    Args:
        destination (Path): Timestamped archive directory.
        records (dict[str, Any]): Result from collect_legacy_records.
    """
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "scenarios.json").write_text(
        json.dumps(records, indent=2, default=str), encoding="utf-8"
    )
    tagged_rows = [
        {"source_table": table, **row}
        for table, rows in (
            ("PIO_AML_PRODUCTION_SCENARIOS", records["production_rows"]),
            ("PIO_AML_SCENARIO", records["scenario_metadata_rows"]),
        )
        for row in rows
    ]
    fieldnames = ["source_table"] + sorted(
        {key for row in tagged_rows for key in row if key != "source_table"}
    )
    with (destination / "scenarios.csv").open(
        "w", newline="", encoding="utf-8-sig"
    ) as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(tagged_rows)


def move_local_state(sqlite_path: Path, destination: Path) -> list[Path]:
    """Move the legacy sqlite database and sidecars into the archive folder.

    Args:
        sqlite_path (Path): Legacy checkpoint/sidebar database path.
        destination (Path): Timestamped archive directory.

    Returns:
        list[Path]: Destination paths for files moved.
    """
    files = local_state_files(sqlite_path)
    destination.mkdir(parents=True, exist_ok=True)
    moved: list[Path] = []
    for source in files:
        target = destination / source.name
        shutil.move(str(source), str(target))
        moved.append(target)
    return moved


def main() -> int:
    """Run an owner-selected preview, export, or local-state move.

    Returns:
        int: Process exit status.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Preview only (the default).")
    parser.add_argument("--export", action="store_true", help="Write read-only Oracle extracts.")
    parser.add_argument(
        "--move-local-state", action="store_true", help="Move the legacy sqlite files."
    )
    parser.add_argument(
        "--sqlite-path",
        type=Path,
        default=Path("artifacts/checkpoints.sqlite"),
        help="Legacy checkpoint/sidebar sqlite path (default: artifacts/checkpoints.sqlite).",
    )
    args = parser.parse_args()
    preview = args.dry_run or not (args.export or args.move_local_state)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = Path("old_data") / timestamp

    records = (
        collect_legacy_records()
        if preview or args.export
        else {
            "production_rows": [],
            "scenario_metadata_rows": [],
            "scenario_ids": [],
            "alert_counts": {},
        }
    )
    files = local_state_files(args.sqlite_path)
    if records["alert_counts"] or preview or args.export:
        print(f"Legacy production rows: {len(records['production_rows'])}")
        print(f"Legacy PRD_ IDs: {records['scenario_ids']}")
        print(f"Matching PIO_AML_SCENARIO rows: {len(records['scenario_metadata_rows'])}")
        for table, count in records["alert_counts"].items():
            print(f"Alert rows in {table}: {count}")
    print(f"Local sqlite files that would move: {[str(path) for path in files]}")
    print(f"Archive destination: {destination}")

    if preview:
        return 0
    if args.export:
        write_exports(destination, records)
        print(f"Wrote read-only extracts under {destination}")
    if args.move_local_state:
        moved = move_local_state(args.sqlite_path, destination)
        print(f"Moved local sqlite files: {[str(path) for path in moved]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
