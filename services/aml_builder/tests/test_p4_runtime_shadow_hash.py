"""Verify P4 records a shadow SQL hash through the workflow port."""

from services.aml_builder.web.domain.deploy_service import stable_hash_sql
from services.aml_builder.web.workflow.runtime import record_tool_success


def test_shadow_result_and_normalized_sql_hash_are_captured_without_sqlite():
    class FakeRepo:
        enforcement = "observe"

        def __init__(self):
            self.workflow = type("Workflow", (), {"version": 7})()
            self.revision = type("Revision", (), {"revision_id": "r1"})()
            self.updated = None

        def current(self, _thread_id):
            return self.revision

        def get_workflow(self, _thread_id):
            return self.workflow

        def update_revision(self, revision_id, *, expected_version, **changes):
            self.updated = (revision_id, expected_version, changes)
            return self.revision

    repo = FakeRepo()
    revision_id = record_tool_success(
        repo,
        "thread",
        "execute_oracle_dwh_shadow_test",
        {"raw_sql": "SELECT  1", "validation_success": True},
    )
    assert revision_id == "r1"
    assert repo.updated[1] == 7
    assert repo.updated[2]["sql_hash"] == stable_hash_sql("SELECT 1")
    assert repo.updated[2]["shadow_json"]["validation_success"] is True
    assert repo.updated[2]["stage"] == "SHADOW_PASSED"
