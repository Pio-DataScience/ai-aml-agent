"""Isolated characterization of Service B's handoff contract."""

from __future__ import annotations

import importlib.util
import ast
import json
from pathlib import Path
from typing import Any

import pytest


SOURCE_CONTRACT = (
    Path(__file__).resolve().parents[6]
    / "PioTech-AI"
    / "services"
    / "dwh_scenario_agent"
    / "web"
    / "services"
    / "scenario_contract.py"
).resolve()
SERVICE_B_AVAILABLE = SOURCE_CONTRACT.is_file()


@pytest.fixture(scope="module")
def contract() -> Any:
    """Load Service B's dependency-light contract without adding its package to sys.path.

    Args:
        None.

    Returns:
        The isolated scenario_contract module.

    Raises:
        ImportError: If the source checkout or contract module is unavailable.
    """
    if not SOURCE_CONTRACT.is_file():
        pytest.skip("PioTech-AI source checkout is not available")
    spec = importlib.util.spec_from_file_location("b0_isolated_scenario_contract", SOURCE_CONTRACT)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load isolated contract at {SOURCE_CONTRACT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _intent() -> dict[str, Any]:
    """Build the minimal Service A intent used by the source contract tests.

    Args:
        None.

    Returns:
        JSON-compatible handoff intent.

    Raises:
        None.
    """
    return {
        "scenario_name": "Cash activity",
        "scenario_type": "TRANSACTION",
        "detection_logic": "Detect cash activity",
        "transaction_type": "CASH DEPOSIT",
        "explanation_codes": ["4"],
        "thresholds": [],
        "time_window": {"unit": "DAYS", "value": 30, "is_rolling": True},
        "baseline_window": None,
    }


def test_envelope_parsing_and_readiness(contract: Any) -> None:
    """Verify typed envelope parsing and readiness guard.

    Args:
        contract: Isolated Service B contract module.

    Returns:
        None.

    Raises:
        AssertionError: If valid or unready requests are handled incorrectly.
    """
    envelope = {"request_type": "AML_SCENARIO_GENERATION", "intent": _intent()}
    parsed = contract.parse_service_a_intent(json.dumps(envelope))
    assert parsed["scenario_name"] == "Cash activity"
    assert contract.parse_service_a_intent(json.dumps({"request_type": "CHAT", "intent": {}})) is None
    with pytest.raises(ValueError, match="not ready"):
        contract.parse_service_a_intent(
            json.dumps({"request_type": "AML_SCENARIO_GENERATION", "intent": {**_intent(), "ready_for_handoff": False}})
        )


def test_execution_record_and_required_projection(contract: Any) -> None:
    """Verify candidate execution consistency and customer projection checks.

    Args:
        contract: Isolated Service B contract module.

    Returns:
        None.

    Raises:
        AssertionError: If execution evidence or projection is inconsistent.
    """
    result = contract.CandidateExecution(sql="SELECT CUS_NUM FROM T", success=True, row_count=0)
    assert result.sql == "SELECT CUS_NUM FROM T"
    assert contract.required_projection_error(_intent(), ["cus_num"]) is None
    assert contract.required_projection_error(_intent(), ["OTHER"])
    with pytest.raises(ValueError):
        contract.CandidateExecution(sql="SELECT 1", success=True, row_count=None)


def test_returned_sql_identity_and_fail_closed_behavior(contract: Any) -> None:
    """Verify SQL identity normalization and execution failure representation.

    Args:
        contract: Isolated Service B contract module.

    Returns:
        None.

    Raises:
        AssertionError: If SQL identity or failure validation changes unexpectedly.
    """
    sql = "SELECT CUS_NUM FROM T"
    execution = contract.CandidateExecution(sql=sql, success=True, row_count=1)
    assert execution.sql == sql
    assert execution.identity() == contract.CandidateExecution(sql=sql, success=True, row_count=1).identity()
    with pytest.raises(ValueError):
        contract.CandidateExecution(sql=sql, success=False, row_count=1)


@pytest.mark.skipif(not SERVICE_B_AVAILABLE, reason="PioTech-AI checkout is not available")
def test_critic_output_is_not_implicitly_approved() -> None:
    """Verify the critic node approves only its explicit APPROVE verdict.

    Args:
        None.

    Returns:
        None.

    Raises:
        AssertionError: If the source allows implicit approval or SQL on rejection.
    """
    agent_path = SOURCE_CONTRACT.parents[1] / "services" / "agent.py"
    agent_source = agent_path.read_text(encoding="utf-8")
    module = ast.parse(agent_source)
    critique = next(
        node for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "critique_node"
    )
    source = ast.get_source_segment(agent_source, critique)
    assert source is not None
    assert 'verdicts[0] == "APPROVE"' in source
    assert '"approved_sql": candidate.sql' in source
    assert '"approved_sql": None' in source
    assert 'elif not candidate.success' in source
    assert 'normalize_sql(proposed_sql) != candidate.sql' in source


@pytest.mark.skipif(not SERVICE_B_AVAILABLE, reason="PioTech-AI checkout is not available")
def test_executed_sql_is_the_sql_in_candidate_record() -> None:
    """Verify the SQL handed to Oracle is also recorded as executed evidence.

    Args:
        None.

    Returns:
        None.

    Raises:
        AssertionError: If the source stops recording the sanitized executed SQL.
    """
    source = (SOURCE_CONTRACT.parents[1] / "services" / "agent.py").read_text(encoding="utf-8")
    assert "executed_sql = sql_query" in source
    assert "run_readonly(sql_query)" in source
    assert "CandidateExecution(\n            sql=executed_sql, success=True" in source


@pytest.mark.skipif(not SERVICE_B_AVAILABLE, reason="PioTech-AI checkout is not available")
def test_final_sql_sse_event_is_guarded_by_approval() -> None:
    """Inspect the API AST to ensure final SQL is emitted only in approved_sql branch.

    Args:
        None.

    Returns:
        None.

    Raises:
        AssertionError: If the final-answer event is no longer under the approval guard.
    """
    main_path = SOURCE_CONTRACT.parents[1] / "api" / "main.py"
    module = ast.parse(main_path.read_text(encoding="utf-8"))
    guards = [
        node for node in ast.walk(module)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Name)
        and node.test.id == "approved_sql"
    ]
    assert guards
    guarded_source = "\n".join(ast.unparse(node) for node in guards[0].body)
    assert "final_answer" in guarded_source
    assert "approved_sql" in guarded_source
