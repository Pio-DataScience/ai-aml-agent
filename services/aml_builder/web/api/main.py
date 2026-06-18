"""
FastAPI entry point for the AML Builder agent service.

Exposes:
  POST /chat/stream  — SSE streaming chat with the AML scenario agent
  GET  /health       — Health check (used by Docker / load balancer)

Follows the same API contract as the existing PioTech AI services
(AML reporting, DWH) for frontend compatibility.
"""

import json
import logging
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from langchain_core.messages import HumanMessage

from web.services.agent import AMLScenarioState, get_graph
from web.services.logging_config import setup_logging
from web.services.oracle import close_pool, init_pool
from web.services.schemas import ChatRequest, SSEEvent
from web.services.settings import settings

logger = logging.getLogger(__name__)


# =============================================================================
# LIFESPAN — Startup / Shutdown
# =============================================================================


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifecycle: initialize and clean up shared resources.

    Args:
        app (FastAPI): The FastAPI application instance.

    Yields:
        None: Control returns to FastAPI during the app's lifetime.
    """
    # Startup
    setup_logging()
    logger.info("[STARTUP] AML Builder service starting. port=%d", settings.SERVICE_PORT)

    # Initialize Oracle pool
    try:
        init_pool()
    except Exception as exc:
        logger.error("[STARTUP] Oracle pool init failed: %s", exc)
        # Non-fatal — service can still run and return meaningful errors

    # Pre-warm the LangGraph
    try:
        await get_graph()
        logger.info("[STARTUP] LangGraph compiled and warmed.")
    except Exception as exc:
        logger.error("[STARTUP] LangGraph compilation failed: %s", exc)

    yield

    # Shutdown
    logger.info("[SHUTDOWN] Closing SQLite checkpointer connection.")
    try:
        from web.services.agent import close_checkpointer
        await close_checkpointer()
    except Exception as exc:
        logger.error("[SHUTDOWN] Error closing checkpointer: %s", exc)

    logger.info("[SHUTDOWN] Closing Oracle pool.")
    close_pool()
    logger.info("[SHUTDOWN] AML Builder service stopped.")


# =============================================================================
# APP
# =============================================================================


app = FastAPI(
    title="PioTech AI — AML Scenario Builder",
    description=(
        "Autonomous multi-agent system that converts compliance manager intent "
        "into live, validated AML detection scenarios in the PioTech Oracle QB engine."
    ),
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Restrict in production to known frontend origins
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =============================================================================
# HEALTH
# =============================================================================


@app.get("/health", tags=["System"])
async def health_check() -> dict:
    """Health check endpoint for Docker and load balancer probes.

    Returns:
        dict: Service status and version.
    """
    return {
        "status": "healthy",
        "service": settings.SERVICE_NAME,
        "version": "1.0.0",
    }


# =============================================================================
# CHAT STREAM
# =============================================================================


@app.post("/chat/stream", tags=["Chat"])
async def chat_stream(request: ChatRequest) -> StreamingResponse:
    """Stream a chat response from the AML Scenario Agent.

    Accepts the user's message and conversation history, invokes the
    LangGraph agent, and streams Server-Sent Events back to the client.

    SSE event types emitted:
    - ``tool_call`` — when a node/tool is being executed
    - ``thinking``  — intermediate processing status
    - ``content``   — streaming text chunks
    - ``final_answer`` — the complete final response
    - ``scenario_result`` — structured scenario data (JSON)
    - ``error``     — error details
    - ``done``      — stream termination signal

    Args:
        request (ChatRequest): The incoming chat request with messages and metadata.

    Returns:
        StreamingResponse: An SSE stream of JSON-encoded events.
    """
    metadata = request.metadata
    user_id = metadata.get("user_id", "anonymous")
    chat_id = metadata.get("chat_id", uuid.uuid4().hex)
    project_id = metadata.get("project_id", "0")

    # Build thread ID for LangGraph state isolation per conversation
    thread_id = f"{project_id}_{chat_id}_{user_id}"

    logger.info(
        "[API] /chat/stream — user=%s thread=%s messages=%d",
        user_id,
        thread_id,
        len(request.messages),
    )

    # Convert API messages to LangChain message objects
    lc_messages = [
        HumanMessage(content=m.content)
        if m.role == "user"
        else __import__("langchain_core.messages", fromlist=["AIMessage"]).AIMessage(content=m.content)
        for m in request.messages
    ]

    graph = await get_graph()
    config = {"configurable": {"thread_id": thread_id}}

    initial_state: AMLScenarioState = {
        "messages": lc_messages,
        "user_intent": request.messages[-1].content if request.messages else "",
        "enriched_intent": None,
        "raw_sql": None,
        "sql_metadata": None,
        "scenario_parameters": None,
        "decomposition_confidence": 0.0,
        "scenario_code": None,
        "scenario_write_success": False,
        "validation_result": None,
        "validation_retry_count": 0,
        "next_action": "INTENT",
        "iteration_count": 0,
        "error_log": [],
    }

    async def event_generator() -> AsyncIterator[str]:
        """Async generator yielding SSE-formatted events.

        Yields:
            str: SSE-formatted event strings (``data: {...}\\n\\n``).
        """

        def _sse(event: SSEEvent) -> str:
            return f"data: {event.model_dump_json()}\n\n"

        try:
            # Signal start
            yield _sse(SSEEvent(type="thinking", status="Analyzing your request..."))

            # Stream graph execution
            async for event in graph.astream(initial_state, config=config):
                for node_name, node_output in event.items():
                    if not isinstance(node_output, dict):
                        continue

                    # Emit node activity signal
                    yield _sse(SSEEvent(type="tool_call", tool=node_name))

                    # Stream agent messages
                    messages = node_output.get("messages", [])
                    for msg in messages:
                        if hasattr(msg, "content") and msg.content:
                            content = msg.content
                            if isinstance(content, str) and content.strip():
                                # Stream in chunks for natural feel
                                chunk_size = 50
                                for i in range(0, len(content), chunk_size):
                                    yield _sse(SSEEvent(
                                        type="content",
                                        text=content[i:i + chunk_size],
                                    ))

                    # Emit validation result as structured data
                    val_result = node_output.get("validation_result")
                    if val_result and isinstance(val_result, dict):
                        yield _sse(SSEEvent(
                            type="scenario_result",
                            data=val_result,
                        ))

            # Get final state for the complete answer
            final_state = graph.get_state(config)
            if final_state and final_state.values:
                final_msgs = final_state.values.get("messages", [])
                if final_msgs:
                    last_msg = final_msgs[-1]
                    if hasattr(last_msg, "content") and last_msg.content:
                        yield _sse(SSEEvent(
                            type="final_answer",
                            text=last_msg.content,
                        ))

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
            "X-Accel-Buffering": "no",  # Disable Nginx buffering
            "Connection": "keep-alive",
        },
    )
