# Canonical Project Overview

> **Status:** Canonical repository-level technical overview, verified against the working-tree implementation on 2026-10-20.
>
> Read this after the applicable `AGENTS.md`. Use it to navigate and reason about the system, then inspect the current code, tests, schemas, and configuration relevant to the task. Those remain authoritative. Claims below are **Verified** unless explicitly marked **Inferred** or **Unclear**.

## 1. Project purpose and scope

This repository contains the Python backend for an anti-money-laundering (AML) scenario builder and on-demand alert executor. Compliance users describe detection scenarios in natural language. A FastAPI service runs a single LangGraph ReAct agent that extracts a structured intent, discovers relevant transaction explanation codes, renders a review plan, delegates Oracle SQL generation to an external service, shadow-tests that SQL, collects governance metadata, and can persist the result to Oracle.

The same service can query its persisted scenario registry and run saved scenario SQL to populate AML alert tables. It does **not** contain the frontend, the external text-to-SQL service, an automatic scheduler, or deployment infrastructure.

Primary implementation evidence: `services/aml_builder/web/api/main.py`, `services/aml_builder/web/services/graph.py`, `services/aml_builder/web/services/tools.py`, and `services/aml_builder/web/services/alert_engine.py`.

## 2. Architecture and responsibility boundaries

The application has four practical layers:

1. **HTTP interface:** FastAPI startup and routes under `services/aml_builder/web/api/` expose chat/SSE, workflow state and revision reads, typed approval actions, authenticated UI copy, session history and sidebar operations, direct deployment, alert-engine execution, and health.
2. **Agent orchestration:** `services/aml_builder/web/services/graph.py:get_tool_driven_graph` builds one process-level ReAct agent with six tools, an `AsyncSqliteSaver`, and a pre-model hook that adds current workflow context and stubs superseded tool results in model input. Tool selection remains LLM-driven; deterministic workflow state and guards are enforced per policy gate. P5 enforces approvals, P6 enforces parameter-card confirmation, P6b creates copied-forward revisions after a plan exists, P8 enforces conflict checks, and P9 adds typed version actions outside the LLM tool surface.
3. **Domain and integration services:** pure intent normalization, catalog-driven filter resolution, SQL filter verification, code binding, metadata rules, window conversion, SQL extraction, hashing, deterministic deployment, and version lifecycle rules live under `web/domain/`. Oracle registry-v2 reads and transactional version/status/audit writes live under `web/adapters/oracle_repo.py`; Oracle-backed metadata lookups, plan rendering, explanation-code search, registry analytics, external text-to-SQL calls, and alert processing remain under `web/services/`.
4. **Persistence:** SQLite stores LangGraph checkpoints in `STATE_DIR/checkpoints.sqlite`; workflow, revision, card-audit, workflow-event, and composite-key chat-sidebar data live in `STATE_DIR/app_state.sqlite`. Oracle stores source/lookups, scenario definitions and governance records, and generated alerts.

The service is synchronous at several integration boundaries despite async HTTP routes: Oracle helpers, OpenAI/LangChain invocations inside tools, the `httpx.Client` call to the text-to-SQL service, and alert-engine execution are synchronous. FastAPI engine routes invoke that work directly rather than enqueueing it.

## 3. Repository map

