"""Offline identity and policy checks for P2."""

from fastapi import HTTPException
import pytest

from services.aml_builder.web.api.dependencies import (
    Identity,
    assert_identity_matches,
    validate_identity,
)
from services.aml_builder.web.domain.policy import authorize, get_permissions, get_role
from services.aml_builder.web.services.settings import Settings


@pytest.mark.parametrize(
    ("user_id", "auth_mode", "action", "expected"),
    [
        ("1002", "dev", "chat.read", True),
        ("1002", "dev", "scenario.create", False),
        ("1002", "dev", "scenario.revise", False),
        ("1002", "dev", "scenario.rollback", False),
        ("1002", "dev", "scenario.delete", False),
        ("1001", "dev", "chat.read", True),
        ("1001", "dev", "scenario.create", True),
        ("1001", "dev", "scenario.revise", False),
        ("1001", "dev", "scenario.rollback", False),
        ("1001", "dev", "scenario.delete", False),
        ("999", "dev", "chat.read", True),
        ("999", "dev", "scenario.create", True),
        ("999", "dev", "scenario.revise", True),
        ("999", "dev", "scenario.delete", True),
        ("unknown", "dev", "scenario.rollback", True),
        ("unknown", "jwt", "chat.read", True),
        ("unknown", "jwt", "scenario.create", False),
    ],
)
def test_authorize_role_matrix(user_id: str, auth_mode: str, action: str, expected: bool) -> None:
    """Apply configured roles and mode-specific defaults without external services."""
    assert authorize(user_id, action, auth_mode=auth_mode) is expected


def test_permissions_report_role_and_allowed_actions() -> None:
    """Permission endpoint payload reflects the policy catalog."""
    assert get_role("1002", auth_mode="dev") == "viewer"
    assert get_permissions("1002", auth_mode="dev") == {
        "role": "viewer",
        "allowed_actions": ["chat.read"],
    }


def test_email_mapping_is_case_insensitive_and_mode_controls_unknown_users(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verified email mappings work while unknown users get the configured mode default."""
    from types import SimpleNamespace

    from services.aml_builder.web.domain import policy

    fake_policy = SimpleNamespace(
        users={"Creator.Test@Example.com": "creator"},
        roles={"creator": [], "manager": [], "viewer": []},
        default_role={"dev": "manager", "jwt": "viewer"},
        auth_mode="dev",
    )
    monkeypatch.setattr(policy, "load_catalogs", lambda _directory: SimpleNamespace(policy=fake_policy))
    assert get_role("unlisted", "Creator.Test@Example.com", auth_mode="dev") == "creator"
    assert get_role("unlisted", "unlisted@example.com", auth_mode="dev") == "manager"
    assert get_role("unlisted", "unlisted@example.com", auth_mode="jwt") == "viewer"


@pytest.mark.parametrize(
    ("headers", "body_user", "body_project", "path_user", "path_project", "status"),
    [
        ({"x-user-id": "999", "x-user-email": "a@b", "x-project-id": "default", "x-service-token": "secret"}, None, None, None, None, 200),
        ({"x-user-id": "999", "x-project-id": "default", "x-service-token": "bad"}, None, None, None, None, 403),
        ({"x-user-id": "999", "x-project-id": "default"}, None, None, None, None, 403),
        ({"x-user-id": "999", "x-project-id": "default", "x-service-token": "secret"}, "1001", None, None, None, 403),
        ({"x-user-id": "999", "x-project-id": "default", "x-service-token": "secret"}, None, None, "1001", None, 403),
        ({"x-user-id": "999", "x-project-id": "default", "x-service-token": "secret"}, None, "other", None, None, 403),
        ({"x-user-id": "999", "x-project-id": "default", "x-service-token": "secret"}, None, None, None, "other", 403),
    ],
)
def test_require_identity_rejects_missing_or_forged_token(
    headers: dict[str, str],
    body_user: str | None,
    body_project: str | None,
    path_user: str | None,
    path_project: str | None,
    status: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Service A dependency accepts only shared-token identity headers."""
    config = Settings(
        ORACLE_DSN="unused",
        ORACLE_USER="unused",
        ORACLE_PASSWORD="unused",
        SERVICE_AUTH_TOKEN="secret",
        _env_file=None,
    )
    monkeypatch.setattr("services.aml_builder.web.api.dependencies.settings", config)
    valid_token = headers.get("x-service-token") == "secret"
    if valid_token:
        identity = validate_identity(
            headers=headers,
            service_token="secret",
        )
        assert identity == Identity(user_id="999", user_email=headers.get("x-user-email", ""), project_id="default")
        if body_user or body_project or path_user or path_project:
            with pytest.raises(HTTPException) as raised:
                assert_identity_matches(
                    identity,
                    body_user_id=body_user,
                    body_project_id=body_project,
                    path_user_id=path_user,
                    path_project_id=path_project,
                )
            assert raised.value.status_code == 403
        else:
            assert status == 200
    else:
        with pytest.raises(HTTPException) as raised:
            validate_identity(headers=headers, service_token="secret")
        assert raised.value.status_code == status


def test_identity_resource_check_blocks_cross_user_chat() -> None:
    """chat.read remains owner-scoped when a resource is supplied."""
    assert authorize("1002", "chat.read", {"user_id": "1002"}, auth_mode="jwt")
    assert not authorize("1002", "chat.read", {"user_id": "1001"}, auth_mode="jwt")


def test_viewer_tool_guard_returns_permission_error_before_work() -> None:
    """A viewer cannot invoke the scenario creation tool even with forged tool arguments."""
    from services.aml_builder.web.api.codec import decode_tool_result
    from services.aml_builder.web.services.tools import analyze_intent_and_discover_explanation_codes

    result = analyze_intent_and_discover_explanation_codes.invoke(
        {"user_prompt": "create a scenario"},
        config={"configurable": {"user_id": "1002", "user_email": "viewer@example.com"}},
    )

    decoded = decode_tool_result(result, "analyze_intent_and_discover_explanation_codes")
    assert decoded.payload["error"].startswith("PERMISSION_DENIED")
