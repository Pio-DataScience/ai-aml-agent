# Technical Deep-Dive & System Architecture Specification
## Service A: AI AML Scenario Architect & Batch Alert Engine (`AI_AML_AGENT`)

> **Document Version:** 2.4.0  
> **Target Audience:** Core AI Engineers, Compliance Tech Leads, Solutions Architects, and New Hires.  
> **Scope:** Internal implementation of Service A (`AI_AML_AGENT`), its interface protocol, and its Oracle DWH persistence layer. Service B (PioTech AI text-to-SQL agent) is treated as an external black-box service over an HTTP/SSE contract.

---

## 1. Executive Summary & Problem Context

### 1.1 The Compliance Dilemma
Traditional Anti-Money Laundering (AML) monitoring systems rely on monolithic rule builders (Legacy Query Builders) that require months of manual SQL development, custom ETL coding, and cumbersome database-level schema configurations to deploy a single compliance rule. When regulatory mandates change or emerging financial crime typologies surface (e.g., structuring, rapid movement of funds, high-risk channel smurfing), compliance officers are blocked by slow IT development lifecycles.

### 1.2 The Solution
The **AI AML Scenario Architect** (`AI_AML_AGENT` / Service A) is an enterprise-grade agentic AI platform. It allows compliance officers to express complex behavioral detection rules in natural business language (e.g., *"Flag retail customers who execute outward transfers totaling more than $50,000 across 3 or more transactions within 5 days"*).

The platform autonomously:
1. Translates conversational requirements into mathematically verified, strictly validated Pydantic contracts (`AMLIntent`).
2. Discovers matching domain-specific banking explanation codes (`PIO_EXPLANATION_CODE`) via high-dimensional vector embeddings (RAG).
3. Synthesizes a deterministic 11-section Markdown Implementation Plan rendered in a dedicated frontend side panel.
4. Delegates SQL synthesis to an external text-to-SQL service (Service B) and safely executes non-destructive **Shadow Tests** on multi-million-row historical datasets in an isolated Oracle sandbox (`AMLDB`).
5. Assembles compliance governance metadata from live Oracle lookup tables, presenting interactive options to the officer.
6. Atomically persists verified scenarios into production DWH tables (`PIO_AML_PRODUCTION_SCENARIOS` and `PIO_AML_SCENARIO`).
7. Runs an offline **Standalone Batch Alert Engine** (`alert_engine.py`) that executes active scenario SQL against production tables, materializing triage alerts (`PIO_AML_CUSTOMERS`) and transaction evidence (`PIO_AML_CUSTOMERS_DET`).
8. Provides conversational auditability and intelligence over all deployed scenarios via natural language semantic search and alert telemetry (`query_production_scenario_registry`).

---

## 2. System Stack & Technology Matrix

| Layer / Concern | Technology / Library | Purpose & Technical Function |
|---|---|---|
| **Runtime & Language** | Python 3.11+ / asyncio | High-concurrency asynchronous runtime for I/O operations and SSE streaming. |
| **API Web Framework** | FastAPI + Uvicorn (ASGI) | Exposes Server-Sent Events (SSE) chat streams, session management, and deployment endpoints. |
| **Agent Framework** | LangGraph + LangChain Core | Compiles a single-node autonomous ReAct agent with explicit tool invocation capabilities. |
| **State Persistence** | `aiosqlite` (`AsyncSqliteSaver`) | Checkpoints multi-turn conversation states in SQLite (`artifacts/checkpoints.sqlite`) with thread isolation (`{project_id}_{chat_id}_{user_id}`). |
| **LLM & Reasoning** | OpenAI API (`gpt-4o`, `gpt-4o-mini`, `gpt-5.6-luna`, `o1`/`o3`) | Core agent reasoning, intent extraction, and explanation code semantic ranking. Dynamic adaptation disables `reasoning_effort` for function-calling compatibility. |
| **Vector Search & RAG** | `text-embedding-3-small` (Cosine Similarity) | Vector similarity search over 6,900+ banking transaction codes in `PIO_EXPLANATION_CODE`. |
| **Database Driver** | `python-oracledb` (Thin Client) | Dual connection pools: Primary read/write pool (`AIDB`) and Shadow Sandbox pool (`AMLDB`). |
| **Data Validation** | Pydantic V2 | Strict type enforcement, schema coercion, and contract validation (`AMLIntent`, `Threshold`, `ScenarioMetadata`). |
| **Interface Protocol** | Server-Sent Events (SSE) | Real-time bi-directional UI streaming (`thinking`, `tool_call`, `content`, `plan_artifact`, `scenario_metadata_catalog`, `scenario_result`). |
| **Batch Processing** | Python Batch CLI / REST API | Standalone batch alert generation engine generating sequence-numbered customer alerts and transaction logs. |