| Path | Responsibility |
|---|---|
| `services/aml_builder/web/api/main.py` | FastAPI app, required service-token startup check, configured CORS, router registration, `/health`. |
| `services/aml_builder/web/api/dependencies.py` | Validates BFF service-token/identity headers and rejects body/path ID mismatches. |
| `services/aml_builder/web/api/routes/me.py` | `GET /me/permissions` returns the caller's YAML policy role and allowed actions. |
| `services/aml_builder/web/api/routes/chat.py` | `POST /chat/stream`; converts graph messages and selected tool results to SSE events. Card-confirmation acknowledgements return a short SSE response without invoking LangGraph or writing a checkpoint. |
| `services/aml_builder/web/api/routes/workflow.py` | Own-chat workflow state, revision summaries/details, hash-bound approvals, P9 version creation/rollback/retirement/history, and authenticated UI-copy endpoint. |
| `services/aml_builder/web/api/routes/sessions.py` | Checkpoint-backed conversation-text reconstruction and sidebar list/rename/pin/soft-delete endpoints; current artifacts come from workflow state. |
| `services/aml_builder/web/api/routes/deploy.py` | Direct side-panel deployment from the current typed workflow revision through `DeployService`; returns the deployment receipt. |
| `services/aml_builder/web/api/codec.py` | Versioned tool-result envelope, per-tool payload models, and legacy checkpoint decoding. |
| `services/aml_builder/web/api/sse_mapper.py` | Maps decoded tool results to the existing UI-visible SSE events. |
| `services/aml_builder/web/api/routes/engine.py` | Synchronous alert-engine trigger and active-scenario listing. |
| `services/aml_builder/web/services/graph.py` | ReAct graph/checkpointer singleton lifecycle and six-tool registration. |
| `services/aml_builder/web/workflow/` | Typed workflow records, stage transitions, SQLite repositories, atomic card-confirmation copy-forward, revision history/eviction, stage guards, and transient model context. |
| `services/aml_builder/web/services/tools.py` | Agent-facing intent, plan, shadow-test, metadata, persistence, and registry-query tools. |
| `services/aml_builder/web/domain/intent_normalize.py` | Pure intent normalization with default provenance and `applied_defaults` entries. |
| `services/aml_builder/web/domain/filter_resolution.py` | Catalog-driven dimension resolution, advisor prompt section, and Service B filter payload assembly. |
| `services/aml_builder/web/domain/filter_applied_check.py` | SQLGlot-based comparison of generated SQL predicates with confirmed filter values. |
| `services/aml_builder/web/domain/code_binding.py` | Pure transaction-scope selection and explanation-code binding. |
| `services/aml_builder/web/domain/sql_extract.py` | Pure extraction of SQL from response text; `services/sql_extraction.py` remains a compatibility export. |
| `services/aml_builder/web/domain/metadata_rules.py` | Pure metadata seeding, normalization, and validation rules; Oracle lookups stay in the service adapter. |
| `services/aml_builder/web/domain/window.py` | Shared pure time-window conversion used by the tool and deploy route. |
| `services/aml_builder/web/domain/hashing.py` | Canonical SQL normalization and stable JSON SHA-256 hashing. |
| `services/aml_builder/web/domain/policy.py` | YAML-backed role lookup, action authorization, and own-chat read scope. |
| `services/aml_builder/web/domain/deploy_service.py` | Hard deployment guards, per-gate approval enforcement, shared deploy-blocker calculation, idempotent retry recovery, and receipt construction. |
| `services/aml_builder/web/adapters/oracle_repo.py` | Create-if-missing registry-v2 schema, version/audit reads, and transactional exact-status, paired metadata-flag, parent supersession, and audit writes. |
| `services/aml_builder/web/domain/versioning_service.py` | Deterministic create-version, rollback, retirement, registry seed validation, and stored-payload comparison rules. |
| `services/aml_builder/web/services/schemas.py` | `AMLIntent` domain contract plus chat and SSE Pydantic models. |
| `services/aml_builder/web/services/plan_renderer.py` | Deterministic Markdown plan renderer, including resolved filters. |
| `services/aml_builder/web/services/explanation_code_search.py` | Oracle-backed explanation-code embedding cache, similarity search, and LLM reranking. |
| `services/aml_builder/web/services/scenario_metadata.py` | Governance field registry, defaults, live lookup options, normalization, and validation. |
| `services/aml_builder/web/services/production_registry.py` | Read-only compatibility access; legacy in-place writes are disabled in favor of `DeployService`. |
| `services/aml_builder/web/services/scenario_registry_analytics.py` | Read-only scenario search, counts, detail, alert telemetry, version listing, and stored-JSON comparison. |
| `services/aml_builder/web/services/alert_engine.py` | Loads active scenarios, executes saved SQL, and writes alert headers/details. |
| `services/aml_builder/web/services/oracle.py` | Primary and shadow Oracle pools plus query/transaction helpers. |
| `services/aml_builder/web/services/session_store.py` | Composite-key `chat_sessions` table and sidebar CRUD in the app-state database. |
| `services/aml_builder/web/services/settings.py` | Pydantic environment configuration. |
| `services/aml_builder/web/services/config_loader.py` | Pydantic validation and mtime-based reload for the four AML YAML catalogs. |
| `config/aml_catalogs/` | Demo parameter, filter, policy, and UI-copy catalogs. |
| `scripts/archive_legacy.py` | Owner-run, SELECT-only Oracle export preview/extract and optional local sqlite move. |
| `scripts/sql/` | Owner-reviewed TOAD check, backup/export, exact-ID removal, and restore runbooks. |
| `Docs/P0_foundations.md` | P0 archive usage, verification commands, and settings reference. |
| `services/aml_builder/web/services/prompts/` | Runtime system prompts loaded relative to the package. |
| `run_dev.ps1` | Windows development launcher on port 8005. |
| `run_alert_engine.py` | CLI alert-engine entry point. |
| `app.py` | Root wrapper exporting the FastAPI `app`. |
| `services/aml_builder/tests/test_registry_analytics.py` | Manually executable live integration check, not an offline unit suite. |
| `services/aml_builder/tests/test_all_transaction_code_scope.py` | Offline regression test for all/any transaction-code discovery. |
| `services/aml_builder/tests/test_p1_intent_normalization_baseline.py` | Frozen legacy normalization fixtures and default provenance checks. |
| `services/aml_builder/tests/test_p1_codec_domain.py` | Offline codec, history, SSE, hashing, SQL extraction, metadata, and window tests. |
| `services/aml_builder/tests/test_config_loader.py` | Offline catalog validation, reload, and TBD status tests. |
| `services/aml_builder/tests/test_p0_settings_and_sse.py` | Offline settings, SSE, tracing configuration, LLM defaults, and fresh-state lifespan tests. |
| `services/aml_builder/tests/test_archive_legacy.py` | Fake-backed verification that archive queries are SELECT-only and ID-scoped. |
| `Docs/` | Specialized runbooks, historical design notes, proposals, and SQL references; currency varies. |

There is one dependency manifest, root `requirements.txt`. There is no current `services/aml_builder/requirements.txt`, migration framework, `pyproject.toml`, container/orchestrator manifest, or CI workflow in the inspected tree. Root `.env.example` documents the supported local settings without real credentials.

## 4. Entry points and HTTP surface

### Service startup

The recommended launcher is `run_dev.ps1`. It requires root `.env` and `.venv\Scripts\python.exe`, sets `PYTHONPATH` to `services\aml_builder`, creates `artifacts/`, loads `.env` into the process environment, and starts:

```powershell
.\run_dev.ps1
```

The equivalent repository-root command is:

```powershell
$env:PYTHONPATH = "services\aml_builder"
.\.venv\Scripts\python.exe -m uvicorn web.api.main:app --reload --port 8005 --host 0.0.0.0
```

`services.aml_builder.web.api.main:lifespan` exports configured LangSmith values to the process environment when absent, validates the AML catalogs, initializes logging and the SQLite session table, initializes primary/shadow Oracle pools, and warms the graph. Invalid catalogs fail startup with a clear path and validation error. Oracle and graph warm-up failures are logged but do not abort startup. Shutdown closes the SQLite checkpointer and both Oracle pools. The `/health` response is process health only; it does not probe Oracle, the LLM, or the external SQL service.

`app.py` also exposes the app for `uvicorn app:app` when the repository root is importable.

### Routes

