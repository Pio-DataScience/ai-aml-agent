"""
Chat streaming route — SSE endpoint that drives the tool-driven AML agent.

Translates LangGraph ReAct agent events (AIMessage tool calls, ToolMessage
outputs) into the SSE event contract the frontend chat UI and side panel
expect: tool_call, content, plan_artifact, scenario_result, done.
"""

import asyncio
import json
import logging
import re
import uuid
from typing import Any, Dict, Optional

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from services.aml_builder.web.services import scope_guard
from services.aml_builder.web.services.clarification import (
    clarification_event_data,
    describe_answers,
    pending_clarification,
)
from services.aml_builder.web.services.graph import get_tool_driven_graph
from services.aml_builder.web.services.schemas import ChatRequest, SSEEvent
from services.aml_builder.web.services.session_store import upsert_chat_session
from services.aml_builder.web.services.settings import settings

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Chat"])

# Conversations with a run in progress. One run per conversation: a second request for the same
# thread (a double submit, a second tab, a retry) is answered with a short notice instead of a
# second, concurrent agent run on the same history.
_ACTIVE_THREADS: set = set()
BUSY_NOTICE = "Your previous message in this chat is still being processed. Please wait for it to finish."

# "give me the side panel", "show the form", "أين النموذج": a request to see what is already on screen.
_PANEL_WORDS = re.compile(
    r"\b(?:side\s*panel|panel|form)\b|\b(?:the|my|that|this)\s+plan\b|\bplan\s+again\b|اللوحة|النموذج|الاستمارة|الخطة",
    re.IGNORECASE,
)
_SHOW_WORDS = re.compile(
    r"\b(?:give|show|open|display|see|bring|reopen|view|where)\b|أعط|اعط|أظهر|اظهر|اعرض|افتح|أين|اين|وين",
    re.IGNORECASE,
)
PANEL_FORM_REPLY = "Here is the form again — it is open in the side panel on the right."
# The agent says it asked for details in a form ("please complete the form in the side panel").
_FORM_CLAIM = re.compile(r"\bform\b.{0,60}\b(?:side\s*panel|panel)\b|\bcomplete the form\b|النموذج|الاستمارة",
                         re.IGNORECASE | re.DOTALL)
INTENT_TOOL = "analyze_intent_and_discover_explanation_codes"
PANEL_PLAN_REPLY = "The plan is open in the side panel on the right."


def is_panel_request(message: str) -> bool:
    """Whether a message only asks to see the side panel (form or plan) again.

    Args:
        message (str): What the user typed.

    Returns:
        bool: True for a short request that names the panel / form / plan and asks to see it.
    """
    text = (message or "").strip()
    return len(text.split()) <= 12 and bool(_PANEL_WORDS.search(text)) and bool(_SHOW_WORDS.search(text))


def latest_panel_event(messages: Any) -> Optional[SSEEvent]:
    """The side-panel content to show again: the open clarification form, else the latest plan.

    Args:
        messages (Any): The agent state's messages.

    Returns:
        Optional[SSEEvent]: A clarification_request or plan_artifact event, or None when there is nothing to show.
    """
    pending = pending_clarification(messages or [])
    if pending:
        return SSEEvent(type="clarification_request", data=clarification_event_data(pending))
    for message in reversed(messages or []):
        if getattr(message, "type", None) != "tool":
            continue
        try:
            payload = json.loads(getattr(message, "content", "") or "{}")
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get("plan_artifact"):
            return SSEEvent(type="plan_artifact", text=payload["plan_artifact"])
    return None



