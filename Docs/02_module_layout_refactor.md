# 02 — Module Layout Refactor (Bug Fix + Responsibility Split)

> Companion to [`01_architecture.md`](01_architecture.md). Documents the 2026-08-10 refactor that
> restored `/chat/stream` and split `agent_tool_driven.py` / `main.py` into single-responsibility modules.
> No business behavior, prompts, API request/response contracts, or environment variables changed
> (except the removal of the dead `USE_TOOL_DRIVEN_AGENT` flag — see below).

---

## 1. Why

A prior refactor (`cd06ca1`) deleted the legacy multi-node `agent.py` but left `main.py` calling its
`get_graph()` function, which no longer existed. **Every call to `POST /chat/stream` raised
`NameError` before it could even check which agent implementation to use** — the service's primary
endpoint was non-functional. This pass fixes that, removes the now-dead legacy-graph code paths
(confirmed with the user before deleting), and splits two oversized modules
(`agent_tool_driven.py`, 1041 lines; `main.py`, 879 lines) along their actual seams — tool
definitions, SQL parsing, plan rendering, graph lifecycle, session persistence, HTTP routing —
without changing what any of them do. `agent_tool_driven.py` itself was renamed to `tools.py`:
once the other seams were extracted, the file held only the four `@tool` definitions, and the old
name (which described the *whole* tool-driven architecture, pre-split) was misleading.

## 2. What changed

### Bugs fixed
| Bug | Symptom | Fix |
|---|---|---|
| `main.py` called undefined `get_graph()` | `/chat/stream` crashed on every request | Removed dead legacy-graph branches; tool-driven ReAct agent is now the only path |
| `explanation_code_search.py` called undefined `_build_llm(fast=True)` | Sub-LLM reranking of explanation codes silently failed every time, falling back to raw vector matches | Now imports the shared `build_llm()` from `llm_client.py` |
| `settings.SERVICE_NAME` returned a `FieldInfo` object, not a string | `GET /health` returned HTTP 500 | Removed the `Final[str]` annotation (Pydantic v2 treats `Final`-annotated attributes as class constants, not model fields) |

### Dead code removed
- The classic multi-node graph branches in `main.py` (`run_scenario_graph_task`, the `AMLScenarioState`-shaped state-init blocks, the unconditional `get_graph()` call).
- `POST /chat/{project_id}/{chat_id}/{user_id}` and its `/status` endpoint — entirely built around the deleted graph's state shape (`error_log`, `next_action`, etc.), never functional post-`cd06ca1`.
- `USE_TOOL_DRIVEN_AGENT` setting — no longer had any effect since only one agent implementation remains.

### New module layout
```text
services/aml_builder/web/
├── api/
│   ├── main.py                 # App creation, CORS, lifespan, router includes only
│   └── routes/
│       ├── chat.py             # POST /chat/stream (SSE)
│       └── sessions.py         # history/list/rename/pin/delete endpoints
└── services/
    ├── tools.py                 # The 4 @tool definitions only (formerly agent_tool_driven.py)
    ├── graph.py                # get_tool_driven_graph(), close_checkpointer(), prompt loading
    ├── plan_renderer.py        # build_plan_markdown() — 11-section deterministic plan renderer
    ├── sql_extraction.py       # extract_sql() — pulls SQL out of PioTech AI's SSE text
    ├── llm_client.py           # build_llm(), safe_parse_json() — shared across agent + explanation search
    ├── session_store.py        # chat_sessions sidebar table CRUD (sqlite3)
    ├── explanation_code_search.py   # unchanged except its llm_client import
    ├── oracle.py                    # unchanged
    ├── production_registry.py       # unchanged
    ├── schemas.py                   # unchanged
    ├── settings.py                  # unchanged except SERVICE_NAME fix + USE_TOOL_DRIVEN_AGENT removal
    └── logging_config.py            # unchanged
```

## 3. How to run it

Same as before — nothing about startup changed:

```powershell
.\run_dev.ps1
```

or manually:

```powershell
$env:PYTHONPATH = "services\aml_builder"
uvicorn app:app --reload --port 8005
```

## 4. Configuration

No new environment variables. `USE_TOOL_DRIVEN_AGENT` (default `False`, previously unread due to the
crash above) has been removed from `settings.py` — if it's set in any deployment's `.env`, it is now
silently ignored (Pydantic settings has `extra="ignore"`), which is safe.

## 5. Verification performed

- `ast.parse()` on every new/modified file — no syntax errors.
- `import app` with dummy Oracle/OpenAI credentials — imports cleanly, no `NameError`/`ImportError`.
- Started the service with `uvicorn app:app`, confirmed all routes registered as expected.
- `GET /health` → `200 {"status":"healthy","service":"aml-builder","version":"1.0.0"}`.
- `POST /chat/stream` with a sample scenario prompt → reaches the OpenAI call and fails only on the
  dummy API key (`401 invalid_api_key`), proving the request path that used to `NameError` before
  reaching any LLM call now runs end-to-end up to the external dependency boundary.
- No automated test suite exists in this repository to run as a regression baseline (`Docs/known_issues.md` / repo search confirm no `tests/` directory).

## 6. Remaining technical debt (not addressed in this pass)

- `schemas.py` defines `ChatMessage` twice (lines ~922 and ~1036); the second definition silently
  shadows the first at import time. Both call sites already resolve to the second (richer) one, so
  behavior is unaffected, but the duplicate should be removed in a follow-up.
- No automated tests exist for any module. Given the LLM- and live-Oracle-DWH-dependent nature of
  most business logic (intent extraction, SQL generation via the external PioTech AI service, shadow
  testing), meaningful coverage would need either recorded fixtures or a test Oracle instance —
  out of scope for this pass, flagged here per the collaboration protocol.
- `get_next_numeric_code()` in `oracle.py` is defined but unused by any current caller — left in place
  since removing possibly-still-referenced-elsewhere utility code wasn't part of this refactor's scope.
