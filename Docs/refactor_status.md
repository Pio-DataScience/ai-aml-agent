# Refactor Status

## P6 — Parameter catalog, gap detection, and confirmation card

- **Branch:** Service A and UI `refactor/p6-param-catalog-confirm-card`.
- **Status:** Implemented; no next package started.
- **Enforcement:** `approvals: enforce`, `card_confirmation: enforce`, `conflict_check: observe`. P6b keeps approvals and card confirmation enforced while replacing the interim reset with new-revision history. P8 must flip only `conflict_check` to `enforce` and set `workflow.conflict_check_required: true` after implementing the authoritative conflict gate.
- **Workflow:** Intent extraction creates a catalog-driven card. Code computes required gaps regardless of the LLM `ready_for_handoff` flag. Confirm/discard are typed actions requiring `scenario.create`; a confirmed card is applied with provenance and deterministically renders the plan. Non-UI calls must submit explicit values or explicitly accept defaults. Incomplete and invalid submissions return distinct 422 codes and append `card_refused` workflow events.
- **Audit:** `card_shown` records rows and `card_refused` records the missing/unresolved list in workflow events. `param_card_audit` is inserted once, only on resolution (`confirmed`, `auto_confirmed`, `discarded`, `superseded`), and includes the shown rows, choices, `shown_at`, `resolved_at`, and refusal-attempt count. The audit row is never updated.
- **Revision behavior:** P6 creates only the initial revision. If a card is confirmed after a plan exists, P6 updates that current revision in place and clears plan, shadow, approvals, metadata, signatures, and conflict data. This is interim until P6b implements copy-forward revisions, history, and eviction.
- **Files changed:** Service A: `CHANGELOG.MD`, `Docs/P6_parameter_card.md`, `Docs/refactor_status.md`, `PROJECT_OVERVIEW.md`, `config/aml_catalogs/params.yaml`, `config/aml_catalogs/policy.yaml`, `services/aml_builder/tests/test_config_loader.py`, `services/aml_builder/tests/test_p3_workflow_guards.py`, `services/aml_builder/tests/test_p5_workflow_actions.py`, `services/aml_builder/tests/test_p6_param_cards.py`, `services/aml_builder/tests/test_p6_param_gaps.py`, `services/aml_builder/web/api/codec.py`, `services/aml_builder/web/api/routes/workflow.py`, `services/aml_builder/web/api/sse_mapper.py`, `services/aml_builder/web/domain/param_gaps.py`, `services/aml_builder/web/services/config_loader.py`, `services/aml_builder/web/services/plan_renderer.py`, `services/aml_builder/web/services/prompts/tool_driven_system.md`, `services/aml_builder/web/services/schemas.py`, `services/aml_builder/web/services/tools.py`, `services/aml_builder/web/workflow/cards.py`, `services/aml_builder/web/workflow/repo.py`, `services/aml_builder/web/workflow/runtime.py`. UI: `src/GlassChat.module.css`, `src/app/chat/[sessionId]/page.tsx`, `src/components/ScenarioGovernanceTabs.tsx`, `src/hooks/useChatStream.ts`, `src/state/WorkflowContext.tsx`, `src/types/agentEvents.ts`.
- **Tests added:** catalog schema validation and enforcement policy; required-gap detection despite the model-ready flag; safe structured `required_if`; indexed threshold rows and read-only typed structures; provenance and `applied_defaults`; range/all-time rejection; card audit only on resolution with refusal attempts snapshotted; auto-confirm; fake-LLM ready flag with missing window; confirmed-intent authority and mismatch events for shadow and persist; latest-intent merge and changed-row marking; current-revision interim reset; durable pending card; and P5 full-flow coverage with the card confirmation step. Updated P3 fixtures to retain their intended complete-intent cases under the enforced card gate.
- **Deviations:** P6 creates only the initial revision. Confirming after a plan exists resets that revision in place and clears the plan, shadow, approvals, metadata, signature, and conflict data until P6b adds copy-forward revision history. No P6b history picker or revision-copy flow was started. No live LLM, Oracle, or Service B call was made.
- **Verification:** Service A `compileall` passed. Focused P6/P5/config/P3 regression command passed **57 tests**. UI `npm run lint` passed with existing warnings; `npm run build` completed successfully with existing warnings. `git diff --check` passed in both repositories. No live Oracle, LLM, or Service B call was made.

## P5 — UI server state and approvals

