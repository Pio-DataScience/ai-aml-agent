"""P5 history keeps text while excluding legacy ToolMessage artifacts."""

from langchain_core.messages import ToolMessage

from services.aml_builder.web.api.routes.sessions import _ignore_history_tool_message
from services.aml_builder.web.services.schemas import ChatMessage


def test_tool_message_artifacts_are_not_added_to_history() -> None:
    history = [ChatMessage(role="assistant", content="I prepared a plan.")]
    old_tool_result = ToolMessage(
        content='{"plan_artifact":"old plan","raw_sql":"SELECT 1"}',
        tool_call_id="tool-1",
        name="generate_scenario_execution_plan",
    )

    _ignore_history_tool_message(history, old_tool_result)

    assert len(history) == 1
    assert history[0].content == "I prepared a plan."
    assert history[0].plan_artifact is None
    assert history[0].scenario_result is None
