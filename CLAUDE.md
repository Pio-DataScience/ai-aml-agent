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

The service is a **LangGraph multi-agent system** exposed via a single FastAPI SSE endpoint (`POST /chat/stream`). All source code lives under `services/aml_builder/` with `PYTHONPATH` set to that directory, so all imports are `web.*`.

### Graph topology
```
orchestrator → intent_analyst → sql_bridge → decomposer
                                                  ↓
              validator ←─────────────────── qb_writer
                  ↓ (retry → decomposer)
              orchestrator (final answer)
```

### Node responsibilities (`services/aml_builder/web/services/agent.py`)

| Node | Role |
|------|------|
| `orchestrator` | Entry point for every turn. Routes between nodes, formats final user-facing message, enforces iteration cap. |
| `intent_analyst` | Calls OpenAI to convert natural language into a structured `AMLIntent` JSON object. Raises clarification questions if intent is ambiguous. |
| `sql_bridge` | Calls the **external PioTech AI DWH agent** (`PIOTECH_AI_URL`) via HTTP SSE to get an Oracle SQL query. Parses SQL into `SQLMetadata`. |
| `decomposer` | Maps SQL conditions to Oracle AML table parameter codes. Queries `PIO_AML_PARAMETERS` live to build `ScenarioParameters`. |
| `qb_writer` | Inserts rows into 4 Oracle tables: `PIO_AML_SCENARIO`, `PIO_AML_RULES`, `PIO_AML_SCENARIO_RULES`, `PIO_AML_RULES_DETAILS`. Deletes existing rows first to support retry loops. |
| `validator` | Calls `FILL_PIO_AML_CUSTOMERS` stored procedure, counts generated alerts in `PIO_AML_CUSTOMERS`, pulls samples. Retries via decomposer up to `MAX_VALIDATION_RETRIES` times on failure. |

### State (`AMLScenarioState`)
Single `TypedDict` passed between every node. Thread-isolated per conversation via `thread_id = "{project_id}_{chat_id}_{user_id}"`. Persisted to SQLite via LangGraph's `AsyncSqliteSaver` at `artifacts/checkpoints.sqlite`.

### Key modules

- **`web/services/agent.py`** — all LangGraph nodes, routing functions, and graph builder.
- **`web/services/schemas.py`** — all Pydantic models for inter-node communication (single source of truth).
- **`web/services/oracle.py`** — Oracle connection pool (`init_pool`/`close_pool`), `run_readonly`, `run_write`, `run_write_many`, `get_connection`.
- **`web/services/settings.py`** — Pydantic `BaseSettings` reading all config from `.env`. Includes AML domain defaults (`AML_COUNTRY_CODE`, `AML_INST_CODE`, etc.).
- **`web/api/main.py`** — FastAPI app, lifespan (pool init + graph warm-up), `/chat/stream` SSE streaming, `/health`.
- **`web/services/prompts/`** — Markdown system prompt files loaded at runtime by `_load_prompt()`.

### Parameter mapping in the decomposer
The decomposer queries `PIO_AML_PARAMETERS` and `PIO_AML_COLUMNS` live to build a `column_map`. It maps SQL WHERE conditions, HAVING conditions, and intent thresholds to `PARAMETER_CODE` values. Key codes: `'2'`=Count, `'5'`=Amount, `'6'`=Summation, `'7'`=Customer Class, `'104'`=Individual/Corporate. The last `QBRuleDetail` row always has `combined_rule='-'`.

### External dependency
The `sql_bridge` node calls a **separate PioTech AI DWH service** (text-to-SQL agent) at `PIOTECH_AI_URL` (default `http://localhost:8001/chat/stream`). That service must be running independently for SQL generation to work.

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
- `AML_COUNTRY_CODE`, `AML_INST_CODE`, `AML_CREATED_BY`, `AML_CATEGORY_CODE`, etc.

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

1. **`FILL_PIO_AML_CUSTOMERS` hardcoded filter**: The stored procedure originally had `AND SCENARIO_CODE IN ('1782638293246757')` in its cursor — this was commented out to allow all active scenarios to be evaluated. Any future DB refresh may reintroduce this filter.

2. **Test dataset referential integrity**: In the dev/test Oracle instance, only customer `20210249` has a matching profile in `PIO_CUSTOMERS`. Other customers may appear in `PIO_AML_CUSTOMERS_DET` but not in `PIO_AML_CUSTOMERS` due to missing master records. This is a test data constraint, not a code bug.
