"""
Chat streaming route — SSE endpoint that drives the tool-driven AML agent.

Translates LangGraph ReAct agent events (AIMessage tool calls, ToolMessage
outputs) into the SSE event contract the frontend chat UI and side panel
expect: tool_call, content, plan_artifact, scenario_result, done.
"""

import json
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from services.aml_builder.web.services.graph import get_tool_driven_graph
from services.aml_builder.web.services.schemas import ChatRequest, SSEEvent
from services.aml_builder.web.services.session_store import upsert_chat_session
from services.aml_builder.web.services.settings import settings
from services.aml_builder.web.api.sse_mapper import map_tool_result_to_sse
from services.aml_builder.web.api.sse_mapper import stage_changed_event
from services.aml_builder.web.api.dependencies import (
    Identity,
    assert_identity_matches,
    require_identity,
)
from services.aml_builder.web.domain.policy import authorize
from services.aml_builder.web.workflow.runtime import get_workflow_repo

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Chat"])


@router.post("/chat/stream")
async def chat_stream(
    request: ChatRequest,
    identity: Identity = Depends(require_identity),
) -> StreamingResponse:
    """Stream a chat response from the AML Scenario Agent.

    Accepts the user's message and conversation history, invokes the
    tool-driven LangGraph ReAct agent, and streams Server-Sent Events
    back to the client.

    SSE event types emitted:
    - ``tool_call``                  — when a tool is being invoked
    - ``thinking``                   — intermediate processing status
    - ``content``                    — streaming text chunks
    - ``plan_artifact``              — structured markdown execution plan (side panel)
    - ``scenario_metadata_catalog``  — PIO_AML_AGENT_SCENARIOS field catalog + live lookup options (side panel)
    - ``scenario_result``            — structured scenario + validation data (JSON)
    - ``error``                      — error details
    - ``done``                       — stream termination signal

    Args:
        request (ChatRequest): The incoming chat request with messages and metadata.

    Returns:
        StreamingResponse: An SSE stream of JSON-encoded events.
    """
    metadata = request.metadata
    assert_identity_matches(
        identity,
        body_user_id=metadata.get("user_id"),
        body_project_id=metadata.get("project_id"),
    )
    if not authorize(identity.user_id, "chat.read", {"user_id": identity.user_id}, user_email=identity.user_email):
        raise HTTPException(status_code=403, detail="Permission denied.")
    user_id = identity.user_id
    chat_id = metadata.get("chat_id", uuid.uuid4().hex)
    project_id = identity.project_id
    if not project_id or project_id in ("0", "None"):
        project_id = "no_project"

    thread_id = f"{project_id}_{chat_id}_{user_id}"

    workflow_repo = get_workflow_repo()
    workflow_repo.get_or_create_workflow(thread_id, project_id, chat_id, user_id)

    first_msg = request.messages[0].content if request.messages else "New Chat"
    upsert_chat_session(chat_id, user_id, project_id, first_msg)

    logger.info(
        "[API] /chat/stream — user=%s thread=%s messages=%d",
        user_id,
        thread_id,
        len(request.messages),
    )

    tool_graph = await get_tool_driven_graph()
    config = {
        "configurable": {
            "thread_id": thread_id,
            "user_id": identity.user_id,
            "user_email": identity.user_email,
            "project_id": identity.project_id,
        },
        "recursion_limit": settings.AGENT_RECURSION_LIMIT,
    }
    user_msg = request.messages[-1].content if request.messages else ""
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

        try:
            yield _sse(SSEEvent(type="thinking", status="Analyzing your request..."))

            async for event in tool_graph.astream(inputs, config=config):
                for node_name, node_output in event.items():
                    if not isinstance(node_output, dict):
                        continue
                    msgs = node_output.get("messages", [])
                    tool_round_completed = False
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
                            tool_round_completed = True
                            tool_call_id = getattr(m, "tool_call_id", None)
                            tool_name = tool_call_id_to_name.get(tool_call_id, "")

                            try:
                                mapped_events = map_tool_result_to_sse(
                                    tool_name, getattr(m, "content", "{}")
                                )
                                for mapped_event in mapped_events:
                                    yield _sse(mapped_event)
                                    logger.info(
                                        "[API] Emitted %s SSE from %s.",
                                        mapped_event.type,
                                        tool_name or "tool",
                                    )
                            except Exception as exc:
                                logger.warning(
                                    "[API] Could not map ToolMessage to SSE: %s", exc
                                )

                            # All other ToolMessages are suppressed — raw JSON stays internal

                    if tool_round_completed:
                        current_workflow = workflow_repo.get_workflow(thread_id)
                        if current_workflow is not None:
                            current_revision = workflow_repo.current(thread_id)
                            yield _sse(
                                stage_changed_event(
                                    current_workflow.stage,
                                    current_revision.revision_id if current_revision else None,
                                )
                            )

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
