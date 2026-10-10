"""
Scenario Deployment Route.

Handles the direct 'Deploy Scenario to Production' action from the
frontend Governance Metadata sidebar panel.
"""

import logging
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from services.aml_builder.web.services.graph import get_tool_driven_graph
from services.aml_builder.web.services.deployment_guard import (
    DeploymentGuardError,
    authorize_direct_deployment,
)
from services.aml_builder.web.services.production_registry import save_production_scenario
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

    project_id = request.project_id
    if not project_id or project_id in ("0", "None"):
        project_id = "no_project"
    thread_id = f"{project_id}_{request.session_id}_{request.user_id}"

    try:
        graph = await get_tool_driven_graph()
        config = {"configurable": {"thread_id": thread_id}}
        state = await graph.aget_state(config)
        messages = state.values.get("messages", []) if state else []
        evidence = authorize_direct_deployment(
            messages=messages,
            metadata=request.metadata_json,
            thread_id=thread_id,
            approved_by=request.user_id,
        )
    except DeploymentGuardError as exc:
        logger.warning("[DEPLOY] Deployment rejected (%s): %s", exc.code, exc)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "success": False,
                "message": str(exc),
                "validation_errors": [exc.code],
            },
        ) from exc
    except Exception as exc:
        logger.error("[DEPLOY] Error retrieving session state from checkpointer: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "success": False,
                "message": "Deployment evidence could not be verified. Please retry.",
                "validation_errors": ["deployment_evidence_unavailable"],
            },
        ) from exc

    intent_dict = evidence.intent
    raw_sql = evidence.raw_sql
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

    try:
        success = save_production_scenario(
            scenario_id=scenario_id,
            scenario_name=scenario_name,
            scenario_type=scenario_type,
            detection_logic=detection_logic,
            raw_sql=raw_sql,
            scenario_metadata=evidence.metadata,
            time_window_days=time_window_days,
            created_by=str(
                evidence.metadata.get("CREATED_BY") or settings.AML_CREATED_BY
            ),
            deployment_evidence=evidence,
        )
    except DeploymentGuardError as exc:
        logger.warning("[DEPLOY] Deployment claim rejected (%s): %s", exc.code, exc)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "success": False,
                "message": str(exc),
                "validation_errors": [exc.code],
            },
        ) from exc

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
