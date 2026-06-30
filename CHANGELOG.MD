# Changelog — AI AML Agentic Scenario Builder

All notable changes to this project are documented here.
Format: `[DATE] TYPE: Description`

---

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
