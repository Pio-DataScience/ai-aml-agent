"""P3 tool-result revision association tests."""

import json
from pathlib import Path

from langchain_core.runnables import RunnableConfig
import pytest

from services.aml_builder.web.api.codec import decode_tool_result, typed_tool
from services.aml_builder.web.services import settings as settings_module
from services.aml_builder.web.workflow.repo import WorkflowRepo


def test_tool_result_envelope_carries_current_revision_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shared wrapper associates successful tool outputs with current state."""
    monkeypatch.setattr(
        settings_module.settings, "APP_STATE_DB_PATH", tmp_path / "tool.sqlite"
    )
    monkeypatch.setattr(
        settings_module.settings, "CATALOG_DIR", Path("config/aml_catalogs")
    )
    repo = WorkflowRepo(tmp_path / "tool.sqlite", enforcement="observe")
    repo.get_or_create_workflow("p1_c1_999", "p1", "c1", "999")
    revision = repo.create_revision("p1_c1_999", card={"intent": True}, stage="INTAKE")

    def sample_tool(config: RunnableConfig = {}) -> str:
        """Return a small fake tool payload."""
        return json.dumps({"result": "ok"})

    wrapped = typed_tool(sample_tool)
    response = wrapped.invoke(
        {}, config={"configurable": {"thread_id": "p1_c1_999", "user_id": "999"}}
    )

    assert (
        decode_tool_result(response, "sample_tool").revision_id == revision.revision_id
    )