| Method and path | Current behavior |
|---|---|
| `GET /health` | Returns static service/version health data. |
| `GET /me/permissions` | Requires BFF identity headers; returns policy role and allowed actions. |
| `POST /chat/stream` | Runs the ReAct graph and streams SSE. |
| `GET /workflow/{project_id}/{chat_id}/{user_id}/state` | Returns current stage, pending-card slot, revision summaries, `can_deploy` and typed blockers; `include_current_revision=true` includes the current payload. |
| `POST /workflow/{project_id}/{chat_id}/{user_id}/actions/approve_plan` | Approves the current plan only when `revision_id` and `expected_hash` match. Requires `scenario.create`. |
| `POST /workflow/{project_id}/{chat_id}/{user_id}/actions/approve_result` | Approves the successful shadow result only when `revision_id` and `expected_hash` match. Requires `scenario.create`. |
| `POST /workflow/{project_id}/{chat_id}/{user_id}/actions/confirm_params` | Validates explicit values/default choices for the exact pending card, atomically creates or updates the current revision, renders its plan, resolves the card audit, and returns revision/action details. Requires `scenario.create`. |
| `POST /workflow/{project_id}/{chat_id}/{user_id}/actions/discard_card` | Discards and audits the exact pending card. Requires `scenario.create`. |
| `POST /workflow/{project_id}/{chat_id}/{user_id}/actions/create_new_version` | Seeds an idempotent child chat from the latest eligible Oracle version row. Requires `scenario.revise`. |
| `POST /workflow/{project_id}/{chat_id}/{user_id}/actions/rollback_version` | Reactivates one superseded version and supersedes the expected active version atomically. Requires `scenario.rollback` and a reason. |
| `POST /workflow/{project_id}/{chat_id}/{user_id}/actions/retire_version` | Soft-retires one active version with paired metadata flag and audit updates. Requires `scenario.delete` and a reason. |
| `GET /workflow/{project_id}/{chat_id}/{user_id}/versions` | Lists all versions and audit summary from the latest version's locked chat. Requires `chat.read`. |
| `GET /ui/copy` | Returns the authenticated YAML UI-copy catalog. |
| `GET /workflow/{project_id}/{chat_id}/{user_id}/revisions` | Lists current, historical, and evicted revision summaries without payloads. |
| `GET /workflow/{project_id}/{chat_id}/{user_id}/revisions/{revision_id}` | Returns one full revision after checking chat ownership. |
| `GET /chat/{project_id}/{chat_id}/{user_id}/history` | Reconstructs user/assistant text from the graph checkpoint; artifacts are read from the current workflow revision. |
| `GET /chats/user/{user_id}/list` | Lists non-deleted sidebar sessions, optionally filtered by project query parameter. |
| `DELETE /chat/{project_id}/{chat_id}/{user_id}` | Soft-deletes sidebar metadata only. |
| `PUT /chat/{project_id}/{chat_id}/{user_id}/rename` | Renames sidebar metadata. |
| `PUT /chat/{project_id}/{chat_id}/{user_id}/pin` | Toggles sidebar pin state. |
| `POST /scenario/deploy` | Validates the current workflow revision and supplied metadata, writes registry v2 transactionally, and returns a receipt. |
| `POST /engine/run-scenarios` | Synchronously evaluates all active scenarios or one requested scenario. |
| `GET /engine/active-scenarios` | Lists active registry rows and governance fields. |

The chat stream emits `thinking`, `tool_call`, `content`, `stage_changed`, selected artifact/result events, `error`, and `done`. Raw tool messages are suppressed. The Service A `SSEEvent` schema contains only event types used by the route; `ChatRequest.reasoning_mode` remains accepted but is documented as ignored.

`ChatRequest.reasoning_mode` is accepted but not used to choose a model or graph configuration. The request may contain full message history, but the route sends only the latest message content to LangGraph; prior context comes from the checkpoint for the derived thread ID. If `chat_id` is omitted, the route generates one internally but does not emit that generated ID in a dedicated response event.

## 5. Core abstractions and domain rules

### `AMLIntent`

`services/aml_builder/web/services/schemas.py:AMLIntent` is the main handoff contract between intent extraction, plan rendering, and external SQL generation. It carries raw `filters` mentions and confirmed `resolved_filters` on the revision. Important nested models are `Threshold`, `TimeWindow`, `BaselineWindow`, `AggregationProfile`, `SemanticCondition`, `Clarification`, and `FilterMention`.

Rules implemented in code or runtime prompts include:

- Every threshold has `target_scope` of `DETAIL` or `AGGREGATE`, representing pre-aggregation versus aggregate conditions.
- For `CUSTOMER`/`ACCOUNT` scenarios with `SUM` or `COUNT`, `AMLIntent.validate_and_deduplicate_thresholds` removes a detail threshold whose value duplicates an aggregate threshold value. Function matching is case-sensitive.
- Percentage strings normalize to ratios and multiplier strings such as `<N>x` normalize to numbers; other strings may remain relational field names.
- Multiple transaction types and explanation codes can be represented globally and per threshold/semantic condition. `tools.py:_bind_channels_and_codes_to_intent` uses text matching and, when counts align, positional fallback to associate them.
- The intent prompt requires historical comparisons to use a non-overlapping `baseline_window`, although correctness still depends on LLM output and Service B honoring the contract.
- Plans are rendered deterministically by `plan_renderer.py:build_plan_markdown`; intent extraction also includes a plan artifact in its own result, while `generate_scenario_execution_plan` can re-render it.

### Explanation-code discovery

`explanation_code_search.py` reads `PIO_EXPLANATION_CODE` for configured country/institution values (including rows whose tenant columns are null), embeds the catalog with `OpenAIEmbeddings`, and caches normalized vectors both in-process and in `artifacts/explanation_codes_vector_cache_<country>_<institution>.pkl`. Discovery runs only for specific transaction types; `ANY`/`ALL` and unspecified types skip the search so they do not create an `EXPL_CODE` restriction. Query embeddings rank by cosine similarity using a 70% relative cutoff with an absolute floor; the fast chat model reranks candidates. If reranking fails, vector candidates are returned.

