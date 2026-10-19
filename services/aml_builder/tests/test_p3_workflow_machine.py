"""P3 stage transition and guard behavior tests."""

import pytest

from services.aml_builder.web.workflow.machine import Stage, can_transition, transition


@pytest.mark.parametrize(
    ("current", "target", "allowed"),
    [
        (Stage.EMPTY, Stage.INTAKE, True),
        (Stage.INTAKE, Stage.PARAMS_PENDING, True),
        (Stage.PARAMS_PENDING, Stage.PARAMS_CONFIRMED, True),
        (Stage.PARAMS_CONFIRMED, Stage.CONFLICT_CHECKED, True),
        (Stage.CONFLICT_CHECKED, Stage.PLAN_PENDING, True),
        (Stage.PLAN_PENDING, Stage.PLAN_APPROVED, True),
        (Stage.PLAN_APPROVED, Stage.SHADOW_PASSED, True),
        (Stage.SHADOW_PASSED, Stage.RESULT_APPROVED, True),
        (Stage.RESULT_APPROVED, Stage.METADATA_READY, True),
        (Stage.METADATA_READY, Stage.DEPLOYED, True),
        (Stage.DEPLOYED, Stage.INTAKE, False),
        (Stage.EMPTY, Stage.SHADOW_PASSED, False),
    ],
)
def test_revision_transition_table(
    current: Stage, target: Stage, allowed: bool
) -> None:
    assert can_transition(current, target) is allowed
    if allowed:
        assert transition(current, target) == target
    else:
        with pytest.raises(ValueError):
            transition(current, target)
