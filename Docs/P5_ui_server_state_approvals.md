# P5 — Server-driven workflow state and approvals

## What changed and why

P5 makes typed workflow state authoritative for approval, deployment, locking, and revision artifacts. The API now exposes hash-bound plan and shadow-result approvals, includes deployment readiness blockers in workflow state, and serves authenticated UI copy. ToolMessage artifacts are no longer reconstructed into chat history; the current revision supplies the Plan, Shadow, and Governance views.

Workflow rollout is gate-specific. `workflow.enforcement.approvals: enforce` is enabled in P5. `card_confirmation` and `conflict_check` remain `observe`; P6 must switch the former to `enforce`, and P8 must switch the latter to `enforce`. Hard permission, deployed-lock, and other safety guards remain always-on. While card confirmation is observed, a successful intent extraction advances the revision to `PLAN_PENDING` as the current flow expects.

## API

- `POST /workflow/{project_id}/{chat_id}/{user_id}/actions/approve_plan`
- `POST /workflow/{project_id}/{chat_id}/{user_id}/actions/approve_result`
- Both accept `{ "revision_id": "...", "expected_hash": "..." }` and return updated workflow state. The endpoints require authenticated own-chat access and `scenario.create` permission. A repeat with the same approved hash is idempotent.
- `GET /workflow/{project_id}/{chat_id}/{user_id}/state?include_current_revision=true` returns the current revision payload, `can_deploy`, and typed `blockers[]`.
- `GET /ui/copy` returns the authenticated UI-copy catalog.

Each approval appends a `workflow_events` row with the actor and a detail payload containing role, revision ID, and expected hash. A chat message such as “approved” has no approval authority; only the typed action endpoints advance approval stages.

## Run and verify

From the Service A repository:

```powershell
New-Item -ItemType Directory -Force .tmp-test-run | Out-Null
$env:TEMP = (Resolve-Path .tmp-test-run).Path
$env:TMP = $env:TEMP
.\.venv\Scripts\python.exe -m compileall -q services app.py run_alert_engine.py
.\.venv\Scripts\python.exe -m pytest services/aml_builder/tests/test_p5_workflow_actions.py services/aml_builder/tests/test_p5_history_artifacts.py -q
```

From the UI repository:

```powershell
npm run lint
npm run build
```

The tests use a temporary directory under the repository because the Windows sandbox's default pytest temporary directory is not writable.

## Manual UI checks

1. Start a new chat, submit a scenario, and verify the Plan tab shows the current revision badge and **Proceed** is enabled only at `PLAN_PENDING` for an authorized creator.
2. With a pending-card state returned by the service, reload the chat and verify its rows and notice are restored from state. (The typed card editing/confirmation flow is P6.)
3. Try an incorrect or stale approval from a stale browser tab; verify the UI refreshes workflow state and displays the stale notice.
4. Approve the plan, run a shadow test, then verify **Approve result** is disabled until the shadow result passes. Approve it and verify the server state advances.
5. Switch tabs, close and reopen the artifact panel, and reload the chat. Verify Plan, Shadow, and Governance data come from the current revision and remain visible. After P6 enables card edits, change a confirmed card after planning and verify the displayed current revision refreshes.
6. Verify Deploy is enabled only when `can_deploy` is true. Its disabled tooltip should explain the permission or first workflow blocker. On successful deploy, verify state reload reports the lock; the locked tip, composer placeholder, and **New chat** action appear.
7. While locked, verify **Create new version** remains disabled. Starting **New chat** must navigate to a fresh chat ID.
8. Submit the ordinary chat text “approved” before using an approval button; verify it does not change workflow approval state.
9. Confirm the UI loads `/ui/copy` once per browser session and remains usable with the built-in English copy if that request fails.

## Configuration for the next packages

- P6 changes only `workflow.enforcement.card_confirmation` from `observe` to `enforce` after typed card confirmation is implemented.
- P8 changes only `workflow.enforcement.conflict_check` from `observe` to `enforce` after the authoritative conflict checker is ready.
