# Canonical Project Overview

> **Status:** Canonical repository-level technical overview, verified against the working-tree implementation on 2026-10-10.
>
> Read this after the applicable `AGENTS.md`. Use it to navigate and reason about the system, then inspect the current code, tests, schemas, and configuration relevant to the task. Those remain authoritative. Claims below are **Verified** unless explicitly marked **Inferred** or **Unclear**.

## 1. Project purpose and scope

This repository contains the Python backend for an anti-money-laundering (AML) scenario builder and on-demand alert executor. Compliance users describe detection scenarios in natural language. A FastAPI service runs a single LangGraph ReAct agent that extracts a structured intent, discovers relevant transaction explanation codes, renders a review plan, delegates Oracle SQL generation to an external service, shadow-tests that SQL, collects governance metadata, and can persist the result to Oracle.

The same service can query its persisted scenario registry and run saved scenario SQL to populate AML alert tables. It does **not** contain the frontend, the external text-to-SQL service, an automatic scheduler, or deployment infrastructure.

Primary implementation evidence: `services/aml_builder/web/api/main.py`, `services/aml_builder/web/services/graph.py`, `services/aml_builder/web/services/tools.py`, and `services/aml_builder/web/services/alert_engine.py`.

## 2. Architecture and responsibility boundaries

The application has four practical layers:

1. **HTTP interface:** FastAPI startup and routes under `services/aml_builder/web/api/` expose chat/SSE, session history and sidebar operations, direct deployment, alert-engine execution, and health.
2. **Agent orchestration:** `services/aml_builder/web/services/graph.py:get_tool_driven_graph` builds one process-level ReAct agent with six tools and an `AsyncSqliteSaver`. There is no deterministic multi-node workflow in Python. Tool order and approval pauses are directed by `prompts/tool_driven_system.md` and tool descriptions.
3. **Domain and integration services:** intent schemas, deterministic plan rendering, explanation-code search, metadata validation, registry analytics, SQL extraction, external text-to-SQL calls, and alert processing live under `web/services/`.
4. **Persistence:** SQLite stores LangGraph checkpoints and chat-sidebar metadata; Oracle stores source/lookups, scenario definitions and governance records, and generated alerts.

The service is synchronous at several integration boundaries despite async HTTP routes: Oracle helpers, OpenAI/LangChain invocations inside tools, the `httpx.Client` call to the text-to-SQL service, and alert-engine execution are synchronous. FastAPI engine routes invoke that work directly rather than enqueueing it.

## 3. Repository map

