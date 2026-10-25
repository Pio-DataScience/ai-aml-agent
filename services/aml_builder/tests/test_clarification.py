"""Tests for the Clarification Question Flow (ask for missing mandatory parameters, never guess).

No database, network or LLM is needed: the intent LLM, the code search, the resolver and the agent graph are mocked.

Usage:
    PYTHONPATH=services/aml_builder pytest services/aml_builder/tests/test_clarification.py
"""

import json
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, ToolMessage

from services.aml_builder.web.api.routes import chat as chat_route
from services.aml_builder.web.services import clarification as cl
from services.aml_builder.web.services import tools as aml_tools
from services.aml_builder.web.services.schemas import ClarificationQuestion, ClarificationResponse

VAGUE = "Flag suspicious deposits"
VAGUE_INTENT: Dict[str, Any] = {
    "scenario_name": "Suspicious deposits", "scenario_type": "CUSTOMER",
    "detection_logic": "Customers making suspicious deposits.",
    "transaction_type": "CASH DEPOSIT", "transaction_types": ["CASH DEPOSIT"],
    "aggregation": {"metric": "cumulative_transaction_volume", "function": "SUM", "grain": "PER CUSTOMER PER WINDOW"},
    "thresholds": [{"field": "cumulative_transaction_volume", "operator": ">", "value_from": 10000,
                    "target_scope": "AGGREGATE", "provenance": "assumed_default"}],
    "time_window": {"unit": "DAYS", "value": 30, "is_rolling": True, "provenance": "assumed_default"},
    "semantic_conditions": [],
}