- **Branch:** Service A `refactor/p5-ui-server-state-approvals`; UI `refactor/p5-ui-server-state-approvals`. The Service A branch is based on the merged P4 commit; the UI branch was already present and clean, and P4 introduced no UI file changes.
- **Status:** Implemented; no next package started.
- **Enforcement:** Per gate in `config/aml_catalogs/policy.yaml`: `approvals: enforce`, `card_confirmation: observe`, `conflict_check: observe`. P6 must flip only `card_confirmation` to `enforce`; P8 must flip only `conflict_check` to `enforce`. Hard safety guards remain always-on. Successful intent extraction advances the current revision to `PLAN_PENDING` while card confirmation remains observed.
- **Files changed:** Service A: `CHANGELOG.MD`, `Docs/P5_ui_server_state_approvals.md`, `PROJECT_OVERVIEW.md`, `config/aml_catalogs/policy.yaml`, `docs/refactor_status.md`, `services/aml_builder/tests/test_config_loader.py`, `services/aml_builder/tests/test_p3_workflow_guards.py`, `services/aml_builder/tests/test_p4_deploy_route.py`, `services/aml_builder/tests/test_p5_history_artifacts.py`, `services/aml_builder/tests/test_p5_workflow_actions.py`, `services/aml_builder/web/api/routes/sessions.py`, `services/aml_builder/web/api/routes/workflow.py`, `services/aml_builder/web/domain/deploy_service.py`, `services/aml_builder/web/services/config_loader.py`, `services/aml_builder/web/services/graph.py`, `services/aml_builder/web/services/tools.py`, `services/aml_builder/web/workflow/guards.py`, `services/aml_builder/web/workflow/repo.py`, `services/aml_builder/web/workflow/runtime.py`. UI: `CHANGELOG.md`, `src/app/api/chat/deploy/route.ts`, `src/app/api/ui/copy/route.ts`, `src/app/api/workflow/[...segments]/route.ts`, `src/app/chat/[sessionId]/page.tsx`, `src/components/ScenarioGovernanceTabs.tsx`, `src/components/metadata/ScenarioMetadataPanel.tsx`, `src/hooks/useChatStream.ts`, `src/state/WorkflowContext.tsx`, `src/types/agentEvents.ts`.
- **Tests added:** fake-backed plan/result approval hash mismatch, stale revision, permission denial, same-hash idempotency and audit row; state payload and deploy blockers; real-guard intent → plan → Proceed → shadow → result approval → metadata → deploy flow under the three-gate policy; ordinary `approved` chat text does not approve; old ToolMessage artifacts are excluded from history. Existing config/deploy/guard tests were updated for per-gate enforcement.
- **Deviations and findings:** The page’s clear handler is used only immediately before navigation to a new UUID chat; there is no same-ID reset path. The sidebar’s New chat action already generates a fresh ID. Locked chats expose a New chat action and keep Create new version disabled. The action message is sent only after the typed action returns updated server state. The UI uses built-in English fallbacks if `/ui/copy` is unavailable.
- **Verification:** Service A `compileall` passed; the focused Service A P5/config/P3/P4 regression suite passed **50 tests** using fake repositories and offline fixtures. UI `npm run lint` passed with existing warnings; `npm run build` passed. `git diff --check` passed in both repositories. No live Oracle, LLM, or Service B call was made.
- **Manual UI checks:** See `Docs/P5_ui_server_state_approvals.md`; the same checklist is ready to paste into the PR description.

## P4 — Registry v2 and DeployService