---

## 3. High-Level Architectural Topology

```
┌─────────────────────────────────────────────────────────────────────────────────────────┐
│                                COMPLIANCE OFFICER UI                                     │
│                     (React / Next.js Web Interface + Side-Panel Plan)                   │
└────────────────────────────────────────────┬────────────────────────────────────────────┘
                                             │ HTTP POST /chat/stream (SSE)
                                             │ HTTP POST /scenario/deploy
                                             ▼
┌─────────────────────────────────────────────────────────────────────────────────────────┐
│                           SERVICE A — AI AML AGENT (FastAPI)                             │
│                                                                                         │
│  ┌───────────────────────────────────────────────────────────────────────────────────┐  │
│  │                            LangGraph ReAct Agent                                  │  │
│  │   - Memory Checkpointer: AsyncSqliteSaver (Thread-Isolated)                       │  │
│  │   - System Prompt: Goal-Oriented Behavioral Laws (web/services/prompts/)          │  │
│  └─────────────────────────────────────────┬─────────────────────────────────────────┘  │
│                                            │ Invokes 6 Autonomous Tools                 │
│  ┌─────────────────────────────────────────▼─────────────────────────────────────────┐  │
│  │                                 Tool Ecosystem                                    │  │
│  │  1. analyze_intent_and_discover_explanation_codes                                 │  │
│  │  2. generate_scenario_execution_plan (Deterministic Python Renderer)              │  │
│  │  3. execute_oracle_dwh_shadow_test (Delegates to Service B & runs on AMLDB)       │  │
│  │  4. prepare_scenario_metadata_for_persistence (Queries live Oracle lookups)       │  │
│  │  5. persist_and_validate_scenario_in_dwh (Atomic 2-table dual write)              │  │
│  │  6. query_production_scenario_registry (Hybrid Semantic Search & Telemetry)       │  │
│  └───────────────────────────────────────────────────────────────────────────────────┘  │
│                                            │                                            │
│  ┌─────────────────────────────────────────▼─────────────────────────────────────────┐  │
│  │                       Standalone Batch Alert Execution Engine                     │  │
│  │           (run_alert_engine.py / POST /engine/run-scenarios)                       │  │
│  └───────────────────────────────────────────────────────────────────────────────────┘  │
└───────────────────────┬───────────────────────────────────────┬─────────────────────────┘
                        │ External Contract                     │ Oracle Thin Driver
                        │ (HTTP POST SSE)                       │ (Dual Connection Pool)
                        ▼                                       ▼
┌───────────────────────────────────────┐   ┌─────────────────────────────────────────────┐
│   SERVICE B (Text-to-SQL Engine)      │   │             ENTERPRISE ORACLE DWH           │
│   - Receives: AMLIntent payload       │   │                                             │
│   - Generates: Validated Oracle SQL   │   │  [Primary DB — AIDB]                        │
│   - Emits: SQL string via SSE         │   │   ├── PIO_AML_PRODUCTION_SCENARIOS          │
│   *(Internal architecture out of      │   │   ├── PIO_AML_SCENARIO                      │
│     scope for this document)*         │   │   ├── PIO_AML_CUSTOMERS (Header Alerts)     │
│                                       │   │   ├── PIO_AML_CUSTOMERS_DET (Evidence)      │
│                                       │   │   └── PIO_EXPLANATION_CODE (Vector Catalog) │
│                                       │   │                                             │
│                                       │   │  [Shadow DB — AMLDB]                        │
│                                       │   │   └── Historical Transaction Sandbox        │
└───────────────────────────────────────┘   └─────────────────────────────────────────────┘
```

