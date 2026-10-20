"""Deployment route delegates to DeployService and preserves its response contract."""

from __future__ import annotations

from types import SimpleNamespace

from services.aml_builder.web.api.dependencies import Identity
from services.aml_builder.web.api.routes import deploy as deploy_route
from services.aml_builder.web.domain.deploy_service import DeploymentResult


def test_direct_route_uses_current_revision_and_returns_extra_receipt(monkeypatch):
    calls = {}

    class FakeWorkflowRepo:
        def get_workflow(self, _thread_id):
            return SimpleNamespace(parent_scenario_id="PRD_ABCDEF12_V1", parent_version=1)

        def current(self, _thread_id):
            return SimpleNamespace(revision_id="rev-current")

    class FakeDeployService:
        def __init__(self, **kwargs):
            calls["service"] = kwargs

        def deploy(self, **kwargs):
            calls["deploy"] = kwargs
            return DeploymentResult(
                scenario_id="PRD_ABCDEF12_V1",
                scenario_name="Name",
                receipt={"revision_id": "rev-current"},
            )

    monkeypatch.setattr(deploy_route, "get_workflow_repo", FakeWorkflowRepo)
    monkeypatch.setattr(deploy_route, "DeployService", FakeDeployService)
    monkeypatch.setattr(
        deploy_route,
        "load_catalogs",
        lambda _path: SimpleNamespace(
            policy=SimpleNamespace(
                workflow=SimpleNamespace(
                    enforcement=SimpleNamespace(model_dump=lambda: {"approvals": "observe", "card_confirmation": "observe", "conflict_check": "observe"}),
                    conflict_check_required=False,
                ),
                conflict_check=SimpleNamespace(on_unavailable="block"),
            )
        ),
    )
    monkeypatch.setattr(deploy_route, "authorize_role", lambda _identity: "creator")
    monkeypatch.setattr(deploy_route, "validate_scenario_metadata", lambda _metadata: (True, []))

    request = deploy_route.DeployRequest(
        session_id="chat",
        user_id="user",
        project_id="project",
        metadata_json={"COUNTRY_CODE": 1},
        revision_id="rev-current",
        change_reason="Approved revised threshold",
    )
    coroutine = deploy_route.deploy_scenario(request, Identity("user", "", "project"))
    try:
        coroutine.send(None)
    except StopIteration as completed:
        response = completed.value

    assert calls["deploy"]["thread_id"] == "project_chat_user"
    assert calls["deploy"]["revision_id"] == "rev-current"
    assert calls["deploy"]["parent_scenario_id"] == "PRD_ABCDEF12_V1"
    assert calls["deploy"]["parent_version"] == 1
    assert calls["deploy"]["change_reason"] == "Approved revised threshold"
    assert response.success is True
    assert response.scenario_id == "PRD_ABCDEF12_V1"
    assert response.receipt == {"revision_id": "rev-current"}
