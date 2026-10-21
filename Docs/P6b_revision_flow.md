# P6b — Revision flow and capped history

## What changed and why

P6b makes a confirmed parameter change after plan creation a new draft revision. The new revision stores the confirmed intent, re-seeded metadata with valid user edits carried forward, and a freshly rendered deterministic plan. The prior revision remains in history with its card, plan, shadow result, and metadata payload unchanged. This keeps each draft's review artifacts together and prevents approvals or shadow results from being applied to changed parameters.

If the current revision has no `plan_md`, confirmation updates it in place. Otherwise, confirmation creates revision N+1 and moves the prior current revision to history. The new revision starts with no shadow result, SQL hash, approvals, conflict data, or signature. Conflict checks remain observe-only; P8 owns the authoritative gate.

## Atomic persistence and retries

One `BEGIN IMMEDIATE` transaction inserts the new revision, marks the prior revision as history, applies history-cap eviction, inserts the immutable card-audit row, clears the pending card, and updates the workflow stage/current-revision pointer. Any failure rolls the whole operation back. Repeating the same request ID and submitted values returns its committed revision; reusing the request ID with different values returns `CARD_STALE`. A card based on a revision that is no longer current also returns `CARD_STALE`.

The history cap is read from `config/aml_catalogs/policy.yaml` (`revisions.history_cap`, default 5). It counts retained undeployed history rows, excluding the current revision. The oldest excess row becomes an `evicted` tombstone: its card, plan, shadow, conflict, signature, and metadata payloads are nulled while IDs, hashes, and timestamps remain. Deployed revisions and permanent card-audit rows are not evicted.

Metadata carry-over compares stored metadata with values seeded from the prior intent when no explicit edited-field list exists. It persists the resulting `metadata_user_edited` keys on the revision card. Carried fields are checked by `validate_scenario_metadata`; invalid edits are dropped and reported as `carry_over: {kept: [...], dropped: [...]}`. The current metadata is re-seeded for the new intent before those valid edits are overlaid.

## Action response and UI

`confirm_params` returns the full current workflow state plus `revision_summary`, the new `plan_md`, `stage_changed` (including `revision_id`), `carry_over`, and `history_count`. The UI refreshes the plan, records a confirmation note at the card's chat position, clears the prior shadow result, and displays the current revision badge. No history picker or restore endpoint is part of P6b.

## How to run and verify

From the Service A repository root in PowerShell:

```powershell
$env:DEBUG = "false"
.\.venv\Scripts\python.exe -m compileall -q services app.py run_alert_engine.py
.\.venv\Scripts\python.exe -m pytest --basetemp=.pytest-tmp services/aml_builder/tests/test_p6_param_cards.py services/aml_builder/tests/test_p4_deploy_service.py -q
```

From the UI repository root:

```powershell
npm run lint
npm run build
```

The tests use temporary SQLite repositories and fake metadata lookup results. They do not call a live LLM, Oracle, or Service B. UI verification should confirm that card confirmation selects the refreshed Plan tab, the old shadow result is cleared, and the displayed revision badge advances.
