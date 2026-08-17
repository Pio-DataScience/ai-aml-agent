"""
Scenario Deployment Route.

Handles the direct 'Deploy Scenario to Production' action from the
frontend Governance Metadata sidebar panel.
"""

import json
import logging
import os
import uuid
from typing import Any, Dict, List, Optional
from datetime import datetime

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from services.aml_builder.web.services.graph import get_tool_driven_graph
from services.aml_builder.web.services.production_registry import save_production_scenario
from services.aml_builder.web.services.scenario_metadata import validate_scenario_metadata
from services.aml_builder.web.services.settings import settings

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Deployment"])


class DeployRequest(BaseModel):
    session_id: str = Field(..., description="Chat session/thread ID.")
    user_id: str = Field(default="user_dev_01", description="User ID performing deployment.")
    project_id: str = Field(default="no_project", description="Project ID.")
    metadata_json: Dict[str, Any] = Field(..., description="Officer governance metadata JSON.")


class DeployResponse(BaseModel):
    success: bool
    scenario_id: Optional[str] = None
    scenario_name: Optional[str] = None
    message: str
    validation_errors: Optional[List[str]] = None


@router.post("/scenario/deploy", response_model=DeployResponse)
async def deploy_scenario(request: DeployRequest):
    """Deploy a shadow-tested AML scenario directly to the production registry.

    Validates the governance metadata from the side panel and performs an atomic
    dual-write into PIO_AML_PRODUCTION_SCENARIOS and PIO_AML_SCENARIO.
    """
    logger.info(
        "[DEPLOY] Received direct deployment request: session_id=%s, user=%s",
        request.session_id,
        request.user_id,
    )

    # 1. Validate Governance Metadata
    is_valid, validation_errors = validate_scenario_metadata(request.metadata_json)
    if not is_valid:
        logger.warning("[DEPLOY] Metadata validation failed: %s", validation_errors)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "success": False,
                "message": "Governance metadata validation failed.",
                "validation_errors": validation_errors,
            },
        )

    # 2. Retrieve State from Checkpointer (searching candidate thread prefixes)
    candidate_thread_ids = [
        f"{request.project_id}_{request.session_id}_{request.user_id}",
        f"default_{request.session_id}_{request.user_id}",
        f"no_project_{request.session_id}_{request.user_id}",
        f"{request.session_id}_{request.user_id}",
        request.session_id,
    ]

    raw_sql = None
    intent_dict = {}

    try:
        graph = await get_tool_driven_graph()
        for t_id in candidate_thread_ids:
            config = {"configurable": {"thread_id": t_id}}
            state = await graph.aget_state(config)
            messages = state.values.get("messages", []) if state else []

            for m in reversed(messages):
                # Check for shadow test tool output or intent output
                if getattr(m, "type", None) == "tool" or m.__class__.__name__ == "ToolMessage":
                    content_str = getattr(m, "content", "")
                    if isinstance(content_str, str) and content_str.strip().startswith("{"):
                        try:
                            payload = json.loads(content_str)
                            if "raw_sql" in payload and not raw_sql:
                                raw_sql = payload.get("raw_sql")
                            if "sql" in payload and not raw_sql:
                                raw_sql = payload.get("sql")
                            if "enriched_intent" in payload and not intent_dict:
                                intent_dict = payload.get("enriched_intent", {})
                        except Exception:
                            pass
            if raw_sql:
                logger.info("[DEPLOY] Found verified SQL in checkpointer thread: %s", t_id)
                break
    except Exception as exc:
        logger.error("[DEPLOY] Error retrieving session state from checkpointer: %s", exc)

    if not raw_sql:
        logger.error("[DEPLOY] No verified shadow-tested SQL found for session %s", request.session_id)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "success": False,
                "message": "No shadow-tested SQL query found in this session. Please ensure the scenario was shadow tested first.",
                "validation_errors": ["Scenario must be shadow tested before deployment."],
            },
        )

    scenario_name = intent_dict.get("scenario_name", "AML Detection Scenario")
    scenario_type = intent_dict.get("scenario_type", "CUSTOMER")
    detection_logic = intent_dict.get("detection_logic", scenario_name)

    tw: Dict[str, Any] = intent_dict.get("time_window") or {}
    tw_value: int = int(tw.get("value", 0) or 0)
    tw_unit: str = str(tw.get("unit", "DAYS")).upper()
    unit_to_days: Dict[str, int] = {"DAYS": 1, "WEEKS": 7, "MONTHS": 30, "YEARS": 365}
    time_window_days: Optional[int] = (
        tw_value * unit_to_days.get(tw_unit, 1) if tw_value else None
    )

    scenario_id = f"PRD_{uuid.uuid4().hex[:8].upper()}"

    success = save_production_scenario(
        scenario_id=scenario_id,
        scenario_name=scenario_name,
        scenario_type=scenario_type,
        detection_logic=detection_logic,
        raw_sql=raw_sql,
        scenario_metadata=request.metadata_json,
        time_window_days=time_window_days,
        created_by=str(request.metadata_json.get("CREATED_BY") or settings.AML_CREATED_BY),
    )

    if not success:
        logger.error("[DEPLOY] Failed to persist scenario to Oracle DWH.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "success": False,
                "message": "Database error while persisting scenario into PIO_AML_PRODUCTION_SCENARIOS and PIO_AML_SCENARIO.",
            },
        )

    logger.info("[DEPLOY] Scenario %s deployed successfully.", scenario_id)
    return DeployResponse(
        success=True,
        scenario_id=scenario_id,
        scenario_name=scenario_name,
        message=f"Scenario {scenario_id} deployed successfully to production.",
    )