| Path | Responsibility |
|---|---|
| `services/aml_builder/web/api/main.py` | FastAPI app, lifespan, CORS, router registration, `/health`. |
| `services/aml_builder/web/api/routes/chat.py` | `POST /chat/stream`; converts graph messages and selected tool results to SSE events. |
| `services/aml_builder/web/api/routes/sessions.py` | Checkpoint-backed history reconstruction and sidebar list/rename/pin/soft-delete endpoints. |
| `services/aml_builder/web/api/routes/deploy.py` | Direct side-panel deployment by recovering SQL/intent from checkpoint messages. |
| `services/aml_builder/web/api/routes/engine.py` | Synchronous alert-engine trigger and active-scenario listing. |
| `services/aml_builder/web/services/graph.py` | ReAct graph/checkpointer singleton lifecycle and six-tool registration. |
| `services/aml_builder/web/services/tools.py` | Agent-facing intent, plan, shadow-test, metadata, persistence, and registry-query tools. |
| `services/aml_builder/web/services/schemas.py` | `AMLIntent` domain contract plus chat and SSE Pydantic models. |
| `services/aml_builder/web/services/plan_renderer.py` | Deterministic 12-section Markdown plan renderer, including semantic-contract review. |
| `services/aml_builder/web/services/explanation_code_search.py` | Oracle-backed explanation-code embedding cache, similarity search, and LLM reranking. |
| `services/aml_builder/web/services/scenario_metadata.py` | Governance field registry, defaults, live lookup options, normalization, and validation. |
| `services/aml_builder/web/services/deployment_guard.py` | Reconstructs checkpoint evidence, binds approval to an immutable intent/SQL/metadata artifact, and prevents concurrent/replayed deployment. |
| `services/aml_builder/web/services/production_registry.py` | Table initialization and atomic dual-table scenario writer. |
| `services/aml_builder/web/services/scenario_registry_analytics.py` | Read-only scenario search, counts, detail, and alert telemetry. |
| `services/aml_builder/web/services/alert_engine.py` | Loads active scenarios, executes saved SQL, and writes alert headers/details. |
| `services/aml_builder/web/services/oracle.py` | Primary and shadow Oracle pools plus query/transaction helpers. |
| `services/aml_builder/web/services/session_store.py` | SQLite `chat_sessions` table and sidebar CRUD. |
| `services/aml_builder/web/services/settings.py` | Pydantic environment configuration. |
| `services/aml_builder/web/services/prompts/` | Runtime system prompts loaded relative to the package. |
| `run_dev.ps1` | Windows development launcher on port 8005. |
| `run_alert_engine.py` | CLI alert-engine entry point. |
| `app.py` | Root wrapper exporting the FastAPI `app`. |
| `services/aml_builder/tests/test_registry_analytics.py` | Manually executable live integration check, not an offline unit suite. |
| `Docs/` | Specialized runbooks, historical design notes, proposals, and SQL references; currency varies. |

There is one dependency manifest, root `requirements.txt`. There is no current `services/aml_builder/requirements.txt`, `.env.example`, migration framework, `pyproject.toml`, container/orchestrator manifest, or CI workflow in the inspected tree.

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

`services.aml_builder.web.api.main:lifespan` initializes logging, the SQLite session table, primary/shadow Oracle pools, and the graph. Oracle and graph warm-up failures are logged but do not abort startup. Shutdown closes the SQLite checkpointer and both Oracle pools. The `/health` response is process health only; it does not probe Oracle, the LLM, or the external SQL service.

`app.py` also exposes the app for `uvicorn app:app` when the repository root is importable.

### Routes

| Method and path | Current behavior |
|---|---|
| `GET /health` | Returns static service/version health data. |
| `POST /chat/stream` | Runs the ReAct graph and streams SSE. |
| `GET /chat/{project_id}/{chat_id}/{user_id}/history` | Reconstructs user/assistant turns and artifacts from the graph checkpoint. |
| `GET /chats/user/{user_id}/list` | Lists non-deleted sidebar sessions, optionally filtered by project query parameter. |
| `DELETE /chat/{project_id}/{chat_id}/{user_id}` | Soft-deletes sidebar metadata only. |
| `PUT /chat/{project_id}/{chat_id}/{user_id}/rename` | Renames sidebar metadata. |
| `PUT /chat/{project_id}/{chat_id}/{user_id}/pin` | Toggles sidebar pin state. |
| `POST /scenario/deploy` | Validates supplied governance metadata, recovers SQL from checkpoint state, then dual-writes the scenario. |
| `POST /engine/run-scenarios` | Synchronously evaluates all active scenarios or one requested scenario. |
| `GET /engine/active-scenarios` | Lists active registry rows and governance fields. |

The chat stream emits `thinking`, `tool_call`, `content`, selected artifact/result events, `error`, and `done`. Raw tool messages are suppressed. `SSEEvent` declares `final_answer` and `escalation_report`, but the current chat route does not explicitly emit those types.

`ChatRequest.reasoning_mode` is accepted but not used to choose a model or graph configuration. The request may contain full message history, but the route sends only the latest message content to LangGraph; prior context comes from the checkpoint for the derived thread ID. If `chat_id` is omitted, the route generates one internally but does not emit that generated ID in a dedicated response event.

## 5. Core abstractions and domain rules

### `AMLIntent`

