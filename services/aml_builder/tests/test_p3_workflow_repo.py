"""P3 workflow SQLite schema, revisions, eviction, and concurrency tests."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from services.aml_builder.web.workflow.repo import WorkflowRepo, WorkflowConflict


def _repo(path: Path, history_cap: int = 5) -> WorkflowRepo:
    return WorkflowRepo(path, history_cap=history_cap)


def test_workflow_store_uses_wal_busy_timeout_and_schema_version(
    tmp_path: Path,
) -> None:
    path = tmp_path / "app_state.sqlite"
    repo = _repo(path)
    repo.initialize()

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] >= 5000
        assert connection.execute("PRAGMA user_version").fetchone()[0] >= 1
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert {
        "scenario_workflow",
        "scenario_revision",
        "param_card_audit",
        "workflow_events",
        "chat_sessions",
    } <= tables


def test_get_or_create_allocates_scenario_identity_and_is_idempotent(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path / "app_state.sqlite")
    first = repo.get_or_create_workflow("t1", "p1", "c1", "u1")
    second = repo.get_or_create_workflow("t1", "p1", "c1", "u1")

    assert first.scenario_id.startswith("PRD_")
    assert first.scenario_key.startswith("PRD_")
    assert first.scenario_id == second.scenario_id
    assert first.version == second.version


def test_revision_supersession_preserves_payload_and_evicts_oldest(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path / "app_state.sqlite", history_cap=5)
    repo.get_or_create_workflow("t1", "p1", "c1", "u1")
    current = repo.create_revision("t1", card={"n": 1}, stage="PLAN_PENDING")
    current = repo.update_revision(
        current.revision_id,
        expected_version=repo.get_workflow("t1").version,
        shadow_json={"passed": True},
        metadata_json={"risk": "high"},
        stage="SHADOW_PASSED",
    )
    audit = repo.card_audit_repo
    audit.append(
        request_id="r1",
        thread_id="t1",
        revision_id=current.revision_id,
        user_id="u1",
        user_role="creator",
        source="ui",
        outcome="confirmed",
        rows=[],
    )

    revisions = [current]
    for revision_number in range(2, 8):
        next_revision = repo.create_revision(
            "t1", card={"n": revision_number}, stage="PLAN_PENDING"
        )
        revisions.append(next_revision)

    history = repo.list_history("t1")
    assert len([revision for revision in history if revision.status == "history"]) == 5
    assert history[0].revision_id == revisions[5].revision_id
    evicted = repo.get_revision(revisions[0].revision_id)
    assert evicted.status == "evicted"
    assert evicted.evicted_at is not None
    assert evicted.card_json is None
    assert evicted.shadow_json is None
    assert evicted.metadata_json is None
    newest_history = repo.get_revision(revisions[1].revision_id)
    assert newest_history.shadow_json is None
    assert newest_history.metadata_json == '{"risk": "high"}'
    assert newest_history.plan_approved_at is None
    assert audit.get("r1") is not None


def test_deployed_revision_survives_history_eviction(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "app_state.sqlite", history_cap=1)
    repo.get_or_create_workflow("t1", "p1", "c1", "u1")
    deployed = repo.create_revision("t1", card={"n": 1})
    second = repo.create_revision("t1", card={"n": 2})
    with sqlite3.connect(repo.path) as connection:
        connection.execute(
            "UPDATE scenario_revision SET status='deployed' WHERE revision_id=?",
            (deployed.revision_id,),
        )
    repo.create_revision("t1", card={"n": 3})
    fourth = repo.create_revision("t1", card={"n": 4})

    assert repo.get_revision(deployed.revision_id).status == "deployed"
    assert repo.get_revision(deployed.revision_id).card_json is not None
    assert repo.get_revision(second.revision_id).status == "evicted"
    assert repo.current("t1").revision_id == fourth.revision_id


def test_optimistic_lock_allows_only_one_concurrent_update(tmp_path: Path) -> None:
    path = tmp_path / "app_state.sqlite"
    repo = _repo(path)
    repo.get_or_create_workflow("t1", "p1", "c1", "u1")
    rev = repo.create_revision("t1", card={"initial": True})

    expected_version = repo.get_workflow("t1").version

    def update_at_same_version(value: str) -> str:
        local_repo = _repo(path)
        try:
            local_repo.update_revision(
                rev.revision_id,
                expected_version=expected_version,
                card_json={"value": value},
            )
            return "updated"
        except WorkflowConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(update_at_same_version, ["a", "b"]))
    assert sorted(outcomes) == ["conflict", "updated"]