---

## 4. End-to-End Lifecycle: The 5 Core Phases

The scenario lifecycle transitions through 5 distinct phases from initial prompt to production alert generation:

```
[Phase 1: Intent & Plan] ──► [Phase 2: Shadow Test] ──► [Phase 3: Governance] ──► [Phase 4: Persistence] ──► [Phase 5: Alert Engine]
```

### Phase 1: Natural Language Intent Extraction, Vector Discovery & Plan Synthesis
1. **User Prompt Ingestion**: The compliance officer submits a natural language description through the web chat.
2. **Intent Extraction (`analyze_intent_and_discover_explanation_codes`)**:
   - The LLM parses the input against the **13 Intent Extraction Laws** defined in `intent_extraction.md`.
   - Converts unstructured text into a typed `AMLIntent` Pydantic model.
   - Normalizes percentages (e.g., `"150%"` → `1.50`), extracts time windows (`PERIOD_NUM`, `PERIOD_TYPE`), identifies transaction channels (`OUTWARD TRANSFER`, `CASH DEPOSIT`), and handles relational/demographic comparisons (`Threshold.value_from: Union[float, str]`).
3. **Multi-Channel Vector Discovery (`explanation_code_search.py`)**:
   - Embeds scenario keywords using OpenAI's `text-embedding-3-small`.
   - Performs cosine similarity search over `PIO_EXPLANATION_CODE` (6,900+ banking transaction codes in Oracle).
   - Groups searches per channel, applies an auxiliary LLM ranking step to filter domain irrelevancies, and assigns origin tags (e.g., `[OUTWARD TRANSFER] (023, 089)`).
4. **Deterministic Channel-to-Code Binding**:
   - `_bind_channels_and_codes_to_intent()` maps specific explanation codes directly into corresponding `Threshold` and `SemanticCondition` objects so downstream SQL generators know precisely which filter applies to which branch.
5. **Initial Governance Seeding**:
   - `seed_scenario_metadata()` derives initial values for `PIO_AML_SCENARIO` (e.g., mapping time windows to `PERIOD_TYPE`, creating human-readable descriptions capped at 400 characters, applying system tenant codes).
6. **Execution Plan Generation (`generate_scenario_execution_plan`)**:
   - Executes `plan_renderer.py` (zero-LLM deterministic renderer).
   - Generates an 11-section Markdown implementation plan artifact and streams it via SSE (`type="plan_artifact"`) to hydrate the frontend side panel.

### Phase 2: SQL Delegation & Safe Sandbox Shadow Testing
1. **Delegation to Service B (`execute_oracle_dwh_shadow_test`)**:
   - Transmits the structured `AMLIntent` JSON payload via HTTP POST to Service B (`PIOTECH_AI_URL`).
   - Streams Service B's SSE response and defensively extracts the executable Oracle SQL using `sql_extraction.py`.
2. **Non-Destructive Shadow Testing**:
   - Uses the dedicated **Shadow Connection Pool** (`AMLDB` sandbox) to prevent performance degradation or locking on production transaction tables.
   - Encloses the generated query in safety aggregations:
     ```sql
     SELECT COUNT(*) FROM (<GENERATED_SQL>)
     ```
   - Automatically injects historical date anchors (`DAY_DATE = TO_DATE('...', 'YYYY-MM-DD')`) to test against populated financial periods.
