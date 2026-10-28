"""Offline-testable Chroma retrieval and optional Neo4j reranking adapters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Any, Callable, Protocol

from services.aml_builder.web.services.dwh_scenario.embeddings import (
    EmbeddingIdentity,
    embedding_identity,
)
from services.aml_builder.web.services.dwh_scenario.collection_names import (
    EXPERIENCE_COLLECTION_NAME,
    SCHEMA_COLUMNS_COLLECTION_NAME,
    SCHEMA_TABLES_COLLECTION_NAME,
)
from services.aml_builder.web.services.settings import Settings, settings


class Retriever(Protocol):
    """Retrieve institution-scoped documents from one indexed collection."""

    def search(
        self,
        query: str,
        institution_id: str,
        limit: int,
        metadata_filter: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]: ...


class GraphReranker(Protocol):
    """Rerank candidate documents using optional graph evidence."""

    def rerank(
        self, candidates: list[dict[str, Any]], limit: int
    ) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class IndexManifest:
    """Identity and build evidence stored with an index."""

    provider: str
    model: str
    dimension: int
    fingerprint: str
    built_at: str
    source_row_counts: dict[str, int]
    institution_id: str | None = None

    @classmethod
    def from_metadata(cls, metadata: dict[str, Any]) -> "IndexManifest":
        """Decode a manifest from Chroma metadata, rejecting malformed values."""
        return cls(
            provider=str(metadata["embedding_provider"]),
            model=str(metadata["embedding_model"]),
            dimension=int(metadata["embedding_dimension"]),
            fingerprint=str(metadata["embedding_fingerprint"]),
            built_at=str(metadata["built_at"]),
            source_row_counts=(
                json.loads(metadata["source_row_counts"])
                if isinstance(metadata["source_row_counts"], str)
                else dict(metadata["source_row_counts"])
            ),
            institution_id=(
                str(metadata["institution_id"])
                if metadata.get("institution_id") is not None
                else None
            ),
        )

    def validate(self, identity: EmbeddingIdentity) -> None:
        """Fail closed when persisted embeddings differ from the active model."""
        if (self.provider, self.model, self.dimension) != (
            identity.provider,
            identity.model,
            identity.dimension,
        ) or self.fingerprint != identity.fingerprint:
            raise ValueError("Chroma index embedding identity is incompatible")


def make_index_manifest(
    identity: EmbeddingIdentity,
    source_row_counts: dict[str, int],
    *,
    institution_id: str | None = None,
) -> dict[str, Any]:
    """Return serializable Chroma collection metadata for an index build."""
    manifest = {
        "embedding_provider": identity.provider,
        "embedding_model": identity.model,
        "embedding_dimension": identity.dimension,
        "embedding_fingerprint": identity.fingerprint,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "source_row_counts": json.dumps(source_row_counts, sort_keys=True),
    }
    if institution_id is not None:
        manifest["institution_id"] = str(institution_id)
    return manifest


class ChromaRetriever:
    """Lazy collection adapter with manifest checks and tenant-safe filtering."""

    def __init__(
        self,
        collection_name: str,
        collection_provider: Any,
        identity: EmbeddingIdentity,
        *,
        query_embedder: Callable[[str], list[float]] | None = None,
        optional_empty: bool = False,
        threshold: float = 0.0,
    ) -> None:
        """Store dependencies; collection/provider access is deferred to use."""
        self.collection_name = collection_name
        self.collection_provider = collection_provider
        self.identity = identity
        self.query_embedder = query_embedder
        self.optional_empty = optional_empty
        self.threshold = threshold

    def _collection(self) -> Any | None:
        """Resolve a collection lazily, treating only optional experience as absent."""
        try:
            collection = self.collection_provider(self.collection_name)
        except (KeyError, LookupError):
            if self.optional_empty:
                return None
            raise RuntimeError(
                f"Required Chroma collection missing: {self.collection_name}"
            )
        if collection is None:
            if self.optional_empty:
                return None
            raise RuntimeError(
                f"Required Chroma collection missing: {self.collection_name}"
            )
        metadata = collection.metadata or {}
        if not metadata and self.optional_empty and int(collection.count()) == 0:
            return collection
        IndexManifest.from_metadata(metadata).validate(self.identity)
        return collection

    def readiness(self) -> dict[str, Any]:
        """Report collection status without initializing embeddings or networks."""
        try:
            collection = self._collection()
            if collection is None:
                return {"status": "empty", "count": 0}
            count = int(collection.count())
            return {"status": "ok" if count else "empty", "count": count}
        except Exception as exc:
            return {"status": "down", "error": type(exc).__name__}

    def search(
        self,
        query: str,
        institution_id: str,
        limit: int,
        metadata_filter: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Query within a required institution scope and return stable ranked hits."""
        if not institution_id:
            raise ValueError("institution_id is required for retrieval")
        collection = self._collection()
        if collection is None or collection.count() == 0:
            return []
        if str((collection.metadata or {}).get("institution_id", "")) != str(
            institution_id
        ):
            raise ValueError("Chroma index institution scope is incompatible")
        if self.query_embedder is None:
            raise RuntimeError(
                "Configured embedding provider is required for Chroma retrieval"
            )
        query_embedding = self.query_embedder(query)
        if len(query_embedding) != self.identity.dimension:
            raise ValueError(
                "Query embedding dimension does not match the Chroma index identity"
            )
        if (
            metadata_filter
            and "institution_id" in metadata_filter
            and metadata_filter["institution_id"] != institution_id
        ):
            raise ValueError("metadata_filter cannot change the institution scope")
        predicates = {**(metadata_filter or {}), "institution_id": institution_id}
        where: dict[str, Any] = (
            predicates
            if len(predicates) == 1
            else {"$and": [{key: value} for key, value in predicates.items()]}
        )
        result = collection.query(
            query_embeddings=[query_embedding],
            n_results=max(1, limit),
            where=where,
            # Chroma always returns ids and rejects "ids" in include on current
            # releases; request only optional payload fields.
            include=["documents", "metadatas", "distances"],
        )
        ids = (result.get("ids") or [[]])[0]
        documents = (result.get("documents") or [[]])[0]
        metadatas = (result.get("metadatas") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]
        rows = []
        for idx, doc_id in enumerate(ids):
            metadata = metadatas[idx] or {}
            # Defensive tenant check even if the vector backend ignores `where`.
            if metadata.get("institution_id") != institution_id:
                continue
            if any(
                metadata.get(key) != value
                for key, value in (metadata_filter or {}).items()
            ):
                continue
            score = 1.0 - float(distances[idx])
            if score < self.threshold:
                continue
            rows.append(
                {
                    "id": str(doc_id),
                    "document": documents[idx],
                    "metadata": metadata,
                    "score": score,
                }
            )
        rows.sort(key=lambda row: (-row["score"], row["id"]))
        return rows[:limit]


