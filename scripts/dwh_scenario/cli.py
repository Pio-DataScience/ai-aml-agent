"""Parametrized CLI entry point for owner-run DWH maintenance.

Commands are deliberately not imported by the application and never execute SQL
writes. Commands requiring external backends are assembled only after invocation.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from services.aml_builder.web.services.dwh_scenario.toolchain import (
    build_experience_collection,
    build_schema_index as _build_schema_index,
    build_join_projection,
    harvest_dictionary as _harvest_dictionary,
    ingest_pairs,
    read_jsonl,
    schema_collection_names,
    scoped_enrichment_sql,
)


def build_parser() -> argparse.ArgumentParser:
    """Create the command parser without loading any optional dependencies."""
    parser = argparse.ArgumentParser(prog="dwh-scenario")
    commands = parser.add_subparsers(dest="command", required=True)
    harvest = commands.add_parser(
        "harvest-dictionary", help="bounded Oracle dictionary export"
    )
    harvest.add_argument("--output", type=Path, required=True)
    harvest.add_argument("--max-rows", type=int, default=5000)
    harvest.add_argument("--dry-run", action="store_true")
    schema = commands.add_parser(
        "build-schema-index", help="stage and promote schema vectors"
    )
    schema.add_argument("--input", type=Path, required=True)
    schema.add_argument("--dry-run", action="store_true")
    experience = commands.add_parser(
        "build-experience-index", help="stage experience JSONL"
    )
    experience.add_argument("--input", type=Path, required=True)
    experience.add_argument("--dry-run", action="store_true")
    ingest = commands.add_parser("ingest-pairs", help="normalize future officer pairs")
    ingest.add_argument("--input", type=Path, required=True)
    ingest.add_argument("--output", type=Path, required=True)
    ingest.add_argument("--dry-run", action="store_true")
    graph = commands.add_parser(
        "build-join-graph", help="stage and promote join graph projection"
    )
    graph.add_argument("--module", default="DWH")
    graph.add_argument("--input", type=Path, required=True)
    graph.add_argument("--output", type=Path, required=True)
    graph.add_argument("--max-rows", type=int, default=10000)
    graph.add_argument("--dry-run", action="store_true")
    enrich = commands.add_parser(
        "enrich-dictionary", help="emit owner-reviewed SQL files"
    )
    enrich.add_argument("--input", type=Path, required=True)
    enrich.add_argument("--output-dir", type=Path, required=True)
    enrich.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Validate arguments or perform only explicitly supported local operations."""
    args = build_parser().parse_args(argv)
    if args.command == "ingest-pairs":
        if args.dry_run:
            print(
                json.dumps(
                    {"valid_pairs": len(read_jsonl(args.input)), "written": False}
                )
            )
        else:
            print(json.dumps({"ingested": ingest_pairs(args.input, args.output)}))
        return 0
    if args.command == "enrich-dictionary":
        records = read_jsonl(args.input)
        backup, updates, reverse = scoped_enrichment_sql(
            records, description_column="OBJECT_NAME", project_column="PROJECT_CODE"
        )
        if not args.dry_run:
            args.output_dir.mkdir(parents=True, exist_ok=True)
            for name, content in (
                ("backup.sql", backup),
                ("updates.sql", updates),
                ("reverse.sql", reverse),
            ):
                (args.output_dir / name).write_text(content + "\n", encoding="utf-8")
        print(
            json.dumps(
                {"rows": len(records), "written": not args.dry_run, "executed": False}
            )
        )
        return 0
    if args.command == "harvest-dictionary":
        if args.max_rows < 1:
            raise SystemExit("--max-rows must be positive")
        if args.dry_run:
            print(
                json.dumps(
                    {"max_rows": args.max_rows, "connected": False, "written": False}
                )
            )
            return 0
        from services.aml_builder.web.adapters.dwh_oracle_repo import (
            DwhOracleRepository,
            OracleRequestBudget,
        )
        from services.aml_builder.web.services import oracle
        from services.aml_builder.web.services.settings import settings

        oracle.init_pool()
        try:
            budget = OracleRequestBudget(
                max_statements=2, deadline_seconds=settings.DWH_SQL_DEADLINE_SECONDS
            )
            sql = (
                "SELECT TABLE_NAME, COLUMN_NAME, OBJECT_NAME, FLAGS "
                "FROM PIO_BANKBI_DICTIONARY_COL ORDER BY TABLE_NAME, COLUMN_NAME "
                f"FETCH FIRST {args.max_rows} ROWS ONLY"
            )
            repo = DwhOracleRepository()
            headers, rows = repo.read_dictionary(
                sql, budget=budget, max_rows=args.max_rows
            )
            records = [dict(zip(headers, row)) for row in rows]
            tables = sorted(
                {
                    str(row.get("TABLE_NAME") or "")
                    for row in records
                    if row.get("TABLE_NAME")
                }
            )
            documents = [
                {
                    "kind": "table",
                    "id": name,
                    "text": f"Table: {name}",
                    "metadata": {"table_name": name},
                }
                for name in tables
            ]
            documents.extend(
                {
                    "kind": "column",
                    "id": f"{row.get('TABLE_NAME')}.{row.get('COLUMN_NAME')}",
                    "text": f"Table: {row.get('TABLE_NAME')} Column: {row.get('COLUMN_NAME')} Description: {row.get('OBJECT_NAME') or ''}",
                    "metadata": {
                        key: str(value)
                        for key, value in row.items()
                        if value is not None
                    },
                }
                for row in records
            )
            manifest = _harvest_dictionary(
                iter(documents), args.output, max_rows=args.max_rows * 2
            )
        finally:
            oracle.close_pool()
        print(
            json.dumps(
                {
                    "row_count": manifest["row_count"],
                    "manifest": str(
                        args.output.with_suffix(args.output.suffix + ".manifest.json")
                    ),
                }
            )
        )
        return 0
    if args.command in {"build-schema-index", "build-experience-index"}:
        if args.dry_run:
            records = read_jsonl(args.input)
            print(
                json.dumps(
                    {
                        "records": len(records),
                        "written": False,
                        "collections": (
                            list(schema_collection_names())
                            if args.command == "build-schema-index"
                            else ["experience_templates"]
                        ),
                    }
                )
            )
            return 0
        import chromadb
        from services.aml_builder.web.services.dwh_scenario.embeddings import (
            EmbeddingProviderManager,
        )
        from services.aml_builder.web.services.dwh_scenario.embeddings import (
            embedding_identity,
        )
        from services.aml_builder.web.services.settings import settings

        if settings.DWH_SQL_CHROMA_PERSIST_DIR is None:
            raise SystemExit("DWH_SQL_CHROMA_PERSIST_DIR is required")
        client = chromadb.PersistentClient(
            path=str(settings.DWH_SQL_CHROMA_PERSIST_DIR)
        )
        manager = EmbeddingProviderManager(settings)
        records = read_jsonl(args.input)
        if args.command == "build-experience-index":
            build_experience_collection(
                client, records, manager, embedding_identity(settings)
            )
        else:
            tables = [row for row in records if row.get("kind") == "table"]
            columns = [row for row in records if row.get("kind") == "column"]
            _build_schema_index(
                client, tables, columns, manager, embedding_identity(settings)
            )
        print(json.dumps({"records": len(records), "promoted": True}))
        return 0
    if args.command == "build-join-graph":
        rows = read_jsonl(args.input)
        if args.dry_run:
            if len(rows) > args.max_rows:
                raise SystemExit("join source exceeds configured row limit")
            print(
                json.dumps(
                    {
                        "module": args.module,
                        "rows": len(rows),
                        "written": False,
                        "gds_required_for_load": True,
                    }
                )
            )
            return 0
        manifest = build_join_projection(rows, args.output, max_rows=args.max_rows)
        print(
            json.dumps(
                {
                    "module": args.module,
                    "manifest": manifest,
                    "projection": str(args.output),
                    "promoted": False,
                    "gds_required_for_load": True,
                }
            )
        )
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
