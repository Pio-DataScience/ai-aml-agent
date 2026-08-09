"""
FastAPI entry point for the AML Builder agent service.

Exposes:
  POST /chat/stream  — SSE streaming chat with the AML scenario agent
  GET  /health       — Health check (used by Docker / load balancer)

Follows the same API contract as the existing PioTech AI services
(AML reporting, DWH) for frontend compatibility.
"""
import os
import json
import logging
import uuid
import sqlite3
from datetime import datetime
from contextlib import asynccontextmanager
from typing import AsyncIterator, List, Optional

from fastapi import FastAPI, HTTPException, Request, Body, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from langchain_core.messages import HumanMessage

from web.services.agent import AMLScenarioState, get_graph
from web.services.logging_config import setup_logging
from web.services.oracle import close_pool, init_pool
from web.services.schemas import (
    ChatRequest,
    SSEEvent,
    ChatHistoryResponse,
    ChatListResponse,
    RenameChatRequest,
    DeleteChatResponse,
    RenameChatResponse,
    TogglePinResponse,
    ChatMessage,
    ChatSessionItem,
)
from web.services.settings import settings

logger = logging.getLogger(__name__)

# Global registry to track active scenario background tasks
RUNNING_TASKS = {}


async def run_scenario_graph_task(thread_id: str, initial_state: dict, config: dict):
    """Asynchronously execute the LangGraph scenario run in the background."""
    try:
        RUNNING_TASKS[thread_id] = {
            "status": "RUNNING",
            "active_node": "orchestrator",
            "error": None
        }
        graph = await get_graph()
        async for event in graph.astream(initial_state, config=config):
            for node_name, _ in event.items():
                RUNNING_TASKS[thread_id]["active_node"] = node_name

        RUNNING_TASKS[thread_id]["status"] = "COMPLETED"
        logger.info("[BACKGROUND TASK] Thread %s completed successfully.", thread_id)
    except Exception as exc:
        logger.error("[BACKGROUND TASK] Thread %s failed: %s", thread_id, exc, exc_info=True)
        RUNNING_TASKS[thread_id]["status"] = "FAILED"
        RUNNING_TASKS[thread_id]["error"] = str(exc)