@pytest.fixture(autouse=True)
def _flow_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clarification flow on, with the agreed defaults.

    Args:
        monkeypatch (pytest.MonkeyPatch): The pytest monkeypatch fixture.
    """
    monkeypatch.setattr(aml_tools.settings, "AML_CLARIFICATION_FLOW", True)
    monkeypatch.setattr(aml_tools.settings, "CLARIFICATION_DEFAULT_AMOUNT", 10000.0)
    monkeypatch.setattr(aml_tools.settings, "CLARIFICATION_DEFAULT_WINDOW_DAYS", 30)
    monkeypatch.setattr(aml_tools.settings, "CLARIFICATION_DEFAULT_CHANNELS", "CASH DEPOSIT,CASH WITHDRAWAL")
    monkeypatch.setattr(aml_tools.settings, "CLARIFICATION_DEFAULT_ENTITY", "CUSTOMER")
    monkeypatch.setattr(aml_tools.settings, "CLARIFICATION_ASK_ENTITY", False)


# ─────────────────────────── gap analysis ───────────────────────────


def test_a_vague_request_misses_amount_timeframe_and_channels() -> None:
    """Values the LLM assumed, or a type the user never named, are gaps; the entity is never asked."""
    assert cl.find_gaps(VAGUE_INTENT, VAGUE) == ["threshold_amount", "timeframe", "channels"]


def test_the_entity_question_can_be_switched_back_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """CLARIFICATION_ASK_ENTITY=True asks 'What should be monitored?' again."""
    monkeypatch.setattr(aml_tools.settings, "CLARIFICATION_ASK_ENTITY", True)
    assert cl.find_gaps(VAGUE_INTENT, VAGUE) == ["target_entity", "threshold_amount", "timeframe", "channels"]


def test_per_customer_is_the_business_default() -> None:
    """Unstated entity -> per customer; a request that names accounts or single transactions keeps it."""
    llm_guess = {**VAGUE_INTENT, "scenario_type": "ACCOUNT", "aggregation": {"function": "SUM", "grain": "PER ACCOUNT PER WINDOW"}}
    decided = cl.default_entity(llm_guess, VAGUE)

    assert decided["scenario_type"] == "CUSTOMER" and decided["aggregation"]["grain"] == "PER CUSTOMER PER WINDOW"
    assert "clarified_fields" not in decided
    assert cl.default_entity(llm_guess, "Flag accounts with more than 20 cash withdrawals in a month")["scenario_type"] == "ACCOUNT"
    single = {**VAGUE_INTENT, "scenario_type": "TRANSACTION", "aggregation": None}
    assert cl.default_entity(single, "Flag any single cash withdrawal above 50,000 in 2019")["scenario_type"] == "TRANSACTION"


@pytest.mark.parametrize(
    "text, intent_patch",
    [
        ("Flag customers whose cash deposits total more than 10,000 JOD within 7 days",
         {"thresholds": [{"field": "cumulative_transaction_volume", "value_from": 10000, "target_scope": "AGGREGATE"}],
          "time_window": {"unit": "DAYS", "value": 7}}),
        ("Flag any single cash withdrawal above 50,000 between 2016 and 2022",
         {"scenario_type": "TRANSACTION", "aggregation": None, "transaction_types": ["CASH WITHDRAWAL"],
          "thresholds": [{"field": "transaction_amount", "value_from": 50000, "target_scope": "DETAIL"}],
          "time_window": {"unit": "YEARS", "value": 7, "is_rolling": False}}),
        ("Flag accounts with more than 20 cash withdrawals in a month",
         {"scenario_type": "ACCOUNT", "transaction_types": ["CASH WITHDRAWAL"],
          "thresholds": [{"field": "transaction_count", "value_from": 20, "target_scope": "AGGREGATE"}],
          "time_window": {"unit": "MONTHS", "value": 1}}),
        ("Flag customers with any transactions above 5k daily",
         {"transaction_type": "ALL", "transaction_types": None,
          "thresholds": [{"field": "transaction_amount", "value_from": 5000, "target_scope": "DETAIL"}],
          "time_window": {"unit": "DAYS", "value": 1}}),
    ],
)
def test_a_fully_stated_request_has_no_gaps(text: str, intent_patch: Dict[str, Any]) -> None:
    """Entity, number, timeframe and channel written by the user: nothing is asked."""
    assert cl.find_gaps({**VAGUE_INTENT, **intent_patch}, text) == []


def test_a_number_the_user_never_wrote_is_a_gap() -> None:
    """Even marked 'stated', an amount absent from the text is a guess."""
    intent = {**VAGUE_INTENT, "thresholds": [{"field": "transaction_amount", "value_from": 10000, "provenance": "stated"}]}
    assert "threshold_amount" in cl.find_gaps(intent, "Flag customers with large cash deposits within 7 days")


def test_the_users_words_win_over_the_llm_label() -> None:
    """A written amount or period is never asked again, even if the LLM labels it 'assumed_default'."""
    intent = {**VAGUE_INTENT,
              "thresholds": [{"field": "transaction_amount", "value_from": 50000, "provenance": "assumed_default"}],
              "time_window": {"unit": "DAYS", "value": 7, "provenance": "assumed_default"}}
    gaps = cl.find_gaps(intent, "Flag customers with cash deposits above 50,000 JOD within 7 days")
    assert "threshold_amount" not in gaps and "timeframe" not in gaps


def test_any_without_a_type_means_all_transaction_types() -> None:
    """Live case: 'Flag any above 50,000 JOD' asks only the timeframe, and the scope is all types."""
    intent = {"scenario_type": "CUSTOMER", "transaction_type": None, "transaction_types": None,
              "thresholds": [{"field": "transaction_amount", "operator": ">", "value_from": 50000.0}],
              "time_window": {"unit": "DAY", "value": 1, "is_rolling": False}}
    text = "Flag any above 50,000 JOD"

    assert cl.find_gaps(intent, text) == ["timeframe"]
    assert cl.all_types_if_any(intent, text)["transaction_type"] == "ALL"
    assert cl.all_types_if_any({**intent, "transaction_types": ["CASH DEPOSIT"]}, text)["transaction_types"] == ["CASH DEPOSIT"]
    assert cl.all_types_if_any(intent, "Flag large deposits") == intent


def test_a_debit_credit_filter_states_the_channel() -> None:
    """'credit transactions' is answered by the attribute filter, not a transaction type."""
    intent = {"scenario_type": "TRANSACTION", "transaction_types": None, "transaction_type": None,
              "attribute_filters": [{"attribute": "debit_credit", "codes": ["2"]}],
              "thresholds": [{"field": "transaction_amount", "value_from": 500000, "target_scope": "DETAIL"}],
              "time_window": {"unit": "YEARS", "value": 1, "is_rolling": False}}
    assert cl.find_gaps(intent, "Flag credit transactions above 500,000 JOD in 2019") == []


def test_an_amount_on_each_transaction_states_the_entity() -> None:
    """Live case: 'credit transactions above 500,000' means each transaction, even if the LLM chose CUSTOMER."""
    intent = {"scenario_type": "CUSTOMER", "aggregation": {"function": "SUM"},
              "attribute_filters": [{"attribute": "debit_credit", "codes": ["2"]}],
              "thresholds": [{"field": "transaction_amount", "value_from": 500000, "target_scope": "DETAIL"}],
              "time_window": {"unit": "YEARS", "value": 1, "is_rolling": False}}
    assert cl.find_gaps(intent, "Flag credit transactions above 500,000 JOD in 2019") == []


def test_a_bare_channel_word_is_asked_with_its_options_preselected() -> None:
    """Live case: 'deposits' alone does not say cash or cheque; the form pre-selects both."""
    intent = {**VAGUE_INTENT, "transaction_type": "DEPOSIT", "transaction_types": ["DEPOSIT"]}
    gaps = cl.find_gaps(intent, VAGUE)
    channels = next(q for q in cl.build_questions(gaps, intent) if q["id"] == "channels")

    assert "channels" in gaps
    assert channels["default_value"] == ["CASH DEPOSIT", "CHEQUE DEPOSIT"]
    assert cl.find_gaps({**intent, "transaction_types": ["OUTWARD TRANSFER"]}, "large outward transfers")[-1:] != ["channels"]


def test_a_modification_of_a_confirmed_scenario_asks_nothing() -> None:
    """'Change the amount to 5,000' keeps the confirmed entity, timeframe and channels."""
    confirmed = {"scenario_type": "CUSTOMER", "thresholds": [{"value_from": 10000}],
                 "time_window": {"unit": "DAYS", "value": 7}, "transaction_types": ["CASH DEPOSIT"]}
    intent = {**VAGUE_INTENT, "thresholds": [{"field": "amount", "value_from": 5000, "provenance": "stated"}]}
    assert cl.find_gaps(intent, "Change the amount to 5,000", known=confirmed) == []


# ─────────────────────────── the form ───────────────────────────


def test_questions_carry_the_agreed_industry_defaults() -> None:
    """10,000 JOD, 30 days, cash deposit + withdrawal, per customer; validated by the schema."""
    payload = cl.clarification_payload(VAGUE_INTENT, cl.find_gaps(VAGUE_INTENT, VAGUE), VAGUE)
    questions = {q["id"]: q for q in payload["questions"]}

    assert payload["status"] == "NEEDS_CLARIFICATION" and payload["missing_fields"] == [
        "threshold_amount", "timeframe", "channels"]
    assert "target_entity" not in questions
    assert questions["threshold_amount"]["default_value"] == 10000.0 and questions["threshold_amount"]["unit"] == "JOD"
    assert questions["timeframe"]["default_value"] == 30
    assert questions["channels"]["default_value"] == ["CASH DEPOSIT"]  # pre-selects what the request suggested
    assert questions["date_range"]["required"] is False and questions["date_range"]["show_if"] == {
        "question_id": "timeframe", "equals": "custom"}
    assert [o["value"] for o in questions["timeframe"]["options"]][-5:] == ["Q1", "Q2", "Q3", "Q4", "custom"]
    assert questions["quarter_year"]["show_if"] == {"question_id": "timeframe", "in": ["Q1", "Q2", "Q3", "Q4"]}
    assert questions["channels"]["options"][-1] == {"value": "OTHER", "label": "Other (type it)"}
    assert questions["other_channels"]["input_type"] == "text"
    assert questions["other_channels"]["show_if"] == {"question_id": "channels", "includes": "OTHER"}
    ClarificationResponse(**{k: payload[k] for k in ("understood_summary", "missing_fields", "questions")})
    assert all(ClarificationQuestion(**q) for q in payload["questions"])


def test_the_summary_never_shows_guessed_values() -> None:
    """Assumed amount and window stay out of 'What I understood'."""
    summary = cl.understood_summary(VAGUE_INTENT, cl.find_gaps(VAGUE_INTENT, VAGUE))
    assert summary == ["Goal: Customers making suspicious deposits.",
                       "Monitoring: Per customer (all of a customer's accounts together)"]


# ─────────────────────────── answers ───────────────────────────

ANSWERS = {"target_entity": "ACCOUNT", "threshold_amount": 25000, "timeframe": 7, "channels": ["CASH DEPOSIT", "INWARD TRANSFER"]}


def test_answers_override_the_guesses() -> None:
    """Each answer lands in its field as 'stated'; codes are reset for a fresh search."""
    updated = cl.apply_answers({**VAGUE_INTENT, "explanation_codes": ["1"]}, ANSWERS)

    assert updated["scenario_type"] == "ACCOUNT" and updated["aggregation"]["grain"] == "PER ACCOUNT PER WINDOW"
    assert updated["thresholds"][0]["value_from"] == 25000.0 and updated["thresholds"][0]["provenance"] == "stated"
    assert updated["time_window"] == {"unit": "DAYS", "value": 7, "is_rolling": True, "provenance": "stated"}
    assert updated["transaction_types"] == ["CASH DEPOSIT", "INWARD TRANSFER"] and updated["explanation_codes"] is None
    assert updated["clarified_fields"] == ["channels", "target_entity", "threshold_amount", "timeframe"]
    assert cl.find_gaps(updated, VAGUE, answered=cl.answered_fields(ANSWERS)) == []


def test_a_fixed_date_range_answer_sets_the_period() -> None:
    """'A fixed date range' -> evaluation date = last day, exact non-rolling window."""
    updated = cl.apply_answers(VAGUE_INTENT, {"timeframe": "custom", "date_range": {"start": "2016-01-01", "end": "2019-12-31"}})
    assert updated["anchor_date"] == "2019-12-31"
    assert updated["time_window"] == {"unit": "YEARS", "value": 4, "is_rolling": False, "provenance": "stated"}


@pytest.mark.parametrize(
    "quarter, year, start, end",
    [("Q1", 2019, "2019-01-01", "2019-03-31"), ("Q2", 2018, "2018-04-01", "2018-06-30"),
     ("Q3", 2017, "2017-07-01", "2017-09-30"), ("Q4", 2016, "2016-10-01", "2016-12-31")],
)
def test_a_quarter_answer_is_that_exact_quarter(quarter: str, year: int, start: str, end: str) -> None:
    """First / second / third / last quarter of a year -> a fixed 3-month period."""
    answers = {"timeframe": quarter, "quarter_year": year}
    updated = cl.apply_answers(VAGUE_INTENT, answers)

    assert updated["anchor_date"] == end
    assert updated["time_window"] == {"unit": "MONTHS", "value": 3, "is_rolling": False, "provenance": "stated"}
    assert f"({start} to {end})" in cl.describe_answers(answers)


def test_a_quarter_without_a_year_is_not_answered() -> None:
    """The year is needed; without it the timeframe stays open."""
    assert "timeframe" not in cl.answered_fields({"timeframe": "Q2"})
    assert "timeframe" not in cl.answered_fields({"timeframe": "Q2", "quarter_year": "20"})


def test_other_transaction_types_are_typed_in() -> None:
    """'Other (type it)' adds the typed types to the chosen ones, for a fresh code search."""
    answers = {"channels": ["CASH DEPOSIT", "OTHER"], "other_channels": "loan repayment, Swift transfer"}
    updated = cl.apply_answers(VAGUE_INTENT, answers)

    assert updated["transaction_types"] == ["CASH DEPOSIT", "LOAN REPAYMENT", "SWIFT TRANSFER"]
    assert cl.describe_answers(answers).endswith("Channels: Cash deposit, Loan Repayment, Swift Transfer")
    assert "channels" not in cl.answered_fields({"channels": ["OTHER"]})
    assert cl.apply_answers(VAGUE_INTENT, {"channels": ["OTHER"], "other_channels": "Trade finance"})["transaction_types"] == [
        "TRADE FINANCE"]


def test_all_channels_and_a_new_threshold() -> None:
    """'All transaction types' clears the type list; an amount with no threshold adds one."""
    intent = {**VAGUE_INTENT, "thresholds": [], "aggregation": None}
    updated = cl.apply_answers(intent, {"channels": ["ALL", "CASH DEPOSIT"], "threshold_amount": "15000"})
    assert updated["transaction_type"] == "ALL" and updated["transaction_types"] is None
    assert updated["thresholds"] == [{"field": "transaction_amount", "operator": ">=", "value_from": 15000.0, "slot_id": "t0",
                                      "target_scope": "DETAIL", "provenance": "stated", "explanation_codes": None}]


def test_an_incomplete_custom_range_does_not_count_as_answered() -> None:
    """Choosing 'fixed date range' without dates leaves the timeframe open."""
    assert "timeframe" not in cl.answered_fields({"timeframe": "custom", "date_range": {"start": "2019-01-01"}})


def test_answers_read_as_a_plain_chat_message() -> None:
    """The user's bubble shows the answers in business words."""
    assert cl.describe_answers(ANSWERS) == (
        "Clarification answers: Target entity: Per account; Threshold amount: 25,000 JOD; Timeframe: last 7 days; "
        "Channels: Cash deposit, Inward transfer")


