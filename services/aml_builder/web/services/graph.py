"""
LangGraph ReAct agent wiring and lifecycle for the tool-driven AML agent.

Compiles the autonomous agent (goal prompt + tool registry + SQLite
checkpointer) and manages its process-lifetime singleton and shutdown.
"""

import logging
from pathlib import Path
from typing import Any

from services.aml_builder.web.services.settings import settings
from services.aml_builder.web.services.llm_client import build_llm
from services.aml_builder.web.services.tools import (
    analyze_intent_and_discover_explanation_codes,
    generate_scenario_execution_plan,
    execute_oracle_dwh_shadow_test,
    persist_and_validate_scenario_in_dwh,
)

logger = logging.getLogger(__name__)

_tool_graph = None
_checkpointer_conn = None


def _load_prompt(filename: str) -> str:
    """Load a system prompt from the prompts directory.

    Args:
        filename (str): Filename of the prompt markdown file.

    Returns:
        str: File contents as a string. Returns empty string on failure.
    """
    prompt_path = Path(__file__).parent / "prompts" / filename
    try:
        return prompt_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.warning("[PROMPT] Not found: %s — using empty prompt.", filename)
        return ""


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
            persist_and_validate_scenario_in_dwh,
        ]

        llm = build_llm()

        goal_prompt = _load_prompt("tool_driven_system.md")
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
