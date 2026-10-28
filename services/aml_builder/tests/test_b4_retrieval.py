"""Offline contracts for B4 retrieval adapters."""

from __future__ import annotations

from typing import Any

import pytest

from services.aml_builder.web.services.dwh_scenario.embeddings import EmbeddingIdentity
from services.aml_builder.web.services.dwh_scenario.embeddings import (
    EmbeddingProviderManager,
)
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
        self.query_embeddings: list[list[float]] | None = None

    def count(self) -> int:
        return len(self.rows)

    def query(self, *, where: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        self.where = where
        self.query_embeddings = _kwargs.get("query_embeddings")

        predicates = (
            where.get("$and", [])
            if "$and" in where
            else [{key: value} for key, value in where.items()]
        )
        rows = [
            row
            for row in self.rows
            if all(
                row["metadata"].get(key) == value
                for predicate in predicates
                for key, value in predicate.items()
            )
        ]
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
        metadata=make_index_manifest(IDENTITY, {"tables": 3}, institution_id="bank-a"),
        rows=[
            {
                "id": "z",
                "document": "later",
                "metadata": {"institution_id": "bank-a", "kind": "schema"},
                "distance": 0.2,
            },
            {
                "id": "a",
                "document": "first tie",
                "metadata": {"institution_id": "bank-a", "kind": "schema"},
                "distance": 0.1,
            },
            {
                "id": "x",
                "document": "leak",
                "metadata": {"institution_id": "bank-b", "kind": "schema"},
                "distance": 0.0,
            },
        ],
    )
    retriever = ChromaRetriever(
        "schema_tables",
        lambda _name: collection,
        IDENTITY,
        query_embedder=lambda _query: [0.1, 0.2, 0.3],
        threshold=0.5,
    )
    hits = retriever.search("tables", "bank-a", 5, {"kind": "schema"})
    assert [hit["id"] for hit in hits] == ["a", "z"]
    assert collection.where == {
        "$and": [{"kind": "schema"}, {"institution_id": "bank-a"}]
    }
    assert collection.query_embeddings == [[0.1, 0.2, 0.3]]
    assert all(hit["metadata"]["institution_id"] == "bank-a" for hit in hits)
    with pytest.raises(ValueError, match="cannot change"):
        retriever.search("tables", "bank-a", 5, {"institution_id": "bank-b"})


def test_chroma_retrieval_fails_closed_without_configured_embedder() -> None:
    collection = FakeCollection(
        metadata=make_index_manifest(IDENTITY, {}, institution_id="bank-a"), rows=[]
    )
    collection.rows = [
        {
            "id": "x",
            "document": "x",
            "metadata": {"institution_id": "bank-a"},
            "distance": 0.0,
        }
    ]
    retriever = ChromaRetriever("schema_tables", lambda _name: collection, IDENTITY)
    with pytest.raises(RuntimeError, match="Configured embedding provider"):
        retriever.search("tables", "bank-a", 2)


def test_embedding_manager_rejects_provider_dimension_mismatch() -> None:
    config = Settings(
        ORACLE_DSN="unused",
        ORACLE_USER="unused",
        ORACLE_PASSWORD="unused",
        DWH_SQL_EMBEDDINGS_PROVIDER="local_bge",
        DWH_SQL_EMBEDDINGS_MODEL="configured-model",
        DWH_SQL_EMBEDDINGS_DIMENSION=3,
        _env_file=None,
    )
    manager = EmbeddingProviderManager(config)
    with pytest.raises(ValueError, match="does not match configured index identity"):
        manager._validate_vectors([[0.1, 0.2]], expected_count=1)


def test_real_chroma_uses_the_configured_query_vector() -> None:
    chromadb = pytest.importorskip("chromadb")
    client = chromadb.EphemeralClient()
    collection = client.create_collection(
        name="b8_query_embedding_contract",
        metadata=make_index_manifest(
            IDENTITY, {"tables": 1}, institution_id="tenant-a"
        ),
    )
    collection.add(
        ids=["table-a"],
        documents=["cash transactions"],
        metadatas=[{"institution_id": "tenant-a"}],
        embeddings=[[1.0, 0.0, 0.0]],
    )
    retriever = ChromaRetriever(
        collection.name,
        lambda _name: collection,
        IDENTITY,
        query_embedder=lambda _query: [1.0, 0.0, 0.0],
        threshold=0.5,
    )
    hits = retriever.search("cash deposits", "tenant-a", 3)
    assert [hit["id"] for hit in hits] == ["table-a"]
    with pytest.raises(ValueError, match="institution scope is incompatible"):
        retriever.search("cash deposits", "tenant-b", 3)


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
    optional = ChromaRetriever(
        "experience_templates", missing, IDENTITY, optional_empty=True
    )
    assert optional.search("x", "bank-a", 2) == []


def test_retrieval_requires_institution_scope() -> None:
    """Callers cannot accidentally perform an unscoped vector search."""
    retriever = ChromaRetriever("schema_tables", lambda _: None, IDENTITY)
    with pytest.raises(ValueError, match="institution_id"):
        retriever.search("x", "", 2)


def test_neo4j_is_lazy_optional_and_requires_secret_when_enabled() -> None:
    """Disabled graph requires no credentials; enabled graph fails clearly."""
    config = Settings(
        ORACLE_DSN="unused",
        ORACLE_USER="unused",
        ORACLE_PASSWORD="unused",
        _env_file=None,
    )
    reranker = Neo4jReranker(config)
    assert reranker.readiness() == {"status": "disabled"}
    enabled = config.model_copy(update={"DWH_SQL_NEO4J_ENABLED": True})
    with pytest.raises(ValueError, match="password"):
        Neo4jReranker(enabled)._get_driver()