class Neo4jReranker:
    """Optional managed Neo4j reranker with bounded, parameterized queries."""

    def __init__(self, config: Settings = settings, driver: Any = None) -> None:
        """Store config and injected driver; create the official driver on first use."""
        self.config = config
        self._driver = driver
        self.metrics = {"rerank_success": 0, "rerank_degraded": 0}

    def _get_driver(self) -> Any:
        """Initialize the Neo4j driver lazily and require explicit credentials."""
        if not self.config.DWH_SQL_NEO4J_ENABLED:
            raise RuntimeError("Neo4j reranking is disabled")
        if not all(
            (
                self.config.DWH_SQL_NEO4J_URI,
                self.config.DWH_SQL_NEO4J_USER,
                self.config.DWH_SQL_NEO4J_PASSWORD,
            )
        ):
            raise ValueError("Neo4j URI, user and password are required when enabled")
        if self._driver is None:
            from neo4j import GraphDatabase

            self._driver = GraphDatabase.driver(
                self.config.DWH_SQL_NEO4J_URI,
                auth=(
                    self.config.DWH_SQL_NEO4J_USER,
                    self.config.DWH_SQL_NEO4J_PASSWORD,
                ),
                connection_timeout=self.config.DWH_SQL_NEO4J_TIMEOUT_SECONDS,
            )
        return self._driver

    def readiness(self) -> dict[str, Any]:
        """Report disabled, ready, or degraded graph status."""
        if not self.config.DWH_SQL_NEO4J_ENABLED:
            return {"status": "disabled"}
        try:
            driver = self._get_driver()
            driver.verify_connectivity()
            if self.config.DWH_SQL_NEO4J_REQUIRED:
                expected = self.config.DWH_SQL_NEO4J_GRAPH_VERSION
                if not expected:
                    raise ValueError("DWH_SQL_NEO4J_GRAPH_VERSION is required")
                with driver.session() as session:
                    records = session.run(
                        "MATCH (m:DwhScenarioGraphManifest {name: 'active'}) "
                        "RETURN m.version AS version LIMIT 1"
                    )
                    record = next(iter(records), None)
                if record is None or str(record["version"]) != expected:
                    raise ValueError("Required Neo4j graph version is not active")
            return {"status": "ok"}
        except Exception as exc:
            return {"status": "degraded", "error": type(exc).__name__}

    def rerank(
        self, candidates: list[dict[str, Any]], limit: int
    ) -> list[dict[str, Any]]:
        """Apply graph score to candidate IDs; return input order on graph failure."""
        if not candidates:
            return []
        try:
            session = self._get_driver().session()
            with session:
                records = session.run(
                    "MATCH (n) WHERE n.id IN $ids RETURN n.id AS id, coalesce(n.rank, 0) AS rank LIMIT $limit",
                    ids=[row["id"] for row in candidates],
                    limit=min(limit, self.config.DWH_SQL_NEO4J_MAX_RESULTS),
                    timeout=self.config.DWH_SQL_NEO4J_TIMEOUT_SECONDS,
                )
                ranks = {record["id"]: float(record["rank"]) for record in records}
            sem_w = self.config.DWH_SQL_SEMANTIC_WEIGHT
            graph_w = self.config.DWH_SQL_GRAPH_WEIGHT
            ranked = sorted(
                candidates,
                key=lambda row: (
                    -(sem_w * row["score"] + graph_w * ranks.get(row["id"], 0.0)),
                    row["id"],
                ),
            )
            self.metrics["rerank_success"] += 1
            return ranked[:limit]
        except Exception:
            self.metrics["rerank_degraded"] += 1
            return sorted(candidates, key=lambda row: (-row["score"], row["id"]))[
                :limit
            ]

    def close(self) -> None:
        """Close an initialized driver during application shutdown."""
        if self._driver is not None:
            self._driver.close()
            self._driver = None


