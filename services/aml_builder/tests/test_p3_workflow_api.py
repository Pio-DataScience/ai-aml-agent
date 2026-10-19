"""P3 workflow route ownership and response contract tests."""

from pathlib import Path

import pytest
from fastapi import HTTPException

from services.aml_builder.web.api.dependencies import Identity
from services.aml_builder.web.api.routes import workflow
from services.aml_builder.web.workflow.repo import WorkflowRepo


def _repo(tmp_path: Path) -> WorkflowRepo:
    repo = WorkflowRepo(tmp_path / "api.sqlite")
    repo.get_or_create_workflow("p1_c1_u1", "p1", "c1", "u1")
    return repo


def test_state_has_current_revision_pending_card_and_payload_free_history(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    current = repo.create_revision(
        "p1_c1_u1", card={"secret": "current"}, stage="INTAKE"
    )
    repo.create_revision("p1_c1_u1", card={"secret": "history"}, stage="INTAKE")
    repo.set_pending_card("p1_c1_u1", {"request_id": "request-1", "rows": []})

    result = workflow.get_workflow_state(
        "p1", "c1", "u1", Identity("u1", "", "p1"), repo
    )

    assert result["current_revision"]["revision_id"] != current.revision_id
    assert result["pending_card"]["request_id"] == "request-1"
    assert all("card_json" not in entry for entry in result["history"])
    revisions = workflow.list_workflow_revisions(
        "p1", "c1", "u1", Identity("u1", "", "p1"), repo
    )
    assert all("card_json" not in entry for entry in revisions["revisions"])
    detail = workflow.get_workflow_revision(
        "p1",
        "c1",
        "u1",
        result["current_revision"]["revision_id"],
        Identity("u1", "", "p1"),
        repo,
    )
    assert detail["card_json"] == '{"secret": "history"}'


@pytest.mark.parametrize(
    ("handler", "suffix"),
    [
        (workflow.get_workflow_state, "state"),
        (workflow.list_workflow_revisions, "revisions"),
        (workflow.get_workflow_revision, "revisions/missing"),
    ],
)
def test_all_workflow_endpoints_reject_identity_mismatch(
    tmp_path: Path, handler: object, suffix: str
) -> None:
    repo = _repo(tmp_path)
    route = handler
    arguments = ["p1", "c1", "u2"]
    if suffix == "revisions/missing":
        arguments.append("missing")

    with pytest.raises(HTTPException) as error:
        route(*arguments, Identity("u1", "", "p1"), repo)
    assert error.value.status_code == 403


def test_workflow_endpoints_reject_chat_owned_by_another_user(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    repo.get_or_create_workflow("p1_c2_u2", "p1", "c2", "u2")

    with pytest.raises(HTTPException) as error:
        workflow.get_workflow_state("p1", "c2", "u1", Identity("u1", "", "p1"), repo)
    assert error.value.status_code == 403
