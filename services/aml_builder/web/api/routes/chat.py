"""
Chat streaming route — SSE endpoint that drives the tool-driven AML agent.

Translates LangGraph ReAct agent events (AIMessage tool calls, ToolMessage
outputs) into the SSE event contract the frontend chat UI and side panel
expect: tool_call, content, plan_artifact, scenario_result, done.
"""

import json
import logging
import re
import time
import uuid
from typing import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Request
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
from services.aml_builder.web.api.codec import decode_tool_result
from services.aml_builder.web.domain.policy import authorize
from services.aml_builder.web.workflow.runtime import get_workflow_repo
from services.aml_builder.web.workflow.runtime import record_tool_success
from services.aml_builder.web.workflow.repo import WorkflowConflict
from services.aml_builder.web.services.conflict_check import (
    has_comparable_core,
    make_conflict_checker,
)
from services.aml_builder.web.services.observability import (
    build_correlation_id,
    emit_tool_metric,
    resolve_request_id,
)
from services.aml_builder.web.services.io_executor import run_in_io_pool

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Chat"])


@router.post("/chat/stream")
async def chat_stream(
    request: ChatRequest,
    http_request: Request,
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
    request_id = resolve_request_id(http_request.headers.get("x-request-id"))
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

    action_text = request.messages[-1].content if request.messages else ""
    action_match = re.fullmatch(r"\[ACTION\] card_confirmed revision (\d+)", action_text)
    if metadata.get("action_message") is True and action_match:
        revision_number = action_match.group(1)

        async def action_acknowledgement() -> AsyncIterator[str]:
            """Acknowledge a typed card action without touching workflow persistence."""
            acknowledgement = (
                f"Parameters confirmed. Revision {revision_number} is ready for plan review."
            )
            yield f"data: {SSEEvent(type='content', text=acknowledgement).model_dump_json()}\n\n"
            yield f"data: {SSEEvent(type='done').model_dump_json()}\n\n"

        return StreamingResponse(
            action_acknowledgement(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
                "X-Request-ID": request_id,
            },
        )

    thread_id = f"{project_id}_{chat_id}_{user_id}"
    correlation_id = build_correlation_id(thread_id, request_id)

    workflow_repo = get_workflow_repo()
    await run_in_io_pool(
        workflow_repo.get_or_create_workflow,
        thread_id,
        project_id,
        chat_id,
        user_id,
    )

    first_msg = request.messages[0].content if request.messages else "New Chat"
    await run_in_io_pool(upsert_chat_session, chat_id, user_id, project_id, first_msg)

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
    trace_metadata = {
        "thread_id": thread_id,
        "correlation_id": correlation_id,
        "request_id": request_id,
    }
    if metadata.get("action_message") is True:
        trace_metadata["action_message"] = True
    config["metadata"] = trace_metadata
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
        tool_call_started_at: dict[str, float] = {}
        tool_token_usage: dict[str, dict[str, object]] = {}

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
                                        tool_call_started_at[t_id] = time.perf_counter()
                                        usage = getattr(m, "usage_metadata", None)
                                        if isinstance(usage, dict):
                                            tool_token_usage[t_id] = usage
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
                            tool_content = getattr(m, "content", "{}")
                            started_at = tool_call_started_at.pop(tool_call_id, None)
                            metric_status = "error"
                            metric_error_code = "INTERNAL_ERROR"
                            metric_revision_id = None
                            try:
                                decoded_tool = decode_tool_result(tool_content, tool_name)
                                metric_status = decoded_tool.status.value
                                metric_error_code = (
                                    decoded_tool.error_code.value
                                    if decoded_tool.error_code
                                    else None
                                )
                                metric_revision_id = decoded_tool.revision_id
                                tool_payload = dict(decoded_tool.payload)
                                if decoded_tool.status.value == "ok":
                                    checker = None
                                    if tool_name == "analyze_intent_and_discover_explanation_codes":
                                        checker = make_conflict_checker(workflow_repo)
                                        intent = tool_payload.get("enriched_intent")
                                        if isinstance(intent, dict) and has_comparable_core(intent):
                                            workflow_snapshot = await run_in_io_pool(
                                                workflow_repo.get_workflow, thread_id
                                            )
                                            tool_payload["conflict_report"] = await run_in_io_pool(
                                                checker.check_intent,
                                                intent,
                                                thread_id=thread_id,
                                                scenario_key=workflow_snapshot.scenario_key if workflow_snapshot else None,
                                                provisional=True,
                                            )
                                    try:
                                        await run_in_io_pool(
                                            record_tool_success,
                                            workflow_repo,
                                            thread_id,
                                            tool_name,
                                            tool_payload,
                                            actor=user_id,
                                            conflict_checker=checker,
                                        )
                                    except WorkflowConflict as exc:
                                        tool_payload["conflict_report"] = exc.details
                                        workflow_snapshot = await run_in_io_pool(
                                            workflow_repo.get_workflow, thread_id
                                        )
                                        tool_payload["parameter_card"] = workflow_snapshot.pending_card if workflow_snapshot else None
                                        tool_payload["card_auto_confirmed"] = False
                                tool_content = json.dumps(tool_payload, ensure_ascii=False)
                            except (ValueError, TypeError):
                                pass
                            if started_at is not None:
                                emit_tool_metric(
                                    correlation_id=correlation_id,
                                    thread_id=thread_id,
                                    revision_id=metric_revision_id,
                                    tool=tool_name or "unknown_tool",
                                    duration_ms=(time.perf_counter() - started_at) * 1000,
                                    status=metric_status,
                                    error_code=metric_error_code,
                                    token_usage=tool_token_usage.pop(tool_call_id, None),
                                )

                            try:
                                mapped_events = map_tool_result_to_sse(
                                    tool_name, tool_content
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
                        current_workflow = await run_in_io_pool(
                            workflow_repo.get_workflow, thread_id
                        )
                        if current_workflow is not None:
                            current_revision = await run_in_io_pool(
                                workflow_repo.current, thread_id
                            )
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
            "X-Request-ID": request_id,
        },
    )