# ─────────────────────────── intent tool, both turns ───────────────────────────


def _fake_intent_llm(monkeypatch: pytest.MonkeyPatch, intent: Dict[str, Any], prompts: List[str]) -> None:
    """Make the intent LLM return a fixed intent and record what it was sent.

    Args:
        monkeypatch (pytest.MonkeyPatch): The pytest monkeypatch fixture.
        intent (Dict[str, Any]): The intent returned.
        prompts (List[str]): Collects the user prompts the LLM received.
    """
    class IntentLLM:
        def invoke(self, messages: Any) -> Any:
            prompts.append(messages[-1].content)
            return SimpleNamespace(content=json.dumps(intent))

    monkeypatch.setattr(aml_tools, "build_llm", lambda fast=False: IntentLLM())
    monkeypatch.setattr(aml_tools, "resolve_attribute_filters", lambda *texts: [])
    monkeypatch.setattr(aml_tools, "seed_scenario_metadata", lambda intent, existing=None: {})


def test_the_intent_tool_stops_and_asks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn 1: no code search, no plan, a NEEDS_CLARIFICATION payload with the form."""
    searched: List[Any] = []
    _fake_intent_llm(monkeypatch, VAGUE_INTENT, [])
    monkeypatch.setattr(aml_tools, "select_relevant_explanation_codes", lambda t, *a, **k: searched.append(t) or [])

    result = json.loads(aml_tools.analyze_intent_and_discover_explanation_codes.invoke({"user_prompt": VAGUE}))

    assert result["status"] == "NEEDS_CLARIFICATION" and "plan_artifact" not in result
    assert result["original_request"] == VAGUE and searched == []
    assert [q["id"] for q in result["questions"]] == [
        "threshold_amount", "timeframe", "quarter_year", "date_range", "channels", "other_channels"]


def test_the_answers_complete_the_original_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn 2: the tool restores the request from state, applies the answers, and builds the plan."""
    prompts: List[str] = []
    searched: List[Any] = []
    _fake_intent_llm(monkeypatch, VAGUE_INTENT, prompts)
    monkeypatch.setattr(aml_tools, "select_relevant_explanation_codes",
                        lambda t, *a, **k: searched.append(t) or [{"code": "1", "description": "CASH DEPOSIT"}])
    turn_1 = cl.clarification_payload(VAGUE_INTENT, cl.find_gaps(VAGUE_INTENT, VAGUE), VAGUE)
    state = {"messages": [ToolMessage(content=json.dumps(turn_1), tool_call_id="t1")]}

    tool = aml_tools.analyze_intent_and_discover_explanation_codes
    result = json.loads(tool.func(user_prompt=cl.describe_answers(ANSWERS), state=state,
                                  config={"configurable": {"clarification_answers": ANSWERS}}))

    assert result["status"] == "COMPLETE"
    assert prompts == []  # the stored parse is completed by the answers; no new LLM parse (stable slots)
    intent = result["enriched_intent"]
    assert intent["scenario_type"] == "ACCOUNT" and intent["time_window"]["value"] == 7
    assert intent["thresholds"][0]["value_from"] == 25000.0
    assert searched == [["CASH DEPOSIT", "INWARD TRANSFER"]]
    assert "Plan" in result["plan_artifact"] or result["plan_artifact"]


