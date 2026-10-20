"""P8 authoritative deploy recheck and acknowledgement rules."""

from __future__ import annotations

import json

import pytest

from services.aml_builder.web.domain.deploy_service import DeployError
from test_p4_deploy_service import _fixture


class _Gate:
    def __init__(self, report):
        self.report = report

    def check(self, _thread_id, _revision_id):
        return dict(self.report)


def _warning_report(scenario_id="SCN_WARN"):
    matches = [{"scenario_id": scenario_id, "relation": "OVERLAP", "action": "warn_confirm"}]
    return {
        "revision_id": "revision-1",
        "provisional": False,
        "status": "needs_ack",
        "conflict_hash": "new-hash",
        "blocking": False,
        "needs_ack": True,
        "matches": matches,
    }


def test_deploy_recheck_rejects_new_identical_active_scenario():
    service, _workflow, oracle, command = _fixture()
    service.conflict_gate = _Gate({
        "revision_id": "revision-1", "provisional": False, "status": "blocked",
        "conflict_hash": "block-hash", "blocking": True, "needs_ack": False,
        "matches": [{"scenario_id": "new-duplicate", "relation": "IDENTICAL", "action": "block"}],
    })
    service.conflict_check_required = True
    with pytest.raises(DeployError) as error:
        service.deploy(**command)
    assert error.value.code == "CONFLICT_BLOCKED"
    assert error.value.details[0]["scenario_id"] == "new-duplicate"
    assert not oracle.rows


def test_new_or_unacknowledged_warn_match_requires_action_and_stays_metadata_ready():
    service, workflow, oracle, command = _fixture()
    service.conflict_gate = _Gate(_warning_report())
    service.conflict_check_required = True
    with pytest.raises(DeployError) as error:
        service.deploy(**command)
    assert error.value.code == "CONFLICT_ACK_REQUIRED"
    assert workflow.revision.stage == "METADATA_READY"
    assert json.loads(workflow.revision.conflict_json)["needs_ack"] is True
    assert not oracle.rows


def test_previous_acknowledgements_survive_same_warn_match_and_enter_receipt():
    service, workflow, oracle, command = _fixture()
    acknowledgement = {
        "revision_id": "revision-1", "scenario_id": "SCN_WARN", "relation": "OVERLAP",
        "actor": "user", "role": "manager", "time": "2026-10-20T00:00:00+00:00",
        "reason": "Reviewed",
    }
    workflow.revision = workflow.revision.__class__(
        **{**workflow.revision.__dict__, "conflict_json": json.dumps({"status": "acknowledged", "acknowledgements": [acknowledgement]})}
    )
    service.conflict_gate = _Gate(_warning_report())
    service.conflict_check_required = True
    result = service.deploy(**command)
    assert result.receipt["conflict_acknowledgements"] == [acknowledgement]
    assert oracle.rows["PRD_ABCDEF12_V1"]["override_conflicts"][0]["other_scenario_id"] == "SCN_WARN"


def test_required_unavailable_registry_refuses_deploy():
    service, _workflow, oracle, command = _fixture()
    service.conflict_gate = _Gate({"status": "unavailable", "matches": [], "blocking": False})
    service.conflict_check_required = True
    service.conflict_on_unavailable = "block"
    with pytest.raises(DeployError) as error:
        service.deploy(**command)
    assert error.value.code == "CONFLICT_CHECK_UNAVAILABLE"
    assert not oracle.rows
