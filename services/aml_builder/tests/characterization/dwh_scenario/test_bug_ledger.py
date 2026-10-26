"""Strict expected-failure tests for B's verified bug ledger."""

from __future__ import annotations

import pytest


@pytest.mark.parametrize(
    ("bug_id", "correct_behavior"),
    [
        ("BL1", "WITH queries pass AST read-only policy; DML and compound statements fail"),
        ("BL2", "index, search and writes use one experience collection constant"),
        ("BL3", "embedding keys come from settings and missing keys give config errors"),
        ("BL4", "memory compaction uses the LLM or is deliberately removed"),
        ("BL5", "critic uses its configured role provider and model"),
        ("BL6", "embedding model is cached by provider, model and device"),
        ("BL7", "readiness reports required and optional dependencies separately"),
        ("BL8", "index and graph rebuilds promote validated versions atomically"),
        ("BL9", "candidate fetch is bounded and reports truncation without total-count claims"),
        ("BL10", "output-token limit and context budget are independent and honored"),
    ],
)
@pytest.mark.xfail(strict=True, reason="B0 baseline: defect tracked in bug ledger; fixed by its assigned package")
def test_bug_ledger_correct_behavior_is_not_implemented_yet(
    bug_id: str, correct_behavior: str
) -> None:
    """Reserve one executable expectation per migration bug.

    Args:
        bug_id: Stable bug-ledger identifier.
        correct_behavior: Required outcome to bind to the future adapter test.

    Returns:
        None.

    Raises:
        AssertionError: Deliberately until the responsible migration package implements it.
    """
    assert bug_id.startswith("BL") and correct_behavior
    pytest.fail(f"{bug_id} has no migrated implementation yet: {correct_behavior}")
