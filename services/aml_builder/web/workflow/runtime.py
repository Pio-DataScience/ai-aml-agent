"""Runtime repository factory shared by routes and tool wrappers."""

from services.aml_builder.web.services.config_loader import load_catalogs
from services.aml_builder.web.services.settings import settings
from services.aml_builder.web.workflow.repo import WorkflowRepo
from services.aml_builder.web.domain.hashing import normalize_sql, stable_hash
from services.aml_builder.web.workflow.cards import prepare_card
from services.aml_builder.web.workflow.repo import WorkflowConflict
from services.aml_builder.web.services.conflict_check import ConflictChecker


def get_workflow_repo() -> WorkflowRepo:
    """Return a configured repository for the active state path and catalogs."""
    policy = load_catalogs(settings.CATALOG_DIR).policy
    return WorkflowRepo(
        settings.APP_STATE_DB_PATH,
        history_cap=policy.revisions.history_cap,
        enforcement=policy.workflow.enforcement.model_dump(),
    )


def record_tool_success(
    repo: WorkflowRepo,
    thread_id: str,
    tool_name: str,
    payload: dict[str, object],
    *,
    actor: str = "system",
    conflict_checker: ConflictChecker | None = None,
) -> str | None:
    """Create the first revision or advance observed state after tool success."""
    if tool_name == "analyze_intent_and_discover_explanation_codes":
        try:
            card = prepare_card(
                repo, thread_id, payload, actor=actor, conflict_checker=conflict_checker
            )
        except WorkflowConflict as exc:
            workflow = repo.get_workflow(thread_id)
            payload["parameter_card"] = workflow.pending_card if workflow else None
            payload["card_auto_confirmed"] = False
            payload["conflict_report"] = exc.details
            return None
        current = repo.current(thread_id)
        payload["parameter_card"] = card or None
        payload["card_auto_confirmed"] = not bool(card)
        return current.revision_id if current else None
    if tool_name in {"generate_scenario_execution_plan", "prepare_scenario_metadata_for_persistence"}:
        current = repo.current(thread_id)
        workflow = repo.get_workflow(thread_id)
        if current and workflow:
            changes: dict[str, object] = {}
            if tool_name == "generate_scenario_execution_plan":
                plan = str(payload.get("plan_artifact") or "")
                changes["stage"] = "PLAN_PENDING"
                if plan:
                    changes.update(plan_md=plan, plan_hash=stable_hash(plan))
            else:
                changes["stage"] = "METADATA_READY"
                metadata = payload.get("metadata")
                if isinstance(metadata, dict):
                    changes.update(metadata_json=metadata, metadata_hash=stable_hash(metadata))
            if changes:
                repo.update_revision(current.revision_id, expected_version=workflow.version, **changes)
                return current.revision_id
    stage_by_tool: dict[str, str] = {}
    if tool_name == "execute_oracle_dwh_shadow_test":
        current = repo.current(thread_id)
        workflow = repo.get_workflow(thread_id)
        if current and workflow:
            shadow = dict(payload)
            sql = str(shadow.get("raw_sql") or "")
            sql_hash = stable_hash(normalize_sql(sql)) if sql else None
            shadow["sql_hash"] = sql_hash
            repo.update_revision(
                current.revision_id,
                expected_version=workflow.version,
                shadow_json=shadow,
                sql_hash=sql_hash,
                stage=("SHADOW_PASSED" if payload.get("validation_success") else "PLAN_APPROVED"),
            )
            return current.revision_id
    if tool_name == "persist_and_validate_scenario_in_dwh" and payload.get("write_success"):
        current = repo.current(thread_id)
        return current.revision_id if current else None
    stage = stage_by_tool.get(tool_name)
    if stage:
        revision = repo.set_stage(
            thread_id, stage, actor=actor, action=f"{tool_name}_succeeded"
        )
        return revision.revision_id if revision else None
    current = repo.current(thread_id)
    return current.revision_id if current else None