The cache has no TTL or source-data invalidation. Changing explanation-code rows without deleting/rebuilding the cache can therefore leave discovery stale.

### Filter resolution and SQL recheck

`config/aml_catalogs/filters.yaml` defines business dimensions and their physical targets. The enabled `explanation_code` dimension uses the existing explanation-code index and continues to populate the legacy `explanation_codes` fields. `account_status` and `customer_profession` remain loaded but disabled until their sources and target columns are confirmed. The extraction prompt receives dimension names and support status from the catalog; it does not receive column names.

`domain/filter_resolution.py` produces ordered candidate options and confidence values. Ambiguous, unresolved, lookup-failed, disabled, and unknown dimensions go through the existing typed parameter-card confirmation flow. Confirmed filter selections are stored with the revision intent and included in `params_hash`. The Service B request includes only resolved filters plus catalog-generated target/join instructions; Service B itself is unchanged.

After SQL extraction, `domain/filter_applied_check.py` parses Oracle SQL with the pinned `sqlglot` dependency and compares filter predicates and value sets. Missing or mismatched filters return `FILTER_NOT_APPLIED`, preserve `raw_sql`, and keep result approval blocked. Unextractable predicates follow `policy.yaml:filter_check.unverifiable` (`warn` by default, or `fail`). `ANY`/`ALL` transaction scope skips the explanation-code predicate check.

### Governance metadata

`scenario_metadata.py:SCENARIO_METADATA_FIELDS` defines the current application schema vocabulary. System/default values are seeded without overwriting existing non-null values. Lookup-backed values are read live from:

- `PIO_AML_SCENARIO_CATEGORY` (`CATEGORY_CODE`)
- `PIO_AML_DEGREE_RISK` (`DEGREE_RISK_FLAG`)
- `PIO_AML_SCENARIO_TYPE` (`SCE_TYPE_CODE`)
- `PIO_AML_SCENARIO_CLASSES` (`CLASS_CODE`)

`validate_scenario_metadata` fills only country, institution, creator, and updater when absent; normalizes friendly risk/violation/active values; requires all mandatory fields; and rejects invalid static or live lookup values. If a lookup query fails or returns no rows, validation fails closed for that value.

`build_metadata_catalog.ready_for_persistence` means only that mandatory fields are nonempty. It does not validate that current values occur in the returned lookup/static options; definitive validation happens later in `validate_scenario_metadata`.

`_map_period` exists but is unused. Current table creation and persistence do not store `PERIOD_TYPE`, `PERIOD_NUM`, or `VERSION_NUM`.

## 6. Key workflows

### Scenario authoring through chat

```text
POST /chat/stream
  -> normalize identifiers, upsert sidebar metadata, and get-or-create workflow row
  -> thread_id = "{project_id}_{chat_id}_{user_id}"
  -> graph.astream(..., recursion_limit=settings.AGENT_RECURSION_LIMIT)
  -> LLM chooses among six tools; the pre-model hook adds current stage/revision context
  -> intent analysis builds a catalog-driven parameter card; code validates required gaps
  -> typed confirmation applies explicit values and deterministically renders the plan
  -> tool wrapper records guard verdicts and successful stage progress
  -> route selected graph/tool messages into SSE events
  -> emit stage_changed after tool results
  -> persist conversation in checkpoints and workflow state in app_state.sqlite
```

P5 enforces typed plan/result approvals and P6 enforces card confirmation in `config/aml_catalogs/policy.yaml`. The conflict-check gate remains in `observe` for P8. Observe mode appends `would_block` workflow events for ordinary stage/stale-card verdicts and still allows the tool to continue; successful tools advance the stored stage. `CHAT_LOCKED_DEPLOYED` and `PERMISSION_DENIED` always block.

The intended prompt-governed workflow is:

```text
user description
  -> analyze_intent_and_discover_explanation_codes
  -> catalog-driven parameter card (pending card survives reload)
  -> typed confirm/discard; confirmed values are stored with provenance
  -> deterministic plan review and explicit user approval
  -> execute_oracle_dwh_shadow_test
  -> shadow-result approval
  -> prepare_scenario_metadata_for_persistence
  -> later explicit persistence instruction
  -> persist_and_validate_scenario_in_dwh
```

The server validates required parameters independently of the LLM's `ready_for_handoff` value. A pending card is emitted through `parameter_card` SSE and returned by `GET state`; each refused submission is recorded as a `card_refused` workflow event. `param_card_audit` receives one immutable row when a card resolves, with the shown rows, final choices, timestamps, and refusal-attempt count. Shadow and persistence tools use the confirmed intent stored on the current revision and log `intent_arg_mismatch` when their model-supplied argument differs.

P6 creates the initial revision. Once a plan exists, P6b confirms changed cards by copying the confirmed intent and newly rendered plan into a new current revision; the prior trio remains in history unchanged apart from its lifecycle fields. The operation atomically updates revision history, applies tombstone eviction, writes the card audit, clears the pending card, and advances the workflow pointer. Planless revisions are updated in place. Metadata is re-seeded and edited fields are revalidated before carry-over. P8 computes the canonical signature and conflict report from the confirmed revision, including P7's resolved filters. A blocking identical scenario leaves the parameter card pending; a warning creates a `PARAMS_CONFIRMED` revision without rendering its plan until `acknowledge_conflicts` records the user's choice. The acknowledgement is a SQLite workflow event and is written to the Oracle audit table only with deployment. The `[ACTION] card_confirmed revision N` chat acknowledgement bypasses LangGraph so it is not added to the checkpoint.