async def _scope_refusal(tool_graph: Any, config: Dict[str, Any], message: str) -> Optional[str]:
    """Return a refusal if the message is outside the agent's scope, else None.

    The refused exchange is written to the conversation state, so it appears in the chat history
    and the next message is judged with it as context. Any failure lets the turn continue.

    Args:
        tool_graph (Any): The compiled ReAct agent graph.
        config (Dict[str, Any]): The LangGraph run config (thread id).
        message (str): The text the user typed.

    Returns:
        Optional[str]: The refusal text, or None when the turn should continue.
    """
    try:
        state = await tool_graph.aget_state(config)
        history = scope_guard.history_of((state.values or {}).get("messages", []) if state else [])
    except Exception as exc:  # history is optional context; judge without it
        logger.warning("[SCOPE] could not read conversation history (%s)", type(exc).__name__)
        history = []

    verdict = await asyncio.to_thread(scope_guard.check, message, history)
    if not verdict.blocked:
        return None

    logger.info("[API] Message declined by the scope guard.")
    try:
        await tool_graph.aupdate_state(
            config,
            {"messages": [HumanMessage(content=message), AIMessage(content=verdict.refusal)]},
            as_node="agent",
        )
    except Exception as exc:  # the refusal is still shown; only the history entry is missing
        logger.warning("[SCOPE] could not record the declined exchange (%s)", type(exc).__name__)
    return verdict.refusal


async def _run_intent_backstop(tool_graph: Any, config: Dict[str, Any], message: str) -> Any:
    """Run the intent tool for a turn whose reply promised a form the agent never produced.

    The result is recorded in the conversation as a normal tool call, so answering the form and
    "give me the side panel" work as for any other form.

    Args:
        tool_graph (Any): The compiled agent graph.
        config (Dict[str, Any]): The run config (thread id, clarification answers).
        message (str): What the user typed this turn.

    Yields:
        SSEEvent: The clarification_request (or plan_artifact) to show.
    """
    from services.aml_builder.web.services.tools import analyze_intent_and_discover_explanation_codes

    try:
        state = await tool_graph.aget_state(config)
        values = (state.values or {}) if state else {}
        raw = await asyncio.to_thread(
            analyze_intent_and_discover_explanation_codes.func, user_prompt=message, state=values, config=config
        )
        payload = json.loads(raw)
    except Exception as exc:  # the reply stays as it was; nothing else is shown
        logger.warning("[API] Intent backstop failed (%s)", type(exc).__name__)
        return
    if not isinstance(payload, dict):
        return
    if payload.get("status") == "NEEDS_CLARIFICATION":
        event = SSEEvent(type="clarification_request", data=clarification_event_data(payload))
    elif payload.get("plan_artifact"):
        event = SSEEvent(type="plan_artifact", text=payload["plan_artifact"])
    else:
        return
    call_id = f"backstop_{uuid.uuid4().hex[:8]}"
    try:
        await tool_graph.aupdate_state(config, {"messages": [
            AIMessage(content="", tool_calls=[{"name": INTENT_TOOL, "args": {"user_prompt": message}, "id": call_id}]),
            ToolMessage(content=raw, tool_call_id=call_id),
        ]}, as_node="agent")
    except Exception as exc:  # the form is still shown; only the history entry is missing
        logger.warning("[API] Could not record the intent backstop (%s)", type(exc).__name__)
    logger.warning("[API] Intent backstop: the agent promised a form without calling the tool; sent %s", event.type)
    yield event


