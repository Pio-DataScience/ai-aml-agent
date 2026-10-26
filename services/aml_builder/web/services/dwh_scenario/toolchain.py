"""Owner-run, offline-testable DWH index maintenance primitives.

All external systems are injected. This module contains no Oracle write path.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable, Protocol

from services.aml_builder.web.services.dwh_scenario.collection_names import (
    EXPERIENCE_COLLECTION_NAME,
    SCHEMA_COLUMNS_COLLECTION_NAME,
    SCHEMA_TABLES_COLLECTION_NAME,
)
from services.aml_builder.web.services.dwh_scenario.retrieval import make_index_manifest


class Embeddings(Protocol):
    """Embedding interface used by index builders."""

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...


def harvest_dictionary(
    rows: Iterable[dict[str, Any]], output: Path, *, max_rows: int
) -> dict[str, Any]:
    """Stream bounded dictionary records to JSONL and write a provenance manifest.

    Args:
        rows: Already bounded iterator of dictionary metadata.
        output: Target JSONL path.
        max_rows: Hard maximum number of emitted rows.

    Returns:
        Manifest data written beside the JSONL file.
    """
    if max_rows < 1:
        raise ValueError("max_rows must be positive")
    output.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    digest = hashlib.sha256()
    with output.open("w", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            if count >= max_rows:
                raise ValueError("dictionary source exceeded configured row limit")
            line = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
            stream.write(line + "\n")
            digest.update((line + "\n").encode("utf-8"))
            count += 1
    manifest = {
        "format": "dwh-dictionary-jsonl-v1",
        "built_at": datetime.now(timezone.utc).isoformat(),
        "row_count": count,
        "sha256": digest.hexdigest(),
        "source": "Service A read-only Oracle repository",
    }
    output.with_suffix(output.suffix + ".manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read canonical one-JSON-object-per-line records, rejecting malformed input."""
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL at line {line_number}") from exc
            if not isinstance(item, dict):
                raise ValueError(f"Expected object at line {line_number}")
            records.append(item)
    return records


def ingest_pairs(source: Path, destination: Path) -> int:
    """Validate, normalize and deduplicate question/SQL pairs into canonical JSONL."""
    required = ("question", "sql")
    seen: set[str] = set()
    records: list[dict[str, str]] = []
    for number, row in enumerate(read_jsonl(source), start=1):
        question = str(row.get("question", row.get("scenario_description", ""))).strip()
        sql = str(row.get("sql", row.get("golden_sql_template", ""))).strip()
        if not question or not sql:
            raise ValueError(f"Pair {number} requires a question and SQL")
        key = hashlib.sha256(f"{question.casefold()}\0{sql}".encode()).hexdigest()
        if key in seen:
            continue
        seen.add(key)
        records.append({"id": key, "question": question, "sql": sql})
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8", newline="\n") as stream:
        for row in records:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return len(records)


def scoped_enrichment_sql(
    rows: Iterable[dict[str, Any]],
    *,
    description_column: str,
    project_column: str | None = None,
) -> tuple[str, str, str]:
    """Emit backup SELECT, scoped updates, and reverse SQL; never execute them."""
    safe = re.compile(r"^[A-Za-z][A-Za-z0-9_$#]{0,127}$")
    identifiers = [description_column]
    if project_column:
        identifiers.append(project_column)
    if any(not safe.fullmatch(value) for value in identifiers):
        raise ValueError("Unsupported SQL identifier")
    materialized = list(rows)
    updates: list[str] = []
    reversals: list[str] = []
    predicates: list[str] = []

    def literal(value: Any) -> str:
        """Render a quoted SQL string literal with embedded quotes escaped."""
        return "'" + str(value if value is not None else "").replace("'", "''") + "'"

    for index, row in enumerate(materialized):
        table = str(row.get("table_name", "")).strip()
        column = str(row.get("column_name", "")).strip()
        if not table or not column:
            raise ValueError("Enrichment row requires table_name and column_name")
        predicate = f"TABLE_NAME = {literal(table)} AND COLUMN_NAME = {literal(column)}"
        if project_column:
            predicate += (
                f" AND {project_column} = {literal(row.get('project_code', ''))}"
            )
        predicates.append(f"({predicate})")
        updates.append(
            f"UPDATE PIO_BANKBI_DICTIONARY_COL SET {description_column} = {literal(row.get('new', ''))} "
            f"WHERE {predicate};"
        )
        reversals.append(
            f"UPDATE PIO_BANKBI_DICTIONARY_COL SET {description_column} = {literal(row.get('old', ''))} "
            f"WHERE {predicate};"
        )
    where = " OR ".join(predicates) if predicates else "1 = 0"
    backup = (
        f"SELECT TABLE_NAME, COLUMN_NAME, {description_column}"
        + (f", {project_column}" if project_column else "")
        + f" FROM PIO_BANKBI_DICTIONARY_COL WHERE {where};"
    )
    return backup, "\n".join(updates), "\n".join(reversals)