- **Branch:** `refactor/p4-registry-v2-deploy-service` (already present at the P3 baseline when this work started).
- **Status:** Implemented; P5 is recorded above.
- **Files changed:** `CHANGELOG.MD`, `Docs/P0_foundations.md`, `Docs/P4_registry_v2.md`, `Docs/refactor_plan.md`, `Docs/refactor_status.md`, `PROJECT_OVERVIEW.md`, `config/aml_catalogs/policy.yaml`, `scripts/registry_v2.sql`, `scripts/sql/05_rename_legacy_registry.sql`, `services/aml_builder/tests/test_p4_deploy_route.py`, `services/aml_builder/tests/test_p4_deploy_service.py`, `services/aml_builder/tests/test_p4_oracle_repo.py`, `services/aml_builder/tests/test_p4_runtime_shadow_hash.py`, `services/aml_builder/web/adapters/__init__.py`, `services/aml_builder/web/adapters/oracle_repo.py`, `services/aml_builder/web/api/routes/chat.py`, `services/aml_builder/web/api/routes/deploy.py`, `services/aml_builder/web/domain/deploy_service.py`, `services/aml_builder/web/services/alert_engine.py`, `services/aml_builder/web/services/config_loader.py`, `services/aml_builder/web/services/graph.py`, `services/aml_builder/web/services/production_registry.py`, `services/aml_builder/web/services/scenario_registry_analytics.py`, `services/aml_builder/web/services/tools.py`, `services/aml_builder/web/services/prompts/tool_driven_system.md`, `services/aml_builder/web/workflow/repo.py`, `services/aml_builder/web/workflow/runtime.py`.
- **Tests added:** Fake-backed deployment guards for permission, locked chat, pending card, wrong stage, failed shadow, SQL hash mismatch, invalid metadata, and enforce/observe approval behavior; idempotent retry recovery after SQLite lock failure; fake parent supersession and rollback/audit; typed duplicate signature error; conflict-gate policy; scenario ID format; ignored legacy shared tables and create-only agent schema; typed incompatible agent-schema error; nullable signature index; forbidden SQL scan across new adapter/deploy modules; production and metadata alert-engine column compatibility; direct route receipt/delegation; shadow-result SQL hash capture.
- **Deviations:** Parent supersession is implemented and tested in the service/repository contract but is not wired to a UI action (P9). `revision_id` is accepted by `/scenario/deploy` but remains optional for existing callers; if omitted, the route snapshots the current typed revision. Existing shared scenario tables remain unchanged; the app uses agent-prefixed registry, metadata, audit, and schema-version tables. No database query, DDL, or write was executed.
- **Owner-run steps:** Owner must review `scripts/registry_v2.sql` in TOAD and approve creation of `PIO_AML_AGENT_PRODUCTION_SCENARIOS` and `PIO_AML_AGENT_SCENARIOS`. No rename is required; existing shared tables remain untouched. The application will not rename or alter it.
- **Next package switches:** P6 enabled `workflow.enforcement.card_confirmation`. P6b replaces P6's interim in-place reset with revision history. P8 enables `workflow.enforcement.conflict_check` and `workflow.conflict_check_required` after the authoritative conflict check is implemented.
- **Verification:** `compileall` passed; the four offline P4 test files pass (**23 tests**). They use fake repositories and patched schema inspection only. No live Oracle, LLM, Service B call, DDL, or non-SELECT query was run.

## P3 — Workflow state, revisions, and guards

- **Branch:** `refactor/p3-workflow-state-guards`
- **Status:** Implemented; no next package started.
- **Enforcement at P3:** workflow gates initially ran in `observe`; P5 now enables approval enforcement only, with card-confirmation and conflict-check enforcement reserved for P6 and P8.
- **Files changed:** `CHANGELOG.MD`, `PROJECT_OVERVIEW.md`, `Docs/P3_workflow_state.md`, `Docs/revisions_api.md`, `Docs/refactor_status.md`, `config/aml_catalogs/policy.yaml`, `services/aml_builder/tests/test_all_transaction_code_scope.py`, `services/aml_builder/tests/test_config_loader.py`, `services/aml_builder/tests/test_p0_settings_and_sse.py`, `services/aml_builder/tests/test_p1_codec_domain.py`, `services/aml_builder/tests/test_p1_intent_normalization_baseline.py`, `services/aml_builder/tests/test_p3_session_store.py`, `services/aml_builder/tests/test_p3_workflow_api.py`, `services/aml_builder/tests/test_p3_workflow_context.py`, `services/aml_builder/tests/test_p3_workflow_guards.py`, `services/aml_builder/tests/test_p3_workflow_machine.py`, `services/aml_builder/tests/test_p3_workflow_repo.py`, `services/aml_builder/tests/test_p3_workflow_sse.py`, `services/aml_builder/tests/test_p3_workflow_tool_results.py`, `services/aml_builder/web/api/codec.py`, `services/aml_builder/web/api/main.py`, `services/aml_builder/web/api/routes/chat.py`, `services/aml_builder/web/api/routes/sessions.py`, `services/aml_builder/web/api/routes/workflow.py`, `services/aml_builder/web/api/sse_mapper.py`, `services/aml_builder/web/services/config_loader.py`, `services/aml_builder/web/services/graph.py`, `services/aml_builder/web/services/schemas.py`, `services/aml_builder/web/services/session_store.py`, `services/aml_builder/web/workflow/__init__.py`, `services/aml_builder/web/workflow/context.py`, `services/aml_builder/web/workflow/guards.py`, `services/aml_builder/web/workflow/machine.py`, `services/aml_builder/web/workflow/models.py`, `services/aml_builder/web/workflow/repo.py`, `services/aml_builder/web/workflow/revisions.py`, `services/aml_builder/web/workflow/runtime.py`.
- **Tests added:** workflow transitions, SQLite schema and WAL/busy-timeout/version marker, revision/history/eviction/tombstones, policy guards in observe and enforce modes, hard safety guards, API identity/ownership (including 403), state/revision summaries, projected history context, tool revision IDs and stage progress, SSE stage events, and composite session identity. Existing P0/P1 fixtures were updated to include trusted identity where the protected routes/tools now require it.
- **Deviations:** No legacy `chat_sessions` rows are copied. The chat route creates the workflow row and allocates scenario identifiers; analyze intent creates revision 1 on first success. Revision lifecycle/history is implemented, but card-triggered revisions remain unwired for P6/P6b. The observe default keeps existing tool flows available while emitting `would_block`; `CHAT_LOCKED_DEPLOYED` and `PERMISSION_DENIED` always block.
- **Verification:** Full offline suite passed **111 tests** before the final hook assertion correction; the P3/config/fresh-state focused suite passed **52 tests** afterward. Compileall, Black checks for new P3 files, flake8 for changed/new Python files, and `git diff --check` passed. A second full-suite run stalled in the Windows fresh-state test; no live Oracle, LLM, or Service B calls were made.

