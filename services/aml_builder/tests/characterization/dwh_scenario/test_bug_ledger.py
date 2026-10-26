"""Strict expected-failure source contracts for B's verified bug ledger.

The source probes intentionally avoid importing B's heavy application graph.
When a B# implementation lands in A, its package must replace its corresponding
legacy source probe with an offline behavioral test of the new adapter.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


SERVICE_B = Path(__file__).resolve().parents[6] / "PioTech-AI" / "services" / "dwh_scenario_agent"
pytestmark = pytest.mark.skipif(not SERVICE_B.is_dir(), reason="PioTech-AI checkout is not available")


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


@pytest.mark.xfail(strict=True, reason="BL1: Service B tool and Oracle guards disagree on CTEs")
def test_bl1_readonly_policy_accepts_cte_and_uses_ast() -> None:
    """Require one AST policy to replace B's conflicting prefix checks."""
    agent = _read_source("web/services/agent.py")
    oracle = _read_source("web/services/oracle.py")
    assert "sqlglot" in agent and "sqlglot" in oracle
    assert "(SELECT|WITH)" not in agent
    assert 'startswith("select")' not in oracle


@pytest.mark.xfail(strict=True, reason="BL2: feedback and retrieval use different collection names")
def test_bl2_experience_collection_name_is_shared() -> None:
    """Require feedback and search to reference the same collection constant."""
    agent = _read_source("web/services/agent.py")
    main = _read_source("web/api/main.py")
    builder = _read_source("scripts/build_experience_index.py")
    assert '"experience_templates"' not in agent + main + builder
    assert '"experience_pairs"' not in agent + main + builder


@pytest.mark.xfail(strict=True, reason="BL3: startup embedding creation passes no API key")
def test_bl3_embedding_factory_receives_configured_key() -> None:
    """Require embedding startup to forward its configured provider credential."""
    main = _read_source("web/api/main.py")
    assert "api_key=None" not in main
    assert "api_key=settings." in main


@pytest.mark.xfail(strict=True, reason="BL4: failed memory compaction silently substitutes canned text")
def test_bl4_memory_failure_never_replaces_context_with_canned_summary() -> None:
    """Require compaction failures to propagate or be deliberately removed."""
    agent = _read_source("web/services/agent.py")
    assert "Summary of prior conversation was lost due to an error." not in agent


@pytest.mark.xfail(strict=True, reason="BL5: critic falls through to OpenAI for unsupported providers")
def test_bl5_critic_has_no_implicit_openai_fallback() -> None:
    """Require unsupported critic providers to fail clearly or use role config."""
    agent = ast.parse(_read_source("web/services/agent.py"))
    critique = next(
        node for node in agent.body
        if isinstance(node, ast.FunctionDef) and node.name == "call_critique_llm_node"
    )
    source = ast.unparse(critique)
    assert "else" not in source or "Unsupported LLM provider" in source


@pytest.mark.xfail(strict=True, reason="BL6: warmup and first-use embedding caches use different device keys")
def test_bl6_embedding_cache_key_matches_warmup_device() -> None:
    """Require the application embedding manager to request the configured device."""
    providers = _read_source("web/services/embeddings_provider.py")
    main = _read_source("web/api/main.py")
    manager = providers.split("class EmbeddingProviderManager:", 1)[1]
    assert "device=settings.EMBED_DEVICE" in main
    assert "device=settings.EMBED_DEVICE" in manager


@pytest.mark.xfail(strict=True, reason="BL7: health is static and startup failures are swallowed")
def test_bl7_health_exposes_dependency_readiness() -> None:
    """Require dependency-level readiness instead of a static healthy response."""
    main = _read_source("web/api/main.py")
    assert 'status="ok"' not in main
    assert '"neo4j"' in main.lower()


@pytest.mark.xfail(strict=True, reason="BL8: index/graph rebuild removes the live version first")
def test_bl8_rebuild_preserves_current_index_and_graph_until_promotion() -> None:
    """Require rebuild code to avoid dropping the active index or graph in place."""
    index_builder = _read_source("scripts/build_hierarchical_index.py")
    graph_builder = _read_source("scripts/build_graph_from_joins.py")
    assert "delete_collection(" not in index_builder
    assert "gds.graph.drop" not in graph_builder


@pytest.mark.xfail(strict=True, reason="BL9: candidate execution fetches all result rows")
def test_bl9_candidate_fetch_is_bounded_and_reports_truncation() -> None:
    """Require candidate SQL retrieval to use bounded fetch and explicit truncation."""
    module = ast.parse(_read_source("web/services/rag_adapter.py"))
    execute = next(
        node for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "execute_sql_query"
    )
    source = ast.unparse(execute)
    assert ".fetchall(" not in source
    assert "fetchmany" in source and "truncated" in source


@pytest.mark.xfail(strict=True, reason="BL10: output-token limit is conflated with context budget")
def test_bl10_generator_honors_separate_output_limit() -> None:
    """Require the generator provider call to pass the configured output limit."""
    agent = ast.parse(_read_source("web/services/agent.py"))
    reasoner = next(
        node for node in agent.body
        if isinstance(node, ast.FunctionDef) and node.name == "call_llm_reasoner"
    )
    source = ast.unparse(reasoner)
    assert "max_tokens=llm_max_tokens" in source
    assert "LLM_CONTEXT_BUDGET" in source
