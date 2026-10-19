"""Pure per-revision stage transition rules."""

from enum import StrEnum


class Stage(StrEnum):
    """Stages in one scenario revision's lifecycle."""

    EMPTY = "EMPTY"
    INTAKE = "INTAKE"
    PARAMS_PENDING = "PARAMS_PENDING"
    PARAMS_CONFIRMED = "PARAMS_CONFIRMED"
    CONFLICT_CHECKED = "CONFLICT_CHECKED"
    PLAN_PENDING = "PLAN_PENDING"
    PLAN_APPROVED = "PLAN_APPROVED"
    SHADOW_PASSED = "SHADOW_PASSED"
    RESULT_APPROVED = "RESULT_APPROVED"
    METADATA_READY = "METADATA_READY"
    DEPLOYED = "DEPLOYED"


_TRANSITIONS: dict[Stage, frozenset[Stage]] = {
    Stage.EMPTY: frozenset({Stage.INTAKE}),
    Stage.INTAKE: frozenset({Stage.PARAMS_PENDING}),
    Stage.PARAMS_PENDING: frozenset({Stage.PARAMS_CONFIRMED}),
    Stage.PARAMS_CONFIRMED: frozenset({Stage.CONFLICT_CHECKED}),
    Stage.CONFLICT_CHECKED: frozenset({Stage.PLAN_PENDING}),
    Stage.PLAN_PENDING: frozenset({Stage.PLAN_APPROVED}),
    Stage.PLAN_APPROVED: frozenset({Stage.SHADOW_PASSED}),
    Stage.SHADOW_PASSED: frozenset({Stage.PLAN_APPROVED, Stage.RESULT_APPROVED}),
    Stage.RESULT_APPROVED: frozenset({Stage.METADATA_READY}),
    Stage.METADATA_READY: frozenset({Stage.DEPLOYED}),
    Stage.DEPLOYED: frozenset(),
}


def can_transition(current: Stage | str, target: Stage | str) -> bool:
    """Return whether a revision may move from current to target."""
    try:
        source = Stage(current)
        destination = Stage(target)
    except ValueError:
        return False
    return destination in _TRANSITIONS[source]


def transition(current: Stage | str, target: Stage | str) -> Stage:
    """Validate and return a target stage."""
    destination = Stage(target)
    if not can_transition(current, destination):
        raise ValueError(f"Invalid workflow transition: {current} -> {target}")
    return destination
