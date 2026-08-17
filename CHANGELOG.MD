# Changelog — AI AML Agentic Scenario Builder

All notable changes to this project are documented here.
Format: `[DATE] TYPE: Description`

---

## [2026-08-17] fix: History Metadata Rehydration, Resilient Deploy Thread Resolution & Next-Step Guidance

### What
- **History Rehydration (`sessions.py`)**: Added `scenario_metadata_catalog` to `ChatMessage` in [`schemas.py`](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/schemas.py) and updated `get_chat_history` in [`sessions.py`](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/api/routes/sessions.py) to extract and attach the governance catalog to history messages, ensuring the Governance & Metadata side-panel re-hydrates on browser refresh instead of reverting to blank.
- **Deploy Button 400 Fix (`deploy.py`)**: Updated [`routes/deploy.py`](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/api/routes/deploy.py) to search candidate thread ID prefixes (`default_`, `no_project_`, raw) and added friendly value normalizations (`HIGH` -> `H`, `MEDIUM` -> `M`, `LOW` -> `L`) in [`scenario_metadata.py`](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/scenario_metadata.py) to eliminate 400 Bad Request deployment errors.
- **Agent Next-Step Guidance & Conversational Edits (`tool_driven_system.md`)**:
  - Instructed the agent to explicitly guide the user on next steps after shadow testing (prompting them to proceed to Governance & Metadata review).
  - Added conversational metadata edit rule: if the user asks in chat to change a governance field (*"change risk to low"*), the agent updates `scenario_metadata` and re-calls `prepare_scenario_metadata_for_persistence` to immediately update the frontend sidebar dropdowns.

---

## [2026-08-17] fix: Direct Scenario Deployment Endpoint (`/scenario/deploy`) & Bidirectional Metadata State Sync

### What
- **Direct Deployment Endpoint (`/scenario/deploy`)**: Implemented `POST /scenario/deploy` in [`routes/deploy.py`](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/api/routes/deploy.py) to service the frontend "Deploy Scenario to Production" button. It retrieves the verified shadow-tested SQL and intent from the session checkpointer, validates officer governance metadata from the side panel, and commits atomic dual-table persistence to `PIO_AML_PRODUCTION_SCENARIOS` and `PIO_AML_SCENARIO`.
- **Chat Metadata Injection**: Updated `ChatRequest` in [`schemas.py`](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/schemas.py) and [`chat.py`](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/api/routes/chat.py) to accept `metadata_json` forwarded by the frontend, so conversational persistence commands (`"persist the scenario"`) use the officer's exact sidebar selections instead of reverting to default parameters.
- **Verification**: Verified compilation of `deploy.py`, `chat.py`, `schemas.py`, and `main.py`.

---

## [2026-08-17] fix: Enforce Mandatory Human-in-the-Loop Stop Gate Before Database Persistence

### What
- Updated [tool_driven_system.md](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/prompts/tool_driven_system.md) Phase 3 and Phase 4 rules:
  - Mandated that immediately after calling `prepare_scenario_metadata_for_persistence`, the agent MUST stop calling tools and output a conversational response prompting the user to review the sidebar catalog.
  - Strictly prohibited the agent from calling `persist_and_validate_scenario_in_dwh` in the same turn or autonomously without explicit user confirmation in a subsequent turn.
- Updated tool docstrings for `prepare_scenario_metadata_for_persistence` and `persist_and_validate_scenario_in_dwh` in [tools.py](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/tools.py) to reinforce the human-in-the-loop confirmation requirement.

### Why
- Fixes an issue where the ReAct agent autonomously chained `prepare_scenario_metadata_for_persistence` -> `persist_and_validate_scenario_in_dwh` in the same turn, attempting to persist to Oracle DWH before the compliance officer had a chance to fill or review governance fields in the sidebar.

### Verify
- Verified tool docstrings, system prompt rules, and agent stopping constraints.

---

## [2026-08-17] fix: Enforce full verbatim enriched_intent payload to text-to-SQL shadow testing tool

### What
- Updated `execute_oracle_dwh_shadow_test` in [tools.py](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/tools.py) to automatically unwrap `enriched_intent`, `AMLIntent`, or `intent` wrapper keys before building the HTTP request payload sent to PioTech AI.
- Updated [tool_driven_system.md](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/prompts/tool_driven_system.md) Phase 2 instructions to strictly mandate passing the full verbatim `enriched_intent` JSON (including `scenario_name`, `scenario_type`, `transaction_type`, `detection_logic`, `thresholds`, `time_window`, `aggregation`, `semantic_conditions`, etc.) and prohibit inventing dummy summary keys.

### Why
- Fixes an issue where the ReAct LLM passed a truncated, hallucinated dummy object (`{"AMLIntent": {"transaction_amount_threshold": 10000, ...}}`) to `execute_oracle_dwh_shadow_test` instead of the full `AMLIntent` contract extracted in Phase 1.

### Verify
- Verified tool argument handling, unwrapping logic, and PioTech AI payload structure.

---

## [2026-08-17] fix: Eliminate parallel tool call race condition by generating plan directly in intent extraction

### What
- **Direct Plan Generation**: Updated `analyze_intent_and_discover_explanation_codes` in [tools.py](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/tools.py) to generate `plan_artifact` directly via `build_plan_markdown()` and include it in its returned JSON payload.
- **SSE Interception**: Updated [chat.py](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/api/routes/chat.py) and [sessions.py](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/api/routes/sessions.py) to stream `plan_artifact` SSE events from `analyze_intent_and_discover_explanation_codes`.
- **Automatic Unwrapping**: Updated `generate_scenario_execution_plan` in [tools.py](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/tools.py) to automatically unwrap `enriched_intent` and `explanation_code_checkpoint` if a wrapped dictionary is passed.
- **Prompt Simplification**: Updated [tool_driven_system.md](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/prompts/tool_driven_system.md) to eliminate parallel 2-tool chaining instructions.

### Why
- Fixes a race condition where the LLM generated parallel tool calls in the same turn (`analyze_intent_and_discover_explanation_codes` + `generate_scenario_execution_plan(intent_json="{}")`), causing the plan generator to run simultaneously with an empty placeholder before intent extraction completed.

### Verify
- Verified tool execution, SSE event streaming, and plan artifact generation.

---

## [2026-08-17] fix: Enforce Mandatory 2-Tool Chaining for Intent Analysis & Plan Generation

### What
- **Root Cause Analysis**: When a scenario did not require multi-turn explanation code discovery, the ReAct agent called `analyze_intent_and_discover_explanation_codes` and stopped without calling `generate_scenario_execution_plan`, but hallucinated the confirmation phrase *"I've prepared the implementation plan — you can review it in the side panel"*.
- **Fix Applied**:
  - Updated [`tool_driven_system.md`](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/prompts/tool_driven_system.md) to define **Phase 1 as a mandatory 2-tool atomic chain in the same turn** (`analyze_intent_and_discover_explanation_codes` -> `generate_scenario_execution_plan`).
  - Added explicit tool docstring directive in [`tools.py`](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/tools.py) instructing the agent to always chain `generate_scenario_execution_plan` immediately after intent extraction so the side panel artifact is guaranteed to be rendered.
- **Verification**: Verified agent system prompt and Python compilation.

---

## [2026-08-17] feat: Phase 3 Standalone Daily AML Alert Execution Engine & Alert Populator

