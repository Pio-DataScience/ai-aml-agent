"""Trusted BFF identity extraction and request identity checks."""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Mapping

from fastapi import HTTPException, Request, status

from services.aml_builder.web.services.settings import settings


@dataclass(frozen=True)
class Identity:
    """Identity fields asserted by the authenticated BFF."""

    user_id: str
    user_email: str
    project_id: str


def validate_identity(
    headers: Mapping[str, str], *, service_token: str | None
) -> Identity:
    """Validate the shared service token and parse identity headers.

    Args:
        headers: Incoming HTTP headers.
        service_token: Configured shared secret.

    Returns:
        Trusted identity values supplied by the BFF.

    Raises:
        HTTPException: When the token or required headers are missing or invalid.
    """
    supplied_token = headers.get("x-service-token", "")
    user_id = headers.get("x-user-id", "").strip()
    project_id = headers.get("x-project-id", "").strip()
    user_email = headers.get("x-user-email", "").strip()
    if not service_token or not hmac.compare_digest(supplied_token, service_token):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid service identity.")
    if not user_id or not project_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Missing service identity headers.")
    return Identity(user_id=user_id, user_email=user_email, project_id=project_id)


async def require_identity(request: Request) -> Identity:
    """FastAPI dependency that validates the BFF identity headers.

    Args:
        request: Incoming request.

    Returns:
        Validated identity.
    """
    return validate_identity(request.headers, service_token=settings.SERVICE_AUTH_TOKEN)


def assert_identity_matches(
    identity: Identity,
    *,
    body_user_id: str | None = None,
    body_project_id: str | None = None,
    path_user_id: str | None = None,
    path_project_id: str | None = None,
) -> None:
    """Reject body or route identity values that disagree with trusted headers.

    Args:
        identity: Validated BFF identity.
        body_user_id: Optional body user identifier.
        body_project_id: Optional body project identifier.
        path_user_id: Optional path user identifier.
        path_project_id: Optional path project identifier.

    Raises:
        HTTPException: If any supplied ID disagrees with the trusted identity.
    """
    if any(
        supplied is not None and supplied != expected
        for supplied, expected in (
            (body_user_id, identity.user_id),
            (body_project_id, identity.project_id),
            (path_user_id, identity.user_id),
            (path_project_id, identity.project_id),
        )
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Request identity does not match authenticated identity.")
