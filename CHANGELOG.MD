# Changelog — AI AML Agentic Scenario Builder

All notable changes to this project are documented here.
Format: `[DATE] TYPE: Description`

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
