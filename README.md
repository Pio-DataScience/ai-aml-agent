# AI AML Scenario Builder Agent

Backend service for designing, testing, registering, and executing anti-money-laundering (AML) detection scenarios.

The service accepts natural-language scenario descriptions through a FastAPI Server-Sent Events (SSE) endpoint. A single LangGraph ReAct agent extracts an `AMLIntent`, discovers explanation codes from Oracle, renders a deterministic review plan, delegates SQL generation to an external PioTech AI service, shadow-tests the SQL, collects governance metadata, and can persist the confirmed scenario. A separate on-demand alert engine executes saved scenarios and writes alert headers/details to Oracle.

The frontend and external PioTech AI text-to-SQL service are outside this repository. The current project-wide technical reference is [PROJECT_OVERVIEW.md](PROJECT_OVERVIEW.md).

## Requirements

- Python environment with dependencies from the root `requirements.txt`
- Root `.env` containing Oracle credentials (`ORACLE_DSN`, `ORACLE_USER`, `ORACLE_PASSWORD`)
- OpenAI credentials for the default chat and embedding operations, or an OpenAI-compatible chat provider for chat calls
- Access to the external PioTech AI SQL service when running shadow tests

Install dependencies from the repository root:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

There is no `services/aml_builder/requirements.txt` or `.env.example` in the current tree. Do not commit `.env` or secret values.

## Run locally

The Windows helper loads `.env`, sets `PYTHONPATH`, creates `artifacts/`, and starts Uvicorn on port 8005:

```powershell
.\run_dev.ps1
```

Equivalent repository-root command:

```powershell
$env:PYTHONPATH = "services\aml_builder"
.\.venv\Scripts\python.exe -m uvicorn web.api.main:app --reload --port 8005 --host 0.0.0.0
```

The root wrapper can also be used when the repository root is importable:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app:app --reload --port 8005
```

Once running:

- API docs: `http://localhost:8005/docs`
- Health endpoint: `GET /health`
- Chat stream: `POST /chat/stream`

`/health` reports process health only; it does not verify Oracle, OpenAI, or PioTech AI connectivity. Startup initializes SQLite session metadata, Oracle pools, and the LangGraph checkpointer. Oracle/graph warm-up failures are logged as non-fatal startup errors.

## HTTP API

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/health` | Static service/version health response. |
| `POST` | `/chat/stream` | Run the ReAct agent and stream SSE events. |
| `GET` | `/chat/{project_id}/{chat_id}/{user_id}/history` | Reconstruct conversation history and artifacts from the checkpoint. |
| `GET` | `/chats/user/{user_id}/list` | List non-deleted sidebar sessions; optional `project_id` filter. |
| `PUT` | `/chat/{project_id}/{chat_id}/{user_id}/rename` | Rename sidebar metadata. |
| `PUT` | `/chat/{project_id}/{chat_id}/{user_id}/pin` | Toggle sidebar pin state. |
| `DELETE` | `/chat/{project_id}/{chat_id}/{user_id}` | Soft-delete sidebar metadata. |
| `POST` | `/scenario/deploy` | Validate governance metadata, recover checkpoint SQL, and dual-write a scenario. |
| `POST` | `/engine/run-scenarios` | Execute all active scenarios or a selected scenario synchronously. |
| `GET` | `/engine/active-scenarios` | List scenarios available to the all-scenarios engine path. |

`POST /chat/stream` emits `thinking`, `tool_call`, `content`, selected plan/metadata/result artifacts, `error`, and `done` events. The declared `final_answer` and `escalation_report` event types are not explicitly emitted by the current route.

## Scenario-authoring workflow

The six agent tools are registered in `services/aml_builder/web/services/graph.py`:

1. `analyze_intent_and_discover_explanation_codes` extracts and normalizes `AMLIntent`, searches `PIO_EXPLANATION_CODE`, and seeds governance metadata.
2. `generate_scenario_execution_plan` renders the deterministic 11-section Markdown plan.
3. `execute_oracle_dwh_shadow_test` calls PioTech AI over HTTP SSE, extracts Oracle SQL, and runs a count wrapper through the shadow Oracle pool.
4. `prepare_scenario_metadata_for_persistence` builds the governance field catalog and live lookup options.
5. `persist_and_validate_scenario_in_dwh` validates metadata and atomically writes both registry tables.
6. `query_production_scenario_registry` performs read-only search, statistics, alert telemetry, or detail queries for existing scenarios.

The intended sequence is:

```text
intent extraction → plan review/approval → SQL generation and shadow test
→ shadow-result approval → governance review → explicit persistence instruction
→ atomic Oracle dual-write
```

Approval gates are currently prompt/tool instructions, not server-side state transitions. The direct `/scenario/deploy` route does not require `validation_success=true` on the recovered shadow result, so treat deployment as a sensitive trusted-gateway operation.

## Alert engine

The alert engine is on demand; this repository contains no scheduler, queue, worker, or automatic daily invocation.

CLI examples:

```powershell
.\.venv\Scripts\python.exe run_alert_engine.py
.\.venv\Scripts\python.exe run_alert_engine.py --scenario-id PRD_XXXXXXXX
.\.venv\Scripts\python.exe run_alert_engine.py --date 2026-08-17
```

The CLI supports `--scenario-id` and `--date`. There is no `--dry-run` option. The HTTP request field is `evaluation_date`, not `target_date`.

Execution reads active scenarios, runs each saved `RAW_SQL` through the shadow pool (falling back to primary read-only access if shadow access fails), groups rows by `CUS_NUM`, and writes `PIO_AML_CUSTOMERS` headers plus optional `PIO_AML_CUSTOMERS_DET` evidence rows in a primary Oracle transaction. SQL must return `CUS_NUM` for alerts to be inserted. Detail metrics from the interactive shadow test are synthetic (`header_count * 4`) and are not measured transaction counts.

## Configuration

Configuration is defined in `services/aml_builder/web/services/settings.py` and loaded from environment variables or root `.env`. Important keys include:

- Oracle: `ORACLE_DSN`, `ORACLE_USER`, `ORACLE_PASSWORD`, pool sizes
- Shadow Oracle: `SHADOW_ORACLE_DSN`, `SHADOW_ORACLE_USER`, `SHADOW_ORACLE_PASSWORD`, pool sizes; unset values fall back individually to primary settings
- Chat model: `LLM_PROVIDER` (`openai` or `lmstudio`), `LLM_BASE_URL`, `LLM_MODEL`, `LLM_MODEL_FAST`, `OPENAI_API_KEY`, `LLM_TEMPERATURE`, `LLM_REASONING_EFFORT`
- AML defaults: `AML_COUNTRY_CODE`, `AML_INST_CODE`, `AML_CREATED_BY`
- PioTech AI: `PIOTECH_AI_URL` (default `http://localhost:8006/chat/stream`), timeout, user/project IDs
- State: `CHECKPOINT_DB_PATH` (default `./artifacts/checkpoints.sqlite`)