3. **Telemetry & Metrics Output**:
   - Calculates `header_alert_count` (unique customers breached), `transaction_detail_count` (breaching transaction rows), and `alert_density_ratio`.
   - Emits a `scenario_result` SSE event containing the SQL, runtime duration, and alert distribution metrics.

### Phase 3: Compliance Governance & Metadata Catalog Assembly
1. **Metadata Catalog Build (`prepare_scenario_metadata_for_persistence`)**:
   - Once the shadow test passes and the officer approves the logic, the agent inspects required governance fields in `PIO_AML_SCENARIO`.
   - Executes live Oracle queries against lookup tables (`PIO_AML_CATEGORY`, `PIO_AML_DEGREE_RISK`, `PIO_AML_SCENARIO_LOOKUPS`) to fetch valid compliance codes.
2. **Frontend Catalog Hydration**:
   - Streams `scenario_metadata_catalog` SSE event to the client with:
     - Field kinds (`system_default`, `derived`, `static_enum`, `oracle_lookup`).
     - Allowed option lists (e.g., Risk Degrees: `H - High`, `M - Medium`, `L - Low`).
     - Current selections and `missing_mandatory_fields`.
3. **Dual Input Path**:
   - The compliance officer can provide missing governance fields either by clicking dropdowns/radios in the frontend side panel or by specifying them conversationally in chat.

### Phase 4: Atomic Dual-Write Production Persistence
1. **Pre-Persistence Validation Guardrail (`validate_scenario_metadata`)**:
   - Validates that all mandatory fields are populated.
   - Strictly verifies that every entered value exists within the live Oracle lookup table or static enum. The LLM is forbidden from inventing or hallucinating arbitrary codes.
2. **Atomic Dual-Write (`persist_and_validate_scenario_in_dwh`)**:
   - Calls `save_production_scenario()` in `production_registry.py`.
   - Opens a single transactional cursor using `oracle.atomic_connection()`.
   - Generates a deterministic scenario ID (`PRD_{8-char-uuid}`).
   - Executes atomic dual-insert:
     1. `PIO_AML_PRODUCTION_SCENARIOS`: Stores the execution SQL (`RAW_SQL`), active state (`IS_ACTIVE=1`), and scenario metadata.
     2. `PIO_AML_SCENARIO`: Stores enterprise governance fields (`RISK_DEGREE`, `CATEG_CODE`, `PERIOD_TYPE`, `VIOLATION_LEVEL`, `VERSION_NUM`).
   - If either table fails, the transaction issues an immediate `ROLLBACK`, guaranteeing zero orphan records.

### Phase 5: Batch Alert Execution & Registry Analytics
1. **Batch Alert Execution Engine (`alert_engine.py`)**:
   - Executes outside the conversational chat loop via CLI (`python run_alert_engine.py`) or REST API (`POST /engine/run-scenarios`).
   - Discovers all active scenarios (`IS_ACTIVE=1`) across both registry tables.
   - Runs `RAW_SQL` against production DWH data for the target evaluation date (`DAY_DATE`).
   - Materializes alerts into two core Oracle tables:
     - `PIO_AML_CUSTOMERS`: Unique header alerts per flagged customer, calculating next sequential ID (`SEQ`), triage status (`NEW`), and assigned risk severity.
     - `PIO_AML_CUSTOMERS_DET`: Granular audit evidence inserting each specific breaching transaction (`TRA_AMT`, `EQU_TRA_AMT`, `EXPL_CODE`, transaction sequence keys).
