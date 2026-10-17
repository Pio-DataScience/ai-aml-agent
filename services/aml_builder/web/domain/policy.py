"""Catalog-backed role authorization for Service A."""

from __future__ import annotations

from typing import Any, Literal

from services.aml_builder.web.services.config_loader import load_catalogs
from services.aml_builder.web.services.settings import settings

Action = Literal["chat.read", "scenario.create", "scenario.revise", "scenario.rollback", "scenario.delete"]


def get_role(
    user_id: str,
    user_email: str | None = None,
    *,
    auth_mode: str | None = None,
) -> str:
    """Resolve a user's configured policy role, applying the mode default.

    Args:
        user_id: Canonical JWT subject identifier.
        user_email: Verified email claim, when present.
        auth_mode: Optional mode override for focused tests.

    Returns:
        The configured role name.
    """
    policy = load_catalogs(settings.CATALOG_DIR).policy
    mode = auth_mode or policy.auth_mode
    mapped = policy.users.get(user_id)
    if mapped is None and user_email:
        mapped = next(
            (
                role
                for key, role in policy.users.items()
                if "@" in key and key.casefold() == user_email.casefold()
            ),
            None,
        )
    if mapped in policy.roles:
        return mapped
    if isinstance(policy.default_role, dict):
        role = policy.default_role.get(mode)
    else:
        role = policy.default_role
    return role if role in policy.roles else "viewer"


def get_permissions(
    user_id: str,
    user_email: str | None = None,
    *,
    auth_mode: str | None = None,
) -> dict[str, Any]:
    """Return the user's role and its allowed actions.

    Args:
        user_id: Canonical JWT subject identifier.
        user_email: Verified email claim, when present.
        auth_mode: Optional mode override for focused tests.

    Returns:
        JSON-compatible role and action list.
    """
    policy = load_catalogs(settings.CATALOG_DIR).policy
    role = get_role(user_id, user_email, auth_mode=auth_mode)
    return {"role": role, "allowed_actions": list(policy.roles.get(role, []))}


def authorize(
    user_id: str,
    action: Action | str,
    resource: dict[str, Any] | None = None,
    *,
    user_email: str | None = None,
    auth_mode: str | None = None,
) -> bool:
    """Check an action against the policy catalog and resource ownership.

    Args:
        user_id: Canonical caller identifier.
        action: Catalog action being requested.
        resource: Optional resource attributes, including ``user_id`` for chats.
        user_email: Verified caller email used for email-keyed mock mappings.
        auth_mode: Optional mode override for focused tests.

    Returns:
        Whether the role and resource policy allow the action.
    """
    policy = load_catalogs(settings.CATALOG_DIR).policy
    if not user_id:
        return False
    role = get_role(user_id, user_email, auth_mode=auth_mode)
    if action not in policy.roles.get(role, []):
        return False
    if action == "chat.read" and resource and not policy.cross_user_chat_read:
        owner_id = resource.get("user_id")
        owner_email = resource.get("user_email")
        return owner_id == user_id or (
            bool(owner_email)
            and bool(user_email)
            and str(owner_email).casefold() == user_email.casefold()
        )
    return True
