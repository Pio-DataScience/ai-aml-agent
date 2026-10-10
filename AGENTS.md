# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

---

## Canonical project overview

[`PROJECT_OVERVIEW.md`](PROJECT_OVERVIEW.md) is the canonical repository-level technical overview. Read it after the applicable `AGENTS.md` instructions when work requires project architecture, business logic, cross-cutting workflows, repository structure, or system-wide behavior.

Use the overview for navigation and context, not as a substitute for inspecting the implementation relevant to the task. Current code, tests, schemas, and configuration remain the ultimate source of truth.

### Keep it synchronized

Update `PROJECT_OVERVIEW.md` in the same change when a material change affects documented architecture, module boundaries, entry points, major abstractions, business rules, data models or persistence, important workflows, APIs or external integrations, authentication/authorization, configuration or runtime behavior, background processing, build/test/deployment behavior, or important invariants. Do not update it for trivial implementation details that do not alter the useful project mental model.

When updating it:

1. Verify the new description against the implementation.
2. Edit the affected sections instead of appending disconnected notes.
3. Remove or correct claims made obsolete by the change.
4. Keep paths and symbol references current.
5. Distinguish verified behavior from genuine uncertainty.
6. Avoid temporary details unless future work materially depends on them.

This maintenance contract is intended to prevent the canonical overview from drifting into another stale documentation artifact.

---

## Commands

### Run the service (recommended)

```powershell
.\run_dev.ps1
```

This script sets `PYTHONPATH`, loads `.env`, creates `artifacts/` for SQLite checkpoints, and starts uvicorn on port 8005.

### Manual run (all platforms)

```powershell
# PowerShell
$env:PYTHONPATH = "services\aml_builder"
uvicorn web.api.main:app --reload --port 8005 --host 0.0.0.0
```

```bash
# Bash
PYTHONPATH=services/aml_builder uvicorn web.api.main:app --reload --port 8005 --host 0.0.0.0
```

### Install dependencies

```bash
pip install -r requirements.txt
```

### Verification and tests

```powershell
# Safe structural verification. The DEBUG override avoids collisions with
# unrelated ambient DEBUG variables and applies only to this shell process.
$env:DEBUG = "false"
.\.venv\Scripts\python.exe -m compileall -q services app.py run_alert_engine.py
.\.venv\Scripts\python.exe -m pytest -q services\aml_builder\tests\test_deployment_guard.py
.\.venv\Scripts\python.exe -m pytest --collect-only -q
```

The deployment-guard suite is offline and deterministic. The registry analytics file remains a manually run live integration check that requires configured Oracle/OpenAI access and may query live systems:

```powershell
$env:DEBUG = "false"
.\.venv\Scripts\python.exe services\aml_builder\tests\test_registry_analytics.py
```

### API docs

`http://localhost:8005/docs` — available once service is running.

---

## Architecture

The service is a single autonomous **LangGraph ReAct tool-driven agent** exposed via a FastAPI SSE endpoint (`POST /chat/stream`). All source code lives under `services/aml_builder/`; the development launcher exposes `web.api.main` through `PYTHONPATH`, while implementation imports generally use the full `services.aml_builder.*` package path. There is no multi-node graph and no routing logic in Python — the LLM decides tool invocation order via a goal-oriented system prompt. Use [`PROJECT_OVERVIEW.md`](PROJECT_OVERVIEW.md) for current architecture; [`Docs/01_architecture.md`](Docs/01_architecture.md) and [`Docs/02_module_layout_refactor.md`](Docs/02_module_layout_refactor.md) are historical context.

### The six tools (`web/services/tools.py`)

| Tool                                              | Role                                                                                                                                                                                                                                                      |
| ------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `analyze_intent_and_discover_explanation_codes` | Calls OpenAI to convert natural language into a structured`AMLIntent` JSON object; runs vector similarity search over `PIO_EXPLANATION_CODE` to discover domain explanation codes; seeds `PIO_AML_SCENARIO` governance metadata from the intent.    |
| `generate_scenario_execution_plan`              | Deterministic (zero-LLM) markdown renderer producing the side-panel implementation plan.                                                                                                                                                                  |
| `execute_oracle_dwh_shadow_test`                | Calls the**external PioTech AI DWH agent** (`PIOTECH_AI_URL`) via HTTP SSE to get production Oracle SQL, then shadow-tests it (`SELECT COUNT(*) FROM (...)`) against `BI_DWH` (via the dedicated shadow connection pool).                     |
| `prepare_scenario_metadata_for_persistence`     | Deterministic + live Oracle lookups: builds the`PIO_AML_SCENARIO` field catalog (risk degree, category, etc.) with currently valid options, reports missing mandatory fields.                                                                           |
| `persist_and_validate_scenario_in_dwh`          | Validates the merged metadata, then atomically writes the confirmed scenario into `PIO_AML_PRODUCTION_SCENARIOS` (consumed by the on-demand alert engine) **and** `PIO_AML_SCENARIO` (business metadata for downstream compliance modules) in one transaction. |
| `query_production_scenario_registry`            | Read-only lookup over already-persisted scenarios: `semantic_search` (live per-query embeddings, no cache), `get_statistics`, `get_alert_metrics`, `get_scenario_detail`. Independent of the sequential creation workflow above.                        |

### Key modules

