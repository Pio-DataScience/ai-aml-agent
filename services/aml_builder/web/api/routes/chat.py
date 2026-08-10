"""
Chat streaming route — SSE endpoint that drives the tool-driven AML agent.

Translates LangGraph ReAct agent events (AIMessage tool calls, ToolMessage
outputs) into the SSE event contract the frontend chat UI and side panel
expect: tool_call, content, plan_artifact, scenario_result, done.
"""

import json
import logging
import uuid

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from services.aml_builder.web.services.graph import get_tool_driven_graph
from services.aml_builder.web.services.schemas import ChatRequest, SSEEvent
from services.aml_builder.web.services.session_store import upsert_chat_session

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Chat"])


@router.post("/chat/stream")
async def chat_stream(request: ChatRequest) -> StreamingResponse:
    """Stream a chat response from the AML Scenario Agent.

    Accepts the user's message and conversation history, invokes the
    tool-driven LangGraph ReAct agent, and streams Server-Sent Events
    back to the client.

    SSE event types emitted:
    - ``tool_call``        — when a tool is being invoked
    - ``thinking``         — intermediate processing status
    - ``content``          — streaming text chunks
    - ``plan_artifact``    — structured markdown execution plan (side panel)
    - ``scenario_result``  — structured scenario + validation data (JSON)
    - ``error``            — error details
    - ``done``             — stream termination signal

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
    config = {"configurable": {"thread_id": thread_id}}
    user_msg = request.messages[-1].content if request.messages else ""
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

        try:
            yield _sse(SSEEvent(type="thinking", status="Analyzing your request..."))

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
                                    yield _sse(SSEEvent(type="tool_call", tool=t_name))
                                continue

                            # Natural language reply → stream to chat
                            text = getattr(m, "content", "")
                            if isinstance(text, str) and text.strip():
                                yield _sse(SSEEvent(type="content", text=text))

                        # ── ToolMessage: intercept specific tools, suppress the rest ──
                        elif isinstance(m, ToolMessage) or getattr(m, "type", None) == "tool":
                            tool_call_id = getattr(m, "tool_call_id", None)
                            tool_name = tool_call_id_to_name.get(tool_call_id, "")

                            if tool_name == "generate_scenario_execution_plan":
                                # Emit plan to side panel
                                try:
                                    payload = json.loads(getattr(m, "content", "{}"))
                                    plan_md = payload.get("plan_artifact", "")
                                    if plan_md:
                                        yield _sse(SSEEvent(type="plan_artifact", text=plan_md))
                                        logger.info("[API] Emitted plan_artifact SSE from tool output (%d chars).", len(plan_md))
                                except Exception as exc:
                                    logger.warning("[API] Could not parse plan_artifact from ToolMessage: %s", exc)

                            elif tool_name == "persist_and_validate_scenario_in_dwh":
                                # Emit scenario result confirmation card
                                try:
                                    payload = json.loads(getattr(m, "content", "{}"))
                                    if payload.get("write_success"):
                                        yield _sse(SSEEvent(type="scenario_result", data=payload))
                                        logger.info("[API] Emitted scenario_result SSE. SCENARIO_ID=%s", payload.get("scenario_id"))
                                except Exception as exc:
                                    logger.warning("[API] Could not parse scenario_result from ToolMessage: %s", exc)

                            # All other ToolMessages are suppressed — raw JSON stays internal

        except Exception as exc:
            logger.error("[API] Stream error: %s", exc, exc_info=True)
            yield _sse(SSEEvent(type="error", text=str(exc)))

        finally:
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
