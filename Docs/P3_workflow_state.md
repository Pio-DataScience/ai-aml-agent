# P3: Workflow State and Guards

## What and why

P3 adds a server-side SQLite workflow record for each chat, a per-chat revision
history, append-only card audit storage, typed stage models, tool guard verdicts,
and read-only workflow APIs. LangGraph checkpoints remain in their separate
database and continue to store conversation messages.

The P3 policy defaults to `workflow.enforcement: observe` so the current chat
flow remains usable before P5 supplies typed approval actions. Observe mode
records ordinary guard failures as `would_block` workflow events and lets the
tool continue. Successful tools advance the stored stage. Deployed-chat locks
and permission denials always block. P5 must change the catalog to
`workflow.enforcement: enforce` when its approval actions are ready.

The first successful `analyze_intent_and_discover_explanation_codes` result
creates revision 1. Tool-result envelopes include the current `revision_id`
when one exists. Revision creation, supersession, tombstones, history eviction,
and the read API are implemented, but card-triggered revision creation remains
for P6/P6b. No restore endpoint is implemented.

Sidebar rows now use `(project_id, chat_id, user_id)` in the app-state database.
Legacy sidebar rows are not copied. The old checkpoint database is left as-is.

## How to run

From the repository root in PowerShell:

```powershell
$env:DEBUG = "false"
$env:PYTHONPATH = "services/aml_builder"
.\.venv\Scripts\python.exe -m compileall -q services app.py run_alert_engine.py
.\.venv\Scripts\python.exe -m pytest -q services/aml_builder/tests/test_p3_workflow_*.py services/aml_builder/tests/test_p3_session_store.py
.\run_dev.ps1
```

The compile command checks Python syntax. The pytest command runs isolated
workflow, revision, guard, API, context, SSE, and sidebar tests; it does not call
Oracle, an LLM, or Service B. `run_dev.ps1` starts the FastAPI service on port
8005 and initializes `app_state.sqlite` in the configured state directory.

## Configuration

- `STATE_DIR` defaults to `artifacts/v2`.
- `APP_STATE_DB_PATH` defaults to `STATE_DIR/app_state.sqlite` and can be
  overridden independently.
- `CHECKPOINT_DB_PATH` defaults to `STATE_DIR/checkpoints.sqlite`; it remains
  the graph checkpointer database.
- `config/aml_catalogs/policy.yaml:workflow.enforcement` accepts `observe` or
  `enforce` and currently defaults to `observe`.
- `config/aml_catalogs/policy.yaml:revisions.history_cap` defaults to 5
  undeployed history revisions per chat.

The app-state database uses SQLite WAL mode, a 10-second busy timeout, and
`PRAGMA user_version = 1` as its schema marker. Existing sidebar rows are not
migrated into the new database.

## API

- `GET /workflow/{project_id}/{chat_id}/{user_id}/state` returns the current
  stage, current revision summary, pending-card slot, and payload-free history
  summaries.
- `GET /workflow/{project_id}/{chat_id}/{user_id}/revisions` lists revision
  summaries without heavy payloads.
- `GET /workflow/{project_id}/{chat_id}/{user_id}/revisions/{revision_id}`
  returns a full revision after ownership checks.

All endpoints require the P2 trusted BFF identity and `chat.read` ownership
authorization.