`services/aml_builder/web/services/schemas.py:AMLIntent` is the main handoff contract between intent extraction, plan rendering, and external SQL generation. Legacy fields remain supported. New intents can also carry the optional versioned `semantic_contract`, whose typed models represent evaluation/output grain, distinct populations and metrics, filter applicability and evaluation phase, precise time boundaries, entity relationships, output evidence, ambiguity markers, and unsupported requirements.

Rules implemented in code or runtime prompts include:

- Every threshold has `target_scope` of `DETAIL` or `AGGREGATE`. These are business scopes (record/entity observation versus grouped/computed metric), not mandatory mappings to SQL `WHERE` or `HAVING`; Service B owns implementation placement.
- For legacy `CUSTOMER`/`ACCOUNT` intents with `SUM` or `COUNT`, `AMLIntent.validate_and_deduplicate_thresholds` retains the historical heuristic that removes a detail threshold whose value duplicates an aggregate threshold value. Explicit semantic-contract intents bypass that lossy heuristic because equal values may target different populations/phases.
- Percentage strings normalize to ratios and multiplier strings such as `<N>x` normalize to numbers; other strings may remain relational field names.
- Multiple transaction types and explanation codes can be represented globally, per threshold/semantic condition, and per semantic population/predicate. `tools.py:_bind_channels_and_codes_to_intent` propagates discovered codes only into explicitly matching transaction-type scopes.
- The semantic contract separates numerator and denominator populations, global versus population/metric filters, and required evidence. Ratio/percentage metrics require explicit population references and a zero-denominator policy.
- Time semantics represent window purpose/type, anchor, offset, inclusivity, business precision, and timezone where applicable without prescribing Oracle expressions.
- Before typed validation, `AMLIntent` canonicalizes only known business-equivalent extractor aliases (for example `user_input` provenance, singular time units, `CURRENT` observation purpose, and `PER CUSTOMER PER WINDOW` grain text). Unknown values remain invalid, so this compatibility layer cannot silently change scenario semantics.
- Blocking semantic ambiguities or unsupported requirements force `ready_for_handoff=false`; the existing extraction LLM produces these markers and no additional extraction agent/call is used.
- Plans are rendered deterministically by `plan_renderer.py:build_plan_markdown`; intent extraction also includes a plan artifact in its own result, while `generate_scenario_execution_plan` can re-render it.

### Explanation-code discovery

`explanation_code_search.py` reads `PIO_EXPLANATION_CODE` for configured country/institution values (including rows whose tenant columns are null), embeds the catalog with `OpenAIEmbeddings`, and caches normalized vectors both in-process and in `artifacts/explanation_codes_vector_cache_<country>_<institution>.pkl`. Query embeddings rank by cosine similarity using a 70% relative cutoff with an absolute floor; the fast chat model reranks candidates. If reranking fails, vector candidates are returned.

The cache has no TTL or source-data invalidation. Changing explanation-code rows without deleting/rebuilding the cache can therefore leave discovery stale.

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
  -> normalize identifiers and upsert sidebar metadata
  -> thread_id = "{project_id}_{chat_id}_{user_id}"
  -> graph.astream(..., recursion_limit=10)
  -> LLM chooses among six tools under prompt rules
  -> route selected graph/tool messages into SSE events
  -> persist conversation state in SQLite checkpoints
```

The intended prompt-governed workflow is:

```text
user description
  -> analyze_intent_and_discover_explanation_codes
  -> deterministic plan review and explicit user approval
  -> execute_oracle_dwh_shadow_test
  -> shadow-result approval
  -> prepare_scenario_metadata_for_persistence
  -> later explicit persistence instruction
  -> persist_and_validate_scenario_in_dwh