def init_session_db():
    """Create chat_sessions table in the SQLite checkpointer database if missing."""
    checkpoint_path = settings.CHECKPOINT_DB_PATH
    from pathlib import Path
    Path(checkpoint_path).parent.mkdir(parents=True, exist_ok=True)
    
    conn = sqlite3.connect(checkpoint_path)
    try:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS chat_sessions (
                chat_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                title TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                is_pinned INTEGER DEFAULT 0,
                is_deleted INTEGER DEFAULT 0
            );
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_id ON chat_sessions(user_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_project ON chat_sessions(user_id, project_id);")
        conn.commit()
        logger.info("[DB] SQLite chat_sessions table verified/initialized.")
    except Exception as e:
        logger.error("[DB] Error initializing chat_sessions table: %s", e)
    finally:
        conn.close()


def upsert_chat_session(chat_id: str, user_id: str, project_id: str, first_message_content: str):
    """Insert or update a chat session in the sidebar metadata table."""
    checkpoint_path = settings.CHECKPOINT_DB_PATH
    conn = sqlite3.connect(checkpoint_path)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT title, is_deleted FROM chat_sessions WHERE chat_id = ?", (chat_id,))
        row = cursor.fetchone()
        
        now = datetime.utcnow().isoformat()
        
        if row:
            cursor.execute("""
                UPDATE chat_sessions 
                SET updated_at = ?, is_deleted = 0
                WHERE chat_id = ?
            """, (now, chat_id))
        else:
            title = first_message_content[:50]
            if not title:
                title = "New Chat"
            cursor.execute("""
                INSERT INTO chat_sessions (chat_id, user_id, project_id, title, updated_at, is_pinned, is_deleted)
                VALUES (?, ?, ?, ?, ?, 0, 0)
            """, (chat_id, user_id, project_id, title, now))
        conn.commit()
    except Exception as e:
        logger.error("[DB] Error upserting chat session %s: %s", chat_id, e)
    finally:
        conn.close()


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
    
    # Init sessions metadata table
    init_session_db()

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
    - ``tool_call``        — when a node/tool is being executed
    - ``thinking``         — intermediate processing status
    - ``content``          — streaming text chunks
    - ``final_answer``     — the complete final response
    - ``scenario_result``  — structured scenario + validation data (JSON)
    - ``plan_artifact``    — structured markdown execution plan (side panel)
    - ``escalation_report``— technical failure report for implementation team
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

    # Build thread ID for LangGraph state isolation per conversation
    thread_id = f"{project_id}_{chat_id}_{user_id}"

    # Upsert sidebar chat session metadata
    first_msg = request.messages[0].content if request.messages else "New Chat"
    upsert_chat_session(chat_id, user_id, project_id, first_msg)

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

    # Detect resume vs. first turn — avoid resetting plan/catalog state mid-flow
    existing_state = await graph.aget_state(config)
    is_resuming = bool(
        existing_state
        and existing_state.values
        and existing_state.values.get("messages")
    )

    if is_resuming:
        # Resume turn: only pass the new message + user_intent.
        # LangGraph merges these into the existing checkpoint; all other state
        # fields (plan_approved, plan_conditions, catalog_creations, etc.) remain.
        initial_state: AMLScenarioState = {
            "messages": lc_messages,
            "user_intent": request.messages[-1].content if request.messages else "",
        }
        logger.info("[API] Resuming existing thread — partial state update.")
    else:
        # First turn: full initialization with all v2 state fields
        initial_state = {
            "messages": lc_messages,
            "user_intent": request.messages[-1].content if request.messages else "",
            "enriched_intent": None,
            "raw_sql": None,
            "sql_metadata": None,
            "scenario_parameters": None,
            "decomposition_confidence": 0.0,
            "scenario_code": None,
            "rule_code": None,
            "scenario_write_success": False,
            "validation_result": None,
            "validation_retry_count": 0,
            "next_action": "INTENT",
            "iteration_count": 0,
            "error_log": [],
            # v2 fields
            "plan_artifact": None,
            "plan_conditions": None,
            "plan_approved": False,
            "catalog_creations": [],
            "write_verification": None,
            "escalation_report": None,
            "failure_mode": None,
            "catalog_integrity": True,
        }
        logger.info("[API] New thread — full state initialization.")

    async def event_generator() -> AsyncIterator[str]:
        """Async generator yielding SSE-formatted events.

        Yields:
            str: SSE-formatted event strings (``data: {...}\\n\\n``).
        """

        def _sse(event: SSEEvent) -> str:
            return f"data: {event.model_dump_json()}\n\n"

        has_yielded_content = False

        try:
            yield _sse(SSEEvent(type="thinking", status="Analyzing your request..."))

            if settings.USE_TOOL_DRIVEN_AGENT:
                logger.info("[API] Running production tool-driven ReAct agent engine...")
                from web.services.agent_tool_driven import get_tool_driven_graph
                from langchain_core.messages import AIMessage, ToolMessage

                tool_graph = await get_tool_driven_graph()
                user_msg = request.messages[-1].content if request.messages else ""
                inputs = {"messages": [HumanMessage(content=user_msg)]}

                async for event in tool_graph.astream(inputs, config=config):
                    for node_name, node_output in event.items():
                        if not isinstance(node_output, dict):
                            continue
                        msgs = node_output.get("messages", [])
                        for m in msgs:
                            # Suppress internal ToolMessages (raw JSON outputs)
                            if isinstance(m, ToolMessage) or getattr(m, "type", None) == "tool":
                                continue

                            if isinstance(m, AIMessage) or getattr(m, "type", None) == "ai":
                                # Emit tool call indicator if LLM requested a tool, but suppress raw payload
                                if getattr(m, "tool_calls", None):
                                    for tc in m.tool_calls:
                                        t_name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "tool")
                                        yield _sse(SSEEvent(type="tool_call", tool=t_name))
                                    continue

                                # Stream natural language content to user
                                text = getattr(m, "content", "")
                                if isinstance(text, str) and text.strip():
                                    yield _sse(SSEEvent(type="content", text=text))

                yield _sse(SSEEvent(type="done"))
                return

            async for event in graph.astream(initial_state, config=config):
                for node_name, node_output in event.items():
                    if not isinstance(node_output, dict):
                        continue

                    yield _sse(SSEEvent(type="tool_call", tool=node_name))

                    # Stream agent messages as chunked content
                    messages = node_output.get("messages", [])
                    for msg in messages:
                        if hasattr(msg, "content") and msg.content:
                            content = msg.content
                            if isinstance(content, str) and content.strip():
                                if has_yielded_content:
                                    yield _sse(SSEEvent(type="content", text="\n\n"))
                                has_yielded_content = True
                                chunk_size = 50
                                for i in range(0, len(content), chunk_size):
                                    yield _sse(SSEEvent(
                                        type="content",
                                        text=content[i:i + chunk_size],
                                    ))

                    # Plan artifact → side panel render
                    plan_artifact = node_output.get("plan_artifact")
                    if plan_artifact and isinstance(plan_artifact, str):
                        yield _sse(SSEEvent(type="plan_artifact", text=plan_artifact))
                        logger.info("[API] Emitted plan_artifact SSE (%d chars).", len(plan_artifact))

                    # Escalation report → technical report panel
                    escalation_report = node_output.get("escalation_report")
                    if escalation_report and isinstance(escalation_report, str):
                        yield _sse(SSEEvent(type="escalation_report", text=escalation_report))
                        logger.info("[API] Emitted escalation_report SSE.")

                    # Validation result → structured scenario result card
                    val_result = node_output.get("validation_result")
                    if val_result and isinstance(val_result, dict):
                        yield _sse(SSEEvent(type="scenario_result", data=val_result))

            # Final answer from last AI message in checkpoint
            final_state = await graph.aget_state(config)
            if final_state and final_state.values:
                final_msgs = final_state.values.get("messages", [])
                if final_msgs:
                    last_msg = final_msgs[-1]
                    if hasattr(last_msg, "content") and last_msg.content:
                        yield _sse(SSEEvent(type="final_answer", text=last_msg.content))

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