@router.post("/chat/stream")
async def chat_stream(request: ChatRequest) -> StreamingResponse:
    """Stream a chat response from the AML Scenario Agent.

    Accepts the user's message and conversation history, invokes the
    tool-driven LangGraph ReAct agent, and streams Server-Sent Events
    back to the client.

    SSE event types emitted:
    - ``tool_call``                  — when a tool is being invoked
    - ``thinking``                   — intermediate processing status
    - ``content``                    — streaming text chunks
    - ``plan_artifact``              — structured markdown execution plan (side panel)
    - ``scenario_metadata_catalog``  — PIO_AML_SCENARIO field catalog + live lookup options (side panel)
    - ``clarification_request``      — mandatory parameters are missing: the form to show (side panel)
    - ``scenario_result``            — structured scenario + validation data (JSON)
    - ``error``                      — error details
    - ``done``                       — stream termination signal

    Args:
        request (ChatRequest): The incoming chat request with messages and metadata.

    Returns:
        StreamingResponse: An SSE stream of JSON-encoded events.
    """
    metadata = request.metadata
    user_id = metadata.get("user_id", "anonymous")
    chat_id = metadata.get("chat_id", uuid.uuid4().hex)
    project_id = metadata.get("project_id", "0")
    if not project_id or project_id in ("0", "None"):
        project_id = "no_project"

    thread_id = f"{project_id}_{chat_id}_{user_id}"

    first_msg = request.messages[0].content if request.messages else "New Chat"
    upsert_chat_session(chat_id, user_id, project_id, first_msg)

    logger.info(
        "[API] /chat/stream — user=%s thread=%s messages=%d",
        user_id,
        thread_id,
        len(request.messages),
    )

    tool_graph = await get_tool_driven_graph()
    answers = request.clarification_answers or None
    config = {
        "configurable": {"thread_id": thread_id, "clarification_answers": answers},
        "recursion_limit": 10,
    }
    user_msg = request.messages[-1].content if request.messages else ""
    if answers and not user_msg.strip():
        user_msg = describe_answers(answers)
    typed_msg = user_msg  # what the user typed, before sidebar metadata is appended
    if request.metadata_json:
        metadata_str = json.dumps(request.metadata_json, ensure_ascii=False)
        user_msg += f"\n\n[Active Scenario Governance Metadata from Sidebar]:\n{metadata_str}"

    inputs = {"messages": [HumanMessage(content=user_msg)]}

    async def event_generator():
        """Async generator yielding SSE-formatted events.

        Yields:
            str: SSE-formatted event strings (``data: {...}\\n\\n``).
        """

        def _sse(event: SSEEvent) -> str:
            return f"data: {event.model_dump_json()}\n\n"

        # Running map of tool_call_id -> tool_name so ToolMessages can be
        # routed by which tool produced them.
        tool_call_id_to_name: dict = {}
        intent_tool_called = False
        clarification_sent = False
        last_reply = ""

        # Check and claim in one step (no await in between), so two requests cannot both pass.
        if thread_id in _ACTIVE_THREADS:
            logger.warning("[API] Duplicate request for a busy thread ignored: %s", thread_id)
            yield _sse(SSEEvent(type="content", text=BUSY_NOTICE))
            yield _sse(SSEEvent(type="done"))
            return
        _ACTIVE_THREADS.add(thread_id)

        try:
            yield _sse(SSEEvent(type="thinking", status="Analyzing your request..."))

            # "Give me the side panel": show the open form (or the latest plan) again, without the LLM.
            if not request.metadata_json and not answers and is_panel_request(typed_msg):
                try:
                    state = await tool_graph.aget_state(config)
                    panel = latest_panel_event((state.values or {}).get("messages", []) if state else [])
                except Exception as exc:  # no state: let the agent answer normally
                    logger.warning("[API] Could not read the side-panel content (%s)", type(exc).__name__)
                    panel = None
                if panel is not None:
                    reply = PANEL_FORM_REPLY if panel.type == "clarification_request" else PANEL_PLAN_REPLY
                    yield _sse(panel)
                    yield _sse(SSEEvent(type="content", text=reply))
                    try:
                        await tool_graph.aupdate_state(
                            config, {"messages": [HumanMessage(content=typed_msg), AIMessage(content=reply)]}, as_node="agent"
                        )
                    except Exception as exc:  # the panel is still shown; only the history entry is missing
                        logger.warning("[API] Could not record the side-panel exchange (%s)", type(exc).__name__)
                    logger.info("[API] Side panel re-sent on request: %s", panel.type)
                    return

            # Scope guard: a message clearly unrelated to AML scenarios gets a fixed refusal
            # and never reaches the agent. Sidebar governance and clarification answers are always in scope.
            if not request.metadata_json and not answers:
                refusal = await _scope_refusal(tool_graph, config, typed_msg)
                if refusal:
                    yield _sse(SSEEvent(type="content", text=refusal))
                    return

            async for event in tool_graph.astream(inputs, config=config):
                for node_name, node_output in event.items():
                    if not isinstance(node_output, dict):
                        continue
                    msgs = node_output.get("messages", [])
                    for m in msgs:

                        # ── AIMessage: register tool calls + emit tool_call SSE ──
                        if isinstance(m, AIMessage) or getattr(m, "type", None) == "ai":
                            if getattr(m, "tool_calls", None):
                                for tc in m.tool_calls:
                                    t_name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "tool")
                                    t_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                                    if t_id and t_name:
                                        tool_call_id_to_name[t_id] = t_name
                                    intent_tool_called = intent_tool_called or t_name == INTENT_TOOL
                                    yield _sse(SSEEvent(type="tool_call", tool=t_name))
                                continue

                            # Natural language reply → stream to chat
                            text = getattr(m, "content", "")
                            if isinstance(text, str) and text.strip():
                                last_reply = text
                                yield _sse(SSEEvent(type="content", text=text))

                        # ── ToolMessage: intercept specific tools, suppress the rest ──
                        elif isinstance(m, ToolMessage) or getattr(m, "type", None) == "tool":
                            tool_call_id = getattr(m, "tool_call_id", None)
                            tool_name = tool_call_id_to_name.get(tool_call_id, "")

                            if tool_name in ("generate_scenario_execution_plan", "analyze_intent_and_discover_explanation_codes") or (isinstance(getattr(m, "content", None), str) and "plan_artifact" in m.content):
                                # Emit plan to side panel
                                try:
                                    payload = json.loads(getattr(m, "content", "{}"))
                                    if payload.get("status") == "NEEDS_CLARIFICATION":
                                        clarification_sent = True
                                        yield _sse(SSEEvent(type="clarification_request", data=clarification_event_data(payload)))
                                        logger.info("[API] Emitted clarification_request SSE. missing=%s", payload.get("missing_fields"))
                                        continue
                                    plan_md = payload.get("plan_artifact", "")
                                    if plan_md:
                                        yield _sse(SSEEvent(type="plan_artifact", text=plan_md))
                                        logger.info("[API] Emitted plan_artifact SSE from %s tool output (%d chars).", tool_name or "tool", len(plan_md))
                                except Exception as exc:
                                    logger.warning("[API] Could not parse plan_artifact from ToolMessage: %s", exc)

                            elif tool_name == "prepare_scenario_metadata_for_persistence":
                                # Emit the PIO_AML_SCENARIO field catalog to the side panel
                                try:
                                    payload = json.loads(getattr(m, "content", "{}"))
                                    if "fields" in payload:
                                        yield _sse(SSEEvent(type="scenario_metadata_catalog", data=payload))
                                        logger.info(
                                            "[API] Emitted scenario_metadata_catalog SSE. missing=%s",
                                            payload.get("missing_mandatory_fields"),
                                        )
                                except Exception as exc:
                                    logger.warning("[API] Could not parse scenario_metadata_catalog from ToolMessage: %s", exc)

                            elif tool_name in ("execute_oracle_dwh_shadow_test", "persist_and_validate_scenario_in_dwh"):
                                # Emit shadow test results or scenario persistence confirmation card
                                try:
                                    payload = json.loads(getattr(m, "content", "{}"))
                                    if "raw_sql" in payload or payload.get("write_success") or "shadow_test_metrics" in payload:
                                        yield _sse(SSEEvent(type="scenario_result", data=payload))
                                        logger.info("[API] Emitted scenario_result SSE from %s.", tool_name)
                                except Exception as exc:
                                    logger.warning("[API] Could not parse scenario_result from ToolMessage: %s", exc)

                            # All other ToolMessages are suppressed — raw JSON stays internal

            # Backstop: the agent said a form is waiting but never called the intent tool in this turn
            # (a small model can copy the sentence without the call). Make the form real.
            if (settings.AML_CLARIFICATION_FLOW and not intent_tool_called and not clarification_sent
                    and _FORM_CLAIM.search(last_reply or "")):
                async for backstop_event in _run_intent_backstop(tool_graph, config, typed_msg):
                    yield _sse(backstop_event)

        except Exception as exc:
            logger.error("[API] Stream error: %s", exc, exc_info=True)
            yield _sse(SSEEvent(type="error", text=str(exc)))

        finally:
            _ACTIVE_THREADS.discard(thread_id)
            yield _sse(SSEEvent(type="done"))

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
