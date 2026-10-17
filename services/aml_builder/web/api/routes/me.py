"""Identity and permissions endpoints for the authenticated BFF."""

from fastapi import APIRouter, Depends

from services.aml_builder.web.api.dependencies import Identity, require_identity
from services.aml_builder.web.domain.policy import get_permissions

router = APIRouter(tags=["Identity"])


@router.get("/me/permissions")
async def my_permissions(identity: Identity = Depends(require_identity)) -> dict[str, object]:
    """Return the caller's policy role and allowed actions.

    Args:
        identity: Validated BFF identity.

    Returns:
        Role and action permissions for UI controls.
    """
    return get_permissions(identity.user_id, identity.user_email)