P7 includes resolved filters in the card-confirmed revision and its parameter hash. P8 builds each scenario signature from that revision's resolved filters. Its deterministic comparator checks only ACTIVE scenarios, excludes the same workflow thread and same `SCENARIO_KEY`, and uses interval, scope, and filter set relations. P9 excludes the version's parent from its own conflict gate, permits a same-signature metadata-only version, and blocks a child only when both signature and metadata hashes match the parent. Different scenario types are not compared; legacy similarity is informational only. `conflict_report` SSE events and `GET state` expose matches and deterministic field diffs. The panel shows stored version history from the latest locked chat.

### SQL generation and shadow testing

`tools.py:execute_oracle_dwh_shadow_test` sends an `AML_SCENARIO_GENERATION` intent payload to `PIOTECH_AI_URL` using HTTP SSE. It prefers a `final_answer` event and otherwise concatenates `content` events, then `sql_extraction.py:extract_sql` extracts the last plausible `SELECT`/`WITH` query.

The tool runs:

```sql
SELECT COUNT(*) FROM (<generated SQL>) shadow_query
```

through `run_shadow_readonly`. `header_alert_count` is the returned row count. `transaction_detail_count` is not measured; it is `header_alert_count * 4`, so `alert_density_ratio` is effectively 4.0 for every nonzero result. On execution failure, the tool still returns the generated `raw_sql` with `validation_success: false`.

Before the count query, the A-side filter check verifies each resolved dimension against its target column and value set. Failed checks skip the Oracle count query and return the SQL with per-filter status details.

### Scenario deployment (P4)

The route and optional persistence tool call `domain/deploy_service.py:DeployService`, which validates the typed current revision and commits through `adapters/oracle_repo.py:OracleRegistryRepo`:

```text
validate current revision, pending-card slot, permission, metadata and shadow SQL hash
  -> check plan/result approvals (hard in enforce mode; recorded as would_block in observe)
  -> for a child version, check the exact latest parent and supersede an active parent first
  -> insert registry version, metadata row and audit row in one Oracle transaction
  -> lock the workflow revision in SQLite
```

`ensure_schema()` creates only missing registry-v2 tables and indexes and inserts the schema-version row when absent. Development registry data uses `PIO_AML_AGENT_PRODUCTION_SCENARIOS` and `PIO_AML_AGENT_SCENARIOS`; the existing shared `PIO_AML_PRODUCTION_SCENARIOS` and `PIO_AML_SCENARIO` are left untouched. Status writes target the deployed parent version and matching agent metadata row by their exact keys.

P3 pre-allocates `PRD_<8_HEX>` stable keys and P4 stores the first version as `PRD_<8_HEX>_V1`. P9 creates a child chat from the deployed Oracle JSON snapshot; it does not copy local source workflow rows. The active parent is superseded before a same-signature child is inserted so the active-signature index remains usable. Retired parents stay retired. Rollback and retirement use exact expected-status updates and update the matching metadata `ACTIVE_FLAG` in the same audited Oracle transaction. A committed Oracle deployment followed by a failed SQLite lock update is recovered by retry: the existing row is looked up by unique `THREAD_ID`, and only the workflow lock is completed. Oracle and SQLite cannot share one transaction.

P5 enforces plan/result approvals and P6 enforces card confirmation. P6b keeps those gates enforced and adds bounded revision history. P8 rechecks the registry before deployment: new blocking conflicts refuse deployment, and warning matches require acknowledgement unless prior acknowledgements still cover the same-or-fewer matches. Registry unavailability blocks deployment when `conflict_check_required` and `conflict_check.on_unavailable: block` are set. The deployment receipt contains acknowledgements; each acknowledged match is written as an `OVERRIDE_CONFLICT` row in `PIO_AML_AGENT_SCENARIO_AUDIT` within the registry deployment transaction. P9 uses the same conflict rules before rollback, records rollback acknowledgements with the required reason, and keeps the alert engine limited to versions with both active flags set.

### Existing-scenario queries

`query_production_scenario_registry` dispatches six read-only modes:

- `semantic_search`: fetches current registry text and creates OpenAI embeddings on each request; no registry-search cache.
- `get_statistics`: inventory, risk/category breakdowns, and lifetime header-alert count.
- `get_alert_metrics`: header/date/customer telemetry from `PIO_AML_CUSTOMERS`, with per-scenario detail counts.
- `get_scenario_detail`: registry/governance data plus header-alert count for one ID.
- `list_versions`: all versions of the stable key for a scenario ID, newest first.
- `compare_versions`: deterministic path-based diff of stored signature, params, filters, and metadata JSON for two IDs from the same key.

Database operations use `run_readonly`, but semantic search still calls an external embedding API. Several analytics queries catch failures and return partial/empty results. Comments and historical changelog text say telemetry responses include an explanatory note; current return dictionaries do not include that note.

### Alert execution

```text
run_alert_engine.py or POST /engine/run-scenarios
  -> get_active_scenarios() from the production registry joined to governance
  -> execute each RAW_SQL through the shadow pool, falling back to primary
  -> group returned rows by CUS_NUM
  -> one atomic primary-Oracle transaction per scenario:
       insert PIO_AML_CUSTOMERS header per customer
       optionally insert PIO_AML_CUSTOMERS_DET rows
  -> return per-scenario and aggregate counts
```

Transaction detail insertion is enabled when output contains `TRA_SEQ1` **or** `TRA_DATE`. Other expected transaction columns receive defaults or nulls. Query output must include `CUS_NUM`; otherwise nothing is inserted. Historical execution performs case-sensitive text replacement of `TRUNC(SYSDATE)` and then `SYSDATE`, not SQL-aware rewriting.

The engine is on demand only. No cron, task scheduler, queue, worker, or automatic invocation exists in this repository. `run_alert_engine.py` accepts `--scenario-id` and `--date`; there is no `--dry-run`. The HTTP request field is `evaluation_date`.

When a specific `scenario_id` is requested, selection requires both `PIO_AML_AGENT_PRODUCTION_SCENARIOS.IS_ACTIVE = 1` and the governance `PIO_AML_AGENT_SCENARIOS.ACTIVE_FLAG` to be active, just like the all-scenarios path. Superseded and retired versions stop running while past alert rows remain.

