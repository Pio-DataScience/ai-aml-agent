"""Tests for the scope guard and its use in POST /chat/stream.

The judge is replaced by a fake, so no LLM is called. The chat route test uses a fake graph,
so no Oracle or LangGraph checkpoint is needed.

Usage:
    PYTHONPATH=services/aml_builder pytest services/aml_builder/tests/test_scope_guard.py
"""

import asyncio
import json
from types import SimpleNamespace
from typing import Any, Dict, List, Sequence, Tuple

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from services.aml_builder.web.api.routes import chat as chat_route
from services.aml_builder.web.services import scope_guard
from services.aml_builder.web.services.schemas import ChatRequest
from services.aml_builder.web.services.settings import settings


@pytest.fixture(autouse=True)
def _fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enable the guard and clear its cache for every test.

    Args:
        monkeypatch (pytest.MonkeyPatch): The pytest monkeypatch fixture.
    """
    monkeypatch.setattr(settings, "AML_SCOPE_GUARD", True)
    scope_guard.forget()


def _judge_returning(category: str, calls: List[str]) -> Any:
    """Build a fake judge that records the messages it is asked about.

    Args:
        category (str): The category to return.
        calls (List[str]): Receives each judged message.

    Returns:
        Any: A judge factory.
    """
    def judge(_history: Sequence[Tuple[str, str]], message: str) -> Dict[str, str]:
        calls.append(message)
        return {"subject": "test", "category": category}

    return lambda: judge


@pytest.mark.parametrize("question", ["who is real madrid", "who is the team that won in the world cup in 2014"])
def test_unrelated_questions_are_declined(monkeypatch: pytest.MonkeyPatch, question: str) -> None:
    """Football questions get the fixed refusal."""
    monkeypatch.setattr(scope_guard, "_judge_factory", _judge_returning("unrelated", []))

    verdict = scope_guard.check(question)

    assert verdict.blocked
    assert verdict.refusal == scope_guard.REFUSAL


def test_arabic_message_gets_arabic_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    """The refusal follows the user's language."""
    monkeypatch.setattr(scope_guard, "_judge_factory", _judge_returning("unrelated", []))

    assert scope_guard.check("من فاز بكأس العالم ٢٠١٤").refusal == scope_guard.REFUSAL_AR


@pytest.mark.parametrize("category", ["scenario_building", "scenario_registry", "aml_knowledge", "assistant", "unsure"])
def test_everything_but_unrelated_continues(monkeypatch: pytest.MonkeyPatch, category: str) -> None:
    """Only "unrelated" is declined; "unsure" continues."""
    monkeypatch.setattr(scope_guard, "_judge_factory", _judge_returning(category, []))

    assert not scope_guard.check("Detect customers with cash deposits over 10,000 JOD in 7 days").blocked


def test_short_reply_in_a_conversation_is_not_judged(monkeypatch: pytest.MonkeyPatch) -> None:
    """'proceed' after the agent asked something is an answer, not a new topic."""
    calls: List[str] = []
    monkeypatch.setattr(scope_guard, "_judge_factory", _judge_returning("unrelated", calls))

    verdict = scope_guard.check("proceed", [("user", "Detect structuring"), ("assistant", "Approve the plan?")])

    assert verdict.status == scope_guard.SKIPPED and calls == []


def test_judge_failure_lets_the_turn_continue(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failing or senseless judge never causes a refusal."""
    def broken() -> Any:
        def judge(_h: Sequence[Tuple[str, str]], _m: str) -> Any:
            raise TimeoutError("slow")
        return judge

    monkeypatch.setattr(scope_guard, "_judge_factory", broken)
    assert scope_guard.check("who is real madrid").status == scope_guard.UNAVAILABLE

    monkeypatch.setattr(scope_guard, "_judge_factory", lambda: (lambda _h, _m: {"category": "banana"}))
    assert scope_guard.check("who is real madrid again").status == scope_guard.UNAVAILABLE


def test_switched_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """AML_SCOPE_GUARD=false skips the check entirely."""
    calls: List[str] = []
    monkeypatch.setattr(settings, "AML_SCOPE_GUARD", False)
    monkeypatch.setattr(scope_guard, "_judge_factory", _judge_returning("unrelated", calls))

    assert scope_guard.check("who is real madrid").status == scope_guard.SKIPPED and calls == []


# ─────────────────────────── chat route ───────────────────────────


class _FakeGraph:
    """Stands in for the compiled agent: records state updates, fails if the agent is run."""

    def __init__(self) -> None:
        self.updates: List[Dict[str, Any]] = []
        self.ran = False

    async def aget_state(self, _config: Dict[str, Any]) -> Any:
        return SimpleNamespace(values={"messages": []})

    async def aupdate_state(self, _config: Dict[str, Any], values: Dict[str, Any], as_node: str = "") -> None:
        self.updates.append({"values": values, "as_node": as_node})

    async def astream(self, *_args: Any, **_kwargs: Any) -> Any:
        self.ran = True
        if False:  # pragma: no cover - makes this an async generator
            yield {}


def _stream(monkeypatch: pytest.MonkeyPatch, graph: _FakeGraph, text: str) -> List[Dict[str, Any]]:
    """Call the chat route and collect its SSE events.

    Args:
        monkeypatch (pytest.MonkeyPatch): The pytest monkeypatch fixture.
        graph (_FakeGraph): The fake agent graph.
        text (str): The user message.

    Returns:
        List[Dict[str, Any]]: The decoded SSE events.
    """
    async def fake_graph() -> _FakeGraph:
        return graph

    monkeypatch.setattr(chat_route, "get_tool_driven_graph", fake_graph)
    monkeypatch.setattr(chat_route, "upsert_chat_session", lambda *a, **k: None)
    request = ChatRequest(messages=[{"role": "user", "content": text}],
                          metadata={"user_id": "u", "project_id": "p", "chat_id": "c"})

    async def collect() -> List[Dict[str, Any]]:
        response = await chat_route.chat_stream(request)
        return [json.loads(chunk[len("data: "):]) async for chunk in response.body_iterator]

    return asyncio.run(collect())


def test_route_declines_without_running_the_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unrelated message is answered with the refusal, recorded in history, and the agent never runs."""
    monkeypatch.setattr(scope_guard, "_judge_factory", _judge_returning("unrelated", []))
    graph = _FakeGraph()

    events = _stream(monkeypatch, graph, "who is real madrid")

    contents = [e["text"] for e in events if e["type"] == "content"]
    assert contents == [scope_guard.REFUSAL]
    assert events[-1]["type"] == "done"
    assert not graph.ran
    recorded = graph.updates[0]["values"]["messages"]
    assert isinstance(recorded[0], HumanMessage) and recorded[0].content == "who is real madrid"
    assert isinstance(recorded[1], AIMessage) and recorded[1].content == scope_guard.REFUSAL


def test_route_runs_the_agent_for_in_scope_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    """An AML scenario request goes to the agent as before."""
    monkeypatch.setattr(scope_guard, "_judge_factory", _judge_returning("scenario_building", []))
    graph = _FakeGraph()

    _stream(monkeypatch, graph, "Detect customers whose cash deposits total more than 10,000 JOD within 7 days")

    assert graph.ran and graph.updates == []