def test_the_flow_can_be_switched_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """AML_CLARIFICATION_FLOW=False restores the assumed-default behaviour."""
    monkeypatch.setattr(aml_tools.settings, "AML_CLARIFICATION_FLOW", False)
    _fake_intent_llm(monkeypatch, VAGUE_INTENT, [])
    monkeypatch.setattr(aml_tools, "select_relevant_explanation_codes", lambda t, *a, **k: [])

    result = json.loads(aml_tools.analyze_intent_and_discover_explanation_codes.invoke({"user_prompt": VAGUE}))
    assert result["status"] == "COMPLETE"


# ─────────────────────────── chat route ───────────────────────────


class FakeGraph:
    """An agent graph that calls the intent tool once and returns its result."""

    def __init__(self, tool_result: Dict[str, Any]) -> None:
        self.tool_result = tool_result
        self.configs: List[Dict[str, Any]] = []
        self.inputs: List[Any] = []

    async def aget_state(self, config: Dict[str, Any]) -> Any:
        return SimpleNamespace(values={"messages": []})

    async def astream(self, inputs: Any, config: Dict[str, Any]) -> Any:
        self.inputs.append(inputs)
        self.configs.append(config)
        call = {"name": "analyze_intent_and_discover_explanation_codes", "id": "c1", "args": {}}
        yield {"agent": {"messages": [AIMessage(content="", tool_calls=[call])]}}
        yield {"tools": {"messages": [ToolMessage(content=json.dumps(self.tool_result), tool_call_id="c1")]}}
        yield {"agent": {"messages": [AIMessage(content="Please complete the form in the side panel.")]}}


def _client(monkeypatch: pytest.MonkeyPatch, graph: FakeGraph) -> TestClient:
    """A test client for the chat route with the graph, session store and scope guard mocked.

    Args:
        monkeypatch (pytest.MonkeyPatch): The pytest monkeypatch fixture.
        graph (FakeGraph): The graph to use.

    Returns:
        TestClient: The client.
    """
    async def get_graph() -> FakeGraph:
        return graph

    monkeypatch.setattr(chat_route, "get_tool_driven_graph", get_graph)
    monkeypatch.setattr(chat_route, "upsert_chat_session", lambda *a, **k: None)
    monkeypatch.setattr(chat_route.scope_guard, "check", lambda message, history: SimpleNamespace(blocked=False))
    app = FastAPI()
    app.include_router(chat_route.router)
    return TestClient(app)


def _events(response: Any) -> List[Dict[str, Any]]:
    """Parse an SSE body.

    Args:
        response (Any): The streamed response.

    Returns:
        List[Dict[str, Any]]: The events.
    """
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]


def test_the_stream_emits_the_form_and_no_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    """The tool result becomes a clarification_request event without the internal partial intent."""
    payload = cl.clarification_payload(VAGUE_INTENT, cl.find_gaps(VAGUE_INTENT, VAGUE), VAGUE)
    client = _client(monkeypatch, FakeGraph(payload))

    events = _events(client.post("/chat/stream", json={"messages": [{"role": "user", "content": VAGUE}],
                                                       "metadata": {"chat_id": "c", "user_id": "u"}}))
    kinds = [e["type"] for e in events]

    assert "clarification_request" in kinds and "plan_artifact" not in kinds and kinds[-1] == "done"
    data = next(e["data"] for e in events if e["type"] == "clarification_request")
    assert data["missing_fields"] == payload["missing_fields"] and "partial_intent" not in data


def test_answers_travel_in_the_run_config_and_read_as_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """Submitted answers reach the tool through the config; the stored message is readable."""
    graph = FakeGraph({"status": "COMPLETE", "plan_artifact": "# Plan"})
    client = _client(monkeypatch, graph)

    client.post("/chat/stream", json={"messages": [{"role": "user", "content": ""}], "metadata": {"chat_id": "c2", "user_id": "u"},
                                      "clarification_answers": ANSWERS})

    assert graph.configs[0]["configurable"]["clarification_answers"] == ANSWERS
    assert graph.inputs[0]["messages"][0].content == cl.describe_answers(ANSWERS)


def test_a_second_request_for_a_busy_chat_gets_a_notice(monkeypatch: pytest.MonkeyPatch) -> None:
    """One run per conversation: a duplicate never starts a second agent run."""
    graph = FakeGraph({"status": "COMPLETE"})
    client = _client(monkeypatch, graph)
    chat_route._ACTIVE_THREADS.add("no_project_busy_u")
    try:
        events = _events(client.post("/chat/stream", json={"messages": [{"role": "user", "content": "go ahead"}],
                                                           "metadata": {"chat_id": "busy", "user_id": "u"}}))
    finally:
        chat_route._ACTIVE_THREADS.discard("no_project_busy_u")

    assert [e["type"] for e in events] == ["content", "done"] and events[0]["text"] == chat_route.BUSY_NOTICE
    assert graph.inputs == []