## 7. Data ownership and persistence

### SQLite

`STATE_DIR` defaults to `artifacts/v2`. Unless explicitly overridden, `CHECKPOINT_DB_PATH` and `APP_STATE_DB_PATH` resolve to `STATE_DIR/checkpoints.sqlite` and `STATE_DIR/app_state.sqlite`. The checkpoint database is currently shared by:

- LangGraph checkpoint tables, which store conversation/agent state keyed by the composite thread string.
- `chat_sessions`, which stores sidebar title, update time, pin, and soft-delete state.

Soft deletion affects only `chat_sessions`; checkpoint messages remain. `chat_sessions.chat_id` alone is the primary key even though reads/mutations also use user and project. Reusing the same chat ID across users/projects can collide, and `upsert_chat_session` updates an existing row by `chat_id` without checking ownership.

### Oracle

- **Primary pool:** explanation/lookups, registry reads and analytics, scenario DDL/writes, and alert-table writes.
- **Shadow pool:** generated SQL tests and alert-engine scenario execution. If unconfigured or unavailable, reads fall back to the primary pool.
- **`PIO_AML_AGENT_PRODUCTION_SCENARIOS`:** development executable registry containing `RAW_SQL` and versioned scenario fields.
- **`PIO_AML_AGENT_SCENARIOS`:** development governance metadata keyed by `(COUNTRY_CODE, INST_CODE, SCENARIO_CODE)`.
- **`PIO_AML_CUSTOMERS` / `PIO_AML_CUSTOMERS_DET`:** pre-existing alert header/evidence tables written by the engine.
- **`PIO_EXPLANATION_CODE`:** source for explanation-code discovery.
- **Lookup tables:** category, risk, scenario type, and class values used during governance validation.

The application creates the two scenario tables if absent but assumes alert, explanation-code, and lookup tables already exist. SQL files under `Docs/database/` are reference/ad-hoc SQL, not ordered migrations.

## 8. External integrations

- **PioTech AI / Service B:** `PIOTECH_AI_URL`, default `http://localhost:8006/chat/stream`. This external SSE service owns schema-aware Oracle SQL generation. Its source and contract tests are absent here.
- **OpenAI-compatible chat models:** `llm_client.py:build_llm` supports `openai` or `lmstudio` through `ChatOpenAI`. The main graph, intent extraction, and explanation-code reranking use it.
- **OpenAI embeddings:** explanation-code discovery and registry semantic search instantiate `OpenAIEmbeddings` directly. They do not use `LLM_PROVIDER`/`LLM_BASE_URL`, so selecting LM Studio does not remove the OpenAI embedding dependency.
- **Oracle:** `python-oracledb` thin pools provide all domain data and durable production writes.

## 9. Configuration and runtime behavior

`settings.py:Settings` reads environment variables and root-relative `.env`, is case-insensitive, and ignores extra keys. Do not commit secret values.

Required at settings construction:

- `ORACLE_DSN`
- `ORACLE_USER`
- `ORACLE_PASSWORD`

Operationally important optional groups:

- Service/logging/auth: `SERVICE_NAME`, `SERVICE_PORT`, `DEBUG`, `SERVICE_AUTH_TOKEN`, `CORS_ALLOWED_ORIGINS`
- Chat model: `LLM_PROVIDER`, `LLM_BASE_URL`, `LLM_MODEL`, `LLM_MODEL_FAST`, `OPENAI_API_KEY`, `LLM_TEMPERATURE`, `LLM_REASONING_EFFORT`
- Oracle pools: `ORACLE_POOL_MIN`, `ORACLE_POOL_MAX`, and `SHADOW_ORACLE_DSN`, `SHADOW_ORACLE_USER`, `SHADOW_ORACLE_PASSWORD`, `SHADOW_ORACLE_POOL_MIN`, `SHADOW_ORACLE_POOL_MAX`
- Blocking I/O and type lookup: `IO_WORKERS` (default 4, bounded executor), `RETRIEVAL_CONCURRENCY` (default 3)
- AML defaults: `AML_COUNTRY_CODE`, `AML_INST_CODE`, `AML_CREATED_BY`
- Service B: `PIOTECH_AI_URL`, `PIOTECH_AI_TIMEOUT_SECONDS`, `PIOTECH_AI_USER_ID`, `PIOTECH_AI_PROJECT_ID`
- State: `STATE_DIR`, `CHECKPOINT_DB_PATH`, `APP_STATE_DB_PATH`, `CATALOG_DIR`, `AGENT_RECURSION_LIMIT`
- Tracing declarations: `LANGCHAIN_TRACING_V2`, `LANGCHAIN_API_KEY`, `LANGCHAIN_PROJECT`

Important caveats:

- `OPENAI_API_KEY` is optional to Pydantic but required by OpenAI chat/embedding operations.
- `run_dev.ps1` hardcodes port 8005 rather than consuming `SERVICE_PORT`.
- Service A startup requires a non-empty `SERVICE_AUTH_TOKEN` shared with the UI BFF; `CORS_ALLOWED_ORIGINS` defaults to `http://localhost:3000` and accepts comma-separated origins.
- Environment variables take precedence over `.env`; the generic `DEBUG` name can collide with ambient tooling variables. Any non-boolean value (the verification environment supplied `DEBUG=release`) prevents settings construction and therefore application/test import.
- LangSmith values loaded from `.env` through `BaseSettings` are copied to the process environment at startup under both `LANGCHAIN_*` and `LANGSMITH_*` names when the corresponding variables are absent, so LangChain's tracing integration can read them. Keys are not logged.
- `LLM_MAX_TOKENS` is optional and only passed to the model when set; reasoning models use `max_completion_tokens`. `AGENT_RECURSION_LIMIT` (default 25) is authoritative for chat. `MAX_AGENT_ITERATIONS` is retained as a reserved overlapping setting, and `MAX_VALIDATION_RETRIES` remains configurable but unused because no validation retry path exists.
- Blocking tool, workflow, sidebar, Oracle, and engine work is dispatched through a dedicated bounded `IO_WORKERS` executor. Service B uses an async HTTP SSE client. SQLite adapters open their own connections per operation; the executor does not share connections between threads.
- Explanation-code searches for separate transaction types run concurrently up to `RETRIEVAL_CONCURRENCY`, preserve input order, and isolate a failed type. Their vector cache uses row count plus an available update timestamp, or a checksum fallback; a failed key lookup rebuilds the vectors.
- Startup validates all four YAML catalogs and fails with the catalog path and validation details when a catalog is invalid. The filter loader allows `TBD` only for dimensions with `status: needs_advisor_input`.