2. **Conversational Registry Intelligence (`query_production_scenario_registry`)**:
   - Allows compliance officers to ask questions about deployed rules at any point.
   - Powered by `scenario_registry_analytics.py` across 4 modes:
     - `semantic_search`: Performs on-demand vector similarity search across persisted scenario descriptions.
     - `get_statistics`: Returns total active/inactive counts, categories, and risk distributions.
     - `get_alert_metrics`: Queries live alert volumes from `PIO_AML_CUSTOMERS` per scenario.
     - `get_scenario_detail`: Pulls the exact production SQL and governance attributes for a scenario ID.

---

## 5. Directory Layout & Module Responsibilities

```text
services/aml_builder/
├── requirements.txt                      # Project dependencies (FastAPI, LangGraph, oracledb, pydantic)
├── run_alert_engine.py                   # Standalone CLI entrypoint for daily batch alert execution
└── web/
    ├── api/
    │   ├── main.py                       # FastAPI application setup, CORS, lifespan connection pool handling
    │   └── routes/
    │       ├── chat.py                   # POST /chat/stream — LangGraph SSE event generator
    │       ├── deploy.py                 # POST /scenario/deploy — Direct UI deployment route
    │       ├── engine.py                 # POST /engine/run-scenarios, GET /engine/active-scenarios
    │       └── sessions.py               # Chat session management (list, history, pin, rename, delete)
    └── services/
        ├── agent_tool_driven.py          # Legacy entrypoint (re-exports tools and graph)
        ├── alert_engine.py               # Core batch alert execution logic (PIO_AML_CUSTOMERS dual populator)
        ├── explanation_code_search.py    # Vector search & LLM ranking over PIO_EXPLANATION_CODE
        ├── graph.py                      # LangGraph ReAct agent compilation & SQLite checkpointer lifecycle
        ├── llm_client.py                 # build_llm() factory with OpenAI reasoning model compatibility
        ├── logging_config.py             # Structured JSON logging configuration
        ├── oracle.py                     # Primary and Shadow Oracle thin connection pools
        ├── plan_renderer.py              # Zero-LLM deterministic 11-section markdown plan generator
        ├── production_registry.py        # Atomic dual-writer for production scenario tables
        ├── scenario_metadata.py          # Field registry, live lookup queries, and persistence validation
        ├── scenario_registry_analytics.py# Read-only search, metrics, and telemetry engine
        ├── schemas.py                    # Pydantic V2 contracts (AMLIntent, Threshold, SSEEvent, Metadata)
        ├── session_store.py              # SQLite store for frontend sidebar chat sessions
        ├── settings.py                   # Environment configuration (Pydantic BaseSettings)
        ├── sql_extraction.py             # Clean SQL parser from Service B SSE stream
        ├── tools.py                      # The 6 autonomous @tool definitions provided to the agent
        └── prompts/
            ├── loader.py                 # Markdown prompt template loader with cache
            ├── intent_extraction.md      # The 13 laws for natural language intent parsing
            └── tool_driven_system.md     # Primary ReAct system prompt and behavior rules
```

---

## 6. Core Data Contracts & Schemas

### 6.1 The Intent Contract (`AMLIntent` in `web/services/schemas.py`)
`AMLIntent` is the canonical contract passed between parsing, plan rendering, and Service B:

```python
class Threshold(BaseModel):
    metric: str                                   # e.g., "TRA_AMT", "COUNT", "RATIO"
    operator: Literal[">", ">=", "<", "<=", "BETWEEN"]
    value_from: Union[float, str]                 # Numeric threshold OR column/demographic expression
    value_to: Optional[float] = None              # Upper bound for BETWEEN
    currency: Optional[str] = "USD"
    stage: Literal["DETAIL", "AGGREGATE"]         # DETAIL -> WHERE clause, AGGREGATE -> HAVING clause
    transaction_type: Optional[str] = None        # Bound transaction channel (e.g., "CASH DEPOSIT")
    explanation_codes: Optional[List[str]] = None # Bound banking codes (e.g., ["001", "042"])

class AMLIntent(BaseModel):
    scenario_name: str
    scenario_type: Literal["CUSTOMER", "TRANSACTION", "ACCOUNT"]
    transaction_type: Optional[str] = None
    transaction_types: Optional[List[str]] = None
    thresholds: List[Threshold]
    time_window: Optional[TimeWindow] = None
    baseline_window: Optional[BaselineWindow] = None
    aggregation: Optional[AggregationProfile] = None
    customer_segments: Optional[List[str]] = None
    exclusions: Optional[List[str]] = None
    semantic_conditions: List[SemanticCondition] = Field(default_factory=list)
    explanation_codes: Optional[List[str]] = None
    explanation_codes_by_type: Optional[Dict[str, List[str]]] = None
    anchor_date: Optional[str] = None
```