def test_the_lock_is_released_after_a_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """After a finished run the same chat accepts the next message."""
    client = _client(monkeypatch, FakeGraph({"status": "COMPLETE"}))
    body = {"messages": [{"role": "user", "content": "hi"}], "metadata": {"chat_id": "free", "user_id": "u"}}
    client.post("/chat/stream", json=body)
    assert "no_project_free_u" not in chat_route._ACTIVE_THREADS
    assert [e["type"] for e in _events(client.post("/chat/stream", json=body))][-1] == "done"


# ─────────────────────────── "give me the side panel" ───────────────────────────


class PanelGraph(FakeGraph):
    """A graph whose conversation state already holds tool results; records state writes."""

    def __init__(self, state_messages: List[Any]) -> None:
        super().__init__({"status": "COMPLETE"})
        self.state_messages = state_messages
        self.recorded: List[Any] = []

    async def aget_state(self, config: Dict[str, Any]) -> Any:
        return SimpleNamespace(values={"messages": self.state_messages})

    async def aupdate_state(self, config: Dict[str, Any], values: Dict[str, Any], as_node: str = "") -> None:
        self.recorded.extend(values["messages"])


@pytest.mark.parametrize("message, expected", [
    ("give me the side panel", True), ("show the form", True), ("أين النموذج", True), ("open the plan again", True),
    ("give me a plan for a structuring scenario", False), ("Flag transactions in the last 90 days.", False),
    ("go ahead", False),
])
def test_panel_requests_are_recognised(message: str, expected: bool) -> None:
    """Short 'show me the panel / form / the plan' requests; a new scenario request is not one."""
    assert chat_route.is_panel_request(message) is expected


def test_give_me_the_side_panel_re_sends_the_open_form(monkeypatch: pytest.MonkeyPatch) -> None:
    """Live case: the form is sent again, without the scope guard or an agent run."""
    form = cl.clarification_payload(VAGUE_INTENT, cl.find_gaps(VAGUE_INTENT, VAGUE), VAGUE)
    graph = PanelGraph([ToolMessage(content=json.dumps(form), tool_call_id="t1")])
    client = _client(monkeypatch, graph)
    monkeypatch.setattr(chat_route.scope_guard, "check",
                        lambda message, history: (_ for _ in ()).throw(AssertionError("scope guard must not run")))

    events = _events(client.post("/chat/stream", json={"messages": [{"role": "user", "content": "give me the side panel"}],
                                                       "metadata": {"chat_id": "p1", "user_id": "u"}}))

    assert [e["type"] for e in events] == ["thinking", "clarification_request", "content", "done"]
    assert events[1]["data"]["missing_fields"] == form["missing_fields"]
    assert graph.inputs == [] and [m.content for m in graph.recorded] == ["give me the side panel", chat_route.PANEL_FORM_REPLY]


def test_with_no_open_form_the_latest_plan_is_shown(monkeypatch: pytest.MonkeyPatch) -> None:
    """After the form was answered, the side panel request shows the plan."""
    graph = PanelGraph([ToolMessage(content=json.dumps({"status": "COMPLETE", "enriched_intent": {}, "plan_artifact": "# Plan"}),
                                    tool_call_id="t2")])
    events = _events(_client(monkeypatch, graph).post("/chat/stream", json={
        "messages": [{"role": "user", "content": "show me the plan"}], "metadata": {"chat_id": "p2", "user_id": "u"}}))
    assert [e["type"] for e in events][1:3] == ["plan_artifact", "content"] and events[1]["text"] == "# Plan"


def test_with_nothing_to_show_the_agent_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    """No form and no plan yet: the message goes to the agent as usual."""
    graph = PanelGraph([])
    _client(monkeypatch, graph).post("/chat/stream", json={
        "messages": [{"role": "user", "content": "show the form"}], "metadata": {"chat_id": "p3", "user_id": "u"}})
    assert len(graph.inputs) == 1


class ClaimingGraph(PanelGraph):
    """A graph whose agent claims a form without calling the intent tool (the live gpt-5.4-mini case)."""

    async def astream(self, inputs: Any, config: Dict[str, Any]) -> Any:
        self.inputs.append(inputs)
        yield {"agent": {"messages": [AIMessage(content="I need a few details — please complete the form in the side panel.")]}}


def test_a_promised_form_is_made_real(monkeypatch: pytest.MonkeyPatch) -> None:
    """The agent skipped the tool: the server runs it, sends the form and records it for the next turn."""
    form = cl.clarification_payload(VAGUE_INTENT, cl.find_gaps(VAGUE_INTENT, VAGUE), VAGUE)
    ran: List[str] = []

    def fake_tool(user_prompt: str, state: Any = None, config: Any = None, **kw: Any) -> str:
        ran.append(user_prompt)
        return json.dumps(form)

    monkeypatch.setattr(aml_tools.analyze_intent_and_discover_explanation_codes, "func", fake_tool)
    graph = ClaimingGraph([])
    events = _events(_client(monkeypatch, graph).post("/chat/stream", json={
        "messages": [{"role": "user", "content": "Flag transactions in the last 90 days."}],
        "metadata": {"chat_id": "b1", "user_id": "u"}}))

    assert ran == ["Flag transactions in the last 90 days."]
    assert [e["type"] for e in events][-2:] == ["clarification_request", "done"]
    assert getattr(graph.recorded[0], "tool_calls", None) and graph.recorded[1].content == json.dumps(form)
    assert cl.pending_clarification(graph.recorded)["original_request"] == VAGUE


def test_no_backstop_when_the_tool_was_called(monkeypatch: pytest.MonkeyPatch) -> None:
    """A normal turn (the tool ran and sent the form) is left alone."""
    form = cl.clarification_payload(VAGUE_INTENT, cl.find_gaps(VAGUE_INTENT, VAGUE), VAGUE)
    monkeypatch.setattr(aml_tools.analyze_intent_and_discover_explanation_codes, "func",
                        lambda **kw: (_ for _ in ()).throw(AssertionError("backstop must not run")))
    events = _events(_client(monkeypatch, FakeGraph(form)).post("/chat/stream", json={
        "messages": [{"role": "user", "content": VAGUE}], "metadata": {"chat_id": "b2", "user_id": "u"}}))
    assert [e["type"] for e in events].count("clarification_request") == 1


