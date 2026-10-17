# Refactor Status

## P1 — Typed envelope, codec, domain extraction

- **Branch:** `refactor/p1-typed-envelope-domain`
- **Status:** Implemented; no next package started.
- **Baseline commit:** `2c45cb1 test: freeze legacy intent normalization outputs`.
- **Files changed:**
  - `CHANGELOG.MD`
  - `PROJECT_OVERVIEW.md`
  - `Docs/P1_typed_envelope.md`
  - `Docs/refactor_status.md`
  - `services/aml_builder/web/api/codec.py`
  - `services/aml_builder/web/api/sse_mapper.py`
  - `services/aml_builder/web/api/routes/chat.py`
  - `services/aml_builder/web/api/routes/deploy.py`
  - `services/aml_builder/web/api/routes/sessions.py`
  - `services/aml_builder/web/domain/__init__.py`
  - `services/aml_builder/web/domain/code_binding.py`
  - `services/aml_builder/web/domain/hashing.py`
  - `services/aml_builder/web/domain/intent_normalize.py`
  - `services/aml_builder/web/domain/metadata_rules.py`
  - `services/aml_builder/web/domain/sql_extract.py`
  - `services/aml_builder/web/domain/window.py`
  - `services/aml_builder/web/services/_intent_normalization_baseline.py`
  - `services/aml_builder/web/services/prompts/tool_driven_system.md`
  - `services/aml_builder/web/services/scenario_metadata.py`
  - `services/aml_builder/web/services/schemas.py`
  - `services/aml_builder/web/services/sql_extraction.py`
  - `services/aml_builder/web/services/tools.py`
  - `services/aml_builder/tests/fixtures/p1_intent_normalization_golden.json`
  - `services/aml_builder/tests/test_all_transaction_code_scope.py`
  - `services/aml_builder/tests/test_p1_codec_domain.py`
  - `services/aml_builder/tests/test_p1_intent_normalization_baseline.py`
- **Tests added:** frozen outputs for ten legacy intent samples; tool-to-domain-helper delegation; default provenance and `applied_defaults`; codec round trip and legacy decode; error status/code behavior; failed-shadow `scenario_result` SSE snapshot; plan/catalog SSE snapshots; synthetic history reconstruction; unchanged tool names; SQL extraction compatibility; SQL and JSON hashing; both legacy window conversion fixtures; pure metadata seeding and validation.
- **Deviations:**
  - Added `field_provenance` to `AMLIntent` for scalar fields that do not have a nested provenance model; nested defaults carry `provenance="default"`. `applied_defaults` remains `list[str]`.
  - Kept the temporary characterization helper after the baseline commit because repository global rules prohibit deleting files. It is no longer used by runtime tool code; the final domain normalizer is covered against its frozen outputs.
  - Kept `services/sql_extraction.py` as the approved compatibility export. `plan_renderer.py` remains in place and is called only by the plan tool.
  - The original tools.py and deploy.py window conversions were identical for the checked fixtures; one shared `time_window_days()` now serves both.
- **Verification:** `python -m compileall -q services app.py run_alert_engine.py` completed successfully. Full `pytest -q` passed **38 tests**. Black check passed for the new P1 modules and tests; flake8 passed with existing `E501` baseline findings and Black's `W503` wrapping ignored. The isolated synthetic history test required execution outside the Windows sandbox and passed. No live Oracle, LLM, or Service B call was made.

## P0 — Foundations, archive, config loader, spikes

- **Branch:** `refactor/p0-foundations-archive`
- **Status:** Implemented; no next package started.
- **Files changed:**
  - `.gitignore`
  - `.env.example`
  - `CHANGELOG.MD`
  - `PROJECT_OVERVIEW.md`
  - `Docs/refactor_status.md`
  - `Docs/P0_foundations.md`
  - `Docs/spikes.md`
  - `config/aml_catalogs/params.yaml`
  - `config/aml_catalogs/filters.yaml`
  - `config/aml_catalogs/policy.yaml`
  - `config/aml_catalogs/ui_copy.yaml`
  - `requirements.txt` (pinned `sqlglot==27.28.1` from S2)
  - `scripts/archive_legacy.py`
  - `scripts/sql/01_check.sql`
  - `scripts/sql/02_archive.sql`
  - `scripts/sql/03_remove.sql`
  - `scripts/sql/04_restore.sql`
  - `services/aml_builder/web/api/main.py`
  - `services/aml_builder/web/api/routes/chat.py`
  - `services/aml_builder/web/services/config_loader.py`
  - `services/aml_builder/web/services/llm_client.py`
  - `services/aml_builder/web/services/schemas.py`
  - `services/aml_builder/web/services/settings.py`
  - `services/aml_builder/tests/test_archive_legacy.py`
  - `services/aml_builder/tests/test_config_loader.py`
  - `services/aml_builder/tests/test_p0_settings_and_sse.py`