```

Plan and shadow-result review remain conversational gates, but production persistence is enforced server-side. The deployment guard requires the latest shadow result to have `validation_success: true`, rejects intent/SQL/metadata drift, requires a later explicit chat persistence instruction for the agent route, and binds the approved values to a SHA-256 artifact identity before the shared writer can run.

### SQL generation and shadow testing

`tools.py:execute_oracle_dwh_shadow_test` sends an `AML_SCENARIO_GENERATION` intent payload to `PIOTECH_AI_URL` using HTTP SSE. It prefers a `final_answer` event and otherwise concatenates `content` events, then `sql_extraction.py:extract_sql` extracts the last plausible `SELECT`/`WITH` query.

The tool runs:

```sql
SELECT COUNT(*) FROM (<generated SQL>) shadow_query
```

through `run_shadow_readonly`. `header_alert_count` is the returned row count. `transaction_detail_count` is not measured; it is `header_alert_count * 4`, so `alert_density_ratio` is effectively 4.0 for every nonzero result. On execution failure, the tool still returns the generated `raw_sql` with `validation_success: false`.

### Scenario persistence

Both the agent persistence tool and the direct deploy route call `production_registry.py:save_production_scenario`:

```text
recover the exact composite-thread checkpoint
  -> require latest successful shadow-test evidence
  -> reject later intent revisions or caller-supplied SQL drift
  -> validate current governance metadata
  -> require explicit chat approval or direct deploy action
  -> atomically claim the immutable artifact in SQLite
  -> initialize registry tables if absent
  -> acquire one primary Oracle connection
  -> upsert PIO_AML_PRODUCTION_SCENARIOS
  -> upsert PIO_AML_SCENARIO
  -> commit both and mark the claim deployed, or release a pre-commit failure for retry