def promote_versioned_collection(
    client: Any,
    *,
    temporary_name: str,
    live_name: str,
    expected_count: int,
    expected_manifest: dict[str, Any],
) -> str | None:
    """Validate staging collection, atomically rename into service and retain backup."""
    staging = client.get_collection(temporary_name)
    if int(staging.count()) != expected_count:
        raise ValueError("Staging collection count does not match expected count")
    metadata = staging.metadata or {}
    for key in (
        "embedding_provider",
        "embedding_model",
        "embedding_dimension",
        "embedding_fingerprint",
    ):
        if metadata.get(key) != expected_manifest.get(key):
            raise ValueError("Staging collection embedding manifest mismatch")
    old_name: str | None = None
    try:
        old = client.get_collection(live_name)
        old_name = f"{live_name}_previous_{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}"
        old.modify(name=old_name)
    except Exception as exc:
        if type(exc).__name__ not in {
            "KeyError",
            "ValueError",
            "NotFoundError",
            "ChromaNotFoundError",
        }:
            raise
    try:
        staging.modify(name=live_name)
    except Exception:
        if old_name:
            client.get_collection(old_name).modify(name=live_name)
        raise
    return old_name


def build_experience_collection(
    client: Any,
    records: list[dict[str, Any]],
    embeddings: Embeddings,
    identity: Any,
    *,
    collection_name: str = EXPERIENCE_COLLECTION_NAME,
) -> Any:
    """Build an empty-valid versioned experience collection from canonical JSONL rows."""
    version = f"{collection_name}_build_{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}"
    manifest = make_index_manifest(identity, {"experience": len(records)})
    collection = client.create_collection(name=version, metadata=manifest)
    if records:
        texts = [str(row["question"]) for row in records]
        collection.add(
            ids=[
                str(row.get("id") or hashlib.sha256(text.encode()).hexdigest())
                for row, text in zip(records, texts)
            ],
            documents=texts,
            metadatas=[{"sql": str(row["sql"])} for row in records],
            embeddings=embeddings.embed_documents(texts),
        )
    if int(collection.count()) != len(records):
        raise ValueError("Experience collection validation failed")
    previous = promote_versioned_collection(
        client,
        temporary_name=version,
        live_name=collection_name,
        expected_count=len(records),
        expected_manifest=manifest,
    )
    return previous


def schema_collection_names() -> tuple[str, str]:
    """Return canonical schema collection identifiers from retrieval configuration."""
    return SCHEMA_TABLES_COLLECTION_NAME, SCHEMA_COLUMNS_COLLECTION_NAME


def build_schema_index(
    client: Any,
    tables: list[dict[str, Any]],
    columns: list[dict[str, Any]],
    embeddings: Embeddings,
    identity: Any,
) -> dict[str, str | None]:
    """Build both schema collections in staging and promote validated versions.

    Args:
        client: Chroma-compatible injected client.
        tables: Table metadata records containing id and text.
        columns: Column metadata records containing id and text.
        embeddings: Configured lazy A embedding adapter.
        identity: Embedding identity persisted in each collection manifest.

    Returns:
        Previous collection names retained for rollback.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    previous: dict[str, str | None] = {}
    for live, records, kind in (
        (SCHEMA_TABLES_COLLECTION_NAME, tables, "tables"),
        (SCHEMA_COLUMNS_COLLECTION_NAME, columns, "columns"),
    ):
        staging_name = f"{live}_build_{stamp}"
        manifest = make_index_manifest(identity, {kind: len(records)})
        collection = client.create_collection(name=staging_name, metadata=manifest)
        if records:
            texts = [str(row["text"]) for row in records]
            collection.add(
                ids=[str(row["id"]) for row in records],
                documents=texts,
                metadatas=[dict(row.get("metadata", {})) for row in records],
                embeddings=embeddings.embed_documents(texts),
            )
        previous[live] = promote_versioned_collection(
            client,
            temporary_name=staging_name,
            live_name=live,
            expected_count=len(records),
            expected_manifest=manifest,
        )
    return previous


def build_join_projection(
    join_rows: Iterable[dict[str, Any]], output: Path, *, max_rows: int
) -> dict[str, Any]:
    """Write a bounded, versioned graph projection for an owner-run Neo4j loader.

    GDS availability is deliberately checked by the optional Neo4j loader, never
    hidden behind a fallback to destructive in-place graph replacement.
    """
    if max_rows < 1:
        raise ValueError("max_rows must be positive")
    if output.exists() or output.with_suffix(output.suffix + ".manifest.json").exists():
        raise FileExistsError(
            "join projection output is versioned and cannot be overwritten"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    digest = hashlib.sha256()
    with output.open("w", encoding="utf-8", newline="\n") as stream:
        for row in join_rows:
            if count >= max_rows:
                raise ValueError("join source exceeded configured row limit")
            fact, ref = (
                str(row.get("fact_table", "")).strip(),
                str(row.get("ref_table", "")).strip(),
            )
            if not fact or not ref:
                raise ValueError("join rows require fact_table and ref_table")
            line = json.dumps(
                {
                    "fact_table": fact,
                    "ref_table": ref,
                    "formula": str(row.get("formula") or ""),
                },
                sort_keys=True,
            )
            stream.write(line + "\n")
            digest.update((line + "\n").encode())
            count += 1
    manifest = {
        "format": "dwh-join-projection-v1",
        "built_at": datetime.now(timezone.utc).isoformat(),
        "row_count": count,
        "sha256": digest.hexdigest(),
    }
    output.with_suffix(output.suffix + ".manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return manifest