### What
- **Core Engine Module (`alert_engine.py`)**: Built the automated batch runner that discovers active scenarios in `PIO_AML_PRODUCTION_SCENARIOS` + `PIO_AML_SCENARIO`, runs production `RAW_SQL` on `BI_DWH`, and populates `PIO_AML_CUSTOMERS` (Header Alerts) and `PIO_AML_CUSTOMERS_DET` (Transaction Details).
- **CLI Runner (`run_alert_engine.py`)**: Command-line entry point supporting automated daily batch runs and on-demand scenario testing (`--scenario-id`, `--date`).
- **REST Endpoints (`routes/engine.py`)**: Added `POST /engine/run-scenarios` and `GET /engine/active-scenarios` to trigger and inspect engine executions via API.
- **Robustness & Constraint Safety**: Dynamic sequence PK incrementing (`NVL(MAX(SEQ), 0) + 1`), DDL non-null fallbacks (`LINK_CUS_NUM`, `REL_TYPE`, `ENTITIY_SIGNATORY`), and atomic per-scenario transaction commits.
- **Verification**: Executed live scenario `PRD_7EB9CBAD` against Oracle `BI_DWH` and verified successful insertion of header alert into `PIO_AML_CUSTOMERS` and transaction detail record into `PIO_AML_CUSTOMERS_DET`.
- **Documentation**: Added [`Docs/04_standalone_alert_engine.md`](file:///c:/Users/abura/Development/AI_AML_AGENT/Docs/04_standalone_alert_engine.md).

---

## [2026-08-16] fix: Live Oracle DWH Schema Alignment for PIO_AML_SCENARIO & Lookup Tables

### What
- **Real Oracle DWH Inspection**: Queried live Oracle data dictionary (`USER_TAB_COLS`, `ALL_TABLES`) via `oracle-db` MCP tool to discover the real table and column names in `AIDB`.
- **Lookup Tables Configured**:
  - `PIO_AML_SCENARIO_CATEGORY`: `CATEGORY_CODE`, `CATEGORY_ENG_NAME` (was placeholder `PIO_AML_CATEGORY`).
  - `PIO_AML_DEGREE_RISK`: `DEGREE_CODE`, `DESC_ENG` (values: `D`, `H`, `M`, `L`).
  - `PIO_AML_SCENARIO_CLASSES`: `CLASS_CODE`, `CLASS_ENG_NAME` (e.g. `1`=Transaction Monitoring).
  - `PIO_AML_SCENARIO_TYPE`: `TYPE_CODE`, `TYPE_ENG_NAME` (e.g. `1`=All Tabs, `7`=Member Check).
- **Exact PIO_AML_SCENARIO Columns Aligned**:
  - Replaced placeholder names (`SCENARIO_DESC`, `CATEG_CODE`, `RISK_DEGREE`) with the real database column names (`SCENARIO_DES_ENG`, `CATEGORY_CODE`, `DEGREE_RISK_FLAG`, `ACTIVE_FLAG`, `RUN_FLAG`, `CLASS_CODE`, `SCE_TYPE_CODE`, `TRANS_WITHOUTTRANS_FLAG`, etc.).
  - Updated atomic dual-write INSERT/UPDATE in [`production_registry.py`](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/production_registry.py) to prevent `ORA-00904: invalid identifier` exceptions.
- **Verification**: Executed live Python test connecting to Oracle connection pool; successfully fetched live lookup options, verified non-null catalog seeding, and confirmed 100% clean validation.
- **Files Updated**: [scenario_metadata.py](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/scenario_metadata.py), [production_registry.py](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/production_registry.py)

---

## [2026-08-11] feat: PIO_AML_SCENARIO Governance Metadata + Atomic Dual-Write Persistence

### What
Phase 2 of the Production Scenario Registry (see `Docs/PIO_AML_SCENARIO_schema_guide.md`): compliance
officers must now supply governance metadata (risk degree, category, violation level, etc.) — sourced
strictly from live Oracle lookup tables, never LLM-invented — before a scenario can be persisted.

- **New `scenario_metadata.py`**: a declarative field registry (`SCENARIO_METADATA_FIELDS`) covering
  every `PIO_AML_SCENARIO` column, with four population kinds (`system_default`, `derived`,
  `static_enum`, `oracle_lookup`). `seed_scenario_metadata()` deterministically maps `AMLIntent` fields
  (`time_window` → `PERIOD_TYPE`/`PERIOD_NUM`, `detection_logic` → `SCENARIO_DESC`) without ever
  overwriting values already picked by the officer. `fetch_lookup_options()` queries live Oracle lookup
  tables filtered by `COUNTRY_CODE`/`INST_CODE`. `validate_scenario_metadata()` is the single guard-rail
  that must pass before any write — every mandatory field non-null, every governed value verified
  against its live/static allowed-value set.
- **Two new/changed tools** in `tools.py`: `analyze_intent_and_discover_explanation_codes` now seeds
  `scenario_metadata` from turn one (new `existing_metadata_json` param preserves prior picks across
  refinement turns). New `prepare_scenario_metadata_for_persistence` tool builds the live field catalog
  (options fetched from Oracle) for the frontend side panel. `persist_and_validate_scenario_in_dwh` now
  requires `metadata_json`, validates it first, and refuses to write anything on failure.
- **Atomic dual-write** in `production_registry.py`: `save_production_scenario()` now writes
  `PIO_AML_PRODUCTION_SCENARIOS` (RAW_SQL) and the new `PIO_AML_SCENARIO` (business metadata) table in
  one Oracle transaction via the previously-unused `oracle.atomic_connection()` — both commit or both
  roll back. Re-persisting an existing `SCENARIO_CODE` increments `PIO_AML_SCENARIO.VERSION_NUM`.
- **New SSE event** `scenario_metadata_catalog`, wired in `routes/chat.py`, carries the field catalog
  to the frontend side panel the same way `plan_artifact` already does.
- **Prompt updated** (`tool_driven_system.md`): step 4 of the workflow is now "gather governance
  metadata, relay any still-missing mandatory fields in chat as a fallback to the panel, never guess a
  value" before the (renumbered) step 5 persist call.

### Known placeholders — not yet confirmed
The exact column names for the `PIO_AML_CATEGORY` and `PIO_AML_DEGREE_RISK` lookup tables are
best-guess placeholders (`CATEG_CODE`/`DESC_ENG`, `DEGREE_CODE`/`DESC_ENG`), clearly marked `TODO
(compliance)` at the top of `scenario_metadata.py`. Fixing them is a one-line `LookupTableConfig` edit,
not a code change — the fetch/validate mechanism is fully generic. `VIOLATION_LEVEL` is implemented as
a static `HIGH`/`MEDIUM`/`LOW` enum per the schema guide (no live table was named for it).

### Verification
`py_compile` across every new/modified file; full `import app` with dummy credentials (routes/graph
unaffected); direct tool invocation test of `seed_scenario_metadata` → `prepare_scenario_metadata_for_persistence`
→ `persist_and_validate_scenario_in_dwh`, confirming the validation gate correctly blocks persistence
with clear error messages when lookup values can't be verified, while the static-enum field passes.

### Files created
`web/services/scenario_metadata.py`, `Docs/03_scenario_metadata_persistence.md`

### Files modified
`web/services/tools.py`, `web/services/graph.py`, `web/services/production_registry.py`,
`web/services/schemas.py`, `web/api/routes/chat.py`, `web/services/prompts/tool_driven_system.md`,
`CLAUDE.md`

---

## [2026-08-11] docs: Document Schema & DDL for 5 Core System Tables in Notes.md

### Documentation
- **Database Schema Documentation**: Documented column structure, data types, nullability, primary key constraints, and standard `CREATE TABLE` DDL statements for the 5 core tables driving the AML scenario creation and execution workflow:
  1. [`PIO_AML_SCENARIO`](file:///c:/Users/abura/Development/AI_AML_AGENT/Docs/Notes.md#1-pio_aml_scenario): High-level scenario metadata and description table.
  2. [`PIO_AML_PRODUCTION_SCENARIOS`](file:///c:/Users/abura/Development/AI_AML_AGENT/Docs/Notes.md#2-pio_aml_production_scenarios): Agent scenario registry table storing production SQL detection statements.
  3. [`PIO_AML_CUSTOMERS`](file:///c:/Users/abura/Development/AI_AML_AGENT/Docs/Notes.md#3-pio_aml_customers): Customer alert header table generated from daily detection runs.
  4. [`PIO_AML_CUSTOMERS_DET`](file:///c:/Users/abura/Development/AI_AML_AGENT/Docs/Notes.md#4-pio_aml_customers_det): Transaction detail breakdown table linked to parent alerts.
  5. [`PIO_TRANSACTIONS`](file:///c:/Users/abura/Development/AI_AML_AGENT/Docs/Notes.md#5-pio_transactions): Core DWH financial transaction ledger table.
- **Files modified**: [`Docs/Notes.md`](file:///c:/Users/abura/Development/AI_AML_AGENT/Docs/Notes.md)

---

## [2026-08-10] feat: Dedicated Shadow Oracle DB Connection for High-Volume Test Execution

### Feature & Architectural Enhancements
- **Isolated Shadow Testing Connection**: Added configuration and pool management for a dedicated secondary Oracle DB connection (`SHADOW_ORACLE_*`), allowing live shadow tests to run against high-volume test databases (e.g. 5M+ `PIO_TRANSACTIONS` records) while keeping all control-plane operations (scenario persistence, parameter decomposer, catalog search) on the primary DB.
- **Environment Variables Added**: Added `SHADOW_ORACLE_DSN`, `SHADOW_ORACLE_USER`, `SHADOW_ORACLE_PASSWORD`, `SHADOW_ORACLE_POOL_MIN`, and `SHADOW_ORACLE_POOL_MAX` to [`.env`](file:///c:/Users/abura/Development/AI_AML_AGENT/.env). If left blank, automatically falls back to primary `ORACLE_*` parameters.
- **Isolated Tool Scope**: Updated `execute_oracle_dwh_shadow_test` in [`tools.py`](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/tools.py#L394) to execute queries via `run_shadow_readonly()`. All other tools remain 100% untouched.
- **Files modified**: [`.env`](file:///c:/Users/abura/Development/AI_AML_AGENT/.env), [`settings.py`](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/settings.py), [`oracle.py`](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/oracle.py), [`tools.py`](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/tools.py), [`main.py`](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/api/main.py)

---

## [2026-08-10] refactor: Externalize Intent Extraction System Prompt to Markdown Catalog

### Refactor & Fixes
- **Restored Missing Import**: Fixed `NameError: name 'build_plan_markdown' is not defined` in `generate_scenario_execution_plan` by restoring `from services.aml_builder.web.services.plan_renderer import build_plan_markdown` in [`tools.py`](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/tools.py).
- **Prompt Externalization**: Migrated the 50+ line intent extraction system instruction out of `analyze_intent_and_discover_explanation_codes()` in `tools.py` into a clean markdown document [`intent_extraction.md`](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/prompts/intent_extraction.md) inside `web/services/prompts/`.
- **Centralized Prompt Loader**: Created [`loader.py`](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/prompts/loader.py) in `web/services/prompts/` to provide a unified `load_prompt(filename)` loader used across both `graph.py` and `tools.py`.
- **Files created**: `web/services/prompts/intent_extraction.md`, `web/services/prompts/loader.py`
- **Files modified**: `web/services/tools.py`, `web/services/graph.py`

---

## [2026-08-10] fix: Clean interactive prompt lines from Section 8 in Scenario Plan Renderer

### Bug fixes
- **Stripped interactive prompt from plan artifact**: `explanation_code_checkpoint` included a call-to-action line (`> **Action Needed:** Reply "confirm codes"...`) designed for interactive chat turns. When `build_plan_markdown()` rendered Section 8 (Explanation Codes), this line was embedded verbatim into the Scenario Implementation Plan artifact. Added `_clean_explanation_checkpoint()` in `plan_renderer.py` using case-insensitive pattern matching to strip out interactive prompts while preserving the discovered codes catalog table.
- **Files modified**: [plan_renderer.py](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/plan_renderer.py)

---

## [2026-08-10] refactor: Remove Dead Legacy QB-Engine Remnants

### What
Removed all remaining code, config, and docs referencing the deleted multi-node graph's QB-engine
tables (`PIO_AML_PARAMETERS`, `PIO_AML_COLUMNS`, `PIO_AML_SCENARIO`, `PIO_AML_RULES`,
`PIO_AML_SCENARIO_RULES`, `PIO_AML_RULES_DETAILS`) and the `FILL_PIO_AML_CUSTOMERS` stored procedure.
None of it was reachable from the current tool-driven agent (verified with repo-wide grep before
removing each piece) — it was pure dead weight left over from the `agent.py` deletion.

- **`schemas.py`**: removed the unused SQL-bridge/decomposer/validation/plan-layer model classes
  (`SQLMetadata`, `QBScenario`, `QBRule`, `QBRuleDetail`, `QBScenarioRule`, `ScenarioParameters`,
  `AlertSample`, `ValidationResult`, `PlanCondition`, `CatalogCreation`, `WriteVerification`,
  `OrchestratorDecision`), plus a duplicate `ChatMessage` definition that was silently shadowed by
  a second one. Kept the intent layer (`AMLIntent` and its nested models) and the API layer intact.
- **`settings.py`**: removed 10 unused QB-engine default fields (`AML_DEFAULT_ACTIVE_FLAG`,
  `AML_DEFAULT_VIOLATION_LEVEL`, `AML_DEFAULT_DEGREE_RISK_FLAG` + its validator, `AML_RUN_FLAG`,
  `AML_APPROVAL_FLAG`, `AML_GROUP_BY_FLAG`, `AML_CLASS_CODE`, `AML_SCE_TYPE_CODE`,
  `AML_CATEGORY_CODE`, `AML_TRANS_WITHOUTTRANS_FLAG`, `AML_RULE_TYPE`). Kept `AML_COUNTRY_CODE`,
  `AML_INST_CODE`, `AML_CREATED_BY` — confirmed still read by `explanation_code_search.py` and
  `tools.py`.
- **`oracle.py`**: removed `get_next_numeric_code()` and `call_procedure()` — both unused, both
  existed solely to serve the deleted QB-engine write/validate flow.
- **`CLAUDE.md`**: rewrote the Architecture section (was still describing the deleted multi-node
  graph topology, `agent.py`, and the QB tables as if current) to match the actual tool-driven
  architecture; removed the now-moot Known Issues entries.
- **Docs cleanup**: deleted `Docs/database/{main tables.sql, metadata_schema.md, RULES.sql,
  RULES_DETAILS.sql, SCENARIO.sql, SCENARIO_RULES.sql}` (pure QB-table reference dumps) and
  `Docs/Implementaion_team_notes.md` (legacy decomposer planning notes). Trimmed
  the `FILL_PIO_AML_CUSTOMERS`-specific section out of `Docs/known_issues.md`, leaving the unrelated
  DWH-service entries (sections 1–6) intact. Kept `Docs/database/PIO_AML_PRODUCTION_SCENARIOS.sql`
  and `Docs/database/DB QUERIES.sql` — both describe/use tables the current system actually touches.
  `Docs/Notes.md` was originally slated for deletion too but was kept as-is per user decision — it
  has since accumulated non-legacy content alongside the legacy notes.

### Verification
`py_compile` on every edited file, and a full `import app` + route-registration check with dummy
Oracle/OpenAI credentials — both clean, same as the prior pass.

### Files deleted
`Docs/database/main tables.sql`, `Docs/database/metadata_schema.md`, `Docs/database/RULES.sql`,
`Docs/database/RULES_DETAILS.sql`, `Docs/database/SCENARIO.sql`, `Docs/database/SCENARIO_RULES.sql`,
`Docs/Implementaion_team_notes.md`

### Files modified
`CLAUDE.md`, `Docs/known_issues.md`, `services/aml_builder/web/services/schemas.py`,
`services/aml_builder/web/services/settings.py`, `services/aml_builder/web/services/oracle.py`

---

## [2026-08-10] fix+refactor: Restore `/chat/stream`, Split Agent & API Modules by Responsibility

### Bug fixes
- **`/chat/stream` was crashing on every call**: the prior "Legacy Agent Removal" pass deleted `agent.py` (and its `get_graph()`/`AMLScenarioState`) but left `main.py` calling `get_graph()` unconditionally before the tool-driven-agent branch even ran. Every chat request raised `NameError`. Removed the dead legacy-graph branches entirely (the tool-driven ReAct agent is now the only path — confirmed via `Docs/01_architecture.md` as already the intended architecture); the `USE_TOOL_DRIVEN_AGENT` flag was dropped since it no longer gates anything. The equally dead `POST /chat/{project_id}/{chat_id}/{user_id}` background-task endpoint (built entirely around the deleted graph's state shape) was removed for the same reason.
- **`explanation_code_search.py` silently lost sub-LLM reranking**: `select_relevant_explanation_codes` called `_build_llm(fast=True)`, a name that was never imported/defined there, caught only by a broad `except Exception` fallback to raw vector-search results. Fixed by importing the shared `build_llm()`.
- **`GET /health` returned 500**: `SERVICE_NAME: Final[str] = Field(...)` in `settings.py` made Pydantic v2 treat the field as a class constant, so `settings.SERVICE_NAME` returned a raw `FieldInfo` object instead of a string, breaking JSON serialization. Fixed by dropping the `Final` annotation.

### Refactor (behavior-preserving)
- **New `llm_client.py`**: `build_llm()`/`safe_parse_json()` extracted from `agent_tool_driven.py` — the only shared home for LLM construction, now used by both the agent tools and explanation-code search instead of being duplicated/missing.
- **New `sql_extraction.py`** and **`plan_renderer.py`**: pulled the SQL-text-parsing helpers and the 11-section plan markdown renderer out of `agent_tool_driven.py`, which now holds only the four `@tool` definitions.
- **New `graph.py`**: LangGraph ReAct agent compilation/lifecycle (`get_tool_driven_graph`, `close_checkpointer`, prompt loading) split out of the tool-definitions module.
- **`agent_tool_driven.py` renamed to `tools.py`**: once the other seams above were extracted, the file held only the four `@tool` definitions — the old name described the pre-split architecture as a whole and was no longer accurate.
- **New `session_store.py`**: the raw `sqlite3` chat-sessions sidebar CRUD (init/upsert/list/rename/pin/delete) extracted out of route handlers in `main.py`.
- **New `api/routes/chat.py`** and **`api/routes/sessions.py`**: `main.py` is now just app creation, CORS, lifespan, and router includes.
- Verified end-to-end: app imports cleanly, all routes register, `/health` returns 200, `/chat/stream` reaches the LLM call (previously died before that point on every request).

### Files created
`web/services/llm_client.py`, `web/services/sql_extraction.py`, `web/services/plan_renderer.py`, `web/services/graph.py`, `web/services/session_store.py`, `web/api/routes/chat.py`, `web/api/routes/sessions.py`

### Files renamed
`web/services/agent_tool_driven.py` → `web/services/tools.py`

### Files modified
`web/api/main.py`, `web/services/tools.py`, `web/services/explanation_code_search.py`, `web/services/settings.py`

---

## [2026-08-10] feat: Standard Root Uvicorn Entrypoint (`app.py`) & Package Imports Fix

### What
- **Package Imports Fix**: Converted all internal imports across `main.py`, `agent_tool_driven.py`, `explanation_code_search.py`, `oracle.py`, `logging_config.py`, and `production_registry.py` from top-level `web...` to explicit package paths `services.aml_builder.web...`.
- **Root Entrypoint (`app.py`)**: Created `app.py` in the root directory so standard `uvicorn app:app --reload --port 8005` works directly from root without requiring `PYTHONPATH` exports or helper scripts.
- **Files Created/Updated**: [app.py](file:///c:/Users/abura/Development/AI_AML_AGENT/app.py), [README.md](file:///c:/Users/abura/Development/AI_AML_AGENT/README.md), [main.py](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/api/main.py)

---

## [2026-08-10] refactor: Workspace Cleanup & Legacy agent.py Migration

### What
- **Legacy Agent Removal**: Migrated `_load_prompt`, `_is_real_sql`, and `_extract_sql` into [`agent_tool_driven.py`](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/agent_tool_driven.py) and added `close_checkpointer()`. Deleted the 148 KB legacy multi-node `agent.py`.
- **Imports Cleanup**: Updated [`explanation_code_search.py`](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/explanation_code_search.py) and [`main.py`](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/api/main.py) to import directly from `agent_tool_driven.py`. Removed all references to the legacy multi-node graph.
- **Documentation**: Created [`Docs/01_architecture.md`](file:///c:/Users/abura/Development/AI_AML_AGENT/Docs/01_architecture.md) documenting the complete tool-driven system architecture, data contracts, and tool pipeline.
- **Files Deleted**: `agent.py`
- **Files Created/Updated**: [agent_tool_driven.py](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/agent_tool_driven.py), [explanation_code_search.py](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/explanation_code_search.py), [main.py](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/api/main.py), [01_architecture.md](file:///c:/Users/abura/Development/AI_AML_AGENT/Docs/01_architecture.md)

---

## [2026-08-10] fix: History Endpoint Artifact Resolution for Tool-Driven Agent

### What
- **`main.py` (`get_chat_history`):** Enhanced history response construction to inspect preceding `ToolMessage` payloads. When `generate_scenario_execution_plan` or `persist_and_validate_scenario_in_dwh` returns `plan_artifact` or `scenario_result` in tool outputs, `get_chat_history` now extracts and attaches them directly to the corresponding `ChatMessage` assistant response.
- **Result**: Reloading the chat UI session (`GET /api/chat/history`) now re-hydrates the side panel plan artifact and scenario results card 1-to-1 with the live SSE streaming session.
- **Files Changed:** [main.py](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/api/main.py)

---

## [2026-08-10] feat: Scenario Implementation Plan — Deterministic Renderer

### What
- **`agent_tool_driven.py` (`_build_plan_markdown`):** Replaced the vague LLM-driven plan prompt with a deterministic Python renderer. The plan is now built directly from the parsed `AMLIntent` dict — no LLM involved. This guarantees consistent, structured output regardless of which model is running.
- **Plan Structure — 11 Sections (every non-empty AMLIntent field is reflected):**
  1. **Status Banner** — `✅ Ready for Handoff` or `⚠️ Clarification Required`
  2. **Scenario Overview** — Name, type, transaction type, customer segments, evaluation mode
  3. **Detection Logic** — Rendered as a blockquote
  4. **Observation Window & Time Parameters** — Window size, rolling vs fixed, plus Historical Baseline Window table
  5. **Aggregation Profile** — Metric, function, grain, provenance badges
  6. **Thresholds & Alert Conditions** — Split into `5.1 Detail (WHERE)` and `5.2 Aggregate (HAVING)` tables with `BETWEEN` range support and provenance labels (✅ Stated / ⚙️ Assumed / ❓ Needs User)
  7. **Behavioral & Semantic Rules** — Typed table with emoji badges per `logical_type`
  8. **Exclusion Rules** — Bullet list with 🚫 icons
  9. **Explanation Codes** — Selected codes + full DWH discovery table
  10. **Applied Defaults** — Disclosed assumptions with ⚙️ icons
  11. **Open Clarifications** — Structured Q&A with `why_it_matters` and options
  12. **Shadow Testing Scope** — Production vs historical shadow test mode
- **`plan_conditions`:** Now built dynamically from what is actually populated in the intent (not hardcoded list).
- **Files Changed:** [agent_tool_driven.py](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/agent_tool_driven.py)

---

## [2026-08-10] feat: Intent Layer Hardening & Disambiguation Engine

### What
- **`schemas.py` (`Threshold._normalize_numeric_value`):** Added `@field_validator("value_from", "value_to", mode="before")` to automatically convert percentage strings (e.g. `"300%"` -> `3.0`, `"90%"` -> `0.90`) and string multipliers into clean `float` values.
- **`schemas.py` (`Threshold.target_scope`):** Removed `default="DETAIL"` and made `target_scope` required (`...`). Forces LLMs to explicitly classify thresholds as `DETAIL` or `AGGREGATE`.
- **`schemas.py` (`AMLIntent.validate_and_deduplicate_thresholds`):** Added `@model_validator(mode="after")` to programmatically purge redundant `DETAIL` thresholds on cumulative `SUM`/`COUNT` scenarios when an `AGGREGATE` threshold exists for the same metric value.
- **`agent_tool_driven.py` System Prompt**: Enforced 9 Extraction Laws in `system_instruction` covering `BETWEEN` operator bounds (`value_from` + `value_to`), ratio conversions, mandatory `baseline_window` construction, demographic filters (`customer_age`), distinct counts (`distinct_branch_count`), risk ratings, exclusions, and snapshot `anchor_date` fields.
- **Files Changed:** [schemas.py](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/schemas.py), [agent_tool_driven.py](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/agent_tool_driven.py)

---

## [2026-08-09] feat: anchor_date field added to AMLIntent for historical snapshot testing

### What
- **`schemas.py`:** Added `anchor_date: Optional[str]` field to `AMLIntent`. Default `null` = production mode (`TRUNC(SYSDATE)`). Populated (e.g. `"2024-01-04"`) = shadow test against a seeded historical DWH snapshot.
- **`agent_tool_driven.py`:** Updated intent extraction system instruction to recognize and extract `anchor_date` when the user explicitly specifies a historical test date.
- **Service B prompt update required:** Data Agent and `DwhMechanicsAuditor` must be updated to honor `anchor_date` in the intent contract (see separate Service B prompt note).
- **Files Changed:** [schemas.py](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/schemas.py), [agent_tool_driven.py](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/agent_tool_driven.py)

---

## [2026-08-09] feat: Plan Artifact Side-Panel Rendering for Tool-Driven Agent

### What
- **`main.py` SSE Intercept:** In the tool-driven streaming loop, built a `tool_call_id → tool_name` lookup map from each `AIMessage.tool_calls` list. `ToolMessage` outputs are now routed by source tool:
  - `generate_scenario_execution_plan` → parses `plan_artifact` from JSON and emits `SSEEvent(type="plan_artifact")` → side panel renders automatically.
  - `persist_and_validate_scenario_in_dwh` → parses result and emits `SSEEvent(type="scenario_result")` → confirmation card renders.
  - All other `ToolMessage` outputs remain suppressed (raw JSON stays internal).
- **`tool_driven_system.md` UX Rule:** Added concise principle — the plan is automatically rendered in the side panel; agent must only acknowledge it in chat and not reproduce the full markdown content in the conversation.
- **Files Changed:** [main.py](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/api/main.py), [tool_driven_system.md](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/prompts/tool_driven_system.md)

---

## [2026-08-09] feat: Production-Ready Tool-Driven Agent — Shadow Test & Persistence Upgrades

### What
- **`execute_oracle_dwh_shadow_test` — Real PioTech AI SQL Generation:** Replaced the placeholder `PIO_EXPLANATION_CODE COUNT(*) * 4` stub with a full SSE streaming call to the PioTech AI text-to-SQL service (same `PIOTECH_AI_URL` used by classic `agent.py`). SQL is extracted via `_extract_sql()`, then wrapped in a `SELECT COUNT(*) FROM (...)` shadow query executed live against Oracle `BI_DWH`.
- **`persist_and_validate_scenario_in_dwh` — Production Registry Write:** Replaced 3-table legacy QB INSERT statements (`PIO_AML_SCENARIOS`, `PIO_AML_RULES`, `PIO_AML_SCENARIO_RULES`) with a single call to `production_registry.save_production_scenario()`. Writes to `PIO_AML_PRODUCTION_SCENARIOS` table with idempotent INSERT-or-UPDATE semantics and `IS_ACTIVE=1`.
- **`generate_scenario_execution_plan` — Richer Plan Artifact:** Now accepts optional `explanation_code_checkpoint` to embed the discovered codes table directly inside the plan markdown.
- **Graph-Level Interrupt Gate:** Added `interrupt_after=["generate_scenario_execution_plan"]` to `create_react_agent(...)`. The graph now **hard-stops** after plan generation and only resumes when the user sends a new approval message — no longer relies solely on system prompt instruction.
- **Scenario ID Format:** Persisted scenarios use `PRD_{8-char-hex}` format (e.g. `PRD_A1B2C3D4`).
- **Files Changed:** [agent_tool_driven.py](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/agent_tool_driven.py)

---

## [2026-08-09] fix: Declare explanation_codes field in AMLIntent Pydantic Schema


### What
- **AMLIntent Schema Fix:** Added `explanation_codes: Optional[List[str]]` to `AMLIntent` Pydantic model contract in [schemas.py](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/schemas.py#L226-L231).
- **AttributeError Prevention:** Eliminates `AttributeError: 'AMLIntent' object has no attribute 'explanation_codes'` during intent serialization and handoff to execution planner.

---

## [2026-08-09] fix: Code Regex Match, Silent Orchestrator Routing & Automatic Plan Regeneration

### What
- **1-6 Digit Regex Matching:** Updated regex in [agent.py](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/agent.py#L779) to `\b\d{1,6}\b` (was `\b\d{3,6}\b`), enabling 1-digit and 2-digit codes (e.g. code `1` or `660`) to be parsed correctly.
- **Silent Orchestrator Routing:** Added mechanical safety rule in [agent.py](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/agent.py#L453) setting `decision.message_to_user = None` when routing to `INTENT` or `PLANNER` to eliminate raw table dumps and conversational chatter during background processing.
- **Automatic Code Confirmation & Plan Regeneration:** `intent_analyst_node` automatically sets `explanation_codes_confirmed = True` when the user selects a code subset, causing `route_after_intent` to immediately route to `planner_node` to regenerate the plan artifact.

---

## [2026-08-09] fix: Register explanation code state fields in AMLScenarioState TypedDict

### What
- Added `explanation_code_checkpoint`, `discovered_explanation_codes`, and `explanation_codes_confirmed` fields to `AMLScenarioState` TypedDict in [agent.py](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/agent.py#L115-L118).
- Resolves LangGraph state key filtering so explanation code table views pass into orchestrator prompt context.

---

## [2026-08-09] feat: Relative Top 70% Cutoff for Explanation Code Vector Search

### What
- **Relative Similarity Cutoff:** Updated `search_explanation_candidates` in [explanation_code_search.py](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/explanation_code_search.py#L126-L156) to filter candidate explanation codes using relative top 70% score cutoff (`max_score * 0.70`) up to top 100 items.

---

## [2026-07-24] upgrade: mandatory baseline_window schema contract & zero-overlap isolation rule

### What
- Added `BaselineWindow` Pydantic model and `baseline_window: Optional[BaselineWindow]` field to `AMLIntent` contract in [schemas.py](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/schemas.py).
- Added `[INTENT EXTRACTION RULE: HISTORICAL BASELINE ISOLATION]` to Intent Analyst system prompts in [agent.py](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/agent.py) and [intent_analyst_system_v2.md](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/prompts/intent_analyst_system_v2.md).
- Updated [planner_system.md](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/prompts/planner_system.md) to render the non-overlapping `baseline_window` explicitly under the plan's Time Interval section.

### Why
- Enforces strict zero-overlap temporal isolation whenever current activity is compared against a historical baseline (e.g. 6-month average).
- Guarantees downstream SQL generators (Service B) receive explicit `offset_days` and date formula boundaries to prevent data leakage between current observation windows and baseline averages.

### Verify
- Verified Pydantic schema validation and prompt compilation.

## [2026-08-08] fix: Orchestrator prompt architectural invariant for plan modifications

### What
- Updated [orchestrator_system.md](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/prompts/orchestrator_system.md):
  - Added architectural invariant instructing the Orchestrator LLM that only `planner_node` generates plan artifacts, mandating `next_action = "INTENT"` with `message_to_user = null` whenever a user requests plan changes or updates during plan review.
- Removed hardcoded Python keyword list override from [agent.py](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/agent.py).

### Why
- Fixes the root cause at the system prompt level without resorting to hardcoded Python word lists, ensuring proper LLM reasoning and domain autonomy.

---

## [2026-08-08] fix: explicitly list transaction_type under Filters in Planner prompt

### What
- Updated [planner_system.md](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/prompts/planner_system.md):
  - Mandated that when `intent.transaction_type` is specified (e.g. `"OUTWARD TRANSFERS"`), the Planner MUST render it as a numbered entry under the **Filters** section of the generated markdown plan.

### Why
- Prevents transaction type filters from being omitted from the visible plan UI.

---

## [2026-08-08] enhancement: Orchestrator context awareness & Intent Analyst delta preservation

### What
- Updated `_build_state_context()` in [agent.py](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/agent.py):
  - Injected full `enriched_intent` JSON payload and `plan_artifact` markdown into the Orchestrator's system context table, giving Orchestrator complete awareness of scenario parameters and plan details.
- Updated `intent_analyst_node` in [agent.py](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/agent.py):
  - Added delta modification preservation prompt context when an existing `enriched_intent` is present in state, forcing the model to apply only the user's requested delta while preserving all existing thresholds and conditions.
- Updated Intent Analyst system prompt [intent_analyst_system_v2.md](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/prompts/intent_analyst_system_v2.md):
  - Added **Rule 18**: Mandates delta preservation and prohibits re-opening clarified dimensions during scenario modifications.

### Why
- Fixes Orchestrator context blindness and prevents Intent Analyst from hallucinating irrelevant clarification questions when the user asks to modify an existing scenario.

---

## [2026-08-08] fix: route_after_planner returns END to prevent infinite orchestrator loop

### What
- Updated `route_after_planner()` in [agent.py](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/agent.py):
  - Changed return value on `WAIT_APPROVAL` from `"orchestrator"` to `END`.
  - Updated `planner_node` response message to emit explicit user-facing plan introduction text alongside the markdown `plan_artifact`.

### Why
- Fixes an infinite orchestrator routing loop where `planner` routed to `orchestrator` in the same turn without pausing, causing the Orchestrator LLM to re-evaluate prior user messages and trigger `INTENT` repeatedly. Returning `END` correctly pauses execution until the user provides approval or requests further plan adjustments.

---

## [2026-08-08] fix: support 'WEEKS' in TimeWindow & BaselineWindow schemas

### What
- Updated `TimeWindow` and `BaselineWindow` models in [schemas.py](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/schemas.py):
  - Expanded `unit` field allowed choices to `Literal["DAYS", "WEEKS", "MONTHS", "YEARS"]`.

### Why
- Resolves Pydantic `ValidationError` (`input_value='WEEKS'`) when the user prompt or Intent Analyst specifies a weekly observation window (e.g. 2 weeks, 1 week).

---

## [2026-08-06] enhancement: explicit Threshold target_scope (DETAIL vs AGGREGATE) for SQL clause placement

### What
- Updated `Threshold` model in [schemas.py](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/schemas.py):
  - Added `target_scope: Literal["DETAIL", "AGGREGATE"]` to explicitly designate whether a numeric threshold applies to single raw transaction rows (for `WHERE` clause) or aggregated metrics/sums/counts over a period (for `HAVING` clause).
- Updated Intent Analyst system prompt [intent_analyst_system_v2.md](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/prompts/intent_analyst_system_v2.md):
  - Added **Rule 17**: Mandates `target_scope: "AGGREGATE"` for period totals, sums, ratios, or counts, and `target_scope: "DETAIL"` for raw row filters.
- Updated Planner system prompt [planner_system.md](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/prompts/planner_system.md):
  - Updated Cardinal Rule to evaluate `threshold.target_scope`, ensuring the user-facing markdown plan and pre-commit drift validation block explicitly distinguish between `WHERE` filters and `HAVING` aggregate rules.

### Why
- Eliminates contract ambiguity between Intent Analyst (Service A), Planner, and SQL Bridge (Service B), ensuring Service B deterministically places accumulated period thresholds in the `HAVING` clause (`HAVING SUM(EQU_TRA_AMT) >= ...`) instead of incorrectly filtering raw rows in the `WHERE` clause.

---

## [2026-07-28] fix: COUNT(DISTINCT ...) decomposer HAVING parsing & p_code int string casting

### What
- Updated `_fetch_and_map_parameters()` in [agent.py](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/agent.py):
  - Added regex matching for `HAVING COUNT(DISTINCT <COLUMN>) [OPERATOR] [VALUE]` to resolve target columns (e.g., `BRA_CODE`) against aggregate parameter codes (`117`) in `PIO_AML_PARAMETERS`.
  - Safely cast `p_code` to string before calling `.strip()` in LLM intent grounding fallback (`str(item.get("parameter_code") or "").strip()`), resolving `AttributeError: 'int' object has no attribute 'strip'`.

### Why
- Fixes scenario creation failures when decomposing queries with distinct column count aggregations (such as distinct branch counts `COUNT(DISTINCT BRA_CODE) > 3`).

### Verify
- Verified parameter resolution mapping `HAVING COUNT(DISTINCT T.BRA_CODE) > 3` to Parameter `117` (`Rule Detail 1`, `> 3`) using `test_distinct_decomposer.py`.

---

## [2026-07-24] upgrade: structured relative threshold prompt rules & dynamic OR connector determination

### What
- Updated `intent_analyst_system_v2.md` in [intent_analyst_system_v2.md](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/prompts/intent_analyst_system_v2.md):
  - Added Rule 14: Mandated that relative/historical baseline thresholds (e.g. *"2x 6-month monthly average"*) are structured under `thresholds` and `mapped_keywords` rather than demoted to text in `semantic_conditions`.
  - Added Rule 15: Mandated explicit boolean expression blueprints in `detection_logic` (e.g. `Count >= 3 AND (Volume >= 50,000 OR Volume > 2 * Historical Baseline)`).
- Upgraded `_determine_combined_connectors()` in [agent.py](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/agent.py):
  - Added regex pattern matching on condition values and condition descriptions in `detection_logic` and raw SQL text.
  - Dynamically sets `combined_rule = "OR"` in `QBRuleDetail` when conditions are joined by `OR` in SQL `WHERE`/`HAVING` clauses.

### Why
- Fixes an issue where multi-period scenarios with relative volume thresholds (e.g. 50k volume OR 2x historical average) had all condition rows inserted as `AND` into `PIO_AML_RULES_DETAILS`, breaking the database evaluation logic.

### Verify
- Verified connector assignment with unit test `test_connector_logic.py` outputting `['AND', 'AND', 'OR', '-']`.

---

## [2026-07-23] fix: generalized <X> vs <Y> dynamic baseline rules & zero re-asking

### What
- Updated `intent_analyst_node` prompt in [agent.py](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/agent.py) and [intent_analyst_system_v2.md](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/prompts/intent_analyst_system_v2.md):
  - Formulated dynamic baseline rules using abstract `<X> vs <Y>` symbolic notation (`<Metric_X> > <Metric_Y>`, `<Metric_X> >= <Ratio_K> * <Baseline_Y>`, or `<Metric_X>` vs `<Historical_Period_Y>`) rather than hardcoded scenario strings.
  - Prohibited the Intent Analyst from setting `provenance: "needs_user"` or asking clarification questions requesting hardcoded numeric values for database-computed calculations `<Y>`.
  - Enforced a strict rule: if all material scenario requirements are provided in the user's prompt, `clarifications` MUST be empty (`[]`) and `ready_for_handoff` MUST be `true`.

### Why
- Prevents prompt overfitting where the LLM only recognizes specific hardcoded phrasing instead of generalizing to all dynamic baseline comparison patterns.

### Verify
- Verified prompt compilation and generalized rule structure.

---

## [2026-07-23] fix: subquery-aware SQL metadata parsing & fallback extraction

### What
- Implemented `_remove_parenthesized_subqueries()` in [agent.py](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/agent.py) to strip nested subqueries before parsing outer `WHERE`, `GROUP BY`, and `HAVING` clauses in `_parse_sql_metadata()`.
- Updated `_extract_sql()` in [agent.py](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/agent.py) to return the maximum length valid SQL block (`max(valid_blocks, key=len)`).
- Updated `sql_bridge_node` in [agent.py](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/agent.py) to automatically fall back to full streamed `content` if `final_answer` does not contain the SQL code block.
- Fixed non-code-block fallback parsing in `_extract_sql()` so semicolons inside subqueries do not truncate the query.

### Why
- Fixes query truncation caused by `sql_candidate.find(";")` slicing at the first semicolon inside a subquery.
- Fixes metadata extraction where subqueries in `SELECT` lists caused `re.search("WHERE ...")` to extract subquery `WHERE` clauses (e.g. `CUS_NUM`) instead of outer query clauses.
- Guarantees complete SQL extraction regardless of whether Service B emits the SQL inside `final_answer` or `content` SSE events.

### Verify
- Verified complete SQL extraction, subquery metadata isolation, and graph compilation.

---

## [2026-07-22] fix: SQL Bridge final answer prioritization & candidate extraction

### What
- Updated `sql_bridge_node` in [agent.py](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/agent.py) to prioritize the `final_answer` SSE event emitted by Service B over raw `content` stream chunks.
- Updated `_extract_sql()` in [agent.py](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/agent.py) to pick the **latest (last) valid SQL candidate** (`valid_blocks[-1]`) instead of `max(valid_blocks, key=len)`.

### Why
- Fixes an issue where intermediate critique rejection text (e.g., `CRITIQUE REJECTED: ...`) emitted by Service B during retry loops was appended to `collected_text` in Service A.
- Fixes an issue where `_extract_sql()` chose an older, rejected SQL query from intermediate critique feedback because its string length was longer than the final approved SQL query.

### Verify
- Verified SSE event handling, `final_answer` capture, and candidate extraction logic.

---

## [2026-07-22] upgrade: dedicated transaction_type field in AMLIntent contract

### What
- Added `transaction_type: Optional[str]` to `AMLIntent` schema in [schemas.py](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/schemas.py).
- Updated [agent.py](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/agent.py), [intent_analyst_system_v2.md](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/prompts/intent_analyst_system_v2.md), and [planner_system.md](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/prompts/planner_system.md):
  - Mandated that explicit transaction categories (e.g. `'LOAN SETTLEMENT'`, `'CASH DEPOSIT'`, `'OUTWARD TRANSFER'`) are extracted into `transaction_type` as uppercase strings.
  - Reserved `semantic_conditions` strictly for non-transaction-type logic (field-to-field comparisons, behavioral conditions, state transitions).

### Why
- Eliminates ambiguity where transaction types were mixed with field-to-field comparisons inside `semantic_conditions`, allowing Service B to deterministically enforce `EXPL_CODE` lookups without LLM guesswork.

### Verify
- Verified schema definition, graph compilation, and serialized intent structure.

---

## [2026-07-22] upgrade: autonomous single-turn multi-scenario transition protocol

### What
- Refactored `orchestrator_node` in [agent.py](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/agent.py) state wiping logic. When `clear_scenario_state` is `True`, `next_action` will preserve `"INTENT"` if the Orchestrator routed directly to `INTENT` (e.g. when the user included a new scenario description), rather than hardcoding `"WAIT_USER"`.
- Updated [orchestrator_system.md](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/prompts/orchestrator_system.md) prompt rules and Decision Table:
  - **Compound Request (Reset + Description):** Route straight to `INTENT` with `clear_scenario_state = true` and `message_to_user = null`. The pipeline instantly wipes state and outputs the new Plan in 1 turn.
  - **Command Only ("Start over"):** Route to `REDEFINE` with `clear_scenario_state = true` and prompt the user to enter their new scenario.

### Why
- Eliminates the redundant 2-turn reset protocol where the user had to read a prompt asking them to describe a new scenario when they had already described it in the same message turn.

### Verify
- Verified graph state compilation and seamless 1-turn transition routing.

---

## [2026-07-20] upgrade: Unified Semantic Handoff Protocol (USHP) in SQL Bridge

### What
- Refactored `sql_bridge_node` in [agent.py](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/agent.py) to replace the rigid, transaction-biased XML-style text prompt wrapper with a raw serialized JSON payload containing the `enriched_intent` structure under `SemanticIntentPayload`.
- Cleaned up hardcoded formatting logic, measure definitions, and grain assumptions from the orchestrator layer.

### Why
- Fixes the transaction-centric bias where Service A assumed and forced transaction measures/grains for all scenarios.
- Allows Service B to dynamically query the data dictionary and map entities based on the monitored domain (e.g. TRANSACTION, ACCOUNT, or CUSTOMER) without layout assumptions from Service A.

### Verify
- Verified graph state compilation and payload serialization.

---

### What
- Implemented **IN clause quoting fix**:
  - Refactored `_fetch_and_map_parameters` in [agent.py](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/agent.py) to remove the extra enclosing outer single quotes from `IN` clause value list. Writes raw double-single-quoted values (`''1'',''660''`) to the database which compile cleanly at runtime without inserting literal quote characters in search strings.
- Implemented **SQL subquery extraction fix**:
  - Refactored `_extract_sql()` in [agent.py](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/agent.py) to select the longest valid SQL candidate when extracting SQL from raw LLM responses.
  - This ensures that if the generated SQL contains a subquery (like matching cash withdrawals with deposits), the outer parent query is always extracted and parsed, rather than matching and extracting the inner subquery statement alone.
- Implemented **SQL parser semicolon stripping**:
  - Refactored `_parse_sql_metadata()` in [agent.py](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/agent.py) to strip all trailing semicolons and trailing whitespace from generated SQL query strings before parsing structural WHERE conditions.
  - Refactored `_fetch_and_map_parameters` in [agent.py](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/agent.py) to explicitly strip trailing semicolons from comparison value strings before removing quotes.

### Why
- Prevents trailing semicolons from generated SQL statements from being captured inside parameter values (e.g. saving `4';` instead of `4`). This ensures that dynamic query construction inside validation PL/SQL stored procedures does not fail with SQL syntax execution errors.

### Verify
- Verified graph compilation, model validation, and schema definitions using the `test_grounding.py` test suite.

---

## [2026-07-18] upgrade: orchestrator loop prevention, field-to-field comparison mapping, and type registration mapping

### What
- Implemented **Orchestrator-Intent loop prevention safeguards**:
  - Refactored `orchestrator_node` in [agent.py](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/agent.py) to intercept decisions when the previous node action was `CLARIFY`. Forces `next_action` to `WAIT_USER`.
  - Added a fallback builder to construct clarification question prompts if the LLM outputted an empty message.
  - Refined [orchestrator_system.md](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/prompts/orchestrator_system.md) to explicitly direct the routing engine to target `WAIT_USER` during the `CLARIFY` phase.
- Implemented **field-to-field comparison handling**:
  - Added **Core Principle #9** in [intent_analyst_system_v2.md](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/prompts/intent_analyst_system_v2.md) to route field comparisons (e.g. `transaction >= loan amount`) as `BusinessQualifier`s rather than numeric `Threshold`s.
  - Prevents validation errors and clarification loops requesting static numeric thresholds when none are required.
- Implemented **auto-provisioning column type mapping**:
  - Refactored `_provision_catalog_entry` in [agent.py](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/agent.py) to map physical Oracle database types (e.g. `NUMBER`, `VARCHAR2`, `DATE`) to their corresponding legacy catalog lookup codes (`'1'`, `'2'`, `'3'`) instead of raw type strings.

### Why
- Prevents infinite processing loops when the user is asked clarification questions.
- Stops the parser from repeatedly requesting threshold numbers for field-to-field comparisons.
- Keeps the metadata catalog consistent and fully aligned with the legacy database type lookup format.

### Verify
- Verified graph compilation, model validation, and schema definitions using the `test_grounding.py` test suite.

---

## [2026-07-16] upgrade: dynamic period/risk lookups, prompt alignment, environment isolation, mandatory time windows, qualifier deduplication, SQL extraction filtering, strict numeric validation, decomposer lookup safeguards, and SQL validation filtering

### What
- Updated `intent_analyst_node` in [agent.py](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/agent.py) to load the newer `intent_analyst_system_v2.md` system prompt.
- Refactored `decomposer_node` in [agent.py](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/agent.py) to query lookup tables `PIO_PERIOD_TYPE` and `PIO_AML_DEGREE_RISK` dynamically instead of hardcoding time windows and default risk levels.
- Implemented structured LLM grounding to semantically map intent time windows and classify scenario risk levels to corresponding database codes.
- Filtered all metadata queries, validation checks, and atomic rollbacks in `decomposer_node`, `verify_scenario_written`, and `_delete_scenario_atomic` on `COUNTRY_CODE` and `INST_CODE` to prevent environment crosstalk.
- Enforced **mandatory time windows** across all scenarios:
  - transactional/detail rules automatically default to a **1-day rolling window** (DAYS = 1, rolling = true) and list it under `applied_defaults`.
  - velocity/aggregate rules raise a structured `Clarification` question asking the user to specify their observation period.
- Implemented **qualifier deduplication**:
  - Directs the Intent Analyst to prune and remove descriptive qualifiers (e.g. "unexpected", "suspicious", "large") from the `qualifiers` list once they are successfully resolved into numeric thresholds (e.g. "> 10,000").
- Upgraded `_extract_sql()` in [agent.py](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/agent.py) to ignore non-SQL code blocks (logs, text critiques, JSON metadata) at the end of SSE streams.
- Enforced **strict numeric type validation** in `value_from` and `value_to` fields in [agent.py](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/agent.py) and [intent_analyst_system_v2.md](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/prompts/intent_analyst_system_v2.md) (Core Principle #8) to prevent float parsing errors from LLM string outputs.
- Added explicit **NoneType safeguards** in `decomposer_node` inside [agent.py](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/agent.py) to handle lookup keys returning `None`/`null` without throwing `TypeError: int() argument must be a string...`.
- Implemented **robust SQL candidate validation** using a helper function `_is_real_sql()` in [agent.py](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/agent.py) to distinguish actual SQL query blocks from English critique text starting with SQL keywords (like `"with"` or `"select"`).

### Why
- Dynamic metadata grounding replaces rigid if/else statements with database-driven lookups, enhancing portability across custom periods and risk degrees.
- Enforcing country and institution filters ensures strict isolation of scenarios, rules, and details in multi-tenant or multi-bank shared database instances.
- Mandatory time windows prevent massive database scans in production and alert duplication.
- Qualifier deduplication prevents downstream SQL agents (Service B) from searching for non-existent database columns representing already-resolved descriptive concepts.
- Filtering non-SQL blocks guarantees only the executable SQL statement is extracted and presented in the UI, avoiding displaying critique logs or text logs.
- Strict numeric validation prevents Pydantic model validation exceptions when LLMs erroneously put string field names or string placeholders in numeric fields.
- Decomposer lookup safeguards prevent casting exceptions when grounding lookups return null/None values, ensuring graceful fallbacks to initialized defaults.
- SQL candidate validation prevents plain English critique sentences beginning with words like "with" or "select" from being misidentified as SQL statements when Service B fails and outputs text critiques.

### Verify
- Verified LLM-grounded classifications, mandatory time window defaults (1-day default for transactional rules), qualifier deduplication, non-SQL block filtering, strict numeric validation, lookup safeguards, SQL validation filtering, and environment isolation using the upgraded `test_extraction.py` and `test_grounding.py` test suites.

## [2026-07-16] refactor: catalog cleanup and dynamic LLM grounding for parameters resolution

### What
- Deleted the duplicate legacy parameter record `200` from `PIO_AML_PARAMETERS` using a clean database script.
- Restructured `_fetch_and_map_parameters` in [agent.py](file:///c:/Users/abura/Development/AI_AML_AGENT/services/aml_builder/web/services/agent.py) to separate catalog mapping tables into `detail_column_map` (where `AGGREGATION_CODE = '1'`) and `aggregate_column_map` (where `AGGREGATION_CODE != '1'`).
- Enforced aggregation-aware mapping so that detail-level SQL queries (e.g. inside `WHERE` clauses) map only to Detail parameters (like `5` for amount) instead of Summation parameters (like `200`).
- Replaced the hardcoded Python keyword fallback parser (`"amount"`/`"count"` string matching) with a structured, catalog-grounded LLM invocation. The LLM matches intent fields directly against the table's parameter list fetched dynamically from the database.

### Why
- Having duplicate parameter codes (`200` and `6` both configured as Summation) caused the query parser to map detail amount checks to summation parameter `200` because the catalog loader did a flat dictionary overwrite. 
- Writing parameter `200` instead of detail parameter `5` caused the Query Builder evaluation engine to return 0 alerts because the rule detail didn't match the query execution grain.
- Naive string matching on `"amount"` could match arbitrary fields (like `my_dick_amount`) and map them incorrectly to parameter `5`. Structured grounding prevents this entirely.

### Verify
- Verified database deletion of parameter `200`.
- Verified parameter resolution maps `WHERE TRA_AMT > 10000` to parameter `5` (Detail) using a python grounding test script.

## [2026-07-07] fix: prevent Next.js fetch caching on polling status and history

### What

- Added `export const dynamic = 'force-dynamic'` to the Route Handlers `/api/chat/status/route.ts` and `/api/chat/history/route.ts` to disable Next.js static page generation and route caching.
- Updated the backend query dispatcher utility `tryAgentPorts` inside `src/lib/agentFetch.ts` to use `{ cache: 'no-store' }` on all `fetch` requests to prevent the BFF proxy server from serving cached agent status responses.
- Appended a dynamic timestamp query parameter `&t=${Date.now()}` to client-side fetches for `history` and `status` in `src/app/chat/[sessionId]/page.tsx` to force browsers and middleware/proxies to bypass the cache.
- Modified the BFF `POST /api/chat` route to return a `202 Accepted` response if `SCENARIO_BUILDER` returns either a `200` or `202`, preventing downstream proxy rewrites from hiding the async status.
- Updated `page.tsx` client message submit logic to automatically trigger `startPollingStatus` when `selectedModel` is `'SCENARIO_BUILDER'` or response status is `202`.

### Why

- Next.js 13/14+ aggressively caches `fetch` calls and GET routes by default. This caused status polling to return cached `RUNNING` or `idle` states, rendering the poll box static or failing to update the UI on scenario completion/failure.
- Proxy and network layers (like corporate firewalls or load balancers) can normalize `202 Accepted` statuses to `200 OK`, which prevented the frontend from entering the status polling branch. Explicitly mapping `SCENARIO_BUILDER` requests guarantees polling starts.

### Verify

- Verified that status route and history route are dynamic, client-side requests bypass cache, and polling starts correctly regardless of normalized `200 OK` statuses.

## [2026-07-04] refactor: WHAT-only Service B handshake + schema version

### What

Rewrote the `<AML_SCENARIO_REQUEST>` prompt built in `sql_bridge_node`
(`services/aml_builder/web/services/agent.py`) so it carries **business intent only**
and no SQL implementation instructions. PioTech AI (Service B) is an LLM layer, so the
message is free-form text it can consume without a structured parser.

- Removed all HOW-leakage: `Use GROUP BY`, `Use HAVING`, `Apply time filters using
  SYSDATE arithmetic`, `Use ONLY BI_DWH schema tables`, and the numbered SQL recipe.
- Replaced the imperative `EVALUATION_MODE` ("Do NOT use GROUP BY…") with a semantic
  `EVALUATION_GRAIN` (per-transaction vs per-<grain>) and a `MEASURE` line. Service B
  now decides GROUP BY/HAVING itself from the grain.
- `TIME_WINDOW` → semantic `OBSERVATION_WINDOW` (business fact, no date mechanics).
- Added `REQUEST_SCHEMA: aml-intent/v2` version marker and an explicit WHAT/HOW
  ownership note in the message header.
- Renamed labels to intent language: `SCENARIO_TYPE`→`MONITORED_DOMAIN`,
  `THRESHOLDS`→`BUSINESS_CONDITIONS`, plus an `OBJECTIVE` that asks for the alert
  identifier without dictating the SELECT shape.

### Why

Enforces the WHAT/HOW service boundary: the intent service states business meaning;
the text-to-SQL service owns schema binding and query mechanics. Prevents the earlier
class of bug where Service A's boilerplate ("Use GROUP BY if counting") nudged Service B
into the wrong query shape.

### Verify

Rendered both a per-transaction and a cumulative (aggregate) intent through the new
builder — per-transaction emits a per-transaction grain with no window; cumulative emits
`MEASURE: sum of …`, grain "Per Customer per rolling 30 days", and the observation
window. No GROUP BY/HAVING/SYSDATE/schema strings in either.

### Known follow-up / weakness

`AMLIntent` has a single `aggregation` object + a flat `thresholds` list, so a mixed-grain
scenario ("more than 5 deposits **each** over 9,000") cannot cleanly express that one
threshold is per-transaction and another is aggregate. Candidate fix: per-threshold
`applies_to` grain, or split `row_conditions` vs `aggregate_conditions`.

## [2026-07-03] feat: ambiguity-aware Intent Analyst (additive v2 schema)

### What

Strengthened the Intent Analyst so it detects and surfaces semantic ambiguity
before handing off to the PioTech text-to-SQL agent, instead of silently forwarding
under-specified scenarios. Additive change — no existing `AMLIntent` field renamed,
so the PioTech handoff contract and all existing consumers keep working.

**`services/aml_builder/web/services/schemas.py`**
- Added optional `provenance` (`stated` | `assumed_default` | `needs_user`) to
  `Threshold` and `TimeWindow`.
- New models: `AggregationProfile` (metric + measure function + grain),
  `BusinessQualifier` (open-ended plain-English non-numeric filters, e.g.
  "is domiciled in a high-risk country"), and `Clarification` (structured, business
  ambiguity with question + options).
- New `AMLIntent` fields: `aggregation`, `qualifiers`, `clarifications`,
  `applied_defaults`, `ready_for_handoff`. Legacy `clarification_needed` /
  `clarification_questions` retained for backward compatibility.

**`services/aml_builder/web/services/agent.py`**
- Intent-analyst: extended inline JSON schema template with the new fields +
  provenance/clarification rules; routing now gates on `ready_for_handoff` (falls
  back to legacy `clarification_needed`).
- `sql_bridge_node`: PioTech prompt now also carries `AGGREGATION` (grain/measure),
  `QUALIFIERS` (plain-English predicates to ground against the dictionary), and
  `ASSUMED_DEFAULTS` (what the intent layer inferred vs. what the user stated).
- Orchestrator status formatter: surfaces structured `clarifications` and a new
  "Defaults Assumed (confirm or correct)" block.

**Prompts**
- New `prompts/intent_analyst_system_v2.md`: senior-AML-analyst framing, an
  open-ended extraction rule (no hardcoded domain enums), a finite reasoning
  checklist of recurring transaction-monitoring dimensions, stated/defaulted/ambiguous
  discipline, and two worked examples (ambiguous "transacting >10k"; clean structuring
  rule). Left as `_v2` alongside the current prompt for review before swap.
- `prompts/planner_system.md`: Assumptions section now driven by `applied_defaults` +
  aggregation grain; Risk Flags section driven by unresolved `clarifications` and
  non-`stated` provenance values.

### Why

The old Intent Analyst was a shallow JSON extractor with no domain awareness: it only
asked for missing basic fields and forwarded semantically ambiguous requests (e.g. no
transaction direction/channel/grain), producing technically-valid but wrong queries.
The v2 design holds the finite/infinite boundary deliberately — grammar (operators,
time units, provenance) is enumerated; domain entities (subjects, predicates, metrics,
grains) stay open-ended plain English for the SQL agent to ground. It notices what is
under-specified, defaults what is safe (and discloses it), and asks only about material
ambiguities — rather than silently guessing.

### How to run / verify

- Schema additions validated by standalone round-trip (legacy dict still parses; full
  v2 payload round-trips). `python3 -m py_compile web/services/agent.py` passes.
- To activate the new prompt: review `prompts/intent_analyst_system_v2.md`, then either
  rename it over `intent_analyst_system.md` or point `_load_prompt` at it.
- Follow-up (not done here): the orchestrator should iterate the structured
  `clarifications` back to the user when `ready_for_handoff` is false.

## [2026-06-30] refactor: fully agentic orchestrator — zero hardcoded routing logic

### What

Complete rewrite of `orchestrator_node` in `services/aml_builder/web/services/agent.py`.

**Before:** ~300-line `if/elif` tree with hardcoded keyword lists (`APPROVAL_KEYWORDS`, `"redefine"`, `"adjust"`), hardcoded message templates (welcome, failure menu, success summary, clarification prompt), and two separate LLM classification micro-calls for greeting detection and approval detection.

**After:** A single `with_structured_output(OrchestratorDecision)` LLM call. The LLM receives the full conversation history and a serialized state context table, then produces `{next_action, message_to_user, clear_scenario_state}`. Python executes only three mechanical side effects: clearing 13 state fields on `REDEFINE`, setting `plan_approved=True` on `SQL_BRIDGE`, and calling `_generate_escalation_report()` on `ESCALATE`.

#### Key changes

- **`OrchestratorDecision` Pydantic model** added to `schemas.py` — three fields: `next_action` (10-value Literal), `message_to_user` (Optional[str]), `clear_scenario_state` (bool). Used as `with_structured_output` target.
- **`orchestrator_system.md`** fully rewritten — comprehensive decision guide with: role definition, state field glossary, per-`next_action` decision rules with triggers and examples, a full transition decision table, and message writing guidelines (greetings, failure menu, clarifications, success summary, adjustment requests). The LLM writes all messages from scratch.
- **`_build_state_context()`** new helper — serializes all relevant state fields into a readable markdown table injected into every LLM call. Includes: pipeline phase, intent, plan status, write integrity, validation counts, density ratio, confidence, error log (last 3), threshold sensitivity, and sample customer rows for FINALIZE.
- **`_finalize_response()`** removed — LLM writes the success message with full context from state.
- **`_format_failure_menu()`** removed — LLM writes the failure menu in context, translating technical errors into business language.
- **`route_after_orchestrator()`** simplified to 6 routes — the orchestrator resolves all intermediate states internally before returning, so the router only handles `INTENT`, `SQL_BRIDGE`, `DECOMPOSE`, `WAIT_USER`, `WAIT_APPROVAL`, `END`.
- **`route_after_sql_bridge()`** and **`route_after_decomposer()`** updated — now handle `FAILURE` same as `ERROR` (both route to orchestrator).
- **`route_after_validator()`** simplified — only `DECOMPOSE` goes to decomposer; everything else (FINALIZE, FAILURE, ERROR) goes to orchestrator.

#### What the LLM now decides autonomously

- When to greet vs. when to route silently to intent analyst
- When a user message is an approval vs. a question vs. a rejection
- When to ask clarification questions (business language only, max 3)
- What failure options to present, translated from technical errors
- Full success summary with real alert counts, density ratio, sample table
- Whether a user "adjust" message contains specific values (→ DECOMPOSE) or is still vague (→ WAIT_USER)
- Whether a rejection of the plan is a threshold change (→ ADJUST) or a full restart (→ REDEFINE)

### Files Changed
| File | Change |
|---|---|
| `web/services/agent.py` | Rewrote `orchestrator_node`; added `_build_state_context`; removed `_finalize_response`, `_format_failure_menu`; updated routing functions |
| `web/services/schemas.py` | Added `OrchestratorDecision` model |
| `web/services/prompts/orchestrator_system.md` | Complete rewrite — agentic decision guide |

---

## [2026-06-30] feat: v2 production-grade upgrades — catalog auto-provisioning, atomic writes, plan-first execution

### What

Five production-grade upgrades to `services/aml_builder/`:

#### Upgrade 1 — Catalog Auto-Provisioning (`agent.py`)
- When the decomposer finds a SQL column not in `PIO_AML_PARAMETERS` / `PIO_AML_COLUMNS`, it now self-provisions the catalog via a 3-step idempotent INSERT sequence: `PIO_AML_TABLES` → `PIO_AML_COLUMNS` → `PIO_AML_PARAMETERS`.
- `COLUMN_TYPE` is always queried from Oracle's `ALL_TAB_COLUMNS` (never LLM-inferred).
- `COLUMN_BUSINESS_NAME`, `COLUMN_BUSINESS_NAME_NAT`, and `AGGREGATION_CODE` are inferred by the LLM using a fast model call.
- `get_next_numeric_code()` added to `oracle.py` — uses `REGEXP_LIKE` guard before `TO_NUMBER` to avoid `ORA-01722`.
- New helpers: `_query_oracle_column_type`, `_infer_column_metadata`, `_provision_catalog_entry`, `_verify_catalog_integrity`.
- All auto-created entries tracked in `catalog_creations: List[Dict]` state field and reported in the escalation report.

#### Upgrade 2 — Enriched Validation (`agent.py`, `schemas.py`)
- `validator_node` now queries both `PIO_AML_CUSTOMERS` (header) and `PIO_AML_CUSTOMERS_DET` (transaction detail).
- New success gate: **both** tables must have ≥ 1 row AND `write_integrity.all_pass` must be `True`.
- New metrics: `alert_density_ratio` (DET rows / header rows), `det_count`, `det_samples` (dynamic columns via `cursor.description`).
- `_check_threshold_sensitivity(rule_code)` — when `alert_count == 0`, reads numeric `COMPARISON_VALUE_FROM` values and reports what a ±20% adjustment would look like (read-only, advisory only).
- New `ValidationResult` fields: `det_count`, `det_samples`, `alert_density_ratio`, `write_integrity`, `catalog_integrity`, `threshold_sensitivity`.
- Terminal validation failures route to `FAILURE` (not `ERROR`) to trigger the structured menu.

#### Upgrade 3 — Atomic Oracle Writes (`oracle.py`, `agent.py`)
- `atomic_connection()` context manager added to `oracle.py` — single `conn.commit()` on success, `conn.rollback()` on any exception.
- All 4 QB table INSERTs (`PIO_AML_SCENARIO`, `PIO_AML_RULES`, `PIO_AML_SCENARIO_RULES`, `PIO_AML_RULES_DETAILS`) now execute on one shared cursor inside a single transaction.
- New cursor-based helpers: `_insert_scenario_cursor`, `_insert_rule_cursor`, `_insert_scenario_rule_cursor`, `_insert_rule_details_cursor` (use `cursor.executemany` for bulk detail rows).
- `_delete_scenario_atomic(cursor, scenario_code)` replaces `_delete_scenario_if_exists()` — runs inside the same transaction.
- `_verify_write_integrity(scenario_code, rule_code, expected_detail_rows)` — post-commit COUNT check across all 4 tables, returns `WriteVerification` object.
- `_extract_table_from_oracle_error(error_str)` — parses error messages to include the offending table name in failure output.

#### Upgrade 4 — Structured Failure UX (`agent.py`, `schemas.py`)
- New routing state `FAILURE` → `WAIT_USER` — instead of silent `ERROR`, terminal failures now show a 3-option menu.
- `_format_failure_menu(error_detail)` — emits a formatted menu: **1. Redefine / 2. Adjust thresholds / 3. Escalate**.
- `orchestrator_node` detects user choice (1/redefine, 2/adjust, 3/escalate) and routes accordingly:
  - `REDEFINE` → clears 11 state fields, routes back to `intent_analyst`
  - `ADJUST` → prompts for new values, routes to `decomposer`
  - `ESCALATE` → calls `_generate_escalation_report(state)`, emits full markdown technical report
- `_generate_escalation_report` outputs intent JSON, error log, catalog creations, parameters, write verification, and validation results — no external ticket system.
- New state fields: `escalation_report: Optional[str]`, `failure_mode: Optional[str]`.
- New SSE event type: `escalation_report`.

#### Upgrade 5 — Plan-First Execution with Approval Gate (`agent.py`, `schemas.py`, `main.py`)
- New `planner_node` inserted between `intent_analyst` and `sql_bridge`.
- LLM generates a two-part output from `planner_system.md` prompt: an 8-section user-facing markdown plan (Overview, Filters, Rules, Time Interval, Customer Scope, Assumptions, Parameter Mapping Preview, Risk Flags) and a machine-readable `CONDITIONS_BLOCK` JSON array.
- Plan is streamed as `plan_artifact` SSE event to the frontend side panel.
- Graph enters `WAIT_APPROVAL` state after the plan is emitted — execution halts until the user sends an approval keyword (`proceed`, `yes`, `approve`, `go ahead`, `confirm`, `start`, `execute`).
- `_assert_plan_drift(plan_conditions, rule_details)` — runs at the start of `qb_writer_node` before any Oracle connection is opened. Diffs final `rule_details` against the approved `plan_conditions`. On mismatch, raises `PlanDriftError` and routes to `FAILURE` without touching the database.
- New models in `schemas.py`: `PlanCondition`, `CatalogCreation`, `WriteVerification`.
- New SSE event type: `plan_artifact`.
- New routing states: `WAIT_APPROVAL`, `FAILURE`, `WAIT_USER`, `REDEFINE`, `ADJUST`, `ESCALATE`.

### Files Changed
| File | Type |
|------|------|
| `web/services/agent.py` | Major refactor — new nodes, helpers, routing states |
| `web/services/schemas.py` | New models: `PlanCondition`, `CatalogCreation`, `WriteVerification`; extended `ValidationResult`; updated `SSEEvent` |
| `web/services/oracle.py` | Added `atomic_connection()`, `get_next_numeric_code()` |
| `web/services/prompts/planner_system.md` | New file — planner LLM system prompt |
| `web/api/main.py` | `is_resuming` state check; `plan_artifact` + `escalation_report` SSE emission |

### Graph After v2
```
orchestrator → intent_analyst → planner → [WAIT_APPROVAL]
                                               ↓ (user: "proceed")
                                           sql_bridge → decomposer → qb_writer → validator
                                                                          ↓ (PlanDriftError)
                                               ↑ ← FAILURE menu ← WAIT_USER ←────────────┘
```

---

## [2026-06-30] feat: Added statistical standard deviation (STDDEV) parameter mapping support

### What
- **Session & History Management Endpoints**: Ported 5 session-management API endpoints to the AML Builder backend (`main.py`) matching the frontend workspace expectations:
  - `GET /chat/{project_id}/{chat_id}/{user_id}/history` (Loads LangGraph state messages using `graph.aget_state` and filters out internal coordination logs).
  - `GET /chats/user/{user_id}/list` (Lists active chat sessions from SQLite database).
  - `DELETE /chat/{project_id}/{chat_id}/{user_id}` (Soft deletes sessions by setting `is_deleted = 1`).
  - `PUT /chat/{project_id}/{chat_id}/{user_id}/rename` (Updates session titles).
  - `PUT /chat/{project_id}/{chat_id}/{user_id}/pin` (Toggles the `is_pinned` status).
- **SQLite Metadata Sidebar Sync**: Created a `chat_sessions` table in the checkpointer database (`checkpoints.sqlite`) initialized on startup to track chat headers, titles, pin states, and soft deletion flags.
- **Standard Deviation (STDDEV) Parsing**: Added support to parse and map Oracle `STDDEV` or `STDDEV_SAMP` functions in SQL aggregate HAVING conditions. The decomposer now extracts standard deviation percentage multipliers (e.g. `0.1 * STDDEV(...)` $\rightarrow$ `from_param_perc = 10`) and dynamically activates the compliance engine's `USE_SD_FLAG = '1'` and `SD_PERIOD_TYPE = '1'` (1-year history) metadata flags.
- **Intent SD Detection**: Integrated standard deviation keyword detection inside intent thresholds to capture statistical constraints directly from structured user requests.

## [2026-06-29] fix: Enforced purely numeric RULE_CODE format and added duplicate scenario prevention

### What
- **Column Name Aliases**: Implemented a column name aliasing dictionary (e.g., `TRA_AMT` $\rightarrow$ `EQU_TRA_AMT`, `TRANS_TYPE` $\rightarrow$ `EXPL_CODE`) inside `_fetch_and_map_parameters` before querying the dynamic compliance catalog mapping. This resolves errors where physically present columns in `PIO_AML_COLUMNS` are not linked to parameter codes in `PIO_AML_PARAMETERS`.
- **Live Catalog Parameter Mapping**: Replaced the hardcoded regex column checks in `_fetch_and_map_parameters` with a dynamic database query joining `PIO_AML_PARAMETERS` and `PIO_AML_COLUMNS` at runtime. The system now maps SQL conditions dynamically to parameter codes based on the live database schema definition.
- **Unsupported Column Safeguard**: Implemented a security guard in the decomposer that raises a `ValueError` if a query filters on a column not registered in the Query Builder's `PIO_AML_COLUMNS` catalog. This prevents the generation of broken or mutated rules in the database and triggers the self-correction rewrite loop.
- **Mapping Deduplication**: Prevented duplicate summation/count rule rows by prioritizing the parsed SQL `HAVING` clause conditions and skipping them from intent thresholds.
- **Known Issues documentation**: Created [known_issues.md](file:///c:/Users/abura/Development/AI_AML_AGENT/Docs/known_issues.md) inside the `Docs/` directory, detailing stored procedure limitations (hardcoded scenario filter in `FILL_PIO_AML_CUSTOMERS`) and test database constraints (missing customer profiles in `PIO_CUSTOMERS`).
- **HAVING COUNT column argument parsing**: Fixed a mapping bug by updating the regex pattern in `_fetch_and_map_parameters` from matching strictly `COUNT(*)` to allowing any column argument (e.g. `COUNT(L.LOAN_KEY)`), allowing column-specific count queries to successfully parse.
- **Flexible parameter mapping**: Updated the decomposer's parameter mapping checks from strict equality matches to robust substring checks (e.g. `"amount" in field` and `"count" in field`), allowing domain-specific columns (like `loan_amount` or `loan_count`) to map correctly to Query Builder codes.
- **Empty Scenario Prevention**: Added validation checks in `decomposer_node` to throw a clean exception if `rule_details` is empty, avoiding writes of empty invalid scenarios.
- **Validation Failure Warning**: Updated `_finalize_response` to show warnings and suggested fixes when validation fails.
- **Clarification Loop Fix**: Resolved an issue where the Intent Analyst repeatedly asked the same clarification questions by refactoring `intent_analyst_node` to pass the entire conversation history as native chat messages to the LLM (instead of sending just the last message as a standalone request), allowing the LLM to understand context and compile clarifications.
- **Constraint Violation Fix (ORA-00001)**: Fixed a primary key violation on `PIO_AML_RULES_DETAILS` by updating the cleanup logic in `_delete_scenario_if_exists` to delete condition rows using the `RULE_CODE` linked via a subquery on `PIO_AML_SCENARIO_RULES` instead of filtering by the null-valued `SCENARIO_CODE` column.
- **Graceful Error Handling**: Resolved a crash when PioTech AI failed to generate a SQL query by wrapping `decomposer_node` in a try-except block and adding `route_after_sql_bridge` conditional routing, allowing the graph to route cleanly to the orchestrator and halt instead of throwing unhandled Pydantic validation exceptions.
- **Infinite Loop Fix**: Resolved an infinite routing loop by changing the validator bypass action from `"ORCHESTRATOR"` to `"ERROR"` when `scenario_write_success` is `False`. This allows write failures to correctly bubble up, print the error details, and safely terminate the execution via `"END"`, instead of falling through the orchestrator to route back to `intent_analyst` autonomously.
- **Purely Numeric `RULE_CODE`**: Changed the rule identifier generation pattern from character-prefixed `RL_YYYYMMDDHHMMSS_XXXX` to a purely numeric string (`YYYYMMDDHHMMSS` + 3-digit random suffix), adhering to compliance engine constraints.
- **Code Reuse**: Updated the decomposer node to check for and reuse existing `scenario_code` and `rule_code` from the state, preventing new codes from being generated during validation retry loops.
- **Targeted Deletions**: Implemented `_delete_scenario_if_exists` in `qb_writer_node` to purge existing records for the current scenario from all 4 schema tables (`PIO_AML_RULES_DETAILS`, `PIO_AML_SCENARIO_RULES`, `PIO_AML_RULES`, and `PIO_AML_SCENARIO`) prior to rewriting, guaranteeing exactly one clean record and avoiding constraint failures.

## [2026-06-28] fix: Schema-correct QB scenario ingestion and parameter mapping

### What
Fixed the **Decomposer** and **QB Writer** nodes to generate and insert database records matching the exact schema requirements and defaults of the reference template:
- Added 14 missing fields in `QBScenario` (e.g. `run_flag`, `group_by_flag`, etc.).
- Added 7 missing fields in `QBRule` and corrected naming (e.g. `rule_desc_arb`).
- Added 6 missing fields in `QBRuleDetail` (e.g. `from_param_perc` for summation rows).
- Added `frequency_days` to `QBScenarioRule`.
- Fixed values: `active_flag` maps to `'1'` (not `'Y'`), `violation_level` to `'1'`, `degree_risk_flag` to `'D'`.
- Replaced fictional parameter mapping dictionary with a live database-backed `_fetch_and_map_parameters` query of `PIO_AML_PARAMETERS` combined with structured rules to match transaction types (`EXPL_CODE` -> `1`), count (`2`), and summation (`6`).
- Unified period representation using `PIO_PERIOD_TYPE` lookup codes (e.g., `'0'` for Last n Days, `'3'` for Monthly).
- Resolved uvicorn async checkpointer crash by awaiting `aget_state()` inside `main.py`.
- Configured a local **`logs/`** folder inside `logging_config.py` that records app.log.
- Dynamically loaded `.env` inside `run_dev.ps1` to automatically export LangSmith settings to terminal sessions.
- Made `scenario_type` a **dynamic string category** (releasing hardcoded TRANSACTION/ACCOUNT/CUSTOMER rules) to support domain queries like `DEBIT` or `COLLECTION`. Dynamic LLM routing handles clarification logic.

### Why
The `FILL_PIO_AML_CUSTOMERS` stored procedure failed or generated no alerts due to missing schema fields and invalid parameter code references in `PIO_AML_RULES_DETAILS`. These fixes guarantee 100% database-compliant inserts matching the implementation team's reference. Awaiting the checkpointer state and loading variables dynamically ensures tracing is fully functional when running manually or using scripts.

---

## [2026-06-18] feat: Full Phase 1–4 implementation — complete agent system

### What
Built the full LangGraph multi-agent system for autonomous AML scenario creation,
covering all phases from architecture to working code.

### Why
All architectural blockers were resolved by the implementation team. The system
can now be tested end-to-end via Postman + LangSmith before UI integration.

### Files Created

#### `services/aml_builder/` — New service module
| File | Purpose |
|------|---------|
| `web/services/settings.py` | Pydantic-settings config (LLM, Oracle, AML defaults, PioTech AI URL) |
| `web/services/schemas.py` | All Pydantic contracts: AMLIntent, SQLMetadata, QBScenario, QBRule, QBRuleDetail, QBScenarioRule, ScenarioParameters, ValidationResult, SSEEvent |
| `web/services/oracle.py` | Oracle connection pool, run_readonly, run_write, run_write_many, call_procedure |
| `web/services/logging_config.py` | Structured JSON logging (production) + readable format (debug) |
| `web/services/agent.py` | Full LangGraph StateGraph — 6 nodes wired with conditional routing |
| `web/api/main.py` | FastAPI app — `/chat/stream` SSE endpoint + `/health` |
| `web/services/prompts/orchestrator_system.md` | Orchestrator persona & lifecycle prompt |
| `web/services/prompts/intent_analyst_system.md` | Intent extraction rules & schema |
| `requirements.txt` | All Python dependencies |
| `.env.example` | Full environment variable template |

### Agent Architecture Implemented
```
orchestrator → intent_analyst → sql_bridge → decomposer → qb_writer → validator
                    ↑                                           ↓ (retry loop)
                    └───────────────────────────────────────────┘
```

### Oracle Tables Written By The System
1. `PIO_AML_SCENARIO` — scenario header
2. `PIO_AML_RULES` — rule definition (period, type)
3. `PIO_AML_SCENARIO_RULES` — scenario-rule linkage
4. `PIO_AML_RULES_DETAILS` — individual conditions (batch insert)

### Procedure Executed By Validator
`FILL_PIO_AML_CUSTOMERS` → alerts land in `PIO_AML_CUSTOMERS` → filtered by `AML_SCENARIO_CODE`

### How To Run

#### On Windows (PowerShell):
```powershell
.\run_dev.ps1
```

#### On Linux/macOS/Git Bash:
```bash
export PYTHONPATH=services/aml_builder
uvicorn web.api.main:app --reload --port 8005 --host 0.0.0.0
```

### Test via Postman
```
POST http://localhost:8005/chat/stream
Content-Type: application/json

{
  "messages": [{"role": "user", "content": "Flag customers doing more than 5 cash transactions above JD 5000 in 30 days"}],
  "metadata": {"user_id": "test_user", "chat_id": "test_001", "project_id": "0"},
  "reasoning_mode": "instant"
}
```

### Key Configuration Parameters (.env)
| Variable | Default | Notes |
|----------|---------|-------|
| `ORACLE_DSN` | — | **Required** — your Oracle connection string |
| `ORACLE_USER` / `ORACLE_PASSWORD` | — | **Required** |
| `OPENAI_API_KEY` | — | **Required** for `LLM_PROVIDER=openai` |
| `AML_COUNTRY_CODE` | `400` | Jordan country code |
| `AML_INST_CODE` | `1` | Institution code |
| `PIOTECH_AI_URL` | `http://localhost:8001/chat/stream` | DWH agent endpoint |
| `MAX_VALIDATION_RETRIES` | `3` | Self-correction cycles before escalating |
| `LANGCHAIN_TRACING_V2` | `false` | Set `true` to enable LangSmith traces |

---

## [2026-06-17] docs: Architecture proposals

- `docs/01_architecture.md` — Full agent system architecture (6-node LangGraph)
- `docs/02_architecture.md` — Interface architecture (LibreChat + SSE adapter)

---

## [2026-06-17] chore: Initial repository setup

- Git repository initialized and pushed to `git@github.com:Pio-DataScience/ai-aml-agent.git`
- `.gitignore`, `README.md`, `.vscode/settings.json` added
