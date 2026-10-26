"""Strict expected-failure source contracts for B's verified bug ledger.

The source probes intentionally avoid importing B's heavy application graph.
When a B# implementation lands in A, its package must replace its corresponding
legacy source probe with an offline behavioral test of the new adapter.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest


SERVICE_B = (
    Path(__file__).resolve().parents[6]
    / "PioTech-AI"
    / "services"
    / "dwh_scenario_agent"
)


def _read_source(relative_path: str) -> str:
    """Read a legacy source file without importing its package.

    Args:
        relative_path: Path relative to Service B's project root.

    Returns:
        Source text for static characterization.

    Raises:
        FileNotFoundError: If the source checkout or requested file is absent.
    """
    return (SERVICE_B / relative_path).read_text(encoding="utf-8")


def test_bl1_readonly_policy_accepts_cte_and_repository_executes_it() -> None:
    """A's AST guard and repository accept the same safe CTE SQL."""
    from contextlib import contextmanager
    from services.aml_builder.web.adapters.dwh_oracle_repo import (
        DwhOracleRepository,
        OracleRequestBudget,
    )
    from services.aml_builder.web.domain.dwh_scenario.sql_policy import (
        validate_oracle_read,
    )
    from services.aml_builder.web.services.settings import settings

    sql = "WITH q AS (SELECT CUS_NUM FROM BI_DWH.TX) SELECT CUS_NUM FROM q"
    validate_oracle_read(sql, allowed_schemas=("BI_DWH",), allowed_objects=("TX",))

    class Cursor:
        description = [("CUS_NUM",)]
        call_timeout = None
        executed = None

        def execute(self, statement, params):
            self.executed = statement

        def fetchmany(self, _size):
            return [("1",)]

    class Connection:
        def cursor(self):
            return cursor

    @contextmanager
    def provider():
        yield Connection()

    old_schema, old_objects = (
        settings.DWH_SQL_ALLOWED_SCHEMAS,
        settings.DWH_SQL_ALLOWED_OBJECTS,
    )
    settings.DWH_SQL_ALLOWED_SCHEMAS, settings.DWH_SQL_ALLOWED_OBJECTS = ("BI_DWH",), (
        "TX",
    )
    cursor = Cursor()
    try:
        result = DwhOracleRepository(connection_provider=provider).execute_candidate(
            sql, budget=OracleRequestBudget(max_statements=1)
        )
    finally:
        settings.DWH_SQL_ALLOWED_SCHEMAS, settings.DWH_SQL_ALLOWED_OBJECTS = (
            old_schema,
            old_objects,
        )
    assert result.success and cursor.executed == sql


def test_bl2_experience_collection_name_is_shared() -> None:
    """Require shared identity and safe behavior when experience is absent."""
    from services.aml_builder.web.services.dwh_scenario.embeddings import (
        EmbeddingIdentity,
    )
    from services.aml_builder.web.services.dwh_scenario.retrieval import (
        EXPERIENCE_COLLECTION_NAME,
        ChromaRetriever,
    )

    assert EXPERIENCE_COLLECTION_NAME == "experience_templates"
    retriever = ChromaRetriever(
        EXPERIENCE_COLLECTION_NAME,
        lambda _name: (_ for _ in ()).throw(KeyError("missing")),
        EmbeddingIdentity("openai", "test", 2, "test"),
        optional_empty=True,
    )
    assert retriever.search("scenario", "inst-a", 3) == []
    assert retriever.readiness()["status"] == "empty"


