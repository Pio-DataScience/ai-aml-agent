"""P10 model-context projection tests; persisted messages stay untouched."""

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from services.aml_builder.web.workflow.context import build_model_messages


def _tool(tool: str, revision_id: str | None, payload: object, call_id: str) -> ToolMessage:
    body = {"schema_version": 1, "status": "ok", "payload": payload}
    if revision_id:
        body["revision_id"] = revision_id
    return ToolMessage(
        content=__import__("json").dumps(body),
        name=tool,
        tool_call_id=call_id,
    )


def test_projection_trims_only_old_tool_messages_and_preserves_protected_items() -> None:
    """Current artifacts, pending cards, latest per tool, and recent turns survive."""
    messages = [
        HumanMessage(content="old user turn"),
        _tool("planner", "rev-old", {"data": "x" * 400}, "planner-old"),
        _tool("planner", "rev-current", {"data": "current plan"}, "planner-current"),
        _tool("intent", None, {"parameter_card": {"request_id": "req-pending"}, "data": "x" * 400}, "pending-card"),
        _tool("intent", None, {"data": "newer intent"}, "newer-intent"),
        _tool("shadow", "rev-old", {"data": "x" * 400}, "shadow-old"),
        HumanMessage(content="latest user turn"),
        _tool("shadow", "rev-old", {"data": "latest shadow"}, "shadow-latest"),
    ]
    original = [message.content for message in messages]

    projected = build_model_messages(
        messages,
        current_revision={"revision_id": "rev-current", "rev_no": 2, "stage": "PLAN_PENDING"},
        superseded={"rev-old": 3},
        pending_request_id="req-pending",
        context_policy={
            "trimming": True,
            "keep_latest_per_tool": True,
            "max_tool_payload_chars": 80,
            "keep_latest_turns": 1,
        },
    )

    projected_tools = [message for message in projected if isinstance(message, ToolMessage)]
    planner_old = next(message for message in projected_tools if message.tool_call_id == "planner-old")
    shadow_old = next(message for message in projected_tools if message.tool_call_id == "shadow-old")
    pending_card = next(message for message in projected_tools if message.tool_call_id == "pending-card")
    shadow_latest = next(message for message in projected_tools if message.tool_call_id == "shadow-latest")
    intent_latest = next(message for message in projected_tools if message.tool_call_id == "newer-intent")
    assert len(planner_old.content) < 100
    assert len(shadow_old.content) < 100
    assert any("current plan" in message.content for message in projected_tools)
    assert "req-pending" in pending_card.content
    assert len(shadow_latest.content) > 80
    assert "superseded by revision 3" not in shadow_latest.content
    assert "superseded by revision 3" in shadow_old.content
    assert "newer intent" in intent_latest.content
    assert messages[1].content == original[1]
    assert [message.content for message in messages] == original
    assert isinstance(projected[0], SystemMessage)


def test_projection_can_be_disabled() -> None:
    """The policy switch returns full tool payloads in model input."""
    source = _tool("planner", "rev-old", {"data": "x" * 200}, "planner-disabled")
    result = build_model_messages(
        [source],
        current_revision=None,
        superseded={},
        context_policy={"trimming": False, "max_tool_payload_chars": 20},
    )
    assert result[-1].content == source.content


def test_superseded_tool_only_history_still_gets_revision_stub() -> None:
    """A history with no human turn does not classify every tool result as recent."""
    source = _tool("", "rev-old", {"data": "old result"}, "legacy-tool")
    result = build_model_messages(
        [source],
        current_revision={"revision_id": "rev-current", "rev_no": 2, "stage": "PLAN_PENDING"},
        superseded={"rev-old": 2},
        context_policy={"trimming": True, "keep_latest_turns": 3},
    )
    projected = next(message for message in result if isinstance(message, ToolMessage))
    assert "[superseded by revision 2]" in projected.content


def test_repository_context_policy_enables_trimming_and_keeps_latest_per_tool() -> None:
    """The shipped catalog enables trimming with the protected-latest invariant."""
    from services.aml_builder.web.services.config_loader import load_catalogs

    context = load_catalogs().policy.context
    assert context.trimming is True
    assert context.keep_latest_per_tool is True
    assert context.max_tool_payload_chars == 4000
