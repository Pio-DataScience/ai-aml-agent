"""
Chat session management routes — history retrieval and sidebar CRUD.

History reconstruction reads the tool-driven agent's LangGraph checkpoint
and reassembles it into conversation-level ChatMessage records (folding
tool-call/tool-result message pairs into their parent assistant turn).
Sidebar CRUD (list/rename/pin/delete) operates on the session_store table.
"""

import logging
import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

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
from services.aml_builder.web.api.dependencies import Identity, assert_identity_matches, require_identity
from services.aml_builder.web.domain.policy import authorize

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Session"])


def _ignore_history_tool_message(chat_messages: list[ChatMessage], _message: object) -> None:
    """Keep old ToolMessage artifacts out of chat history reconstruction."""


def _normalize_project(project_id: str) -> str:
    return project_id if project_id not in ("0", "None") else "no_project"


@router.get("/chat/{project_id}/{chat_id}/{user_id}/history", response_model=ChatHistoryResponse)
async def get_chat_history(project_id: str, chat_id: str, user_id: str, identity: Identity = Depends(require_identity)):
    """Retrieve chat history for a specific conversation."""
    assert_identity_matches(identity, path_user_id=user_id, path_project_id=project_id)
    if not authorize(user_id, "chat.read", {"user_id": user_id}, user_email=identity.user_email):
        raise HTTPException(status_code=403, detail="Permission denied.")
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
                    meta_cat = None
                    if hasattr(msg, "additional_kwargs") and isinstance(msg.additional_kwargs, dict):
                        plan_art = msg.additional_kwargs.get("plan_artifact")
                        esc_rep = msg.additional_kwargs.get("escalation_report")
                        val_res = msg.additional_kwargs.get("validation_result")
                        meta_cat = msg.additional_kwargs.get("scenario_metadata_catalog")

                    # Skip if message is entirely empty (no text and no artifacts)
                    if not content.strip() and not (plan_art or val_res or esc_rep or meta_cat):
                        continue

                    chat_messages.append(ChatMessage(
                        role="assistant",
                        content=content,
                        plan_artifact=plan_art,
                        scenario_metadata_catalog=meta_cat,
                        scenario_result=val_res,
                        escalation_report=esc_rep,
                        timestamp=msg.additional_kwargs.get("timestamp") if hasattr(msg, "additional_kwargs") else None
                    ))
                elif msg_class == "ToolMessage" or getattr(msg, "type", None) == "tool":
                    _ignore_history_tool_message(chat_messages, msg)

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
async def list_user_chats(user_id: str, project_id: Optional[str] = None, identity: Identity = Depends(require_identity)):
    """List all chat sessions for a user, optionally filtered by project."""
    assert_identity_matches(identity, path_user_id=user_id, path_project_id=project_id)
    if not authorize(user_id, "chat.read", {"user_id": user_id}, user_email=identity.user_email):
        raise HTTPException(status_code=403, detail="Permission denied.")
    try:
        sessions = list_chat_sessions(user_id, project_id)
        chats = [ChatSessionItem(**s) for s in sessions]
        return ChatListResponse(chats=chats)
    except Exception as e:
        logger.error("[API] Error listing chats: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/chat/{project_id}/{chat_id}/{user_id}", response_model=DeleteChatResponse)
async def delete_chat_session(project_id: str, chat_id: str, user_id: str, identity: Identity = Depends(require_identity)):
    """Soft delete a chat session for a user by setting is_deleted = 1."""
    assert_identity_matches(identity, path_user_id=user_id, path_project_id=project_id)
    if not authorize(user_id, "chat.read", {"user_id": user_id}, user_email=identity.user_email):
        raise HTTPException(status_code=403, detail="Permission denied.")
    normalized_project = _normalize_project(project_id)
    if not os.path.exists(settings.APP_STATE_DB_PATH):
        raise HTTPException(status_code=404, detail="Database not found")

    found = soft_delete_chat_session(chat_id, user_id, normalized_project)
    if not found:
        raise HTTPException(status_code=404, detail="Chat session not found")

    return DeleteChatResponse(status="success", message="Chat session soft-deleted successfully", chat_id=chat_id)


@router.put("/chat/{project_id}/{chat_id}/{user_id}/rename", response_model=RenameChatResponse)
async def rename_chat_session_route(project_id: str, chat_id: str, user_id: str, request: RenameChatRequest, identity: Identity = Depends(require_identity)):
    """Rename a chat session by updating its title."""
    assert_identity_matches(identity, path_user_id=user_id, path_project_id=project_id)
    if not authorize(user_id, "chat.read", {"user_id": user_id}, user_email=identity.user_email):
        raise HTTPException(status_code=403, detail="Permission denied.")
    normalized_project = _normalize_project(project_id)
    if not os.path.exists(settings.APP_STATE_DB_PATH):
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
async def toggle_chat_pin_route(project_id: str, chat_id: str, user_id: str, identity: Identity = Depends(require_identity)):
    """Toggle the pin status of a chat session."""
    assert_identity_matches(identity, path_user_id=user_id, path_project_id=project_id)
    if not authorize(user_id, "chat.read", {"user_id": user_id}, user_email=identity.user_email):
        raise HTTPException(status_code=403, detail="Permission denied.")
    normalized_project = _normalize_project(project_id)
    if not os.path.exists(settings.APP_STATE_DB_PATH):
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