def test_bl3_embedding_factory_receives_configured_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OpenAI embedding adapter injects the configured key into its client."""
    from services.aml_builder.web.services.dwh_scenario import embeddings
    from services.aml_builder.web.services.settings import Settings

    captured: dict[str, object] = {}

    class FakeEmbeddings:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr("langchain_openai.OpenAIEmbeddings", FakeEmbeddings)
    config = Settings(
        ORACLE_DSN="unused",
        ORACLE_USER="unused",
        ORACLE_PASSWORD="unused",
        _env_file=None,
        OPENAI_API_KEY="key-canary",
        DWH_SQL_EMBEDDINGS_PROVIDER="openai",
    )
    embeddings._MODEL_CACHE.clear()
    embeddings.EmbeddingProviderManager(config).get()
    assert captured["api_key"] == "key-canary"


def test_bl4_local_request_context_does_not_use_memory_compaction() -> None:
    """One scenario request is assembled from typed state without lossy compaction."""
    from services.aml_builder.web.domain.dwh_scenario.contracts import (
        ScenarioSqlRequest,
    )
    from services.aml_builder.web.services.dwh_scenario.context_assembly import (
        assemble_context,
    )

    request = ScenarioSqlRequest(
        intent={
            "scenario_name": "Current scenario",
            "scenario_type": "TRANSACTION",
            "detection_logic": "Current intent",
            "ready_for_handoff": True,
        }
    )
    context = assemble_context(
        request, experience=[], tables=[], columns=[], budget=10000
    )
    assert "Current intent" in context
    assert "Summary of prior conversation was lost due to an error." not in context


def test_bl5_critic_uses_its_role_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Critic construction uses critic provider/model instead of global fallback."""
    from services.aml_builder.web.services import llm_client

    captured: dict[str, object] = {}
    monkeypatch.setattr(
        llm_client, "ChatOpenAI", lambda **kwargs: captured.update(kwargs) or object()
    )
    config = llm_client.settings
    for key, val in (
        ("LLM_PROVIDER", "openai"),
        ("OPENAI_API_KEY", "openai-test"),
        ("DWH_SQL_CRITIC_PROVIDER", "openrouter"),
        ("DWH_SQL_CRITIC_MODEL", "critic-test"),
        ("DWH_SQL_OPENROUTER_API_KEY", "router-test"),
        ("DWH_SQL_BASE_URL", None),
        ("LANGCHAIN_TRACING_V2", False),
    ):
        monkeypatch.setattr(config, key, val)
    llm_client.build_llm(role="critic")
    assert captured["model"] == "critic-test"
    assert captured["api_key"] == "router-test"
    assert captured["base_url"] == "https://openrouter.ai/api/v1"