@app.post("/chat/{project_id}/{chat_id}/{user_id}", status_code=202, tags=["Chat"])
async def create_scenario_run(
    project_id: str,
    chat_id: str,
    user_id: str,
    request: ChatRequest,
    background_tasks: BackgroundTasks
):
    """Start an asynchronous long-running AML scenario run in the background.

    Returns 202 Accepted immediately with status RUNNING.
    """
    normalized_project = project_id if project_id not in ("0", "None") else "no_project"
    thread_id = f"{normalized_project}_{chat_id}_{user_id}"

    # Upsert sidebar chat session metadata
    first_msg = request.messages[0].content if request.messages else "New Chat"
    upsert_chat_session(chat_id, user_id, normalized_project, first_msg)

    logger.info(
        "[API] POST /chat/%s/%s/%s — Starting async scenario run. Thread=%s",
        project_id, chat_id, user_id, thread_id
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

    # Detect resume vs. first turn
    existing_state = await graph.aget_state(config)
    is_resuming = bool(
        existing_state
        and existing_state.values
        and existing_state.values.get("messages")
    )

    if is_resuming:
        initial_state: AMLScenarioState = {
            "messages": lc_messages,
            "user_intent": request.messages[-1].content if request.messages else "",
        }
    else:
        initial_state = {
            "messages": lc_messages,
            "user_intent": request.messages[-1].content if request.messages else "",
            "enriched_intent": None,
            "raw_sql": None,
            "sql_metadata": None,
            "scenario_parameters": None,
            "decomposition_confidence": 0.0,
            "scenario_code": None,
            "rule_code": None,
            "scenario_write_success": False,
            "validation_result": None,
            "validation_retry_count": 0,
            "next_action": "INTENT",
            "iteration_count": 0,
            "error_log": [],
            "plan_artifact": None,
            "plan_conditions": None,
            "plan_approved": False,
            "catalog_creations": [],
            "write_verification": None,
            "escalation_report": None,
            "failure_mode": None,
            "catalog_integrity": True,
        }

    # Register task as running immediately to prevent client status polling race conditions
    RUNNING_TASKS[thread_id] = {
        "status": "RUNNING",
        "active_node": "orchestrator",
        "error": None
    }
    # Queue the background task
    background_tasks.add_task(run_scenario_graph_task, thread_id, initial_state, config)

    return {
        "task_id": thread_id,
        "status": "RUNNING"
    }


@app.get("/chat/{project_id}/{chat_id}/{user_id}/status", tags=["Chat"])
async def get_scenario_run_status(project_id: str, chat_id: str, user_id: str):
    """Retrieve status of the latest scenario run."""
    normalized_project = project_id if project_id not in ("0", "None") else "no_project"
    thread_id = f"{normalized_project}_{chat_id}_{user_id}"

    # Check active registry
    if thread_id in RUNNING_TASKS:
        task_info = RUNNING_TASKS[thread_id]
        return {
            "status": task_info["status"],
            "active_node": task_info["active_node"],
            "error": task_info["error"]
        }

    # Check fallback: query checkpointer state to see if it exists
    config = {"configurable": {"thread_id": thread_id}}
    graph = await get_graph()
    state = await graph.aget_state(config)
    
    if state and state.values:
        error_log = state.values.get("error_log", [])
        last_error = error_log[-1] if error_log else None
        
        next_action = state.values.get("next_action")
        status = "COMPLETED"
        if next_action == "FAILURE":
            status = "FAILED"
            
        return {
            "status": status,
            "active_node": next_action or "done",
            "error": last_error
        }

    # Thread never existed
    return {
        "status": "FAILED",
        "active_node": None,
        "error": "No execution thread or run state found for this session."
    }


# =============================================================================
# SESSION MANAGEMENT ENDPOINTS
# =============================================================================


@app.get("/chat/{project_id}/{chat_id}/{user_id}/history", response_model=ChatHistoryResponse, tags=["Session"])
async def get_chat_history(project_id: str, chat_id: str, user_id: str):
    """Retrieve chat history for a specific conversation."""
    try:
        normalized_project = project_id if project_id not in ("0", "None") else "no_project"
        thread_id = f"{normalized_project}_{chat_id}_{user_id}"
        logger.info("[HISTORY] Retrieving history for thread_id: %s", thread_id)
        
        checkpoint_path = settings.CHECKPOINT_DB_PATH
        if not os.path.exists(checkpoint_path):
            logger.warning("[HISTORY] Checkpoint database not found: %s", checkpoint_path)
            return ChatHistoryResponse(user_id=user_id, project_id=project_id, chat_id=chat_id, messages=[])
            
        config = {"configurable": {"thread_id": thread_id}}
        graph = await get_graph()
        state = await graph.aget_state(config)
        
        chat_messages = []
        
        if state and state.values:
            messages_data = state.values.get("messages", [])
            for msg in messages_data:
                msg_class = msg.__class__.__name__
                if msg_class == "HumanMessage":
                    content = msg.content if hasattr(msg, "content") else str(msg)
                    chat_messages.append(ChatMessage(role="user", content=content))
                elif msg_class == "AIMessage":
                    if hasattr(msg, "tool_calls") and msg.tool_calls:
                        continue
                    content = msg.content if hasattr(msg, "content") else ""
                    
                    plan_art = None
                    val_res = None
                    esc_rep = None
                    if hasattr(msg, "additional_kwargs") and isinstance(msg.additional_kwargs, dict):
                        plan_art = msg.additional_kwargs.get("plan_artifact")
                        esc_rep = msg.additional_kwargs.get("escalation_report")
                        val_res = msg.additional_kwargs.get("validation_result")
                    
                    # Skip if message is entirely empty (no text and no artifacts)
                    if not content.strip() and not (plan_art or val_res or esc_rep):
                        continue
                            
                    chat_messages.append(ChatMessage(
                        role="assistant",
                        content=content,
                        plan_artifact=plan_art,
                        scenario_result=val_res,
                        escalation_report=esc_rep,
                        timestamp=msg.additional_kwargs.get("timestamp") if hasattr(msg, "additional_kwargs") else None
                    ))

        logger.info("[HISTORY] Retrieved %d conversation-level messages.", len(chat_messages))
        return ChatHistoryResponse(
            user_id=user_id,
            project_id=project_id,
            chat_id=chat_id,
            messages=chat_messages,
        )
    except Exception as e:
        logger.error("[HISTORY] Error retrieving history: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/chats/user/{user_id}/list", response_model=ChatListResponse, tags=["Session"])
async def list_user_chats(user_id: str, project_id: Optional[str] = None):
    """List all chat sessions for a user, optionally filtered by project."""
    try:
        checkpoint_path = settings.CHECKPOINT_DB_PATH
        if not os.path.exists(checkpoint_path):
            return ChatListResponse(chats=[])
            
        conn = sqlite3.connect(checkpoint_path)
        try:
            cursor = conn.cursor()
            if project_id and project_id not in ("0", "None"):
                cursor.execute("""
                    SELECT chat_id, project_id, title, updated_at, is_pinned, is_deleted
                    FROM chat_sessions
                    WHERE user_id = ? AND project_id = ? AND is_deleted = 0
                    ORDER BY is_pinned DESC, updated_at DESC
                """, (user_id, project_id))
            else:
                cursor.execute("""
                    SELECT chat_id, project_id, title, updated_at, is_pinned, is_deleted
                    FROM chat_sessions
                    WHERE user_id = ? AND is_deleted = 0
                    ORDER BY is_pinned DESC, updated_at DESC
                """, (user_id,))
                
            rows = cursor.fetchall()
            chats = [
                ChatSessionItem(
                    chat_id=row[0],
                    project_id=row[1],
                    title=row[2],
                    updated_at=row[3],
                    is_pinned=bool(row[4]),
                    is_deleted=bool(row[5]),
                )
                for row in rows
            ]
            return ChatListResponse(chats=chats)
        finally:
            conn.close()
    except Exception as e:
        logger.error("[API] Error listing chats: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/chat/{project_id}/{chat_id}/{user_id}", response_model=DeleteChatResponse, tags=["Session"])
async def delete_chat_session(project_id: str, chat_id: str, user_id: str):
    """Soft delete a chat session for a user by setting is_deleted = 1."""
    try:
        normalized_project = project_id if project_id not in ("0", "None") else "no_project"
        checkpoint_path = settings.CHECKPOINT_DB_PATH
        if not os.path.exists(checkpoint_path):
            raise HTTPException(status_code=404, detail="Database not found")
            
        conn = sqlite3.connect(checkpoint_path)
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT chat_id FROM chat_sessions WHERE chat_id = ? AND user_id = ? AND project_id = ?",
                (chat_id, user_id, normalized_project)
            )
            if not cursor.fetchone():
                raise HTTPException(status_code=404, detail="Chat session not found")
                
            cursor.execute(
                "UPDATE chat_sessions SET is_deleted = 1 WHERE chat_id = ? AND user_id = ? AND project_id = ?",
                (chat_id, user_id, normalized_project)
            )
            conn.commit()
            return DeleteChatResponse(status="success", message="Chat session soft-deleted successfully", chat_id=chat_id)
        finally:
            conn.close()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("[API] Error soft-deleting chat: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/chat/{project_id}/{chat_id}/{user_id}/rename", response_model=RenameChatResponse, tags=["Session"])
