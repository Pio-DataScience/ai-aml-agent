# P0 Foundations and Legacy Archive

## What and why

P0 establishes the Service A base for later workflow work: state paths live under
`STATE_DIR`, the four demo YAML catalogs are validated at startup and reloaded
after file changes, and optional LangSmith/model settings keep provider defaults
when unset. The legacy archive helper reads only legacy `PRD_` scenarios and
matching shared metadata. TOAD SQL files provide owner-reviewed checks, backups,
exports, removal, and restore steps.

`PIO_AML_SCENARIO` is shared data. The runbooks scope its rows to exact
`SCENARIO_CODE` values found in `PIO_AML_PRODUCTION_SCENARIOS` with the legacy
`PRD_` prefix. The archive Python script issues SELECT statements only.

## Commands

Run the default read-only preview from the repository root:

```powershell
.\.venv\Scripts\python.exe scripts\archive_legacy.py
```

It prints counts, matching legacy IDs, shared metadata row counts, alert row
counts, local sqlite files found, and the proposed `old_data/<timestamp>` path.
It does not create the archive directory.

The owner may write JSON/CSV extracts after reviewing the preview:

```powershell
.\.venv\Scripts\python.exe scripts\archive_legacy.py --export
```

The owner may move the legacy checkpoint/sidebar SQLite file and any `-wal` or
`-shm` sidecars separately:

```powershell
.\.venv\Scripts\python.exe scripts\archive_legacy.py --move-local-state
```

For a legacy database at a non-default path, pass it explicitly:

```powershell
.\.venv\Scripts\python.exe scripts\archive_legacy.py --move-local-state --sqlite-path path\to\legacy.sqlite
```

`--export` and `--move-local-state` are owner-run options. The implementation
does not run either option as part of application startup or tests. The SQL
runbooks are opened and executed by the owner in TOAD in order:

1. `scripts/sql/01_check.sql` — set `OWNER`, review all counts, foreign keys,
   triggers, alert references, and IDs outside the legacy prefix.
2. `scripts/sql/02_archive.sql` — replace `YYYYMMDD` consistently, create the
   filtered backup copies, check their row counts, and export each SELECT result
   to CSV using TOAD. If step 1 reports additional FK child tables, add their
   explicit backup and CSV SELECT statements here before proceeding.
3. `scripts/sql/03_remove.sql` — review each affected-row count. It includes no
   COMMIT; the owner decides to commit or roll back in TOAD. Add exact-ID
   statements for each additional child table identified by step 1 before
   removing parent rows.
4. `scripts/sql/04_restore.sql` — if needed, insert missing rows from the backup
   tables. It includes no COMMIT.

## Owner-run legacy cleanup record (2026-10-17)

The owner reported removal of seven duplicate copies, leaving 17 original
legacy scenarios. The owner-provided `_duplicate_removal.txt` also contains a
`ROLLBACK TO before_approved_duplicate_cleanup` and no `COMMIT`; if that rollback
was executed after the duplicate DELETEs, it would undo them. The owner's later
report of 17 remaining scenarios and the following 17-ID archive script are
consistent with the duplicates having been removed, but that SQL file alone
does not establish the transaction history.

The `_NIDAL_20261017_BKP.txt` record contains CTAS backups
`PIO_AML_PROD_NIDAL_20261017_BKP` and
`PIO_AML_SCEN_NIDAL_20261017_BKP`, filtered to the 17 surviving IDs, followed
by DELETE statements for those IDs from both registry tables and a final
`COMMIT`.

The attachments contain SQL but not TOAD result output. The owner reported that
17 production rows remained after duplicate removal and that the duplicate
check returned no rows; backup and final deletion counts are not captured in the
files. The runbook records this as owner-reported completion, not as an
independent live verification.

To restore the backed-up rows, run `scripts/sql/04_restore.sql` in TOAD while
connected to the same schema. It checks backup and target counts, inserts only
missing production keys and governance composite keys, and has no `COMMIT`.
Review the final counts before committing; issue `ROLLBACK` in that session if
the results are unexpected. The CTAS backup tables persist after a rollback.

## Registry-v2 legacy table rename (P4)

If Service A reports `REGISTRY_LEGACY_SCHEMA`, stop deployment and have the
owner review the existing table and backup. The owner-only TOAD rename statement
is in [`scripts/sql/05_rename_legacy_registry.sql`](../scripts/sql/05_rename_legacy_registry.sql).
After review, the owner renames the old production table so the app can create
the v2 table. The application never runs that rename or alters the legacy table.

Run local verification from the repository root:

```powershell
$env:DEBUG = "false"
.\.venv\Scripts\python.exe -m compileall -q services app.py run_alert_engine.py scripts\archive_legacy.py
.\.venv\Scripts\python.exe -m pytest -q --basetemp artifacts\p0-pytest
```

Tests use temporary files and fakes; they do not call Oracle, LLM, Service B,
`--export`, `--move-local-state`, or any SQL runbook.

## Configuration

- `STATE_DIR` defaults to `artifacts/v2`.
- `CHECKPOINT_DB_PATH` and `APP_STATE_DB_PATH` default to
  `STATE_DIR/checkpoints.sqlite` and `STATE_DIR/app_state.sqlite`; set either
  explicitly to override it.
- `CATALOG_DIR` defaults to `config/aml_catalogs`.
- `AGENT_RECURSION_LIMIT` defaults to 25 and is authoritative for chat requests.
  `MAX_AGENT_ITERATIONS` remains configurable but reserved because it overlaps.
- `MAX_VALIDATION_RETRIES` remains configurable; P0 has no validation-retry
  path to wire it to.
- `LLM_MAX_TOKENS` is unset by default, leaving token limits to the provider. If
  configured, the LLM client uses `max_completion_tokens` for detected reasoning
  models and `max_tokens` for other models.
- `LANGCHAIN_TRACING_V2`, `LANGCHAIN_API_KEY`, and `LANGCHAIN_PROJECT` remain
  configurable. Startup exports values loaded from `.env` to the process
  environment as both legacy `LANGCHAIN_*` and current `LANGSMITH_*` names, only
  when the corresponding process variable is not already set. Values and API
  keys are not logged.
- `Settings` ignores unrelated `.env` keys (`extra="ignore"`).
- A filter entry containing `TBD` must have
  `status: needs_advisor_input`; enabled `confirmed` entries with `TBD` fail
  catalog validation.