# ─────────────────────────── echo guard ───────────────────────────

BALANCE = "customer made cash deposits greater than 50 % of his account balance"
REFUSED = "Balances on this database aren't kept day by day, so I can't build rules that compare deposits with the account balance."


class EchoGraph(PanelGraph):
    """A graph that plays one scripted run per astream call, counts tool executions and records state writes."""

    def __init__(self, runs: List[Any]) -> None:
        super().__init__([])
        self.runs = list(runs)
        self.updates: List[Any] = []
        self.tools_run = 0

    async def aupdate_state(self, config: Dict[str, Any], values: Dict[str, Any], as_node: str = "") -> None:
        self.updates.append((as_node, list(values["messages"])))
        self.recorded.extend(values["messages"])

    async def astream(self, inputs: Any, config: Dict[str, Any]) -> Any:
        self.inputs.append(inputs)
        run = self.runs.pop(0)
        if isinstance(run, Exception):
            raise run
        for event in run:
            self.tools_run += len(event.get("tools", {}).get("messages", []))
            yield event


def _echo(message_id: str = "echo-1") -> Dict[str, Any]:
    """The agent's reply that only repeats the user's message."""
    return {"agent": {"messages": [AIMessage(content=BALANCE, id=message_id)]}}


def _refusal_run() -> List[Dict[str, Any]]:
    """A normal run: the intent tool, its result, then the reply."""
    call = {"name": chat_route.INTENT_TOOL, "id": "r1", "args": {}}
    return [{"agent": {"messages": [AIMessage(content="", tool_calls=[call])]}},
            {"tools": {"messages": [ToolMessage(content=json.dumps({"status": "NOT_SUPPORTED"}), tool_call_id="r1")]}},
            {"agent": {"messages": [AIMessage(content=REFUSED)]}}]


def _post(monkeypatch: pytest.MonkeyPatch, graph: EchoGraph, text: str = BALANCE) -> List[Dict[str, Any]]:
    """Send one message through the chat route.

    Args:
        monkeypatch (pytest.MonkeyPatch): The pytest monkeypatch fixture.
        graph (EchoGraph): The graph to use.
        text (str): The user's message.

    Returns:
        List[Dict[str, Any]]: The SSE events.
    """
    return _events(_client(monkeypatch, graph).post("/chat/stream", json={
        "messages": [{"role": "user", "content": text}], "metadata": {"chat_id": "e", "user_id": "u"}}))


def _replies(events: List[Dict[str, Any]]) -> List[str]:
    return [e["text"] for e in events if e["type"] == "content"]


@pytest.mark.parametrize("reply, message, expected", [
    (BALANCE, BALANCE, True),
    (f'  "{BALANCE.upper()}"  ', BALANCE, True),
    (REFUSED, BALANCE, False),
    ("", "", False),
    (BALANCE + " please", BALANCE, False),
])
def test_an_echo_is_the_message_itself(reply: str, message: str, expected: bool) -> None:
    """Spacing, case and surrounding quotes are ignored; any other word makes it a real reply."""
    assert chat_route.is_echo(reply, message) is expected


def test_an_echo_with_no_tool_call_is_retried_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Live case: the echo is never shown; it is removed and the agent step runs once more on the same history."""
    graph = EchoGraph([[_echo()], _refusal_run()])

    events = _post(monkeypatch, graph)

    assert _replies(events) == [REFUSED]
    assert len(graph.inputs) == 2 and graph.inputs[1] is None  # the retry adds no second user message
    as_node, removed = graph.updates[0]
    assert as_node == "tools" and [m.id for m in removed] == ["echo-1"]
    assert graph.tools_run == 1


def test_a_normal_reply_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reply in the agent's own words is shown as it is, after one run."""
    graph = EchoGraph([[{"agent": {"messages": [AIMessage(content="Hello, how can I help with AML scenarios?")]}}]])

    events = _post(monkeypatch, graph, text="hello")

    assert _replies(events) == ["Hello, how can I help with AML scenarios?"]
    assert len(graph.inputs) == 1 and graph.updates == []


def test_a_reply_after_a_tool_call_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """The run called a tool, so its reply is shown even when it repeats the message: nothing runs twice."""
    run = _refusal_run()[:2] + [{"agent": {"messages": [AIMessage(content=BALANCE, id="after-tool")]}}]
    graph = EchoGraph([run])

    events = _post(monkeypatch, graph)

    assert _replies(events) == [BALANCE]
    assert len(graph.inputs) == 1 and graph.updates == [] and graph.tools_run == 1


@pytest.mark.parametrize("text, fallback", [(BALANCE, chat_route.ECHO_FALLBACK),
                                             ("عميل أودع نقداً أكثر من نصف رصيده", chat_route.ECHO_FALLBACK_AR)])
def test_a_second_echo_gets_the_fixed_reply_and_no_third_run(monkeypatch: pytest.MonkeyPatch, text: str, fallback: str) -> None:
    """The retry echoes again: no further run; the fixed reply is shown and replaces the echo in the history."""
    graph = EchoGraph([[{"agent": {"messages": [AIMessage(content=text, id="echo-1")]}}],
                       [{"agent": {"messages": [AIMessage(content=text, id="echo-2")]}}]])

    events = _post(monkeypatch, graph, text=text)

    assert _replies(events) == [fallback]
    assert len(graph.inputs) == 2
    as_node, written = graph.updates[-1]
    assert as_node == "agent" and [getattr(m, "id", None) for m in written[:-1]] == ["echo-2"]
    assert written[-1].content == fallback


def test_a_failed_retry_gets_the_fixed_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    """The retry raises: the user sees the fixed reply, not an error."""
    graph = EchoGraph([[_echo()], RuntimeError("model unavailable")])

    events = _post(monkeypatch, graph)

    assert _replies(events) == [chat_route.ECHO_FALLBACK]
    assert "error" not in [e["type"] for e in events] and [e["type"] for e in events][-1] == "done"
    assert graph.recorded[-1].content == chat_route.ECHO_FALLBACK


