"""
LangGraph ReAct agent wiring and lifecycle for the tool-driven AML agent.

Compiles the autonomous agent (goal prompt + tool registry + SQLite
checkpointer) and manages its process-lifetime singleton and shutdown.
"""

import logging
from pathlib import Path
from typing import Any
from langchain_core.runnables import RunnableConfig

from services.aml_builder.web.services.settings import settings
from services.aml_builder.web.services.llm_client import build_llm
from services.aml_builder.web.services.tools import (
    analyze_intent_and_discover_explanation_codes,
    generate_scenario_execution_plan,
    execute_oracle_dwh_shadow_test,
    prepare_scenario_metadata_for_persistence,
    persist_and_validate_scenario_in_dwh,
    query_production_scenario_registry,
)

from services.aml_builder.web.services.prompts.loader import load_prompt
from services.aml_builder.web.domain.policy import authorize
from services.aml_builder.web.workflow.context import build_model_messages
from services.aml_builder.web.workflow.runtime import get_workflow_repo
from services.aml_builder.web.services.config_loader import load_catalogs

logger = logging.getLogger(__name__)

_tool_graph = None
_checkpointer_conn = None


def workflow_pre_model_hook(
    state: dict[str, Any], config: RunnableConfig
) -> dict[str, Any]:
    """Provide current workflow context and transiently stub superseded results."""
    configurable = config.get("configurable", {})
    thread_id = configurable.get("thread_id")
    messages = list(state.get("messages", []))
    if not thread_id:
        return {"llm_input_messages": messages}
    repo = get_workflow_repo()
    workflow = repo.get_workflow(str(thread_id))
    if workflow is None:
        return {"llm_input_messages": messages}
    revision = repo.current(str(thread_id))
    summary = revision.payload_free_summary() if revision else None
    actions_by_stage = {
        "EMPTY": ["analyze_intent_and_discover_explanation_codes", "query_production_scenario_registry"],
        "INTAKE": ["generate_scenario_execution_plan", "query_production_scenario_registry"],
        "PARAMS_PENDING": ["query_production_scenario_registry"],
        "PARAMS_CONFIRMED": ["query_production_scenario_registry"],
        "CONFLICT_CHECKED": ["generate_scenario_execution_plan", "query_production_scenario_registry"],
        "PLAN_PENDING": ["generate_scenario_execution_plan", "query_production_scenario_registry"],
        "PLAN_APPROVED": ["execute_oracle_dwh_shadow_test", "query_production_scenario_registry"],
        "SHADOW_PASSED": ["prepare_scenario_metadata_for_persistence", "query_production_scenario_registry"],
        "RESULT_APPROVED": ["prepare_scenario_metadata_for_persistence", "query_production_scenario_registry"],
        "METADATA_READY": ["persist_and_validate_scenario_in_dwh", "query_production_scenario_registry"],
        "DEPLOYED": ["query_production_scenario_registry"],
    }
    action_permissions = {
        "analyze_intent_and_discover_explanation_codes": "scenario.create",
        "generate_scenario_execution_plan": "scenario.create",
        "execute_oracle_dwh_shadow_test": "scenario.create",
        "prepare_scenario_metadata_for_persistence": "scenario.create",
        "persist_and_validate_scenario_in_dwh": "scenario.create",
        "query_production_scenario_registry": "chat.read",
    }
    user_id = str(configurable.get("user_id", ""))
    user_email = str(configurable.get("user_email", "")) or None
    chat_may_deploy = load_catalogs(settings.CATALOG_DIR).policy.chat_may_deploy
    if repo.enforcement == "observe" and not workflow.locked:
        allowed_actions = [
            name
            for name, permission in action_permissions.items()
            if (name != "persist_and_validate_scenario_in_dwh" or chat_may_deploy)
            if authorize(
                user_id,
                permission,
                {"user_id": user_id},
                user_email=user_email,
            )
        ]
    else:
        allowed_actions = [
            name
            for name in actions_by_stage.get(workflow.stage, [])
            if (name != "persist_and_validate_scenario_in_dwh" or chat_may_deploy)
            if authorize(
                user_id,
                action_permissions[name],
                {"user_id": user_id},
                user_email=user_email,
            )
        ]
    projected = build_model_messages(
        messages,
        current_revision=summary,
        superseded=repo.list_superseded_revision_numbers(str(thread_id)),
        allowed_actions=allowed_actions,
        enforcement=repo.enforcement,
    )
    return {"llm_input_messages": projected}


async def get_tool_driven_graph() -> Any:
    """Return compiled LangGraph ReAct agent for tool-driven execution (with AsyncSqliteSaver checkpointer)."""
    global _tool_graph, _checkpointer_conn
    if _tool_graph is None:
        import aiosqlite
        from langgraph.prebuilt import create_react_agent
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        checkpoint_path = settings.CHECKPOINT_DB_PATH
        Path(checkpoint_path).parent.mkdir(parents=True, exist_ok=True)

        _checkpointer_conn = await aiosqlite.connect(checkpoint_path)
        checkpointer = AsyncSqliteSaver(_checkpointer_conn)

        tools = [
            analyze_intent_and_discover_explanation_codes,
            generate_scenario_execution_plan,
            execute_oracle_dwh_shadow_test,
            prepare_scenario_metadata_for_persistence,
            query_production_scenario_registry,
        ]
        if load_catalogs(settings.CATALOG_DIR).policy.chat_may_deploy:
            tools.append(persist_and_validate_scenario_in_dwh)

        llm = build_llm()

        goal_prompt = load_prompt("tool_driven_system.md")
        if not goal_prompt:
            goal_prompt = (
                "You are the autonomous AML Scenario Architect. Use your tools to extract scenario requirements, "
                "discover vector-matched domain explanation codes, generate implementation plans, execute DWH shadow tests, "
                "and persist validated scenarios in Oracle DWH."
            )

        _tool_graph = create_react_agent(
            model=llm,
            tools=tools,
            prompt=goal_prompt,
            checkpointer=checkpointer,
            pre_model_hook=workflow_pre_model_hook,
        )
        logger.info(
            "[TOOL_AGENT] Compiled production ReAct graph with checkpointer ready."
        )
    return _tool_graph


async def close_checkpointer() -> None:
    """Close the SQLite checkpointer connection if open."""
    global _checkpointer_conn
    if _checkpointer_conn is not None:
        try:
            await _checkpointer_conn.close()
            logger.info("[TOOL_AGENT] Checkpointer database connection closed.")
        except Exception as exc:
            logger.error(
                "[TOOL_AGENT] Error closing checkpointer database connection: %s", exc
            )
        _checkpointer_conn = None
