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
pytestmark = pytest.mark.skipif(
    not SERVICE_B.is_dir(), reason="PioTech-AI checkout is not available"
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


@pytest.mark.xfail(
    strict=True, reason="BL1: Service B tool and Oracle guards disagree on CTEs"
)
def test_bl1_readonly_policy_accepts_cte_and_uses_ast() -> None:
    """Require one AST policy to replace B's conflicting prefix checks."""
    agent = _read_source("web/services/agent.py")
    oracle = _read_source("web/services/oracle.py")
    assert "sqlglot" in agent and "sqlglot" in oracle
    assert "(SELECT|WITH)" not in agent
    assert 'startswith("select")' not in oracle


@pytest.mark.xfail(
    strict=True, reason="BL2: feedback and retrieval use different collection names"
)
def test_bl2_experience_collection_name_is_shared() -> None:
    """Require feedback and search to reference the same collection constant."""
    agent = _read_source("web/services/agent.py")
    main = _read_source("web/api/main.py")
    builder = _read_source("scripts/build_experience_index.py")
    assert '"experience_templates"' not in agent + main + builder
    assert '"experience_pairs"' not in agent + main + builder


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


@pytest.mark.xfail(
    strict=True, reason="BL4: failed memory compaction silently substitutes canned text"
)
def test_bl4_memory_failure_never_replaces_context_with_canned_summary() -> None:
    """Require compaction failures to propagate or be deliberately removed."""
    agent = _read_source("web/services/agent.py")
    assert "Summary of prior conversation was lost due to an error." not in agent


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


@pytest.mark.xfail(
    strict=True, reason="BL7: health is static and startup failures are swallowed"
)
def test_bl7_health_exposes_dependency_readiness() -> None:
    """Require dependency-level readiness instead of a static healthy response."""
    main = _read_source("web/api/main.py")
    assert 'status="ok"' not in main
    assert '"neo4j"' in main.lower()


@pytest.mark.xfail(
    strict=True, reason="BL8: index/graph rebuild removes the live version first"
)
def test_bl8_rebuild_preserves_current_index_and_graph_until_promotion() -> None:
    """Require rebuild code to avoid dropping the active index or graph in place."""
    index_builder = _read_source("scripts/build_hierarchical_index.py")
    graph_builder = _read_source("scripts/build_graph_from_joins.py")
    assert "delete_collection(" not in index_builder
    assert "gds.graph.drop" not in graph_builder


@pytest.mark.xfail(
    strict=True, reason="BL9: candidate execution fetches all result rows"
)
def test_bl9_candidate_fetch_is_bounded_and_reports_truncation() -> None:
    """Require candidate SQL retrieval to use bounded fetch and explicit truncation."""
    module = ast.parse(_read_source("web/services/rag_adapter.py"))
    execute = next(
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "execute_sql_query"
    )
    source = ast.unparse(execute)
    assert ".fetchall(" not in source
    assert "fetchmany" in source and "truncated" in source


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