### 6.2 SSE Streaming Event Contract (`SSEEvent` in `web/services/schemas.py`)
Streamed by `POST /chat/stream` as `data: {"type": "...", ...}\n\n`:

| SSE Event Type | Key Fields | Purpose |
|---|---|---|
| `thinking` | `status: str` | Displays agent processing progress indicator in the UI. |
| `tool_call` | `tool: str` | Notifies the UI which autonomous tool is currently executing. |
| `content` | `text: str` | Natural language message chunk from the agent to the user. |
| `plan_artifact` | `text: str` | Full Markdown of the 11-section execution plan; triggers side-panel update. |
| `scenario_metadata_catalog`| `data: dict` | Carries field catalog, lookup options, and missing mandatory fields. |
| `scenario_result` | `data: dict` | Contains shadow test counts or persistence confirmation cards. |
| `error` | `text: str` | Transmits error details if an unhandled exception occurs. |
| `done` | *(empty)* | Signals conclusion of the SSE event stream. |

---

## 7. Database Topology & Oracle DWH Schemas

### 7.1 Scenario Persistence Tables (Dual-Write)

#### `PIO_AML_PRODUCTION_SCENARIOS` (SQL Execution Contract)
Stores the production ANSI Oracle SQL executed by the batch alert engine.
- `SCENARIO_ID` (VARCHAR2, PK): Unique code e.g. `PRD_A1B2C3D4`.
- `SCENARIO_NAME` (VARCHAR2): Display title.
- `RAW_SQL` (CLOB): Executable production Oracle SQL.
- `INTENT_JSON` (CLOB): Serialized `AMLIntent` Pydantic payload.
- `EXPLANATION_CODES` (VARCHAR2): Semicolon-delimited list of banking codes.
- `IS_ACTIVE` (NUMBER): `1` for active evaluation, `0` for paused.
- `CREATED_AT` / `UPDATED_AT` (TIMESTAMP).

#### `PIO_AML_SCENARIO` (Compliance Governance Contract)
Stores enterprise metadata consumed by downstream risk triage screens.
- `SCENARIO_CODE` (VARCHAR2, PK): Identical to `SCENARIO_ID`.
- `COUNTRY_CODE` / `INST_CODE` (NUMBER): Tenant identifiers (default `400` / `1`).
- `CATEG_CODE` (VARCHAR2): Category code (FK to `PIO_AML_CATEGORY`).
- `DEGREE_RISK_FLAG` (CHAR): Risk severity (`H`, `M`, `L`).
- `VIOLATION_LEVEL` (VARCHAR2): Regulatory violation tier.
- `PERIOD_TYPE` / `PERIOD_NUM`: Evaluation grain (`D` for Days, `M` for Months, `Y` for Years).
- `ACTIVE_FLAG` (CHAR): `'1'` (Active) or `'0'` (Inactive).
- `VERSION_NUM` (NUMBER): Incremented upon scenario updates.

### 7.2 Alert Materialization Tables (Batch Alert Output)