async def rename_chat_session(project_id: str, chat_id: str, user_id: str, request: RenameChatRequest):
    """Rename a chat session by updating its title."""
    try:
        normalized_project = project_id if project_id not in ("0", "None") else "no_project"
        checkpoint_path = settings.CHECKPOINT_DB_PATH
        if not os.path.exists(checkpoint_path):
            raise HTTPException(status_code=404, detail="Database not found")
            
        conn = sqlite3.connect(checkpoint_path)
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT chat_id FROM chat_sessions WHERE chat_id = ? AND user_id = ? AND project_id = ?",
                (chat_id, user_id, normalized_project)
            )
            if not cursor.fetchone():
                raise HTTPException(status_code=404, detail="Chat session not found")
                
            cursor.execute(
                "UPDATE chat_sessions SET title = ? WHERE chat_id = ? AND user_id = ? AND project_id = ?",
                (request.title, chat_id, user_id, normalized_project)
            )
            conn.commit()
            return RenameChatResponse(
                status="success",
                message="Chat session renamed successfully",
                chat_id=chat_id,
                title=request.title
            )
        finally:
            conn.close()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("[API] Error renaming chat: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/chat/{project_id}/{chat_id}/{user_id}/pin", response_model=TogglePinResponse, tags=["Session"])
async def toggle_chat_pin(project_id: str, chat_id: str, user_id: str):
    """Toggle the pin status of a chat session."""
    try:
        normalized_project = project_id if project_id not in ("0", "None") else "no_project"
        checkpoint_path = settings.CHECKPOINT_DB_PATH
        if not os.path.exists(checkpoint_path):
            raise HTTPException(status_code=404, detail="Database not found")
            
        conn = sqlite3.connect(checkpoint_path)
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT is_pinned FROM chat_sessions WHERE chat_id = ? AND user_id = ? AND project_id = ?",
                (chat_id, user_id, normalized_project)
            )
            row = cursor.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Chat session not found")
                
            current_pin = row[0]
            new_pin = 1 - current_pin
            
            cursor.execute(
                "UPDATE chat_sessions SET is_pinned = ? WHERE chat_id = ? AND user_id = ? AND project_id = ?",
                (new_pin, chat_id, user_id, normalized_project)
            )
            conn.commit()
            return TogglePinResponse(
                status="success",
                message="Chat session pin status toggled successfully",
                chat_id=chat_id,
                is_pinned=bool(new_pin)
            )
        finally:
            conn.close()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("[API] Error pinning chat: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