def test_bl6_local_embedding_cache_is_keyed_by_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same provider/model/device reuses one lazy local model instance."""
    from services.aml_builder.web.services.dwh_scenario import embeddings
    from services.aml_builder.web.services.settings import Settings

    calls: list[tuple[str, str]] = []

    class FakeModel:
        def __init__(self, model: str, device: str) -> None:
            calls.append((model, device))

    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        types.SimpleNamespace(SentenceTransformer=FakeModel),
    )
    embeddings._MODEL_CACHE.clear()
    config = Settings(
        ORACLE_DSN="unused",
        ORACLE_USER="unused",
        ORACLE_PASSWORD="unused",
        _env_file=None,
        DWH_SQL_EMBEDDINGS_PROVIDER="local_bge",
        DWH_SQL_EMBEDDINGS_MODEL="fake",
        DWH_SQL_EMBEDDINGS_DEVICE="cpu",
    )
    manager = embeddings.EmbeddingProviderManager(config)
    assert manager.get() is manager.get()
    assert calls == [("fake", "cpu")]


def test_bl7_health_exposes_dependency_readiness() -> None:
    """Readiness exposes each dependency status, including optional graph outage."""
    from services.aml_builder.web.services.dwh_scenario.retrieval import (
        readiness_report,
    )

    report = readiness_report(
        lambda: {"status": "ok"},
        {
            "schema_tables": lambda: {"status": "ok"},
            "schema_columns": lambda: {"status": "down"},
            "experience_templates": lambda: {"status": "empty"},
        },
        lambda: {"status": "degraded"},
    )
    assert report["status"] == "down"
    assert report["dependencies"]["schema_columns"]["status"] == "down"
    assert report["dependencies"]["neo4j"]["status"] == "degraded"
    assert (
        readiness_report("ok", {"experience_templates": "empty"}, "disabled")["status"]
        == "ready"
    )


def test_bl8_rebuild_preserves_current_index_and_graph_until_promotion() -> None:
    """Staging validation precedes rename and the previous collection is retained."""
    from services.aml_builder.web.services.dwh_scenario.toolchain import (
        promote_versioned_collection,
    )

    manifest = {
        "embedding_provider": "fake",
        "embedding_model": "m",
        "embedding_dimension": 2,
        "embedding_fingerprint": "f",
    }

    class Collection:
        def __init__(self, name, count, metadata):
            self.name, self._count, self.metadata = name, count, metadata

        def count(self):
            return self._count

        def modify(self, *, name):
            del collections[self.name]
            self.name = name
            collections[name] = self

    collections = {
        "schema_tables": Collection("schema_tables", 1, manifest),
        "staging": Collection("staging", 2, manifest),
    }

    class Client:
        def get_collection(self, name):
            return collections[name]

    with pytest.raises(ValueError, match="count"):
        promote_versioned_collection(
            Client(),
            temporary_name="staging",
            live_name="schema_tables",
            expected_count=1,
            expected_manifest=manifest,
        )
    assert "schema_tables" in collections
    collections["staging"]._count = 1
    previous = promote_versioned_collection(
        Client(),
        temporary_name="staging",
        live_name="schema_tables",
        expected_count=1,
        expected_manifest=manifest,
    )
    assert previous and previous in collections and "schema_tables" in collections


def test_bl9_bounded_fetch_and_truncation_are_behaviorally_covered() -> None:
    """B3 fake cursor proves bounded sample and explicit truncation evidence."""
    from services.aml_builder.web.adapters.dwh_oracle_repo import OracleRequestBudget
    from services.aml_builder.web.services.settings import settings
    from tests.test_b3_oracle_sql_policy import FakeCursor, repository

    old_limit = settings.DWH_SQL_MAX_CANDIDATE_ROWS
    old_schemas = settings.DWH_SQL_ALLOWED_SCHEMAS
    settings.DWH_SQL_MAX_CANDIDATE_ROWS = 2
    settings.DWH_SQL_ALLOWED_SCHEMAS = ("BI_DWH",)
    cursor = FakeCursor([(1,), (2,), (3,)])
    try:
        result = repository(cursor).execute_candidate(
            "SELECT CUS_NUM FROM BI_DWH.TX", budget=OracleRequestBudget()
        )
    finally:
        settings.DWH_SQL_MAX_CANDIDATE_ROWS = old_limit
        settings.DWH_SQL_ALLOWED_SCHEMAS = old_schemas
    assert result.rows == ((1,), (2,))
    assert result.row_count == 2 and result.truncated is True
    assert cursor.fetch_limit == 3


def test_b7_scoped_sql_emits_backup_update_and_reverse_without_execution(
    tmp_path,
) -> None:
    """Enrichment outputs scope every update by table and column, including project."""
    from services.aml_builder.web.services.dwh_scenario.toolchain import (
        scoped_enrichment_sql,
    )

    rows = [
        {
            "table_name": "TX_A",
            "column_name": "STATUS",
            "project_code": "AML",
            "old": "old A",
            "new": "new A",
        },
        {
            "table_name": "TX_B",
            "column_name": "STATUS",
            "project_code": "AML",
            "old": "old B",
            "new": "new B",
        },
    ]
    backup, updates, reverse = scoped_enrichment_sql(
        rows, description_column="OBJECT_NAME", project_column="PROJECT_CODE"
    )
    assert (
        "TABLE_NAME = 'TX_A' AND COLUMN_NAME = 'STATUS' AND PROJECT_CODE = 'AML'"
        in updates
    )
    assert (
        "TABLE_NAME = 'TX_B' AND COLUMN_NAME = 'STATUS' AND PROJECT_CODE = 'AML'"
        in updates
    )
    assert "SELECT TABLE_NAME, COLUMN_NAME, OBJECT_NAME, PROJECT_CODE" in backup
    assert "SET OBJECT_NAME = 'old A'" in reverse
    assert "cursor.execute" not in updates


def test_bl10_output_tokens_are_independent_of_context_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DWH factory passes output limit without conflating the input budget."""
    from services.aml_builder.web.services import llm_client

    captured: dict[str, object] = {}
    monkeypatch.setattr(
        llm_client, "ChatOpenAI", lambda **kwargs: captured.update(kwargs) or object()
    )
    config = llm_client.settings
    for key, val in (
        ("LLM_PROVIDER", "openai"),
        ("DWH_SQL_GENERATOR_PROVIDER", "openai"),
        ("DWH_SQL_GENERATOR_MODEL", "gpt-4o"),
        ("DWH_SQL_OPENAI_API_KEY", "test"),
        ("DWH_SQL_OUTPUT_TOKEN_LIMIT", 321),
        ("DWH_SQL_CONTEXT_BUDGET", 9876),
        ("DWH_SQL_RETRY_BUDGET", 2),
        ("DWH_SQL_MAX_ATTEMPTS", 4),
        ("DWH_SQL_READ_TIMEOUT_SECONDS", 31.0),
        ("DWH_SQL_CONNECT_TIMEOUT_SECONDS", 4.0),
        ("DWH_SQL_DEADLINE_SECONDS", 77.0),
        ("LANGCHAIN_TRACING_V2", False),
    ):
        monkeypatch.setattr(config, key, val)
    llm_client.build_llm(role="generator")
    assert captured["max_tokens"] == 321
    assert captured["max_retries"] == 2
    assert captured["timeout"] == 77.0
    assert captured["http_client"].timeout.read == 31.0
    assert captured["http_client"].timeout.connect == 4.0
    assert config.DWH_SQL_CONTEXT_BUDGET == 9876
