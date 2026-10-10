"""Deterministic tests for deployment approval and artifact integrity."""

import json
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from services.aml_builder.web.services import deployment_guard
from services.aml_builder.web.services.deployment_guard import (
    DeploymentEvidence,
    DeploymentGuardError,
    authorize_chat_deployment,
    authorize_direct_deployment,
    assert_evidence_matches,
    claim_deployment,
    compute_artifact_id,
    init_deployment_ledger,
    release_deployment_claim,
)
from services.aml_builder.web.services.production_registry import (
    save_production_scenario,
)


INTENT: Dict[str, Any] = {
    "scenario_name": "Cash monitoring",
    "scenario_type": "CUSTOMER",
    "detection_logic": "Find customers with cash deposits",
    "time_window": {"value": 7, "unit": "DAYS"},
}
SQL = "SELECT cus_num FROM transactions WHERE expl_code = '6'"
METADATA: Dict[str, Any] = {
    "COUNTRY_CODE": 400,
    "INST_CODE": 1,
    "SCENARIO_DES_ENG": "Cash monitoring",
    "SCENARIO_DES_NAT_LAN": "Cash monitoring",
    "ACTIVE_FLAG": "1",
    "VIOLATION_LEVEL": "H",
    "DEGREE_RISK_FLAG": "H",
    "CATEGORY_CODE": "1",
    "SCE_TYPE_CODE": "1",
    "CLASS_CODE": "1",
    "CREATED_BY": 999,
    "UPDATED_BY": 999,
}


def _tool_exchange(
    name: str, arguments: Dict[str, Any], payload: Dict[str, Any], call_id: str
) -> List[Any]:
    """Build a realistic AI tool call followed by its ToolMessage result."""
    return [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": name,
                    "args": arguments,
                    "id": call_id,
                    "type": "tool_call",
                }
            ],
        ),
        ToolMessage(
            content=json.dumps(payload),
            tool_call_id=call_id,
            name=name,
        ),
    ]


def _messages(
    *,
    validation_success: bool = True,
    approval: str = "Deploy this scenario to production",
) -> List[Any]:
    """Build a complete checkpoint lifecycle ending in user approval."""
    messages: List[Any] = [HumanMessage(content="Create a cash scenario")]
    messages.extend(
        _tool_exchange(
            "execute_oracle_dwh_shadow_test",
            {"intent_json": json.dumps(INTENT)},
            {"raw_sql": SQL, "validation_success": validation_success},
            "shadow-1",
        )
    )
    messages.extend(
        _tool_exchange(
            "prepare_scenario_metadata_for_persistence",
            {"metadata_json": json.dumps(METADATA)},
            {
                "metadata": METADATA,
                "ready_for_persistence": True,
                "missing_mandatory_fields": [],
            },
            "metadata-1",
        )
    )
    messages.append(HumanMessage(content=approval))
    return messages


