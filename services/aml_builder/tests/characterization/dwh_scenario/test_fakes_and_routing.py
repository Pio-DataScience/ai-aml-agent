"""Exercise deterministic fake adapters and B0 routing fixtures."""

from __future__ import annotations

import pytest

from .fakes import FakeChroma, FakeLLM, FakeNeo4j, FakeOracle


@pytest.mark.parametrize(
    ("fixture_name", "expected_events"),
    [
        ("one_turn_success", ["generate", "execute", "approve"]),
        ("tool_loop", ["think", "search", "execute", "approve"]),
        ("critique_approve", ["execute", "critique_approve"]),
        ("critique_repair", ["execute", "reject", "repair", "execute", "approve"]),
        ("repeated_rejection", ["execute", "reject", "repair", "reject", "stop"]),
        ("max_tool_calls", ["tool", "tool", "budget_stop"]),
        ("provider_error", ["provider_error", "fail_closed"]),
        ("oracle_error", ["execute_error", "fail_closed"]),
        ("empty_retrieval", ["retrieve_empty", "continue"]),
        ("neo4j_down", ["graph_degraded", "continue_chroma_only"]),
        ("missing_collection", ["collection_missing", "continue_without_examples"]),
    ],
)
def test_graph_routing_fixture_contract(fixture_name: str, expected_events: list[str]) -> None:
    """Keep the planned graph-routing scenarios explicit before B5 exists.

    Args:
        fixture_name: Stable fixture key used by the future injected graph.
        expected_events: Ordered routing events expected from that fixture.

    Returns:
        None.

    Raises:
        AssertionError: If a fixture has no meaningful route or a stop is not terminal.
    """
    assert fixture_name
    assert expected_events
    if "fail_closed" in expected_events:
        assert expected_events[-1] == "fail_closed"
    if fixture_name == "repeated_rejection":
        assert expected_events[-1] == "stop"


def test_fake_adapters_cover_scripted_edges() -> None:
    """Check deterministic behavior required by future routing tests.

    Args:
        None.

    Returns:
        None.

    Raises:
        AssertionError: If a fake does not preserve the configured behavior.
    """
    oracle = FakeOracle({"SELECT 1": (["N"], [(1,)])}, {"SELECT SLOW": set()})
    assert oracle.execute("SELECT 1") == (["N"], [(1,)])
    with pytest.raises(TimeoutError):
        oracle.execute("SELECT SLOW")
    assert len(oracle.statements) == 2

    llm = FakeLLM([{"tool_calls": [{"name": "search", "arguments": "{"}]}])
    assert llm.invoke([])["tool_calls"][0]["arguments"] == "{"
    assert llm.calls == [[]]

    assert FakeChroma({"empty": []}).query("empty", 5) == []
    assert FakeChroma().query("missing", 5) == []
    assert FakeNeo4j().query() == []
    with pytest.raises(ConnectionError):
        FakeNeo4j("down").query()
    with pytest.raises(TimeoutError):
        FakeNeo4j("slow").query()
