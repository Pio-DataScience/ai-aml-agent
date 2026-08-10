"""
Chat session management routes — history retrieval and sidebar CRUD.

History reconstruction reads the tool-driven agent's LangGraph checkpoint
and reassembles it into conversation-level ChatMessage records (folding
tool-call/tool-result message pairs into their parent assistant turn).
Sidebar CRUD (list/rename/pin/delete) operates on the session_store table.
"""

import json
import logging
import os
from typing import Optional

from fastapi import APIRouter, HTTPException

from services.aml_builder.web.services.graph import get_tool_driven_graph
from services.aml_builder.web.services.schemas import (
    ChatHistoryResponse,
    ChatListResponse,
    ChatMessage,
    ChatSessionItem,
    DeleteChatResponse,
    RenameChatRequest,
    RenameChatResponse,
    TogglePinResponse,
)
from services.aml_builder.web.services.session_store import (
    list_chat_sessions,
    rename_chat_session,
    soft_delete_chat_session,
    toggle_chat_pin,
)
from services.aml_builder.web.services.settings import settings

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Session"])


def _normalize_project(project_id: str) -> str:
    return project_id if project_id not in ("0", "None") else "no_project"


@router.get("/chat/{project_id}/{chat_id}/{user_id}/history", response_model=ChatHistoryResponse)
async def get_chat_history(project_id: str, chat_id: str, user_id: str):
    """Retrieve chat history for a specific conversation."""
    try:
        normalized_project = _normalize_project(project_id)
        thread_id = f"{normalized_project}_{chat_id}_{user_id}"
        logger.info("[HISTORY] Retrieving history for thread_id: %s", thread_id)

        checkpoint_path = settings.CHECKPOINT_DB_PATH
        if not os.path.exists(checkpoint_path):
            logger.warning("[HISTORY] Checkpoint database not found: %s", checkpoint_path)
            return ChatHistoryResponse(user_id=user_id, project_id=project_id, chat_id=chat_id, messages=[])

        config = {"configurable": {"thread_id": thread_id}}
        graph = await get_tool_driven_graph()
        state = await graph.aget_state(config)

        chat_messages = []

        if state and state.values:
            messages_data = state.values.get("messages", [])
            tool_call_id_to_name = {}
            pending_plan_artifact = None
            pending_scenario_result = None

            for msg in messages_data:
                msg_class = msg.__class__.__name__
                if msg_class == "HumanMessage":
                    content = msg.content if hasattr(msg, "content") else str(msg)
                    chat_messages.append(ChatMessage(role="user", content=content))
                elif msg_class == "AIMessage":
                    if hasattr(msg, "tool_calls") and msg.tool_calls:
                        for tc in msg.tool_calls:
                            tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                            tc_name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", None)
                            if tc_id and tc_name:
                                tool_call_id_to_name[tc_id] = tc_name
                        continue

                    content = msg.content if hasattr(msg, "content") else ""

                    plan_art = None
                    val_res = None
                    esc_rep = None
                    if hasattr(msg, "additional_kwargs") and isinstance(msg.additional_kwargs, dict):
                        plan_art = msg.additional_kwargs.get("plan_artifact")
                        esc_rep = msg.additional_kwargs.get("escalation_report")
                        val_res = msg.additional_kwargs.get("validation_result")

                    # Fallback to pending artifacts extracted from preceding ToolMessages
                    if not plan_art and pending_plan_artifact:
                        plan_art = pending_plan_artifact
                        pending_plan_artifact = None
                    if not val_res and pending_scenario_result:
                        val_res = pending_scenario_result
                        pending_scenario_result = None

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
                elif msg_class == "ToolMessage" or getattr(msg, "type", None) == "tool":
                    tool_call_id = getattr(msg, "tool_call_id", None)
                    tool_name = tool_call_id_to_name.get(tool_call_id, "") or getattr(msg, "name", "")
                    content_raw = getattr(msg, "content", "")
                    if content_raw:
                        try:
                            payload = json.loads(content_raw) if isinstance(content_raw, str) else content_raw
                            if isinstance(payload, dict):
                                if tool_name == "generate_scenario_execution_plan" or "plan_artifact" in payload:
                                    if payload.get("plan_artifact"):
                                        pending_plan_artifact = payload.get("plan_artifact")
                                elif tool_name in ("persist_and_validate_scenario_in_dwh", "execute_oracle_dwh_shadow_test") or "write_success" in payload or "header_alert_count" in payload:
                                    pending_scenario_result = payload
                        except (json.JSONDecodeError, Exception):
                            pass

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


@router.get("/chats/user/{user_id}/list", response_model=ChatListResponse)
async def list_user_chats(user_id: str, project_id: Optional[str] = None):
    """List all chat sessions for a user, optionally filtered by project."""
    try:
        sessions = list_chat_sessions(user_id, project_id)
        chats = [ChatSessionItem(**s) for s in sessions]
        return ChatListResponse(chats=chats)
    except Exception as e:
        logger.error("[API] Error listing chats: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/chat/{project_id}/{chat_id}/{user_id}", response_model=DeleteChatResponse)
async def delete_chat_session(project_id: str, chat_id: str, user_id: str):
    """Soft delete a chat session for a user by setting is_deleted = 1."""
    normalized_project = _normalize_project(project_id)
    if not os.path.exists(settings.CHECKPOINT_DB_PATH):
        raise HTTPException(status_code=404, detail="Database not found")

    found = soft_delete_chat_session(chat_id, user_id, normalized_project)
    if not found:
        raise HTTPException(status_code=404, detail="Chat session not found")

    return DeleteChatResponse(status="success", message="Chat session soft-deleted successfully", chat_id=chat_id)


@router.put("/chat/{project_id}/{chat_id}/{user_id}/rename", response_model=RenameChatResponse)
async def rename_chat_session_route(project_id: str, chat_id: str, user_id: str, request: RenameChatRequest):
    """Rename a chat session by updating its title."""
    normalized_project = _normalize_project(project_id)
    if not os.path.exists(settings.CHECKPOINT_DB_PATH):
        raise HTTPException(status_code=404, detail="Database not found")

    found = rename_chat_session(chat_id, user_id, normalized_project, request.title)
    if not found:
        raise HTTPException(status_code=404, detail="Chat session not found")

    return RenameChatResponse(
        status="success",
        message="Chat session renamed successfully",
        chat_id=chat_id,
        title=request.title,
    )


@router.put("/chat/{project_id}/{chat_id}/{user_id}/pin", response_model=TogglePinResponse)
async def toggle_chat_pin_route(project_id: str, chat_id: str, user_id: str):
    """Toggle the pin status of a chat session."""
    normalized_project = _normalize_project(project_id)
    if not os.path.exists(settings.CHECKPOINT_DB_PATH):
        raise HTTPException(status_code=404, detail="Database not found")

    new_pin = toggle_chat_pin(chat_id, user_id, normalized_project)
    if new_pin is None:
        raise HTTPException(status_code=404, detail="Chat session not found")

    return TogglePinResponse(
        status="success",
        message="Chat session pin status toggled successfully",
        chat_id=chat_id,
        is_pinned=new_pin,
    )