## 10. Authentication, authorization, and trust boundaries

The UI middleware requires a valid `pio_session` JWT for every `/api/*` route except `/api/auth/login` and `/api/auth/register`. UI BFF handlers verify the JWT again, use `sub` as the canonical user ID, and forward `X-User-Id`, `X-User-Email`, the fixed `X-Project-Id`, and `X-Service-Token`. The shared service token is server-side only.

Service A requires the configured `SERVICE_AUTH_TOKEN` during startup. Its chat stream, session routes, deploy route, and `/me/permissions` require the matching token header plus user and project identity headers. They return HTTP 403 for invalid/missing service identity or body/path IDs that disagree with the headers. CORS origins come from comma-separated `CORS_ALLOWED_ORIGINS`, defaulting to `http://localhost:3000`.

`domain/policy.py` reads roles and user mappings only from `policy.yaml`; JWT database-role labels do not grant access. A user map key can be a JWT subject or email. Unknown users default to `manager` in `dev` mode and `viewer` in `jwt` mode. `authorize()` checks the five configured actions and keeps `chat.read` scoped to the caller when `cross_user_chat_read` is false. Chat creation tools and direct deployment enforce `scenario.create`; the registry tool enforces `chat.read`. Rename, pin, and soft-hide of a caller's own chat use `chat.read`, not `scenario.delete`.

Remaining boundary: `/health` and the standalone `/engine/*` routes are not part of the P2 BFF workflow dependency set and do not require `require_identity`. The shared token is a service-to-service trust boundary; callers able to obtain it can assert identity headers, so it must remain secret and Service A should only be reachable from trusted network paths.

## 11. Testing and verification model

The repository has offline pytest checks plus one manually executable live integration check at `services/aml_builder/tests/test_registry_analytics.py`. That script defines `run_all_checks()` and a `__main__` runner; it requires live Oracle, persisted tables/data, and OpenAI for semantic search. Do not run it as routine verification. Its manual invocation from the repository root is:

```powershell
$env:PYTHONPATH = "services\aml_builder"
.\.venv\Scripts\python.exe services\aml_builder\tests\test_registry_analytics.py
```

Offline tests now cover transaction-code scope, P0 catalog/settings/SSE/archive contracts, and related pure behavior. The registry analytics script remains a live integration check and is not collected as a pytest test.

Safe structural checks include the following. The explicit `DEBUG` override avoids unrelated ambient-variable collisions during verification and lasts only for the current PowerShell process:

```powershell
$env:DEBUG = "false"
.\.venv\Scripts\python.exe -m compileall -q services app.py run_alert_engine.py scripts\archive_legacy.py
.\.venv\Scripts\python.exe -m pytest -q --basetemp artifacts\p0-pytest
```

The `DEBUG` override avoids unrelated ambient-variable collisions and applies only to the current PowerShell process. The pytest suite does not call live Oracle, the LLM, or Service B.

Do not run the integration script, start the app lifespan, invoke agent tools, or trigger the engine merely as a routine test: those paths can call paid APIs, query live systems, create Oracle tables, or write production data.

## 12. Build, run, and deployment model

Install the pinned root dependencies with:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

There is no separate build step or package artifact. Uvicorn imports the app from source. API docs are available at `http://localhost:8005/docs` after successful startup. Runtime directories `artifacts/` and `logs/` are generated and ignored by Git.

No production deployment topology is encoded in this repository. The docs' references to Docker/load balancers, cron/task schedulers, AIDB/AMLDB, or a frontend are external assumptions unless confirmed in the target environment.

## 13. Conventions and invariants to preserve

- Read applicable `AGENTS.md` and this overview before project-wide work; verify task-specific behavior in code.
- Keep one canonical `AMLIntent` shape across schemas, the extraction prompt, plan rendering, tool payloads, and Service B expectations.
- Preserve the distinction between detail (`WHERE`) and aggregate (`HAVING`) thresholds.
- Do not bypass explicit human review stages casually. If approval must become enforceable, add server-side state rather than relying more heavily on prompt wording.
- Scenario persistence must keep the production and governance writes in one `atomic_connection` transaction.
- Preserve primary-versus-shadow pool intent and explicitly assess the configured fallback to primary before running generated SQL.
- Treat governance lookup tables as the validation authority; do not invent codes when lookup evidence is unavailable.
- Alert SQL must return `CUS_NUM`; it must return the transaction evidence fields expected by `populate_scenario_alerts` if detail rows are required.
- Session identity changes must update both the composite LangGraph thread-key behavior and the separate SQLite sidebar schema/queries.
- Runtime prompts are executable behavior. Changes to prompt contracts must remain consistent with schemas, tools, and the external handoff.
- Update this overview in the same change when the project's useful system-level mental model changes.

## 14. Important implementation decisions supported by evidence

