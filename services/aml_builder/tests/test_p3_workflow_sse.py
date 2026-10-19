"""P3 stage-change SSE mapping tests."""

from services.aml_builder.web.api.sse_mapper import stage_changed_event


def test_stage_changed_event_includes_revision_id() -> None:
    event = stage_changed_event("PLAN_PENDING", "rev-1")
    assert event.model_dump(exclude_none=True) == {
        "type": "stage_changed",
        "data": {"stage": "PLAN_PENDING", "revision_id": "rev-1"},
    }
