# P4 Registry v2 and DeployService

## What and why

P4 replaces in-place scenario persistence with versioned registry inserts. A
deployment must refer to the current typed workflow revision, pass the hard
permission, metadata, and shadow-result checks, and write the production row,
governance metadata, and audit entry in one Oracle transaction. The workflow is
locked afterward in SQLite. If Oracle commits and the SQLite lock fails, a retry
finds the row by `THREAD_ID` and only completes the SQLite lock.

`ensure_schema()` creates missing registry-v2 tables and indexes. It never changes
or removes an existing object. Development deployments use
`PIO_AML_AGENT_PRODUCTION_SCENARIOS` and `PIO_AML_AGENT_SCENARIOS`; the existing
`PIO_AML_PRODUCTION_SCENARIOS` and `PIO_AML_SCENARIO` remain untouched for easy
fallback. A legacy shape in either existing table does not block this registry.

The first allocated version uses ID `PRD_<8_HEX>_V1` and key `PRD_<8_HEX>`.
The P4 parent-supersession path is implemented in the repository but is not wired
to a UI action; P9 owns the new-version flow.

## Owner-run Oracle steps

1. Review `scripts/registry_v2.sql` against the target schema. The script is for
   owner-run review in TOAD only.
2. Keep the existing `PIO_AML_PRODUCTION_SCENARIOS` and `PIO_AML_SCENARIO` as-is;
   no rename or copy step is required. `scripts/sql/05_rename_legacy_registry.sql`
   is retained as a historical filename and contains no rename operation.
3. Run the reviewed DDL for the two new agent-owned tables and support objects in TOAD, or let the service
   create missing objects after the owner has approved the schema. Do not run this
   script from application code.
4. No DDL smoke test, database query, or database write was run as part of this
   implementation.

The application checks the `SCENARIO_KEY` column on the agent-owned production
table before proceeding. A schema-version mismatch is reported and requires
owner review; the application does not alter an existing schema-version row.

## Configuration and rollout

- `config/aml_catalogs/policy.yaml` → `workflow.enforcement`: `observe` records
  missing plan/result approvals as `would_block`; `enforce` rejects them.
- `workflow.conflict_check_required` defaults to `false`; P8 must set it to
  `true` when its conflict checker is ready. Until then, the receipt records
  `conflict.status: not_run`.
- Non-numeric authenticated creator IDs use the configured `AML_CREATED_BY`
  numeric value for `PIO_AML_AGENT_SCENARIOS.CREATED_BY`.

P5 must flip `workflow.enforcement` to `enforce` when approval actions are ready.
P8 must flip `workflow.conflict_check_required` to `true` when conflict checks
are authoritative.

## Offline verification

From the repository root, with the project environment configured:

```powershell
$env:PYTHONPATH = "services\aml_builder"
.\.venv\Scripts\python.exe -m compileall -q services app.py run_alert_engine.py
.\.venv\Scripts\python.exe -m pytest -q services/aml_builder/tests/test_p4_deploy_service.py services/aml_builder/tests/test_p4_oracle_repo.py services/aml_builder/tests/test_p4_runtime_shadow_hash.py
```

The P4 tests use fake repositories or patched read-only schema inspection. They
do not call Oracle, the LLM, or Service B.
