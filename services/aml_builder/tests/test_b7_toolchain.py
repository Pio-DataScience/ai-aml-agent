"""Offline regression tests for owner-run DWH maintenance primitives."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.aml_builder.web.services.dwh_scenario.toolchain import (
    build_experience_collection,
    build_join_projection,
    harvest_dictionary,
    ingest_pairs,
    promote_versioned_collection,
    scoped_enrichment_sql,
)


class _Collection:
    """Small Chroma fake with rename and count behavior."""

    def __init__(self, name, metadata, store):
        self.name, self.metadata, self.store = name, metadata, store
        self.items = []

    def add(self, **kwargs):
        self.items.extend(kwargs["ids"])

    def count(self):
        return len(self.items)

    def modify(self, *, name):
        self.store.pop(self.name)
        self.name = name
        self.store[name] = self


class _Chroma:
    """In-memory collection registry."""

    def __init__(self):
        self.collections = {}

    def create_collection(self, *, name, metadata):
        collection = _Collection(name, metadata, self.collections)
        self.collections[name] = collection
        return collection

    def get_collection(self, name):
        return self.collections[name]


class _Embeddings:
    """Deterministic fake embedder that rejects unexpected empty calls."""

    def embed_documents(self, texts):
        assert texts
        return [[float(len(text)), 1.0] for text in texts]


class _Identity:
    provider = "fake"
    model = "fake-model"
    dimension = 2
    fingerprint = "fake-fingerprint"


def test_harvest_stream_is_bounded_and_manifested(tmp_path: Path) -> None:
    """Harvest writes deterministic JSONL and rejects one row over its cap."""
    path = tmp_path / "dictionary.jsonl"
    manifest = harvest_dictionary(iter([{"a": 1}, {"a": 2}]), path, max_rows=2)
    assert manifest["row_count"] == 2
    assert json.loads(path.read_text(encoding="utf-8").splitlines()[0]) == {"a": 1}
    assert path.with_suffix(".jsonl.manifest.json").exists()
    with pytest.raises(ValueError, match="row limit"):
        harvest_dictionary(iter([{}, {}, {}]), tmp_path / "too-many.jsonl", max_rows=2)


def test_experience_index_accepts_empty_input(tmp_path: Path) -> None:
    """An empty experience source creates/promotes a valid zero-count collection."""
    client = _Chroma()
    previous = build_experience_collection(client, [], _Embeddings(), _Identity())
    assert previous is None
    assert client.get_collection("experience_templates").count() == 0


def test_failed_promotion_keeps_live_collection_and_reports_failure() -> None:
    """A bad staging count never renames or deletes the active collection."""
    client = _Chroma()
    active = client.create_collection(name="schema_tables", metadata={})
    active.add(ids=["old"])
    client.create_collection(name="stage", metadata={})
    with pytest.raises(ValueError, match="count"):
        promote_versioned_collection(
            client,
            temporary_name="stage",
            live_name="schema_tables",
            expected_count=1,
            expected_manifest={"embedding_provider": "fake"},
        )
    assert client.get_collection("schema_tables").count() == 1


def test_ingest_pairs_normalizes_and_deduplicates(tmp_path: Path) -> None:
    """Future officer format converts to canonical, deduplicated JSONL."""
    source, output = tmp_path / "pairs.jsonl", tmp_path / "canonical.jsonl"
    source.write_text(
        '{"scenario_description":"Check A","sql":"SELECT 1"}\n'
        '{"question":"Check A","sql":"SELECT 1"}\n',
        encoding="utf-8",
    )
    assert ingest_pairs(source, output) == 1
    row = json.loads(output.read_text(encoding="utf-8"))
    assert row["question"] == "Check A" and row["sql"] == "SELECT 1"


def test_join_projection_is_bounded_and_manifested(tmp_path: Path) -> None:
    """Join projection export validates the row cap and writes provenance."""
    output = tmp_path / "joins.jsonl"
    manifest = build_join_projection(
        [{"fact_table": "TX", "ref_table": "CUSTOMER", "formula": "TX.C=C.C"}],
        output,
        max_rows=1,
    )
    assert manifest["row_count"] == 1
    assert output.with_suffix(".jsonl.manifest.json").exists()
    original = output.read_text(encoding="utf-8")
    with pytest.raises(FileExistsError, match="cannot be overwritten"):
        build_join_projection(
            [{"fact_table": "X", "ref_table": "Y"}], output, max_rows=1
        )
    assert output.read_text(encoding="utf-8") == original
    with pytest.raises(ValueError, match="row limit"):
        build_join_projection(
            [
                {"fact_table": "A", "ref_table": "B"},
                {"fact_table": "C", "ref_table": "D"},
            ],
            tmp_path / "overflow.jsonl",
            max_rows=1,
        )


def test_enrichment_literals_escape_and_scope_duplicate_column_names() -> None:
    """Same column in multiple tables receives independent safely quoted updates."""
    backup, updates, reverse = scoped_enrichment_sql(
        [
            {
                "table_name": "T1",
                "column_name": "STATUS",
                "project_code": "AML",
                "old": "owner's",
                "new": "current",
            },
            {
                "table_name": "T2",
                "column_name": "STATUS",
                "project_code": "AML",
                "old": "old",
                "new": "new",
            },
        ],
        description_column="OBJECT_NAME",
        project_column="PROJECT_CODE",
    )
    assert updates.count("TABLE_NAME = '") == 2
    assert "TABLE_NAME = 'T1' AND COLUMN_NAME = 'STATUS'" in updates
    assert "TABLE_NAME = 'T2' AND COLUMN_NAME = 'STATUS'" in updates
    assert "owner''s" in reverse
    assert "PROJECT_CODE = 'AML'" in backup