#### `PIO_AML_CUSTOMERS` (Header Alert Table)
Every breached customer receives a single header record per run date:
- `DAY_DATE` (DATE): Evaluation date.
- `CUS_NUM` (VARCHAR2): Customer unique identifier.
- `AML_SCENARIO_CODE` (VARCHAR2): Foreign key referencing `SCENARIO_ID`.
- `SEQ` (NUMBER, PK): Sequential alert sequence (`NVL(MAX(SEQ), 0) + ROW_NUMBER()`).
- `RISK_DEGREE` (VARCHAR2): Inherited from scenario governance.
- `CURRENT_STATUS` / `INITIAL_STATUS`: Set to `'PENDING'` / `'NEW'`.
- `CURRENT_STEP`: Set to `'INVESTIGATION'`.
- `WORKCASE`: Defaulted to `'0'` (unassigned case flag).

#### `PIO_AML_CUSTOMERS_DET` (Detail Transaction Evidence Table)
Stores individual breaching transactions linked to the alerted customer:
- `DAY_DATE` (DATE): Evaluation date.
- `TRA_DAY_DATE` / `TRA_DATE` (DATE/TIMESTAMP): Transaction date.
- `TRA_SEQ1` / `TRA_SEQ2` (NUMBER): Primary/secondary transaction sequence numbers.
- `BRA_CODE` (NUMBER): Branch identifier.
- `CUS_NUM` (VARCHAR2): Customer identifier.
- `TRA_AMT` (NUMBER): Raw transaction amount.
- `EQU_TRA_AMT` (NUMBER): Base currency equivalent amount.
- `EXPL_CODE` (VARCHAR2): Transaction explanation code.
- `AML_SCENARIO_CODE` (VARCHAR2): Foreign key referencing `SCENARIO_ID`.

---

## 8. Frontend Interface & Communication Flow

```
+---------------------------------------------------------------------------------------------------+
|  FRONTEND VIEWPORT                                                                                |
|                                                                                                   |
|  [LEFT: Chat Interface]                             [RIGHT: Side Panel Artifact / Catalog]        |
|                                                                                                   |
|  Officer: "Flag customers depositing cash > $10k    ┌──────────────────────────────────────────┐  |
|           over 3 days"                              │ IMPLEMENTATION PLAN                      │  |
|                                                     │ Status: Ready for Review                 │  |
|  Agent: (thinking...)                               │                                          │  |
|         - Invokes analyze_intent...                 │ 1. Overview: CASH_STRUCTURING_01         │  |
|         - Invokes generate_execution_plan...  ====> │ 2. Logic: SUM(TRA_AMT) > 10000           │  |
|                                                     │ 3. Window: 3 Days                        │  |
|  Agent: "I've drafted the execution plan.           │ 5. Thresholds:                           │  |
|          Executing shadow test now..."              │    - Metric: TRA_AMT > 10000             │  |
|         - Invokes execute_shadow_test...            │    - Codes: [001, 004]                   │  |
|                                                     ├──────────────────────────────────────────┤  |
|  Agent: "Shadow test finished:                      │ SCENARIO GOVERNANCE CATALOG              │  |
|          14 alerts detected across 52 txns.         │                                          │  |
|          Please select Category & Risk Degree." ===>│ * Risk Degree: [ High (H) ▼ ]           │  |
|                                                     │ * Category:    [ Structuring (03) ▼ ]    │  |
|  Officer: (Clicks 'Deploy Scenario' Button)         │                                          │  |
|           OR confirms in chat                       │ [ Deploy Scenario to Production ] (Btn)  │  |
|                                                     └──────────────────────────────────────────┘  |
+---------------------------------------------------------------------------------------------------+
```

### 8.1 Side Panel Hydration Mechanics
1. When `generate_scenario_execution_plan` executes, the backend streams `type="plan_artifact"`. The frontend captures `event.text` and renders Markdown inside the right-hand panel.
2. When `prepare_scenario_metadata_for_persistence` executes, the backend streams `type="scenario_metadata_catalog"`. The frontend reads the `fields` object and renders native interactive controls (dropdowns for `oracle_lookup` and radios for `static_enum`).
3. When the officer submits the side panel, the frontend calls `POST /scenario/deploy` with the complete metadata payload, triggering direct atomic persistence.