```

The application creates missing tables directly with DDL; there is no versioned migration system. Every normal persistence call generates a new random `PRD_<8_HEX>` ID, so the update branches are rarely reached through current public paths.

`POST /scenario/deploy` uses only the exact `{project_id}_{session_id}_{user_id}` checkpoint. The POST action is the direct approval signal, but SQL and intent always come from the latest successful shadow-test tool exchange; the request cannot supply or override them. Missing, failed, stale, or malformed evidence returns an actionable fail-closed error. The agent tool route additionally requires an explicit later human message containing a deployment action and an unchanged metadata-review snapshot.

### Existing-scenario queries

`query_production_scenario_registry` dispatches one of four read-only modes:

- `semantic_search`: fetches current registry text and creates OpenAI embeddings on each request; no registry-search cache.
- `get_statistics`: inventory, risk/category breakdowns, and lifetime header-alert count.
- `get_alert_metrics`: header/date/customer telemetry from `PIO_AML_CUSTOMERS`, with per-scenario detail counts.
- `get_scenario_detail`: registry/governance data plus header-alert count for one ID.

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

When a specific `scenario_id` is requested, selection still requires `PIO_AML_PRODUCTION_SCENARIOS.IS_ACTIVE = 1` but does not apply the governance `PIO_AML_SCENARIO.ACTIVE_FLAG` filter used for the all-scenarios path.

## 7. Data ownership and persistence

### SQLite

`CHECKPOINT_DB_PATH` defaults to `./artifacts/checkpoints.sqlite` and is shared by:

- LangGraph checkpoint tables, which store conversation/agent state keyed by the composite thread string.
- `chat_sessions`, which stores sidebar title, update time, pin, and soft-delete state.
- `deployment_artifacts`, a single-use claim ledger keyed by the SHA-256 identity of intent, validated SQL, and governance metadata. `IN_PROGRESS` blocks concurrent/replayed writes; successful Oracle commits become `DEPLOYED`, while pre-commit failures release the claim for retry.

Soft deletion affects only `chat_sessions`; checkpoint messages remain. `chat_sessions.chat_id` alone is the primary key even though reads/mutations also use user and project. Reusing the same chat ID across users/projects can collide, and `upsert_chat_session` updates an existing row by `chat_id` without checking ownership.

### Oracle

- **Primary pool:** explanation/lookups, registry reads and analytics, scenario DDL/writes, and alert-table writes.
- **Shadow pool:** generated SQL tests and alert-engine scenario execution. If unconfigured or unavailable, reads fall back to the primary pool.
- **`PIO_AML_PRODUCTION_SCENARIOS`:** executable scenario registry containing `RAW_SQL` and basic scenario fields.
- **`PIO_AML_SCENARIO`:** governance metadata keyed by `(COUNTRY_CODE, INST_CODE, SCENARIO_CODE)`.
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

- Service/logging: `SERVICE_NAME`, `SERVICE_PORT`, `DEBUG`
- Chat model: `LLM_PROVIDER`, `LLM_BASE_URL`, `LLM_MODEL`, `LLM_MODEL_FAST`, `OPENAI_API_KEY`, `LLM_TEMPERATURE`, `LLM_REASONING_EFFORT`
- Oracle pools: `ORACLE_POOL_MIN`, `ORACLE_POOL_MAX`, and `SHADOW_ORACLE_DSN`, `SHADOW_ORACLE_USER`, `SHADOW_ORACLE_PASSWORD`, `SHADOW_ORACLE_POOL_MIN`, `SHADOW_ORACLE_POOL_MAX`
- AML defaults: `AML_COUNTRY_CODE`, `AML_INST_CODE`, `AML_CREATED_BY`
- Service B: `PIOTECH_AI_URL`, `PIOTECH_AI_TIMEOUT_SECONDS`, `PIOTECH_AI_USER_ID`, `PIOTECH_AI_PROJECT_ID`
- State: `CHECKPOINT_DB_PATH`
- Tracing declarations: `LANGCHAIN_TRACING_V2`, `LANGCHAIN_API_KEY`, `LANGCHAIN_PROJECT`

Important caveats:

- `OPENAI_API_KEY` is optional to Pydantic but required by OpenAI chat/embedding operations.
- `run_dev.ps1` hardcodes port 8005 rather than consuming `SERVICE_PORT`.
- Environment variables take precedence over `.env`; the generic `DEBUG` name can collide with ambient tooling variables. Any non-boolean value (the verification environment supplied `DEBUG=release`) prevents settings construction and therefore application/test import.
- `LLM_MAX_TOKENS`, `MAX_AGENT_ITERATIONS`, and `MAX_VALIDATION_RETRIES` are declared but unused by current code; chat hardcodes `recursion_limit=10`.
- The settings object's LangSmith fields are not passed explicitly by application code; the launcher exports `.env` variables, which third-party libraries may read independently.

## 10. Authentication, authorization, and trust boundaries

No authentication middleware, token validation, role checks, or authorization dependencies are present. `user_id`, `project_id`, `chat_id`, deploy identity, and path identities are accepted from the caller. CORS allows all origins, methods, and headers while enabling credentials.

Consequences for deployments:

- This service must be treated as behind a trusted/authenticated gateway until server-side identity and authorization are added.
- Session path/query identifiers are routing keys, not verified principals.
- `/scenario/deploy` and `/engine/run-scenarios` perform sensitive writes/queries without local authorization.
- Generated SQL is accepted from an external service and guarded only by `run_*_readonly` requiring the text to begin with `SELECT` or `WITH`. The parser is not a full SQL policy validator; database credentials and grants are the final containment boundary.
- Governance and analytics joins often match scenario ID/code without country and institution predicates. **Unclear:** whether IDs are globally unique enough in the deployed data model to make this safe.

## 11. Testing and verification model

`services/aml_builder/tests/test_deployment_guard.py` is an offline deterministic suite covering approval bypass, failed and stale artifacts, metadata drift/validation failure, missing writer evidence, retry, and concurrent deployment claims. `test_semantic_intent_contract.py` covers complex populations, denominator policy, time/evidence preservation, blocking ambiguity, legacy compatibility, reference integrity, and plan rendering. Run them with:

```powershell
$env:DEBUG = "false"
.\.venv\Scripts\python.exe -m pytest -q services\aml_builder\tests\test_deployment_guard.py services\aml_builder\tests\test_semantic_intent_contract.py
```

`services/aml_builder/tests/test_registry_analytics.py` remains a manually run integration check requiring live Oracle, persisted tables/data, and OpenAI for semantic search. Run it from the repository root only with approved live configuration:

```powershell
$env:PYTHONPATH = "services\aml_builder"
.\.venv\Scripts\python.exe services\aml_builder\tests\test_registry_analytics.py
```

There is still no offline alert-engine or full graph/API integration suite. The deployment guard suite mocks live metadata lookup validation and does not write Oracle.

Safe structural checks include the following. The explicit `DEBUG` override avoids unrelated ambient-variable collisions during verification and lasts only for the current PowerShell process:

```powershell
$env:DEBUG = "false"
.\.venv\Scripts\python.exe -m compileall -q services app.py run_alert_engine.py
.\.venv\Scripts\python.exe -m pytest --collect-only -q
```

Collection now includes the deployment guard regression tests; the registry analytics script is still not collected as pytest tests.

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
- Evolve `semantic_contract.contract_version` additively and coordinate any transport-level version enforcement with Package 09; legacy intents without the nested contract remain accepted.
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
- Scenario data is split between executable SQL (`PIO_AML_PRODUCTION_SCENARIOS`) and governance metadata (`PIO_AML_SCENARIO`) and written atomically.
- Alert execution is a standalone/on-demand path so scenario authoring does not itself populate alert tables.

No rationale should be inferred beyond these code- and history-supported decisions.

## 15. Risks and technical debt

Confirmed or directly evidenced concerns:

- **Direct-deploy approval depends on endpoint semantics.** `POST /scenario/deploy` is treated as the user's explicit approval action; local authentication is still absent, so a trusted authenticated gateway remains required to bind that action to a real principal.
- **No local authentication/authorization.** Sensitive endpoints trust caller-supplied identities.
- **Tenant scoping is incomplete.** Several joins use only scenario ID/code; lookup fetching retries without country/institution filters when tenant-filtered lookup returns no rows.
- **Sidebar session key collision.** `chat_sessions` uses `chat_id` alone as its primary key and upsert lookup.
- **Alert sequence concurrency.** `_get_next_seq` uses `MAX(SEQ)+1`; concurrent runs can select the same value despite the module's PK-safety claim.
- **Engine reruns are not idempotent.** Re-executing a scenario/date creates additional headers/details with new sequence values; no deduplication/run ledger is present.
- **Generated SQL safety is shallow.** Read-only helpers check only the leading token, and historical date substitution is string-based.
- **Shadow metrics are partially synthetic.** Detail counts use a fixed 4x heuristic and the reported ratio is not population density.
- **Schema drift risk.** Tables are created ad hoc only when absent; existing schemas are not migrated or validated for compatible columns.
- **Blocking work in async routes.** Agent tools, Oracle access, Service B calls, and engine execution can occupy request workers.
- **Explanation cache staleness.** The persistent cache is not invalidated when Oracle catalog content changes.
- **Partial failure signaling.** Some registry and initialization functions log and return empty/false results, making infrastructure failure resemble absence of data.
- **Metadata readiness is weaker than validity.** The catalog's `ready_for_persistence` checks presence only; lookup/static validation can still fail at persistence.
- **Telemetry filtering is inconsistent.** Per-scenario header counts honor caller filters, but the corresponding detail count query uses only scenario ID and can report lifetime details beside date/customer-filtered headers.
- **Request-contract fields are partly inert.** `reasoning_mode` does not affect execution, supplied prior messages are ignored in favor of checkpoint state, and a server-generated chat ID is not explicitly returned.
- **Documentation and code comments overstate behavior.** Examples include an automatic/daily runner, telemetry notes that are not returned, period/version metadata, and safe sequence generation.
- **Partial automated testing.** Deployment integrity has offline regression coverage, but other critical graph, API, Oracle, and alert-engine behavior still depends on live infrastructure or lacks automated coverage.
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