## P2 — Identity and mock roles

- **Branches:** Service A `refactor/p2-identity-roles`; UI `refactor/master` (the existing UI branch was retained).
- **Status:** Implemented; no next package started.
- **Files changed:**
  - Service A: `.env.example`, `CHANGELOG.MD`, `PROJECT_OVERVIEW.md`, `Docs/P2_identity_roles.md`, `Docs/refactor_status.md`, `config/aml_catalogs/policy.yaml`, `services/aml_builder/tests/test_p0_settings_and_sse.py`, `services/aml_builder/tests/test_p2_identity_policy.py`, `services/aml_builder/web/api/codec.py`, `services/aml_builder/web/api/dependencies.py`, `services/aml_builder/web/api/main.py`, `services/aml_builder/web/api/routes/chat.py`, `services/aml_builder/web/api/routes/deploy.py`, `services/aml_builder/web/api/routes/me.py`, `services/aml_builder/web/api/routes/sessions.py`, `services/aml_builder/web/domain/policy.py`, `services/aml_builder/web/services/config_loader.py`, `services/aml_builder/web/services/settings.py`, `services/aml_builder/web/services/tools.py`.
  - UI: `.env.example`, `CHANGELOG.md`, `middleware.ts`, `package.json`, `package-lock.json`, `src/app/api/auth/login/route.ts`, `src/app/api/auth/register/route.ts`, `src/app/api/chat/[sessionId]/pin/route.ts`, `src/app/api/chat/[sessionId]/rename/route.ts`, `src/app/api/chat/[sessionId]/route.ts`, `src/app/api/chat/deploy/route.ts`, `src/app/api/chat/history/route.ts`, `src/app/api/chat/route.ts`, `src/app/api/chat/status/route.ts`, `src/app/api/chats/list/route.ts`, `src/app/api/me/permissions/route.ts`, `src/app/chat/[sessionId]/page.tsx`, `src/app/providers.tsx`, `src/components/ChatBubble.tsx`, `src/components/MarkdownRenderer.tsx`, `src/components/ScenarioGovernanceTabs.tsx`, `src/components/Sidebar.tsx`, `src/components/metadata/ScenarioMetadataPanel.tsx`, `src/hooks/useChatStream.ts`, `src/lib/db.ts`, `src/lib/serviceIdentity.ts`, `src/state/PermissionsContext.tsx`, `src/types/chat.ts`.
- **Tests added:** `test_p2_identity_policy.py` covers policy role/action matrix, dev/jwt defaults, ID/email mappings, cross-user denial, trusted-header checks, forged/missing token rejection, body/path mismatch rejection, and viewer tool denial. Updated P0 settings tests cover required service token, CORS default, and mode-based policy defaults. No UI test runner was present; manual role checks are documented in `Docs/P2_identity_roles.md`.
- **Deviations and notes:**
  - Applied the user's clarifications to the section 6 draft: `default_role` is mode-specific (`dev: manager`, `jwt: viewer`); dev still requires a valid UI login; no test emails were added; JWT `sub` is the canonical user ID; policy roles ignore database/JWT role labels; deploy uses `scenario.create`; own-chat rename/pin/soft-hide use `chat.read`; project ID is fixed to `default`; and viewer checks are enforced in Service A tools.
  - Added `eslint-plugin-react-hooks` as a UI dev dependency because `npm run lint` could not resolve the configured plugin. The production build also exposed optional artifact callback data types in `ChatBubble`; non-null assertions were added at the already guarded render sites.
  - CORS defaults to `http://localhost:3000`; Service A refuses startup without `SERVICE_AUTH_TOKEN`. Both examples contain the server-side token and CORS variables. No Service B files or live Oracle/LLM/Service B calls were used.
- **Verification:** Service A `compileall` passed; P2 tests passed **28**; related P0 settings/SSE tests passed **9** with the Windows-sensitive fresh-state cases deselected. UI `npm run lint` passed with existing warnings; `npm run build` passed with existing warnings. `git diff --check` passed in both repos.

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