@pytest.fixture(autouse=True)
def _offline_metadata_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep guard tests offline while exercising the definitive validation branch."""
    monkeypatch.setattr(
        deployment_guard,
        "validate_scenario_metadata",
        lambda metadata: (True, []),
    )


@pytest.fixture
def ledger_path() -> str:
    """Provide a repository-local SQLite path compatible with sandboxed CI."""
    path = Path("artifacts") / f"test_deployment_guard_{uuid.uuid4().hex}.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    yield str(path)
    if path.exists():
        path.unlink()


def test_chat_route_rejects_approval_bypass() -> None:
    """A conversational acknowledgement is not deployment approval."""
    with pytest.raises(DeploymentGuardError, match="Explicitly ask") as exc_info:
        authorize_chat_deployment(
            messages=_messages(approval="Looks good"),
            intent_json=json.dumps(INTENT),
            raw_sql=SQL,
            metadata_json=json.dumps(METADATA),
            thread_id="project_chat_user",
            approved_by="user",
        )
    assert exc_info.value.code == "missing_approval"


def test_latest_failed_shadow_test_invalidates_older_sql() -> None:
    """A failed retry cannot fall back to an older successful SQL artifact."""
    messages = _messages()
    messages.extend(
        _tool_exchange(
            "execute_oracle_dwh_shadow_test",
            {"intent_json": json.dumps(INTENT)},
            {"raw_sql": "SELECT broken FROM missing", "validation_success": False},
            "shadow-2",
        )
    )
    with pytest.raises(DeploymentGuardError) as exc_info:
        authorize_direct_deployment(
            messages=messages,
            metadata=METADATA,
            thread_id="project_chat_user",
            approved_by="user",
        )
    assert exc_info.value.code == "shadow_validation_failed"


def test_chat_route_rejects_stale_sql_and_intent() -> None:
    """Tool arguments must match the exact artifact used in shadow validation."""
    with pytest.raises(DeploymentGuardError) as sql_error:
        authorize_chat_deployment(
            messages=_messages(),
            intent_json=json.dumps(INTENT),
            raw_sql="SELECT cus_num FROM other_table",
            metadata_json=json.dumps(METADATA),
            thread_id="project_chat_user",
            approved_by="user",
        )
    assert sql_error.value.code == "stale_sql"

    changed_intent = {**INTENT, "scenario_name": "Edited scenario"}
    with pytest.raises(DeploymentGuardError) as intent_error:
        authorize_chat_deployment(
            messages=_messages(),
            intent_json=json.dumps(changed_intent),
            raw_sql=SQL,
            metadata_json=json.dumps(METADATA),
            thread_id="project_chat_user",
            approved_by="user",
        )
    assert intent_error.value.code == "stale_intent"


def test_intent_revision_after_shadow_requires_revalidation() -> None:
    """An edited intent invalidates an earlier successful shadow test."""
    messages = _messages()
    messages.extend(
        _tool_exchange(
            "analyze_intent_and_discover_explanation_codes",
            {"user_prompt": "Change the threshold"},
            {"enriched_intent": {**INTENT, "scenario_name": "Edited scenario"}},
            "intent-2",
        )
    )
    with pytest.raises(DeploymentGuardError) as exc_info:
        authorize_direct_deployment(
            messages=messages,
            metadata=METADATA,
            thread_id="project_chat_user",
            approved_by="user",
        )
    assert exc_info.value.code == "stale_shadow_test"


def test_pending_new_shadow_test_blocks_old_result() -> None:
    """A direct request cannot race a newer unfinished shadow test."""
    messages = _messages()
    messages.append(
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "execute_oracle_dwh_shadow_test",
                    "args": {"intent_json": json.dumps(INTENT)},
                    "id": "shadow-pending",
                    "type": "tool_call",
                }
            ],
        )
    )
    with pytest.raises(DeploymentGuardError) as exc_info:
        authorize_direct_deployment(
            messages=messages,
            metadata=METADATA,
            thread_id="project_chat_user",
            approved_by="user",
        )
    assert exc_info.value.code == "shadow_test_in_progress"


def test_chat_route_rejects_unreviewed_metadata_change() -> None:
    """Approval does not cover metadata edited after the review snapshot."""
    changed_metadata = {**METADATA, "VIOLATION_LEVEL": "M"}
    with pytest.raises(DeploymentGuardError) as exc_info:
        authorize_chat_deployment(
            messages=_messages(),
            intent_json=json.dumps(INTENT),
            raw_sql=SQL,
            metadata_json=json.dumps(changed_metadata),
            thread_id="project_chat_user",
            approved_by="user",
        )
    assert exc_info.value.code == "stale_metadata"


def test_invalid_metadata_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unavailable or invalid lookup evidence blocks direct deployment."""
    monkeypatch.setattr(
        deployment_guard,
        "validate_scenario_metadata",
        lambda metadata: (False, ["CATEGORY_CODE could not be verified"]),
    )
    with pytest.raises(DeploymentGuardError) as exc_info:
        authorize_direct_deployment(
            messages=_messages(),
            metadata=METADATA,
            thread_id="project_chat_user",
            approved_by="user",
        )
    assert exc_info.value.code == "invalid_metadata"


def test_writer_rejects_calls_without_server_evidence() -> None:
    """The shared database writer cannot be called without the guard."""
    with pytest.raises(TypeError, match="deployment_evidence"):
        save_production_scenario(
            scenario_id="PRD_TEST",
            scenario_name="Test",
            scenario_type="CUSTOMER",
            detection_logic="Test",
            raw_sql=SQL,
            scenario_metadata=METADATA,
        )


def test_evidence_rejects_derived_scenario_field_drift() -> None:
    """Writer-facing evidence also binds fields derived from the approved intent."""
    with pytest.raises(DeploymentGuardError) as exc_info:
        assert_evidence_matches(
            _evidence(),
            scenario_name="Renamed after approval",
            scenario_type="CUSTOMER",
            detection_logic="Find customers with cash deposits",
            raw_sql=SQL,
            scenario_metadata=METADATA,
            time_window_days=7,
        )
    assert exc_info.value.code == "artifact_mismatch"


def _evidence() -> DeploymentEvidence:
    """Create valid immutable evidence for ledger tests."""
    return DeploymentEvidence(
        artifact_id=compute_artifact_id(INTENT, SQL, METADATA),
        thread_id="project_chat_user",
        intent=INTENT,
        raw_sql=SQL,
        metadata=METADATA,
        approved_by="user",
        approval_source="direct_endpoint",
        approved_at="2026-10-10T00:00:00+00:00",
    )


def test_failed_claim_can_be_retried(ledger_path: str) -> None:
    """A pre-commit failure releases the artifact for a controlled retry."""
    evidence = _evidence()
    claim_deployment(evidence, ledger_path)
    release_deployment_claim(evidence.artifact_id, ledger_path)
    claim_deployment(evidence, ledger_path)


def test_concurrent_claims_allow_only_one_deployment(ledger_path: str) -> None:
    """Concurrent requests cannot both claim and persist one artifact."""
    init_deployment_ledger(ledger_path)
    evidence = _evidence()
    barrier = threading.Barrier(2)
    outcomes: List[str] = []

    def _claim() -> None:
        barrier.wait()
        try:
            claim_deployment(evidence, ledger_path)
            outcomes.append("claimed")
        except DeploymentGuardError as exc:
            outcomes.append(exc.code)

    threads = [threading.Thread(target=_claim) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert sorted(outcomes) == ["artifact_already_claimed", "claimed"]