- **`web/services/tools.py`** — the six `@tool` definitions above.
- **`web/services/graph.py`** — compiles the ReAct agent (`get_tool_driven_graph`), manages the SQLite checkpointer lifecycle (`close_checkpointer`).
- **`web/services/plan_renderer.py`** — the 11-section markdown plan builder.
- **`web/services/scenario_metadata.py`** — `PIO_AML_SCENARIO` field registry: seeding from `AMLIntent`, live lookup-table fetching, and the final validation guard-rail before persistence (see `Docs/PIO_AML_SCENARIO_schema_guide.md`).
- **`web/services/scenario_registry_analytics.py`** — read-only search/analytics over persisted scenarios backing `query_production_scenario_registry` (semantic search, statistics, alert telemetry, scenario detail).
- **`web/services/sql_extraction.py`** — pulls clean SQL out of the PioTech AI SSE response text.
- **`web/services/llm_client.py`** — shared `build_llm()`/`safe_parse_json()`.
- **`web/services/explanation_code_search.py`** — vector similarity RAG search over `PIO_EXPLANATION_CODE`.
- **`web/services/production_registry.py`** — atomic dual-writer for `PIO_AML_PRODUCTION_SCENARIOS` + `PIO_AML_SCENARIO`.
- **`web/services/alert_engine.py`** — the Standalone Alert Execution Engine: executes each active scenario's `RAW_SQL` against `BI_DWH` and populates `PIO_AML_CUSTOMERS`/`PIO_AML_CUSTOMERS_DET`. Runs on demand via `run_alert_engine.py` (CLI) or `POST /engine/run-scenarios` — not on an automatic schedule. See `Docs/04_standalone_alert_engine.md`.
- **`web/services/session_store.py`** — chat-sessions sidebar metadata (SQLite).
- **`web/services/schemas.py`** — Pydantic contracts: `AMLIntent` and friends (intent layer), plus HTTP/SSE request-response models.
- **`web/services/oracle.py`** — Oracle connection pool (`init_pool`/`close_pool`), `run_readonly`, `run_write`, `run_write_many`, `get_connection`, `atomic_connection`, plus a dedicated shadow-test pool (`init_shadow_pool`/`run_shadow_readonly`).
- **`web/services/settings.py`** — Pydantic `BaseSettings` reading all config from `.env`.
- **`web/api/main.py`** — FastAPI app, lifespan (pool init + graph warm-up), CORS, `/health`.
- **`web/api/routes/chat.py`** — `POST /chat/stream` SSE streaming.
- **`web/api/routes/sessions.py`** — chat history/list/rename/pin/delete endpoints.
- **`web/api/routes/engine.py`** — `POST /engine/run-scenarios`, `GET /engine/active-scenarios` — triggers/inspects the alert execution engine.
- **`web/api/routes/deploy.py`** — `POST /scenario/deploy` — frontend-facing scenario deployment endpoint.
- **`web/services/prompts/`** — Markdown system prompt files loaded at runtime via `web/services/prompts/loader.py`'s `load_prompt()`.

### Thread isolation

LangGraph conversation state is isolated by `thread_id = "{project_id}_{chat_id}_{user_id}"`, persisted via `AsyncSqliteSaver` at `artifacts/checkpoints.sqlite`. The separate `chat_sessions` sidebar table is keyed only by `chat_id`; see `PROJECT_OVERVIEW.md` before changing session identity behavior.

### External dependency

`execute_oracle_dwh_shadow_test` calls a **separate PioTech AI DWH service** (text-to-SQL agent) at `PIOTECH_AI_URL` (default `http://localhost:8006/chat/stream`). That service must be running independently for SQL generation to work.

---

## Environment variables (`.env`)

Required:

- `ORACLE_USER`, `ORACLE_PASSWORD`, `ORACLE_DSN`
- `OPENAI_API_KEY` for OpenAI chat and all embedding-backed operations (it is optional at settings-validation time, and LM Studio can replace chat calls but not the direct embedding clients)

Optional LLM:

- `LLM_PROVIDER` — `openai` (default) or `lmstudio`
- `LLM_MODEL` — default `gpt-4o`
- `LLM_MODEL_FAST` — default `gpt-4o-mini`

Optional AML domain defaults (all have production-safe defaults in `settings.py`):

- `AML_COUNTRY_CODE`, `AML_INST_CODE`, `AML_CREATED_BY`

Optional shadow-test DB (falls back to the primary `ORACLE_*` values if unset):

- `SHADOW_ORACLE_DSN`, `SHADOW_ORACLE_USER`, `SHADOW_ORACLE_PASSWORD`, `SHADOW_ORACLE_POOL_MIN`, `SHADOW_ORACLE_POOL_MAX`

---

## Code standards (from `.agents/rules/`)

- **Google-style docstrings** on every function/class/module — include Args, Returns, and Raises sections.
- **100% type hints** — use `Final`, `Literal`, `Protocol` where applicable.
- **Async-first** for new I/O. Several current Oracle, LLM, HTTP, and alert-engine paths are synchronous; inspect the canonical overview before changing request concurrency.
- **PEP 8** — code must pass `flake8` and `black`.
- **No bare `except`** — always catch specific exception types.
- **No hardcoded secrets** — all config through `settings`.

## Collaboration protocol (from `.agents/rules/`)

- Before major tasks, briefly state the intended approach for a sanity check.
- Never provide partial implementations or `# ... rest of code here` placeholders.
- After every fix, update `CHANGELOG.MD`.
- After every major feature/refactor/phase, create a `.md` doc in `Docs/` covering: what & why, how to run it with exact commands, and relevant configuration parameters.

---

## Known issues

See `PROJECT_OVERVIEW.md` sections 15-16 for code-verified risks, technical debt, and unresolved infrastructure questions. Do not treat historical `Docs/known_issues.md` as this repository's active issue register.

## Imported Claude Cowork project instructions