def readiness_report(
    oracle_check: Any,
    chroma_checks: dict[str, Any],
    neo4j_check: Any,
) -> dict[str, Any]:
    """Evaluate dependency probes independently and summarize service readiness."""
    dependencies: dict[str, Any] = {}
    for name, check in [
        ("oracle", oracle_check),
        *chroma_checks.items(),
        ("neo4j", neo4j_check),
    ]:
        try:
            value = check() if callable(check) else check
            dependencies[name] = (
                value if isinstance(value, dict) else {"status": str(value)}
            )
        except Exception as exc:
            dependencies[name] = {"status": "down", "error": str(exc)}
    states = [item.get("status") for item in dependencies.values()]
    status = (
        "down"
        if "down" in states
        else (
            "degraded"
            if any(state not in {"ok", "empty", "disabled"} for state in states)
            else "ready"
        )
    )
    return {"status": status, "dependencies": dependencies}


def build_readiness_checks(
    config: Settings = settings, oracle_check: Any = None
) -> tuple[dict[str, Any], Any]:
    """Build lazy readiness probes; no optional client is constructed here."""
    identity = embedding_identity(config)
    client: Any = None

    def collection_provider(name: str) -> Any:
        nonlocal client
        if config.DWH_SQL_CHROMA_PERSIST_DIR is None:
            if name == config.DWH_SQL_EXPERIENCE_COLLECTION:
                return None
            raise RuntimeError("DWH_SQL_CHROMA_PERSIST_DIR is not configured")
        if client is None:
            try:
                import chromadb
            except ImportError as exc:
                raise RuntimeError(
                    "optional chromadb package is not installed"
                ) from exc
            client = chromadb.PersistentClient(
                path=str(config.DWH_SQL_CHROMA_PERSIST_DIR)
            )
        try:
            return client.get_collection(name=name)
        except Exception as exc:
            if type(exc).__name__ == "ChromaNotFoundError":
                raise KeyError(name) from exc
            raise

    names = {
        "schema_tables": config.DWH_SQL_SCHEMA_TABLES_COLLECTION,
        "schema_columns": config.DWH_SQL_SCHEMA_COLUMNS_COLLECTION,
        "experience_templates": config.DWH_SQL_EXPERIENCE_COLLECTION,
    }
    chroma_checks = {
        key: lambda collection_name=name, optional=(
            key == "experience_templates"
        ): ChromaRetriever(
            collection_name,
            collection_provider,
            identity,
            optional_empty=optional,
        ).readiness()
        for key, name in names.items()
    }

    def schema_set_readiness() -> dict[str, Any]:
        """Verify that the active table and column indexes are one build."""
        from services.aml_builder.web.services.dwh_scenario.toolchain import (
            validate_schema_collection_versions,
        )

        try:
            version = validate_schema_collection_versions(
                (
                    collection_provider(config.DWH_SQL_SCHEMA_TABLES_COLLECTION),
                    collection_provider(config.DWH_SQL_SCHEMA_COLUMNS_COLLECTION),
                )
            )
            return {"status": "ok", "version": version}
        except Exception as exc:
            return {"status": "down", "error": type(exc).__name__}

    chroma_checks["schema_set"] = schema_set_readiness
    graph = Neo4jReranker(config)
    return {
        "oracle": oracle_check or {"status": "unknown"},
        "chroma": chroma_checks,
    }, graph
