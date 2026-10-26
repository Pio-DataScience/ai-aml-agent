"""Offline contracts for B4 retrieval adapters."""

from __future__ import annotations

from typing import Any

import pytest

from services.aml_builder.web.services.dwh_scenario.embeddings import EmbeddingIdentity
from services.aml_builder.web.services.dwh_scenario.retrieval import (
    ChromaRetriever,
    IndexManifest,
    Neo4jReranker,
    make_index_manifest,
)
from services.aml_builder.web.services.settings import Settings


class FakeCollection:
    """Small deterministic Chroma collection fake."""

    def __init__(self, *, metadata: dict[str, Any], rows: list[dict[str, Any]]) -> None:
        self.metadata = metadata
        self.rows = rows
        self.where: dict[str, Any] | None = None

    def count(self) -> int:
        return len(self.rows)

    def query(self, *, where: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        self.where = where
        rows = [row for row in self.rows if row["metadata"].get("institution_id") == where["institution_id"]]
        rows.sort(key=lambda row: (row["distance"], row["id"]))
        return {
            "ids": [[row["id"] for row in rows]],
            "documents": [[row["document"] for row in rows]],
            "metadatas": [[row["metadata"] for row in rows]],
            "distances": [[row["distance"] for row in rows]],
        }


IDENTITY = EmbeddingIdentity("openai", "embedding-test", 3, "fp")


def test_chroma_retrieval_scopes_tenant_and_orders_deterministically() -> None:
    """Tenant filtering is enforced in query and again on returned metadata."""
    collection = FakeCollection(
        metadata=make_index_manifest(IDENTITY, {"tables": 3}),
        rows=[
            {"id": "z", "document": "later", "metadata": {"institution_id": "bank-a", "kind": "schema"}, "distance": 0.2},
            {"id": "a", "document": "first tie", "metadata": {"institution_id": "bank-a", "kind": "schema"}, "distance": 0.1},
            {"id": "x", "document": "leak", "metadata": {"institution_id": "bank-b", "kind": "schema"}, "distance": 0.0},
        ],
    )
    retriever = ChromaRetriever("schema_tables", lambda _name: collection, IDENTITY, threshold=0.5)
    hits = retriever.search("tables", "bank-a", 5, {"kind": "schema"})
    assert [hit["id"] for hit in hits] == ["a", "z"]
    assert collection.where == {"institution_id": "bank-a", "kind": "schema"}
    assert all(hit["metadata"]["institution_id"] == "bank-a" for hit in hits)
    with pytest.raises(ValueError, match="cannot change"):
        retriever.search("tables", "bank-a", 5, {"institution_id": "bank-b"})


def test_manifest_mismatch_fails_closed() -> None:
    """A stale or incompatible index cannot be queried."""
    manifest = make_index_manifest(IDENTITY, {"columns": 5})
    IndexManifest.from_metadata(manifest).validate(IDENTITY)
    incompatible = EmbeddingIdentity("cohere", "other", 3, "other")
    with pytest.raises(ValueError, match="incompatible"):
        IndexManifest.from_metadata(manifest).validate(incompatible)


def test_missing_required_index_fails_closed_but_experience_is_empty() -> None:
    """Required schema indexes fail; optional examples remain a valid empty result."""
    missing = lambda _name: (_ for _ in ()).throw(KeyError("missing"))
    required = ChromaRetriever("schema_tables", missing, IDENTITY)
    with pytest.raises(RuntimeError, match="Required Chroma collection missing"):
        required.search("x", "bank-a", 2)
    optional = ChromaRetriever("experience_templates", missing, IDENTITY, optional_empty=True)
    assert optional.search("x", "bank-a", 2) == []


def test_retrieval_requires_institution_scope() -> None:
    """Callers cannot accidentally perform an unscoped vector search."""
    retriever = ChromaRetriever("schema_tables", lambda _: None, IDENTITY)
    with pytest.raises(ValueError, match="institution_id"):
        retriever.search("x", "", 2)


def test_neo4j_is_lazy_optional_and_requires_secret_when_enabled() -> None:
    """Disabled graph requires no credentials; enabled graph fails clearly."""
    config = Settings(ORACLE_DSN="unused", ORACLE_USER="unused", ORACLE_PASSWORD="unused", _env_file=None)
    reranker = Neo4jReranker(config)
    assert reranker.readiness() == {"status": "disabled"}
    enabled = config.model_copy(update={"DWH_SQL_NEO4J_ENABLED": True})
    with pytest.raises(ValueError, match="password"):
        Neo4jReranker(enabled)._get_driver()
