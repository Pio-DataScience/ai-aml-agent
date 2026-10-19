"""
Scenario Deployment Route.

Handles the direct 'Deploy Scenario to Production' action from the
frontend Governance Metadata sidebar panel.
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from services.aml_builder.web.adapters.oracle_repo import OracleRegistryRepo, RegistrySchemaError
from services.aml_builder.web.domain.deploy_service import DeployError, DeployService
from services.aml_builder.web.services.scenario_metadata import validate_scenario_metadata
from services.aml_builder.web.services.settings import settings
from services.aml_builder.web.api.dependencies import Identity, assert_identity_matches, require_identity
from services.aml_builder.web.domain.policy import authorize
from services.aml_builder.web.services.config_loader import load_catalogs
from services.aml_builder.web.workflow.runtime import get_workflow_repo

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Deployment"])


class DeployRequest(BaseModel):
    session_id: str = Field(..., description="Chat session/thread ID.")
    user_id: str = Field(..., description="Canonical JWT subject performing deployment.")
    project_id: str = Field(..., description="Trusted BFF project ID.")
    metadata_json: Dict[str, Any] = Field(..., description="Officer governance metadata JSON.")
    revision_id: Optional[str] = Field(None, description="Current revision shown for deployment.")


class DeployResponse(BaseModel):
    success: bool
    scenario_id: Optional[str] = None
    scenario_name: Optional[str] = None
    message: str
    validation_errors: Optional[List[str]] = None
    receipt: Optional[Dict[str, Any]] = None


@router.post("/scenario/deploy", response_model=DeployResponse)
async def deploy_scenario(
    request: DeployRequest,
    identity: Identity = Depends(require_identity),
):
    """Deploy a shadow-tested AML scenario directly to the production registry.

    Validates the governance metadata from the side panel and performs an atomic
    dual-write into PIO_AML_AGENT_PRODUCTION_SCENARIOS and PIO_AML_AGENT_SCENARIOS.
    """
    assert_identity_matches(
        identity,
        body_user_id=request.user_id,
        body_project_id=request.project_id,
    )
    logger.info(
        "[DEPLOY] Received direct deployment request: session_id=%s, user=%s",
        request.session_id,
        request.user_id,
    )

    workflow_repo = get_workflow_repo()
    thread_id = f"{request.project_id}_{request.session_id}_{request.user_id}"
    workflow = workflow_repo.get_workflow(thread_id)
    revision = workflow_repo.current(thread_id) if workflow else None
    if revision is None:
        raise HTTPException(status_code=400, detail={"error_code": "STAGE_NOT_ALLOWED", "message": "No current workflow revision is available."})
    selected_revision_id = request.revision_id or revision.revision_id
    catalogs = load_catalogs(settings.CATALOG_DIR)
    service = DeployService(
        oracle_repo=OracleRegistryRepo(),
        workflow_repo=workflow_repo,
        metadata_validator=validate_scenario_metadata,
        authorizer=lambda user_id, action: authorize(
            user_id, action, user_email=identity.user_email
        ),
        enforcement=catalogs.policy.workflow.enforcement,
        conflict_check_required=catalogs.policy.workflow.conflict_check_required,
        numeric_created_by_default=settings.AML_CREATED_BY,
    )
    try:
        result = service.deploy(
            thread_id=thread_id,
            revision_id=selected_revision_id,
            actor_id=identity.user_id,
            actor_role=authorize_role(identity),
            metadata=request.metadata_json,
        )
    except (DeployError, RegistrySchemaError, RuntimeError) as exc:
        code = getattr(exc, "code", "DEPLOY_FAILED")
        if code == "PERMISSION_DENIED":
            http_status = status.HTTP_403_FORBIDDEN
        elif code in {
            "REVISION_STALE", "CARD_PENDING", "CHAT_LOCKED_DEPLOYED",
            "DUPLICATE_ACTIVE_SIGNATURE", "REGISTRY_SCHEMA_INCOMPATIBLE",
        }:
            http_status = status.HTTP_409_CONFLICT
        else:
            http_status = status.HTTP_400_BAD_REQUEST
        details = getattr(exc, "details", None)
        raise HTTPException(
            status_code=http_status,
            detail={"success": False, "error_code": code, "message": str(exc), "validation_errors": details},
        ) from exc

    logger.info("[DEPLOY] Scenario %s deployed successfully.", result.scenario_id)
    return DeployResponse(
        success=True,
        scenario_id=result.scenario_id,
        scenario_name=result.scenario_name,
        message=f"Scenario {result.scenario_id} deployed successfully to production.",
        receipt=result.receipt,
    )


def authorize_role(identity: Identity) -> str:
    """Resolve the actor role recorded on the deployment receipt."""
    from services.aml_builder.web.domain.policy import get_role

    return get_role(identity.user_id, identity.user_email)