---

## 9. Operations, Configuration & Runbook

### 9.1 Environment Variables (`.env`)

```ini
# =============================================================================
# Primary Oracle DWH Connection (AIDB)
# =============================================================================
ORACLE_USER=aml_admin
ORACLE_PASSWORD=********
ORACLE_DSN=dwh-prod-scan.internal:1521/AIDB
ORACLE_POOL_MIN=2
ORACLE_POOL_MAX=10

# =============================================================================
# Shadow Testing Oracle Connection (AMLDB Sandbox)
# =============================================================================
SHADOW_ORACLE_USER=aml_shadow
SHADOW_ORACLE_PASSWORD=********
SHADOW_ORACLE_DSN=dwh-shadow-scan.internal:1521/AMLDB
SHADOW_ORACLE_POOL_MIN=1
SHADOW_ORACLE_POOL_MAX=5

# =============================================================================
# External PioTech AI Text-to-SQL Service (Service B)
# =============================================================================
PIOTECH_AI_URL=http://localhost:8001/chat/stream

# =============================================================================
# OpenAI & LLM Configuration
# =============================================================================
OPENAI_API_KEY=sk-proj-********
LLM_PROVIDER=openai
LLM_MODEL=gpt-4o
LLM_MODEL_FAST=gpt-4o-mini
LLM_TEMPERATURE=0.0
LLM_REASONING_EFFORT=none

# =============================================================================
# AML Tenant Defaults
# =============================================================================
AML_COUNTRY_CODE=400
AML_INST_CODE=1
AML_CREATED_BY=AML_AGENT_ADMIN
```

### 9.2 Key Execution Commands

#### 1. Start the API Service
```powershell
# Windows PowerShell (using project script)
.\run_dev.ps1

# Manual Startup (all platforms)
# Ensure PYTHONPATH points to services/aml_builder
$env:PYTHONPATH = "services\aml_builder"
uvicorn web.api.main:app --reload --port 8005 --host 0.0.0.0
```
- **Verification**: Navigate to `http://localhost:8005/docs` to verify OpenAPI schemas.

#### 2. Run the Standalone Daily Batch Alert Engine
```powershell
# Run all active scenarios for today's date
python services\aml_builder\run_alert_engine.py

# Run for a specific historical date (YYYY-MM-DD)
python services\aml_builder\run_alert_engine.py --day-date 2026-06-01

# Execute a specific scenario by ID
python services\aml_builder\run_alert_engine.py --scenario-id PRD_7EB9CBAD
```

#### 3. Run Automated Tests
```powershell
# Run entire test suite
$env:PYTHONPATH = "services\aml_builder"
pytest

# Run tests with async support
pytest --asyncio-mode=auto
```

---

## 10. Summary Checklist for New Engineers

- [ ] **Thread Checkpoints**: Inspect `artifacts/checkpoints.sqlite` using SQLite Browser to see how multi-turn LangGraph conversation states are preserved.
- [ ] **Dual Connection Pools**: Remember that read/write production operations run through `oracle.py` against `AIDB`, while test SQL executes exclusively via `run_shadow_readonly` against `AMLDB`.
- [ ] **Intent Laws**: Review `web/services/prompts/intent_extraction.md` before altering how `AMLIntent` is structured.
- [ ] **Deterministic Plan**: Do not introduce LLM calls into `plan_renderer.py`. The plan must remain 100% deterministic code rendering.
- [ ] **Lookup Integrity**: Never hardcode compliance governance categories. All dropdown selections originate from live Oracle lookups via `scenario_metadata.py`.
- [ ] **Service B Contract**: Service B is an external dependency. If SQL generation fails, inspect network connectivity and verify `PIOTECH_AI_URL`.
