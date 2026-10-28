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

from services.aml_builder.web.services.dwh_scenario.retrieval import make_index_manifest
from services.aml_builder.web.services.settings import Settings, settings


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
    if (
        "schema_index_version" in expected_manifest
        and metadata.get("schema_index_version")
        != expected_manifest["schema_index_version"]
    ):
        raise ValueError("Staging collection schema version mismatch")
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
    collection_name: str | None = None,
    institution_id: str,
) -> Any:
    """Build an empty-valid versioned experience collection from canonical JSONL rows."""
    collection_name = collection_name or settings.DWH_SQL_EXPERIENCE_COLLECTION
    if not institution_id:
        raise ValueError("institution_id is required when building a Chroma index")
    scoped_metadata = [_scoped_metadata(row, institution_id) for row in records]
    version = f"{collection_name}_build_{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}"
    manifest = make_index_manifest(
        identity, {"experience": len(records)}, institution_id=institution_id
    )
    collection = client.create_collection(name=version, metadata=manifest)
    if records:
        texts = [str(row["question"]) for row in records]
        collection.add(
            ids=[
                str(row.get("id") or hashlib.sha256(text.encode()).hexdigest())
                for row, text in zip(records, texts)
            ],
            documents=texts,
            metadatas=[
                {**metadata, "sql": str(row["sql"])}
                for row, metadata in zip(records, scoped_metadata)
            ],
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


def schema_collection_names(config: Settings = settings) -> tuple[str, str]:
    """Return canonical schema collection identifiers from retrieval configuration."""
    return (
        config.DWH_SQL_SCHEMA_TABLES_COLLECTION,
        config.DWH_SQL_SCHEMA_COLUMNS_COLLECTION,
    )


def build_schema_index(
    client: Any,
    tables: list[dict[str, Any]],
    columns: list[dict[str, Any]],
    embeddings: Embeddings,
    identity: Any,
    *,
    institution_id: str,
    config: Settings = settings,
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
    if not institution_id:
        raise ValueError("institution_id is required when building a Chroma index")
    _validate_configured_schema_scope(tables, columns, config)
    scoped_records = {
        "tables": [_scoped_metadata(row, institution_id) for row in tables],
        "columns": [_scoped_metadata(row, institution_id) for row in columns],
    }
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    schema_version = stamp
    previous: dict[str, str | None] = {}
    staged: list[tuple[str, str, list[dict[str, Any]], str]] = []
    try:
        for live, records, kind in (
            (config.DWH_SQL_SCHEMA_TABLES_COLLECTION, tables, "tables"),
            (config.DWH_SQL_SCHEMA_COLUMNS_COLLECTION, columns, "columns"),
        ):
            ids = [str(row["id"]) for row in records]
            if len(ids) != len(set(ids)):
                raise ValueError(f"Duplicate source ids in {kind} index records")
            staging_name = f"{live}_build_{stamp}"
            manifest = make_index_manifest(
                identity, {kind: len(records)}, institution_id=institution_id
            )
            manifest["schema_index_version"] = schema_version
            collection = client.create_collection(
                name=staging_name, metadata=manifest
            )
            if records:
                texts = [str(row["text"]) for row in records]
                collection.add(
                    ids=ids,
                    documents=texts,
                    metadatas=scoped_records[kind],
                    embeddings=embeddings.embed_documents(texts),
                )
            _validate_staged_schema_collection(
                collection,
                records,
                scoped_records[kind],
                identity=identity,
                institution_id=institution_id,
            )
            staged.append((live, staging_name, records, kind))
        staged_collections = [client.get_collection(item[1]) for item in staged]
        if validate_schema_collection_versions(staged_collections) != schema_version:
            raise ValueError("Staged schema collection versions are incompatible")
    except Exception:
        for _live, staging_name, _records, _kind in staged:
            client.delete_collection(staging_name)
        # Include a collection created immediately before a validation failure.
        for live in (
            config.DWH_SQL_SCHEMA_TABLES_COLLECTION,
            config.DWH_SQL_SCHEMA_COLUMNS_COLLECTION,
        ):
            staging_name = f"{live}_build_{stamp}"
            try:
                client.get_collection(staging_name)
            except (KeyError, LookupError):
                continue
            client.delete_collection(staging_name)
        raise
    try:
        for live, staging_name, records, kind in staged:
            manifest = make_index_manifest(
                identity, {kind: len(records)}, institution_id=institution_id
            )
            manifest["schema_index_version"] = schema_version
            previous[live] = promote_versioned_collection(
                client,
                temporary_name=staging_name,
                live_name=live,
                expected_count=len(records),
                expected_manifest=manifest,
            )
    except Exception:
        # Chroma has no cross-collection transaction. Restore each already-
        # promoted collection before returning a failed multi-index build.
        for live, _staging, _records, _kind in reversed(staged):
            old_name = previous.get(live)
            if live not in previous:
                continue
            if old_name:
                failed_name = f"{live}_failed_{stamp}"
                client.get_collection(live).modify(name=failed_name)
                client.get_collection(old_name).modify(name=live)
            else:
                client.delete_collection(live)
        raise
    return previous


def _validate_staged_schema_collection(
    collection: Any,
    records: list[dict[str, Any]],
    expected_metadata: list[dict[str, Any]],
    *,
    identity: Any,
    institution_id: str,
) -> None:
    """Verify staged signatures, embedding manifest and tenant before promotion."""
    from services.aml_builder.web.services.dwh_scenario.retrieval import IndexManifest

    metadata = collection.metadata or {}
    IndexManifest.from_metadata(metadata).validate(identity)
    if metadata.get("institution_id") != institution_id:
        raise ValueError("Staging institution scope validation failed")
    versions = metadata.get("schema_index_version")
    if not versions:
        raise ValueError("Staging schema version is missing")
    result = collection.get(include=["documents", "metadatas"])
    actual = {
        str(record_id): (document, row_metadata)
        for record_id, document, row_metadata in zip(
            result["ids"], result["documents"], result["metadatas"]
        )
    }
    expected = {
        str(row["id"]): (str(row["text"]), row_metadata)
        for row, row_metadata in zip(records, expected_metadata)
    }
    if actual != expected or len(actual) != len(records):
        raise ValueError("Staging source signature validation failed")
    if any(row_metadata.get("institution_id") != institution_id for _, row_metadata in actual.values()):
        raise ValueError("Staging record institution validation failed")


def schema_records_from_oracle_metadata(
    headers: list[str],
    rows: Iterable[tuple[Any, ...]],
    *,
    allowed_schemas: tuple[str, ...],
    allowed_objects: tuple[str, ...],
    internal_objects: tuple[str, ...] = (),
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], set[str]]:
    """Convert actual Oracle dictionary rows into schema index documents.

    Only configured objects are emitted. Text uses actual identifiers, types and
    optional Oracle comments; no business description or join is inferred.
    """
    positions = {name.upper(): index for index, name in enumerate(headers)}
    required = {
        "OWNER",
        "TABLE_NAME",
        "COLUMN_NAME",
        "DATA_TYPE",
        "DATA_LENGTH",
        "DATA_PRECISION",
        "DATA_SCALE",
        "NULLABLE",
        "TABLE_COMMENT",
        "COLUMN_COMMENT",
        "TARGET_OWNER",
        "TARGET_NAME",
        "OBJECT_TYPE",
    }
    if not required.issubset(positions):
        raise ValueError("Oracle schema metadata response has an unexpected shape")
    scopes = {str(item).upper() for item in allowed_schemas}
    allowed = {str(item).upper() for item in allowed_objects}
    internal = {str(item).upper() for item in internal_objects}
    grouped: dict[str, list[tuple[Any, ...]]] = {}
    for row in rows:
        owner = str(row[positions["OWNER"]]).upper()
        table = str(row[positions["TABLE_NAME"]]).upper()
        target_owner = str(row[positions["TARGET_OWNER"]]).upper()
        target_name = str(row[positions["TARGET_NAME"]]).upper()
        object_type = str(row[positions["OBJECT_TYPE"]]).upper()
        qualified = f"{owner}.{table}"
        if owner not in scopes or qualified not in allowed:
            raise ValueError("Oracle metadata exceeded configured object allowlist")
        if target_owner not in scopes:
            raise ValueError(
                "Oracle synonym resolves outside configured schema allowlist"
            )
        grouped.setdefault(qualified, []).append(row)
    tables: list[dict[str, Any]] = []
    columns: list[dict[str, Any]] = []
    found: set[str] = set(grouped)
    for qualified, object_rows in sorted(grouped.items()):
        first = object_rows[0]
        table_comment = first[positions["TABLE_COMMENT"]]
        target_owner = str(first[positions["TARGET_OWNER"]]).upper()
        target_name = str(first[positions["TARGET_NAME"]]).upper()
        object_type = str(first[positions["OBJECT_TYPE"]]).upper()
        category = "internal_metadata" if qualified in internal else "business"
        table_text = f"Table: {qualified}"
        if table_comment:
            table_text += f"\nOracle comment: {str(table_comment).strip()}"
        tables.append(
            {
                "id": f"table_dictionary_{qualified}",
                "text": table_text,
                "metadata": {
                    "table_name": qualified,
                    "object_category": category,
                    "source": "oracle_dictionary",
                    "object_type": object_type,
                    "resolved_object": f"{target_owner}.{target_name}",
                },
            }
        )
        for row in object_rows:
            column = str(row[positions["COLUMN_NAME"]]).upper()
            data_type = str(row[positions["DATA_TYPE"]]).upper()
            length = row[positions["DATA_LENGTH"]]
            precision, scale = (
                row[positions["DATA_PRECISION"]],
                row[positions["DATA_SCALE"]],
            )
            nullable = str(row[positions["NULLABLE"]]).upper()
            column_comment = row[positions["COLUMN_COMMENT"]]
            text = f"Column: {qualified}.{column}; type={data_type}; length={length}"
            if precision is not None:
                text += f"; precision={precision}"
            if scale is not None:
                text += f"; scale={scale}"
            text += f"; nullable={nullable}"
            if column_comment:
                text += f"; Oracle comment={str(column_comment).strip()}"
            columns.append(
                {
                    "id": f"column_dictionary_{qualified}_{column}",
                    "text": text,
                    "metadata": {
                        "table_name": qualified,
                        "column_name": column,
                        "data_type": data_type,
                        "object_category": category,
                        "source": "oracle_dictionary",
                        "object_type": object_type,
                        "resolved_object": f"{target_owner}.{target_name}",
                    },
                }
            )
    return tables, columns, found


def _validate_configured_schema_scope(
    tables: list[dict[str, Any]], columns: list[dict[str, Any]], config: Settings
) -> None:
    """Require indexed schema records to remain inside explicit client allowlists."""
    if not config.DWH_SQL_ALLOWED_SCHEMAS or not config.DWH_SQL_ALLOWED_OBJECTS:
        raise ValueError(
            "Explicit schema and object allowlists are required for indexing"
        )
    allowed_schemas = {item.upper() for item in config.DWH_SQL_ALLOWED_SCHEMAS}
    allowed_objects = {item.upper() for item in config.DWH_SQL_ALLOWED_OBJECTS}
    for record in [*tables, *columns]:
        metadata = record.get("metadata", {})
        table_name = str(metadata.get("table_name", "")).upper()
        if not table_name or table_name not in allowed_objects:
            raise ValueError(
                "Schema index record is outside configured object allowlist"
            )
        if table_name.split(".", 1)[0] not in allowed_schemas:
            raise ValueError(
                "Schema index record is outside configured schema allowlist"
            )
    indexed_objects = {
        str(record.get("metadata", {}).get("table_name", "")).upper()
        for record in [*tables, *columns]
    }
    missing_objects = allowed_objects - indexed_objects
    if missing_objects:
        raise ValueError(
            "Schema index is incomplete for configured allowlist: "
            + ", ".join(sorted(missing_objects))
        )


def _scoped_metadata(record: dict[str, Any], institution_id: str) -> dict[str, Any]:
    """Attach one configured tenant scope and reject cross-scope source records.

    Args:
        record: Source row whose metadata may already declare a tenant.
        institution_id: Explicit tenant selected for this index build.

    Returns:
        Chroma metadata containing the selected institution identifier.

    Raises:
        ValueError: If the source row declares another tenant.
    """
    metadata = dict(record.get("metadata", {}))
    existing = metadata.get("institution_id")
    if existing is not None and str(existing) != str(institution_id):
        raise ValueError("Source record institution scope does not match index build")
    metadata["institution_id"] = str(institution_id)
    return metadata


def validate_schema_collection_versions(
    collections: Iterable[Any],
) -> str:
    """Fail closed unless table and column collections share one manifest version.

    Args:
        collections: Table and column Chroma collections in canonical order.

    Returns:
        The common schema index version.

    Raises:
        ValueError: If a collection is missing a version or versions differ.
    """
    versions = {
        str((collection.metadata or {}).get("schema_index_version", ""))
        for collection in collections
    }
    if len(versions) != 1 or "" in versions:
        raise ValueError("Chroma schema collections have incompatible versions")
    return next(iter(versions))


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
