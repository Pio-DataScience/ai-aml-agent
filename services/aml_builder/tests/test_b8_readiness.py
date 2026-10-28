"""B8 integration guards for semantic scoring and index activation."""

from __future__ import annotations

import pytest

from services.aml_builder.tests.eval.dwh_scenario.runner import semantic_signature
from services.aml_builder.web.services.dwh_scenario.retrieval import Neo4jReranker
from services.aml_builder.web.services.settings import Settings
from services.aml_builder.web.services.dwh_scenario.embeddings import EmbeddingIdentity
from services.aml_builder.web.services.dwh_scenario.toolchain import (
    build_schema_index,
    validate_schema_collection_versions,
)


def test_semantic_signature_ignores_format_and_conjunct_order() -> None:
    """Equivalent predicate order and whitespace receive the same SQL meaning."""
    first = (
        "SELECT CUS_NUM FROM BI_DWH.PIO_TRANSACTIONS "
        "WHERE COUNTRY = 'JO' AND TRA_DATE >= SYSDATE - 30"
    )
    second = (
        "select cus_num from bi_dwh.pio_transactions "
        "where (tra_date >= sysdate - 30) and (country='JO')"
    )
    assert semantic_signature(first) == semantic_signature(second)


def test_semantic_signature_detects_changed_filter_value() -> None:
    """A different business filter value is not classified as parity."""
    first = "SELECT CUS_NUM FROM BI_DWH.TX WHERE COUNTRY = 'JO'"
    second = "SELECT CUS_NUM FROM BI_DWH.TX WHERE COUNTRY = 'US'"
    assert semantic_signature(first) != semantic_signature(second)


def test_schema_collection_activation_rejects_mixed_versions() -> None:
    """A table index and column index from different builds cannot be activated."""

    class Collection:
        def __init__(self, version: str) -> None:
            self.metadata = {"schema_index_version": version}

    with pytest.raises(ValueError, match="incompatible versions"):
        validate_schema_collection_versions(
            (Collection("build-a"), Collection("build-b"))
        )


def test_schema_collection_activation_requires_version_manifest() -> None:
    """Legacy/unversioned schema indexes fail closed in local mode."""

    class Collection:
        metadata: dict[str, str] = {}

    with pytest.raises(ValueError, match="incompatible versions"):
        validate_schema_collection_versions((Collection(), Collection()))


def test_schema_pair_promotion_failure_restores_previous_versions() -> None:
    """Failure on the second collection cannot leave a mixed active pair."""

    class Collection:
        def __init__(self, client, name, count, metadata):
            self.client, self.name, self._count, self.metadata = (
                client,
                name,
                count,
                metadata,
            )
            self.ids, self.documents, self.metadatas = [], [], []

        def count(self):
            return self._count

        def add(self, ids, documents, metadatas, embeddings):
            self._count = len(ids)
            self.ids, self.documents, self.metadatas = ids, documents, metadatas

        def get(self, *, include):
            return {
                "ids": self.ids,
                "documents": self.documents if "documents" in include else None,
                "metadatas": self.metadatas if "metadatas" in include else None,
            }

        def modify(self, *, name):
            if (
                self.name.startswith("schema_columns_build_")
                and name == "schema_columns"
            ):
                raise RuntimeError("simulated second promotion failure")
            del self.client.collections[self.name]
            self.name = name
            self.client.collections[name] = self

    class Client:
        def __init__(self):
            self.collections = {
                "schema_tables": Collection(
                    self, "schema_tables", 1, {"schema_index_version": "old"}
                ),
                "schema_columns": Collection(
                    self, "schema_columns", 1, {"schema_index_version": "old"}
                ),
            }

        def create_collection(self, *, name, metadata):
            collection = Collection(self, name, 0, metadata)
            self.collections[name] = collection
            return collection

        def get_collection(self, name):
            return self.collections[name]

        def delete_collection(self, name):
            del self.collections[name]

    class Embeddings:
        def embed_documents(self, texts):
            return [[0.1, 0.2] for _ in texts]

    client = Client()
    with pytest.raises(RuntimeError, match="second promotion failure"):
        build_schema_index(
            client,
            [
                {
                    "id": "new-table",
                    "text": "table",
                    "metadata": {"table_name": "BI_DWH.PIO_TRANSACTIONS"},
                }
            ],
            [
                {
                    "id": "new-column",
                    "text": "column",
                    "metadata": {"table_name": "BI_DWH.PIO_TRANSACTIONS"},
                }
            ],
            Embeddings(),
            EmbeddingIdentity("fake", "model", 2, "fingerprint"),
            institution_id="tenant-test",
            config=Settings(
                ORACLE_DSN="unused",
                ORACLE_USER="unused",
                ORACLE_PASSWORD="unused",
                DWH_SQL_ALLOWED_SCHEMAS=("BI_DWH",),
                DWH_SQL_ALLOWED_OBJECTS=("BI_DWH.PIO_TRANSACTIONS",),
            ),
        )
    assert (
        client.get_collection("schema_tables").metadata["schema_index_version"] == "old"
    )
    assert (
        client.get_collection("schema_columns").metadata["schema_index_version"]
        == "old"
    )


def test_required_neo4j_graph_needs_matching_owner_manifest() -> None:
    """Required graph reranking stays degraded until its active version matches."""

    class Session:
        def __init__(self, version: str | None) -> None:
            self.version = version

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def run(self, *_args, **_kwargs):
            return [] if self.version is None else [{"version": self.version}]

    class Driver:
        def __init__(self, version: str | None) -> None:
            self.version = version

        def verify_connectivity(self):
            return None

        def session(self):
            return Session(self.version)

    config = Settings(
        ORACLE_DSN="unused",
        ORACLE_USER="unused",
        ORACLE_PASSWORD="unused",
        _env_file=None,
        DWH_SQL_NEO4J_ENABLED=True,
        DWH_SQL_NEO4J_REQUIRED=True,
        DWH_SQL_NEO4J_GRAPH_VERSION="approved-v2",
        DWH_SQL_NEO4J_URI="bolt://fake",
        DWH_SQL_NEO4J_USER="fake",
        DWH_SQL_NEO4J_PASSWORD="fake",
    )
    assert Neo4jReranker(config, Driver("old-v1")).readiness()["status"] == "degraded"
    assert Neo4jReranker(config, Driver("approved-v2")).readiness()["status"] == "ok"