- **Tests added:** catalog valid/invalid/reload and TBD policy; repository catalog draft validation; state path overrides; unknown `.env` key handling; SSE event contract; LangSmith environment propagation; optional token parameter behavior; fresh-state catalog/checkpoint initialization and mocked FastAPI lifespan; and SELECT-only archive queries with exact matching IDs.
- **Deviations and notes:**
  - The repository uses `Docs/`, so spike and status notes are placed there.
  - `AGENT_RECURSION_LIMIT` is authoritative for the chat route. The retained
    `MAX_AGENT_ITERATIONS` setting is reserved because it overlaps; it is not
    wired to a second recursion control. `MAX_VALIDATION_RETRIES` remains
    configurable and unused because P0 has no validation-retry path.
  - `LLM_MAX_TOKENS` now defaults to `None`, preserving the current provider
    default because the former setting was not wired. Explicit values use
    `max_tokens`, or `max_completion_tokens` for detected reasoning models.
  - The TOAD runbooks contain owner-run DELETE statements limited to the legacy
    `PRD_` IDs and include no COMMIT. No SQL runbook, Oracle mutation, export, or
    local-state move was run.
  - The archive helper contains only Oracle SELECTs. `--export` and
    `--move-local-state` are owner-run options; the script has no database DML or
    table DDL path.
  - No live Oracle schema inspection was run. `01_check.sql` reports FK child
    tables; if it finds additional children beyond the alert tables covered by
    the runbooks, the owner must add explicit backup/removal/restore statements
    for those rows before running step 03.
  - `Docs/refactor_plan.md` was already modified when work began and was left
    untouched.
- **Verification:** S1–S3 findings are in [`spikes.md`](spikes.md).
  `compileall` completed successfully. The offline suite passed 17 tests with
  the Windows asyncio lifespan test deselected in the sandbox; that isolated
  mocked lifespan test passed separately outside the sandbox (1 passed).
  The archive preview/export/move commands were not run. The owner later ran
  the TOAD cleanup and archive statements described below.

## P0 — Owner-run legacy scenario cleanup (2026-10-17)

- **Scope:** the 17 surviving legacy `PRD_` scenarios in
  `PIO_AML_PRODUCTION_SCENARIOS` and the 17 corresponding rows in the shared
  `PIO_AML_SCENARIO` table. Unrelated governance rows were excluded by exact
  `SCENARIO_CODE` predicates.
- **Duplicate cleanup:** the owner reported that 17 production rows remained
  and the duplicate-copy check returned no rows after removing
  `PRD_27EA510A`, `PRD_DACB57AC`, `PRD_7F023437`, `PRD_955117A3`,
  `PRD_801DE76B`, `PRD_6C6D9BE0`, and `PRD_C080E225`. The supplied
  `_duplicate_removal.txt` also contains `ROLLBACK TO before_approved_duplicate_cleanup`
  and no `COMMIT`; if that rollback was executed after those DELETEs, it would
  undo them. The later owner report and 17-ID archive script are consistent
  with the duplicate rows having already been removed, but the SQL attachment
  alone does not establish that transaction history.
- **Backups:** the owner-provided `_NIDAL_20261017_BKP.txt` records CTAS backups
  `PIO_AML_PROD_NIDAL_20261017_BKP` and `PIO_AML_SCEN_NIDAL_20261017_BKP`,
  filtered to these 17 surviving IDs:
  `PRD_18CA9F22`, `PRD_22BE26EE`, `PRD_246EAD89`, `PRD_2EED1B4F`,
  `PRD_44A735E1`, `PRD_4D8BB716`, `PRD_642A5342`, `PRD_7B448EEB`,
  `PRD_7FA61FC0`, `PRD_828FDFC2`, `PRD_8A60099E`, `PRD_AD309CEA`,
  `PRD_B0885999`, `PRD_C5DD0964`, `PRD_E237976B`, `PRD_EE7BDB18`, and
  `PRD_FC94CB78`. The same file records deletion of those scoped rows from
  both source tables and a final `COMMIT`.
- **Evidence limit:** the attached SQL files contain statements but not TOAD
  result spools. They do not independently show CTAS row counts, DELETE row
  counts, post-delete counts, or the unrelated-governance-row count. Completion
  is recorded as owner-reported; no new live database query was run for this
  status update.
- **Recovery:** [`scripts/sql/04_restore.sql`](../scripts/sql/04_restore.sql) now
  uses the actual backup table names, verifies backup and target counts, inserts
  only missing keys, and contains no COMMIT. Run it in TOAD using the same
  schema/session; review both counts and then commit manually, or roll back if
  the results are unexpected. CTAS backup tables remain after a rollback.
- **Files changed for this record:** `Docs/refactor_status.md`,
  `Docs/P0_foundations.md`, `CHANGELOG.MD`, and `scripts/sql/04_restore.sql`.
