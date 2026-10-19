"""Runtime repository factory shared by routes and tool wrappers."""

from services.aml_builder.web.services.config_loader import load_catalogs
from services.aml_builder.web.services.settings import settings
from services.aml_builder.web.workflow.repo import WorkflowRepo


def get_workflow_repo() -> WorkflowRepo:
    """Return a configured repository for the active state path and catalogs."""
    policy = load_catalogs(settings.CATALOG_DIR).policy
    return WorkflowRepo(
        settings.APP_STATE_DB_PATH,
        history_cap=policy.revisions.history_cap,
        enforcement=policy.workflow.enforcement,
    )


def record_tool_success(
    repo: WorkflowRepo,
    thread_id: str,
    tool_name: str,
    payload: dict[str, object],
    *,
    actor: str = "system",
) -> str | None:
    """Create the first revision or advance observed state after tool success."""
    if tool_name == "analyze_intent_and_discover_explanation_codes":
        revision = repo.ensure_initial_revision(
            thread_id,
            created_by=actor,
            card={
                "intent": payload.get("enriched_intent"),
                "metadata": payload.get("scenario_metadata"),
            },
        )
        return revision.revision_id
    stage_by_tool = {
        "generate_scenario_execution_plan": "PLAN_PENDING",
        "execute_oracle_dwh_shadow_test": (
            "SHADOW_PASSED" if payload.get("validation_success") else "PLAN_APPROVED"
        ),
        "prepare_scenario_metadata_for_persistence": "METADATA_READY",
    }
    if tool_name == "persist_and_validate_scenario_in_dwh" and payload.get(
        "write_success"
    ):
        current = repo.current(thread_id)
        if current:
            repo.mark_deployed(thread_id, current.revision_id)
            return current.revision_id
    stage = stage_by_tool.get(tool_name)
    if stage:
        revision = repo.set_stage(
            thread_id, stage, actor=actor, action=f"{tool_name}_succeeded"
        )
        return revision.revision_id if revision else None
    current = repo.current(thread_id)
    return current.revision_id if current else None