OpenAI embeddings are instantiated directly for explanation-code and registry semantic search, so selecting LM Studio for chat does not remove the embedding dependency. Environment variables override `.env`; avoid generic ambient values such as a non-boolean `DEBUG`, which prevents settings construction.

## Data stores and security boundary

- SQLite stores LangGraph checkpoints and the `chat_sessions` sidebar table.
- Primary Oracle stores scenario/governance data and generated alerts.
- Shadow Oracle executes generated scenario SQL and alert-engine reads, with configured fallback behavior.
- `PIO_AML_PRODUCTION_SCENARIOS` stores executable SQL; `PIO_AML_SCENARIO` stores governance metadata; alert tables are assumed to exist.

No authentication or authorization middleware is implemented in this service. Caller-supplied user/project identifiers are routing values, not verified identities. CORS currently allows all origins, methods, and headers with credentials enabled. Place the service behind a trusted gateway and review database grants before exposing sensitive endpoints.

## Verification

Safe structural checks (the `DEBUG` override is command-local):

```powershell
$env:DEBUG = "false"
.\.venv\Scripts\python.exe -m compileall -q services app.py run_alert_engine.py
.\.venv\Scripts\python.exe -m pytest --collect-only -q
```

The repository currently has no conventional pytest test cases. The only test file is a live Oracle/OpenAI integration check and should be run manually only with approved infrastructure:

```powershell
$env:DEBUG = "false"
.\.venv\Scripts\python.exe services\aml_builder\tests\test_registry_analytics.py
```

Do not run that integration script, start the application lifespan, invoke agent tools, or trigger the alert engine as a routine offline test: those paths can call external APIs, query live Oracle, create tables, or write alert data.

## Project landmarks

- API: `services/aml_builder/web/api/`
- Agent/domain services: `services/aml_builder/web/services/`
- Runtime prompts: `services/aml_builder/web/services/prompts/`
- CLI: `run_alert_engine.py`
- Canonical overview: [PROJECT_OVERVIEW.md](PROJECT_OVERVIEW.md)
- Historical/specialized documentation: [Docs/](Docs/)

For architecture, data contracts, persistence boundaries, known risks, documentation drift, and unresolved deployment questions, start with [PROJECT_OVERVIEW.md](PROJECT_OVERVIEW.md), then verify the relevant implementation.