- A single ReAct graph replaced a former multi-node graph; `Docs/02_module_layout_refactor.md` records that history, while current `graph.py` confirms the resulting design.
- Plan rendering is deterministic Python rather than another LLM call (`plan_renderer.py`).
- Registry semantic search intentionally embeds live rows per request with no cache (`scenario_registry_analytics.py`); explanation-code search makes the opposite tradeoff and persists a local vector cache.
- Scenario data is split between executable SQL (`PIO_AML_AGENT_PRODUCTION_SCENARIOS`) and governance metadata (`PIO_AML_AGENT_SCENARIOS`) and written atomically.
- Alert execution is a standalone/on-demand path so scenario authoring does not itself populate alert tables.

No rationale should be inferred beyond these code- and history-supported decisions.

## 15. Risks and technical debt

Confirmed or directly evidenced concerns:

- **Approval and shadow success are not enforced server-side.** Prompt rules can be bypassed, and direct deploy can recover `raw_sql` from a failed shadow-test payload.
- **No local authentication/authorization.** Sensitive endpoints trust caller-supplied identities.
- **Tenant scoping is incomplete.** Several joins use only scenario ID/code; lookup fetching retries without country/institution filters when tenant-filtered lookup returns no rows.
- **Sidebar session key collision.** `chat_sessions` uses `chat_id` alone as its primary key and upsert lookup.
- **Alert sequence concurrency.** `_get_next_seq` uses `MAX(SEQ)+1`; concurrent runs can select the same value despite the module's PK-safety claim.
- **Engine reruns are not idempotent.** Re-executing a scenario/date creates additional headers/details with new sequence values; no deduplication/run ledger is present.
- **Generated SQL safety is shallow.** Read-only helpers check only the leading token, and historical date substitution is string-based.
- **Shadow metrics are partially synthetic.** Detail counts use a fixed 4x heuristic and the reported ratio is not population density.
- **Schema drift risk.** Tables are created ad hoc only when absent; existing schemas are not migrated or validated for compatible columns.
- **Bounded I/O worker saturation.** Blocking adapters and synchronous tools use the dedicated bounded executor, so sustained slow Oracle or state-store calls can exhaust its `IO_WORKERS` capacity and queue work.
- **Explanation cache staleness.** The persistent cache is not invalidated when Oracle catalog content changes.
- **Partial failure signaling.** Some registry and initialization functions log and return empty/false results, making infrastructure failure resemble absence of data.
- **Metadata readiness is weaker than validity.** The catalog's `ready_for_persistence` checks presence only; lookup/static validation can still fail at persistence.
- **Telemetry filtering is inconsistent.** Per-scenario header counts honor caller filters, but the corresponding detail count query uses only scenario ID and can report lifetime details beside date/customer-filtered headers.
- **Request-contract fields are partly inert.** `reasoning_mode` does not affect execution, supplied prior messages are ignored in favor of checkpoint state, and a server-generated chat ID is not explicitly returned.
- **Documentation and code comments overstate behavior.** Examples include an automatic/daily runner, telemetry notes that are not returned, period/version metadata, and safe sequence generation.
- **Minimal automated testing.** Critical workflows depend on live infrastructure and lack offline coverage.
- **Generic environment-variable collision.** Ambient `DEBUG` values that are not parseable booleans prevent all imports that construct `Settings`.

## 16. Known uncertainties

Repository evidence cannot establish:

- the deployed Oracle schemas, constraints, grants, indexes, lookup contents, or whether application DDL matches them;
- whether scenario IDs are globally unique across tenants and external writers;
- the exact Service B SSE contract, SQL quality guarantees, output columns, or its authentication and safety controls;
- whether an external gateway enforces authentication/authorization and restricts CORS;
- whether an external scheduler invokes the CLI/API in production;
- the frontend's exact handling of every SSE artifact and approval interaction;
- real performance, locking, duplicate behavior, and transaction constraints under concurrent/live workloads;
- whether external consumers require historical fields such as `PERIOD_TYPE`, `PERIOD_NUM`, `VERSION_NUM`, or `INTENT_JSON`.

Resolving these requires live credentials/infrastructure, Service B/frontend source or contracts, deployed DDL, and business-owner confirmation.

## 17. Documentation status

Use this file for current project-wide understanding. Preserve narrower docs for their specialized value, but verify them against code:

- `Docs/04_standalone_alert_engine.md` remains the closest subsystem runbook; current code is authoritative for fields and invocation.
- `Docs/shadowtesting_layer.md` is explicitly a deferred sampling proposal, not current behavior.
- `Docs/01_architecture.md`, `Docs/02_module_layout_refactor.md`, and `Docs/03_scenario_metadata_persistence.md` are useful history but contain superseded tool counts, paths, schemas, and workflow claims.
- `Docs/PIO_AML_SCENARIO_schema_guide.md` mixes business design/proposal material with schema names that conflict with current persistence code.
- `Docs/SYSTEM_ARCHITECTURE_OVERVIEW.md` is broad but stale in several paths, database/schema fields, and deployment assumptions.
- `README.md` is useful for orientation but its `.env.example`, nested dependency path, `--dry-run`, `target_date`, and some file links are stale.
- `Docs/known_issues.md` describes a different PioTech AI/DWH repository and is not this project's active issue register.
- `Docs/database/` contains references and ad-hoc SQL, not migrations; inspect for destructive statements before use.

## 18. Mental model for future changes

Think of the system as two connected but distinct lifecycles:

1. **Author and register:** chat metadata selects a checkpoint thread; the ReAct model uses six tools to turn prose into `AMLIntent`, plan and code selections; Service B owns SQL generation; this service counts results, validates governance values, and atomically stores executable SQL plus governance metadata.
2. **Execute and observe:** the on-demand engine reads active stored SQL, runs it through the shadow/read path, and writes grouped alerts to primary Oracle; registry analytics reads the scenario and alert tables for user questions.

When changing behavior, locate which contract crosses the boundary: HTTP/SSE, `AMLIntent`, prompt/tool JSON, Service B SSE/SQL, Oracle schema, or alert-query output. Follow that contract end to end, update every producer and consumer, preserve approval and transaction boundaries, add or adapt tests, and then update this overview if the system-level mental model changed.