def test_the_echo_guard_never_repeats_a_side_effect(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tools run once per turn: a retry happens only after a run that executed none, and only once."""
    retried = EchoGraph([[_echo()], _refusal_run()])
    _post(monkeypatch, retried)
    tool_first = EchoGraph([_refusal_run()[:2] + [_echo("after-tool")]])
    _post(monkeypatch, tool_first)
    twice = EchoGraph([[_echo("echo-1")], [_echo("echo-2")], _refusal_run()])
    _post(monkeypatch, twice)

    assert (retried.tools_run, len(retried.inputs)) == (1, 2)
    assert (tool_first.tools_run, len(tool_first.inputs)) == (1, 1)
    assert (twice.tools_run, len(twice.inputs)) == (0, 2) and len(twice.runs) == 1  # the third run never started


@pytest.mark.parametrize("text, asks_amount", [
    ("flag all customer they have 3 high cash deposite in one week in 2019", True),
    ("Flag customers with 3 large cash deposits within 7 days", True),
    ("Flag customers with 3 cash deposits above 10,000 JOD within 7 days", False),
    ("Flag accounts with more than 20 cash withdrawals in a month", False),
    ("Flag 3 cash deposits from high-risk customers within 7 days", False),
])
def test_a_count_is_not_an_amount(text: str, asks_amount: bool) -> None:
    """Live case: '3 high cash deposits' gave EQU_TRA_AMT >= 0; 'high' without a number now asks the amount."""
    import re as _re

    count = int(_re.search(r"\b(3|20)\b", text).group(1))
    amount = 10000 if "10,000" in text else 0
    intent = {**VAGUE_INTENT, "thresholds": [
        {"field": "transaction_count", "operator": ">=", "value_from": count, "target_scope": "AGGREGATE"},
        {"field": "transaction_amount", "operator": ">=", "value_from": amount, "target_scope": "DETAIL"}]}
    assert ("threshold_amount" in cl.find_gaps(intent, text)) is asks_amount


# ─────────────────────────── "X% of Y" ratio rules, slots, validation, balance capability ───────────────────────────

# What the intent LLM produced live for "cash deposits greater than 50 % his account balance".
LLM_SPLIT = [
    {"field": "transaction_amount", "operator": ">", "value_from": 0.5, "target_scope": "DETAIL", "transaction_type": "CASH DEPOSIT"},
    {"field": "account_balance", "operator": "<", "value_from": None, "target_scope": "DETAIL", "transaction_type": "CASH DEPOSIT"},
]


@pytest.mark.parametrize("text", [
    "flag a cash deposit greater than 50% of the account balance",
    "flag a cash deposit greater than 50 % of the account balance",
    "customer maede cash deposits greater than 50 % his account balance",
    "إيداع نقدي أكبر من 50% من الرصيد",
    "إيداع نقدي أكبر من ٥٠٪ من الرصيد",
])
def test_1_x_percent_of_y_is_one_ratio_condition(text: str) -> None:
    """Bug 1: one ratio condition, never 'amount > 0.5 AND account_balance < None'."""
    ratios = cl.parse_ratio_conditions(text)
    assert [{k: r[k] for k in ("type", "left", "right", "operator", "factor")} for r in ratios] == [
        {"type": "ratio", "left": "deposit_amount", "right": "account_balance", "operator": ">", "factor": 0.5}]

    intent = cl.apply_ratio_conditions({**VAGUE_INTENT, "thresholds": LLM_SPLIT}, ratios)
    assert len(intent["thresholds"]) == 1
    ratio = intent["thresholds"][0]
    assert (ratio["kind"], ratio["field"], ratio["operator"], ratio["value_from"], ratio["relative_to"]) == (
        "ratio", "deposit_amount", ">", 0.5, "account_balance")
    assert cl.condition_sql(ratio) == "deposit_amount > 0.5 * account_balance"
    assert cl.apply_ratio_conditions(intent, ratios)["thresholds"] == intent["thresholds"]  # idempotent


@pytest.mark.parametrize("text, expected", [
    ("withdrawals at least 90% of total deposits", ("withdrawal_amount", ">=", 0.9, "total_deposits")),
    ("transfers less than 20 percent of his income", ("transfer_amount", "<", 0.2, "declared_income")),
])
def test_1_the_general_pattern(text: str, expected: tuple) -> None:
    """'<field> greater / less than X% of <other field>' for other fields too."""
    (ratio,) = cl.parse_ratio_conditions(text)
    assert (ratio["left"], ratio["operator"], ratio["factor"], ratio["right"]) == expected


@pytest.mark.parametrize("text", ["deposits growing more than 50% within 7 days", "cash deposits above 10,000 JOD"])
def test_1_no_ratio_without_a_second_field(text: str) -> None:
    """A percentage with no 'of <field>' is not a ratio."""
    assert cl.parse_ratio_conditions(text) == []


@pytest.mark.parametrize("text", ["50%", "50 %", "50 percent", "٥٠٪", "50٪", "50 بالمئة"])
def test_2_every_percentage_format_is_one_half(text: str) -> None:
    """Bug 2: all formats normalize to 50%, read as 0.5; Arabic-Indic digits become Western digits."""
    assert cl.normalize_text(text).startswith("50%")
    assert cl.percentages_in(text) == [0.5]
    assert 0.5 in cl.numbers_in(text)


def test_2_a_spaced_percentage_never_asks_for_an_amount() -> None:
    """Live case: '50 %' made the form ask for an amount the user never meant."""
    text = "flag customers whose cash deposit is greater than 50 % of the account balance within 7 days"
    intent = cl.apply_ratio_conditions({**VAGUE_INTENT, "thresholds": LLM_SPLIT}, cl.parse_ratio_conditions(text))
    assert "threshold_amount" not in cl.find_gaps(intent, text)


def test_3_the_amount_answer_updates_only_its_slot() -> None:
    """Bug 3: the 10,000 JOD answer never reaches a balance or ratio condition."""
    intent = {**VAGUE_INTENT, "thresholds": [
        {"field": "deposit_amount", "kind": "ratio", "operator": ">", "value_from": 0.5, "relative_to": "account_balance",
         "target_scope": "DETAIL"},
        {"field": "account_balance", "operator": "<", "value_from": 2000, "target_scope": "DETAIL"},
        {"field": "transaction_amount", "operator": ">=", "value_from": None, "target_scope": "DETAIL"},
    ]}
    payload = cl.clarification_payload(intent, ["threshold_amount"], "x")
    question = next(q for q in payload["questions"] if q["id"] == "threshold_amount")
    assert question["slot_id"] == "t2"

    updated = cl.apply_answers(payload["partial_intent"], {"threshold_amount": 10000}, questions=payload["questions"])
    ratio, balance, amount = updated["thresholds"]
    assert ratio["value_from"] == 0.5 and ratio["relative_to"] == "account_balance"
    assert balance["value_from"] == 2000
    assert amount["value_from"] == 10000.0


def test_3_the_live_leak_case() -> None:
    """The live intent had transaction_amount and account_balance thresholds: only the amount takes the answer."""
    intent = {**VAGUE_INTENT, "thresholds": [dict(t, value_from=None) for t in LLM_SPLIT]}
    updated = cl.apply_answers(intent, {"threshold_amount": 10000})
    assert [t["value_from"] for t in updated["thresholds"]] == [10000.0, None]


def test_3_no_amount_slot_adds_a_new_condition() -> None:
    """With only a ratio, the amount answer adds its own condition instead of editing the ratio."""
    intent = {**VAGUE_INTENT, "aggregation": None, "thresholds": [
        {"field": "deposit_amount", "kind": "ratio", "operator": ">", "value_from": 0.5, "relative_to": "average_deposit_amount",
         "target_scope": "DETAIL"}]}
    updated = cl.apply_answers(intent, {"threshold_amount": 5000})
    assert updated["thresholds"][0]["value_from"] == 0.5
    assert updated["thresholds"][1]["field"] == "transaction_amount" and updated["thresholds"][1]["value_from"] == 5000.0


def test_4_a_missing_value_is_a_plain_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bug 4: the rule is not sent for SQL generation and no raw expression is shown."""
    sent: List[Any] = []
    monkeypatch.setattr(aml_tools, "_request_piotech_sql", lambda intent: sent.append(intent) or {})
    intent = {"scenario_name": "x", "scenario_type": "CUSTOMER", "detection_logic": "x", "thresholds": LLM_SPLIT}

    result = json.loads(aml_tools.execute_oracle_dwh_shadow_test.invoke({"intent_json": json.dumps(intent)}))

    assert sent == [] and result["validation_success"] is False
    assert result["user_message"] == ("I can't build this rule yet: the value for the account balance is missing. "
                                      "Please add it and try again.")
    assert "None" not in result["user_message"] and "<" not in result["user_message"]


@pytest.mark.parametrize("threshold, problem", [
    ({"field": "", "operator": ">", "value_from": 1}, "one condition does not say what to compare"),
    ({"field": "transaction_amount", "operator": "~", "value_from": 1}, "the comparison for the transaction amount is not clear"),
    ({"field": "transaction_amount", "operator": "BETWEEN", "value_from": 1}, "the upper limit for the transaction amount is missing"),
    ({"field": "deposit_amount", "kind": "ratio", "operator": ">", "value_from": None, "relative_to": "account_balance"},
     "the percentage for the deposit amount is missing"),
])
def test_4_each_kind_of_incomplete_condition(threshold: Dict[str, Any], problem: str) -> None:
    """Missing field, unknown operator, missing bound, missing percentage: one plain sentence each."""
    assert any(p.startswith(problem) for p in cl.validate_conditions({"thresholds": [threshold]}))


def test_4_raw_expressions_from_the_sql_service_are_removed() -> None:
    """A refusal text quoting 'account_balance < None' reaches the user without it."""
    assert cl.plain_text('blocked by "account_balance < None"') == 'blocked by "an incomplete condition"'


@pytest.mark.parametrize("text, message", [
    ("flag a cash deposit greater than 50 % of the account balance", cl.BALANCE_REFUSAL_EN),
    ("إيداع نقدي أكبر من ٥٠٪ من الرصيد", cl.BALANCE_REFUSAL_AR),
])
def test_5_on_openbi2_a_balance_rule_gets_only_the_plain_refusal(monkeypatch: pytest.MonkeyPatch, text: str,
                                                                      message: str) -> None:
    """No balance history on OPENBI2: only the plain refusal, in the user's language."""
    monkeypatch.setattr(aml_tools.settings, "ACTIVE_DATABASE", "OPENBI2")
    monkeypatch.setattr(aml_tools.settings, "DATA_CAPABILITIES", {"OPENBI2": {"balance_history": False}})
    _fake_intent_llm(monkeypatch, {**VAGUE_INTENT, "detection_logic": "Cash deposits relative to the balance.",
                                   "thresholds": LLM_SPLIT}, [])
    monkeypatch.setattr(aml_tools, "select_relevant_explanation_codes",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no code search for a refused rule")))

    result = json.loads(aml_tools.analyze_intent_and_discover_explanation_codes.invoke({"user_prompt": text}))

    assert result["status"] == "NOT_SUPPORTED" and result["capability"] == "balance_history"
    assert result["user_message"] == message and "plan_artifact" not in result
    # The refusal has nothing after it: no alternative rule, no follow-up question.
    assert "alternative" not in result and "questions" not in result
    assert "average" not in result["user_message"] and "متوسط" not in result["user_message"]
    assert "do not suggest another rule" in result["agent_instruction"]


def test_5_the_capability_is_a_configuration_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Extension point: with balance_history = true for the bank, the same rule is not refused (no parser change)."""
    monkeypatch.setattr(aml_tools.settings, "ACTIVE_DATABASE", "BANKX")
    monkeypatch.setattr(aml_tools.settings, "DATA_CAPABILITIES", {"BANKX": {"balance_history": True}})
    intent = cl.apply_ratio_conditions({**VAGUE_INTENT, "thresholds": LLM_SPLIT},
                                       cl.parse_ratio_conditions("deposit greater than 50% of the account balance"))
    assert cl.balance_capability_refusal(intent, "x") is None
    monkeypatch.setattr(aml_tools.settings, "DATA_CAPABILITIES", {})
    assert cl.balance_capability_refusal(intent, "x")["status"] == "NOT_SUPPORTED"  # unknown database: not assumed


@pytest.mark.parametrize("text", [
    "Flag customers whose cash deposits total more than 10,000 JOD within 7 days",
    "Identify accounts with more than 10 cash deposits and 10 cash withdrawals within 30 days without maintaining a balance",
])
def test_6_rules_without_a_balance_comparison_are_unchanged(text: str) -> None:
    """No ratio, no refusal: these rules go on exactly as before."""
    intent = {**VAGUE_INTENT, "thresholds": [{"field": "cumulative_transaction_volume", "operator": ">", "value_from": 10000,
                                              "target_scope": "AGGREGATE"}]}
    assert cl.parse_ratio_conditions(text) == []
    assert cl.apply_ratio_conditions(intent, cl.parse_ratio_conditions(text)) is intent
    assert cl.balance_capability_refusal(intent, text) is None
    assert cl.validate_conditions(intent) == []
