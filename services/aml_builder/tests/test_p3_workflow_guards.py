"""P3 guard mode behavior tests."""

from pathlib import Path

import pytest

from services.aml_builder.web.workflow.repo import WorkflowRepo
from services.aml_builder.web.workflow.guards import guard_tool, GuardCode
from services.aml_builder.web.workflow.runtime import record_tool_success


@pytest.mark.parametrize("mode", ["observe", "enforce"])
@pytest.mark.parametrize(
    ("tool_name", "stage_blocked"),
    [
        ("analyze_intent_and_discover_explanation_codes", False),
        ("generate_scenario_execution_plan", True),
        ("execute_oracle_dwh_shadow_test", True),
        ("prepare_scenario_metadata_for_persistence", True),
        ("persist_and_validate_scenario_in_dwh", True),
        ("query_production_scenario_registry", False),
    ],
)
def test_guard_mode_verdict_and_stage_advance(
    tmp_path: Path, mode: str, tool_name: str, stage_blocked: bool
) -> None:
    repo = WorkflowRepo(tmp_path / f"{mode}.sqlite", enforcement=mode)
    repo.get_or_create_workflow("t1", "p1", "c1", "u1")
    revision = repo.create_revision("t1", card={"intent": True}, stage="INTAKE")

    verdict = guard_tool(repo, "t1", tool_name, revision.revision_id)

    assert verdict.allowed is (mode == "observe" or not stage_blocked)
    if stage_blocked:
        assert verdict.code == GuardCode.STAGE_NOT_ALLOWED
        events = repo.list_events("t1")
        assert any(event["action"] == "would_block" for event in events) is (
            mode == "observe"
        )


def test_hard_lock_and_permission_guards_apply_in_observe_mode(tmp_path: Path) -> None:
    repo = WorkflowRepo(tmp_path / "hard.sqlite", enforcement="observe")
    repo.get_or_create_workflow("t1", "p1", "c1", "u1")
    repo.mark_locked("t1")

    verdict = guard_tool(
        repo, "t1", "analyze_intent_and_discover_explanation_codes", None
    )
    assert verdict.allowed is False
    assert verdict.code == GuardCode.CHAT_LOCKED_DEPLOYED

    permission = guard_tool(
        repo, "t1", "query_production_scenario_registry", None, authorized=False
    )
    assert permission.allowed is False
    assert permission.code == GuardCode.PERMISSION_DENIED


def test_stale_revision_and_request_ids_are_typed(tmp_path: Path) -> None:
    repo = WorkflowRepo(tmp_path / "stale.sqlite", enforcement="enforce")
    repo.get_or_create_workflow("t1", "p1", "c1", "u1")
    revision = repo.create_revision("t1", card={})
    repo.set_pending_card("t1", {"request_id": "new"})

    revision_result = guard_tool(repo, "t1", "generate_scenario_execution_plan", "old")
    card_result = guard_tool(
        repo, "t1", "confirm_params", revision.revision_id, request_id="old"
    )

    assert revision_result.code == GuardCode.REVISION_STALE
    assert card_result.code == GuardCode.CARD_STALE


@pytest.mark.parametrize(
    ("tool_name", "payload", "expected_stage"),
    [
        (
            "analyze_intent_and_discover_explanation_codes",
            {"enriched_intent": {"x": 1}},
            "INTAKE",
        ),
        ("generate_scenario_execution_plan", {"plan_artifact": "plan"}, "PLAN_PENDING"),
        (
            "execute_oracle_dwh_shadow_test",
            {"validation_success": True},
            "SHADOW_PASSED",
        ),
        ("prepare_scenario_metadata_for_persistence", {"fields": []}, "METADATA_READY"),
    ],
)
def test_successful_tools_create_revision_and_advance_stage(
    tmp_path: Path, tool_name: str, payload: dict[str, object], expected_stage: str
) -> None:
    repo = WorkflowRepo(tmp_path / f"{tool_name}.sqlite", enforcement="observe")
    repo.get_or_create_workflow("t1", "p1", "c1", "u1")
    if tool_name != "analyze_intent_and_discover_explanation_codes":
        repo.ensure_initial_revision(thread_id="t1")

    revision_id = record_tool_success(repo, "t1", tool_name, payload)
    revision = repo.get_revision(revision_id)

    assert revision is not None
    assert revision.stage == expected_stage
    assert repo.get_workflow("t1").stage == expected_stage
