"""P3 prompt context projection tests."""

from langchain_core.messages import SystemMessage, ToolMessage
import pytest
from pathlib import Path

from services.aml_builder.web.api.codec import encode_tool_result
from services.aml_builder.web.services import settings as settings_module
from services.aml_builder.web.workflow.context import build_model_messages
from services.aml_builder.web.workflow.repo import WorkflowRepo


def test_superseded_tool_output_is_stubbed_in_model_input_only() -> None:
    previous = ToolMessage(
        content='{"revision_id":"rev-1","payload":{"large":"result"}}',
        tool_call_id="tool-1",
    )
    current = {"revision_id": "rev-2", "rev_no": 2, "stage": "PLAN_PENDING"}

    result = build_model_messages(
        [previous], current_revision=current, superseded={"rev-1": 2}
    )

    assert isinstance(result[0], SystemMessage)
    stub = next(message for message in result if isinstance(message, ToolMessage))
    assert "[superseded by revision 2]" in stub.content
    assert "large" in previous.content


def test_pre_model_hook_keeps_existing_tool_flow_available_in_observe_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The observe hook exposes legacy tools while preserving hard permissions."""
    from services.aml_builder.web.services.graph import workflow_pre_model_hook

    monkeypatch.setattr(
        settings_module.settings, "APP_STATE_DB_PATH", tmp_path / "hook.sqlite"
    )
    monkeypatch.setattr(
        settings_module.settings, "CATALOG_DIR", Path("config/aml_catalogs")
    )
    repo = WorkflowRepo(tmp_path / "hook.sqlite", enforcement="observe")
    repo.get_or_create_workflow("p1_c1_999", "p1", "c1", "999")
    prior = repo.create_revision("p1_c1_999", card={"old": True}, stage="INTAKE")
    repo.create_revision("p1_c1_999", card={"new": True}, stage="PLAN_PENDING")
    old_result = ToolMessage(
        content=encode_tool_result(
            "sample", {"large": "prior"}, revision_id=prior.revision_id
        ),
        tool_call_id="tool-1",
    )

    result = workflow_pre_model_hook(
        {"messages": [old_result]},
        {"configurable": {"thread_id": "p1_c1_999", "user_id": "999"}},
    )

    model_messages = result["llm_input_messages"]
    context = model_messages[0].content
    tool_result = next(
        message for message in model_messages if isinstance(message, ToolMessage)
    )
    assert "execute_oracle_dwh_shadow_test" in context
    assert "advisory in observe mode" in context
    assert "[superseded by revision 1]" in tool_result.content
    assert "large" in old_result.content
