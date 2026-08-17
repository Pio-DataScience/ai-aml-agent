# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

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
pip install -r services/aml_builder/requirements.txt
```

### Run tests

```bash
# All tests
PYTHONPATH=services/aml_builder pytest

# Single test file
PYTHONPATH=services/aml_builder pytest tests/test_decomposer.py

# With async support (required for agent tests)
PYTHONPATH=services/aml_builder pytest --asyncio-mode=auto
```

### API docs

`http://localhost:8005/docs` — available once service is running.

---

## Architecture

The service is a single autonomous **LangGraph ReAct tool-driven agent** exposed via a FastAPI SSE endpoint (`POST /chat/stream`). All source code lives under `services/aml_builder/` with `PYTHONPATH` set to that directory, so all imports are `web.*`. There is no multi-node graph and no routing logic in Python — the LLM decides tool invocation order via a goal-oriented system prompt. Full detail: [`Docs/01_architecture.md`](Docs/01_architecture.md) and [`Docs/02_module_layout_refactor.md`](Docs/02_module_layout_refactor.md).

### The five tools (`web/services/tools.py`)

| Tool                                              | Role                                                                                                                                                                                                                                                      |
| ------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `analyze_intent_and_discover_explanation_codes` | Calls OpenAI to convert natural language into a structured`AMLIntent` JSON object; runs vector similarity search over `PIO_EXPLANATION_CODE` to discover domain explanation codes; seeds `PIO_AML_SCENARIO` governance metadata from the intent.    |
| `generate_scenario_execution_plan`              | Deterministic (zero-LLM) markdown renderer producing the side-panel implementation plan.                                                                                                                                                                  |
| `execute_oracle_dwh_shadow_test`                | Calls the**external PioTech AI DWH agent** (`PIOTECH_AI_URL`) via HTTP SSE to get production Oracle SQL, then shadow-tests it (`SELECT COUNT(*) FROM (...)`) against `BI_DWH` (via the dedicated shadow connection pool).                     |
| `prepare_scenario_metadata_for_persistence`     | Deterministic + live Oracle lookups: builds the`PIO_AML_SCENARIO` field catalog (risk degree, category, etc.) with currently valid options, reports missing mandatory fields.                                                                           |
| `persist_and_validate_scenario_in_dwh`          | Validates the merged metadata, then atomically writes the confirmed scenario into`PIO_AML_PRODUCTION_SCENARIOS` (for the daily ETL runner) **and** `PIO_AML_SCENARIO` (business metadata for downstream compliance modules) in one transaction. |

### Key modules

- **`web/services/tools.py`** — the five `@tool` definitions above.
- **`web/services/graph.py`** — compiles the ReAct agent (`get_tool_driven_graph`), manages the SQLite checkpointer lifecycle (`close_checkpointer`).
- **`web/services/plan_renderer.py`** — the 11-section markdown plan builder.
- **`web/services/scenario_metadata.py`** — `PIO_AML_SCENARIO` field registry: seeding from `AMLIntent`, live lookup-table fetching, and the final validation guard-rail before persistence (see `Docs/PIO_AML_SCENARIO_schema_guide.md`).
- **`web/services/sql_extraction.py`** — pulls clean SQL out of the PioTech AI SSE response text.
- **`web/services/llm_client.py`** — shared `build_llm()`/`safe_parse_json()`.
- **`web/services/explanation_code_search.py`** — vector similarity RAG search over `PIO_EXPLANATION_CODE`.
- **`web/services/production_registry.py`** — atomic dual-writer for `PIO_AML_PRODUCTION_SCENARIOS` + `PIO_AML_SCENARIO`.
- **`web/services/session_store.py`** — chat-sessions sidebar metadata (SQLite).
- **`web/services/schemas.py`** — Pydantic contracts: `AMLIntent` and friends (intent layer), plus HTTP/SSE request-response models.
- **`web/services/oracle.py`** — Oracle connection pool (`init_pool`/`close_pool`), `run_readonly`, `run_write`, `run_write_many`, `get_connection`, `atomic_connection`, plus a dedicated shadow-test pool (`init_shadow_pool`/`run_shadow_readonly`).
- **`web/services/settings.py`** — Pydantic `BaseSettings` reading all config from `.env`.
- **`web/api/main.py`** — FastAPI app, lifespan (pool init + graph warm-up), CORS, `/health`.
- **`web/api/routes/chat.py`** — `POST /chat/stream` SSE streaming.
- **`web/api/routes/sessions.py`** — chat history/list/rename/pin/delete endpoints.
- **`web/services/prompts/`** — Markdown system prompt files loaded at runtime via `web/services/prompts/loader.py`'s `load_prompt()`.

### Thread isolation

Each conversation is isolated by `thread_id = "{project_id}_{chat_id}_{user_id}"`, persisted via LangGraph's `AsyncSqliteSaver` at `artifacts/checkpoints.sqlite`.

### External dependency

`execute_oracle_dwh_shadow_test` calls a **separate PioTech AI DWH service** (text-to-SQL agent) at `PIOTECH_AI_URL` (default `http://localhost:8001/chat/stream`). That service must be running independently for SQL generation to work.

---

## Environment variables (`.env`)

Required:

- `ORACLE_USER`, `ORACLE_PASSWORD`, `ORACLE_DSN`
- `OPENAI_API_KEY`

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
- **Async-first** for all I/O. The Oracle pool is synchronous but wrapped; LangGraph nodes are sync (LangGraph calls them from its async executor).
- **PEP 8** — code must pass `flake8` and `black`.
- **No bare `except`** — always catch specific exception types.
- **No hardcoded secrets** — all config through `settings`.

## Collaboration protocol (from `.agents/rules/`)

- Before major tasks, briefly state the intended approach for a sanity check.
- Never provide partial implementations or `# ... rest of code here` placeholders.
- After every fix, update `CHANGELOG.md`.
- After every major feature/refactor/phase, create a `.md` doc in `Docs/` covering: what & why, how to run it with exact commands, and relevant configuration parameters.

---

## Known issues

No open issues specific to the current tool-driven architecture.
