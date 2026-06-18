# AI AML Agentic Scenario Builder — Architectural Proposal

**PioTech Internal | Confidential | v1.0**
**Date: 2026-06-17**

---

## Vision

This is not another chatbot that explains AML theory or generates ad-hoc SQL. This is a **fully autonomous, production-grade agent** that completes an entire AML scenario lifecycle — from a manager's conversational intent to a live, executed, and self-validated scenario inside the PioTech AML Query Builder engine — with zero manual technical steps.

The gap in the market is not intelligence; it is **grounded autonomy with domain-specific decomposition**. This system closes that gap.

---

## The Problem We Are Solving

The existing PioTech AML module lets implementation teams manually create scenarios by:

1. Picking tables/fields from dropdown lists
2. Adding operators (`>`, `<`, `=`, `BETWEEN`, etc.)
3. Submitting to the legacy Oracle **Query Builder** procedure/engine

This is:

- **Expert-gated** — only a trained implementation team can do it
- **Slow** — each scenario takes hours/days
- **Un-scalable** — a bank with 50 compliance use-cases is bottlenecked

Our system replaces steps 1–3 with a conversational agent that does them autonomously, and then adds step 4 (self-validation) that humans rarely do rigorously.

---

## Architectural Overview — The Agent Topology

```
┌─────────────────────────────────────────────────────────────────────┐
│                     AML SCENARIO AGENT (LangGraph)                  │
│                                                                      │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │                   ORCHESTRATOR NODE                          │   │
│  │   (Supervisor — routes, manages state, owns the lifecycle)  │   │
│  └──────────────┬──────────────────────────────────────────────┘   │
│                 │                                                    │
│    ┌────────────▼───────────┐  ┌──────────────────────────────┐   │
│    │   INTENT ANALYST NODE  │  │  SCENARIO VALIDATOR NODE      │   │
│    │   (Clarify & enrich    │  │  (Agentic hunt: did it fire?) │   │
│    │    the user's goal)    │  └──────────────────────────────┘   │
│    └────────────┬───────────┘              ▲                       │
│                 │                          │                        │
│    ┌────────────▼───────────┐             │                        │
│    │   SQL BRIDGE NODE      │─────────────┘                        │
│    │   (Calls PioTech AI    │                                       │
│    │    text-to-SQL agent)  │                                       │
│    └────────────┬───────────┘                                       │
│                 │                                                    │
│    ┌────────────▼───────────┐                                       │
│    │  DECOMPOSER NODE       │                                       │
│    │  (The Core Brain:      │                                       │
│    │   SQL → QB Parameters) │                                       │
│    └────────────┬───────────┘                                       │
│                 │                                                    │
│    ┌────────────▼───────────┐                                       │
│    │  QB WRITER NODE        │                                       │
│    │  (Writes to AML tables │                                       │
│    │   via Oracle procedure)│                                       │
│    └────────────────────────┘                                       │
└─────────────────────────────────────────────────────────────────────┘
```

This is a **LangGraph StateGraph** — the same battle-tested framework running the existing PioTech AI system, so no new infrastructure.

---

## State Schema — The Single Source of Truth

```python
class AMLScenarioState(TypedDict):
    # === Conversation ===
    messages: Annotated[list[BaseMessage], add_messages]
  
    # === Intent ===
    user_intent: str                        # Raw natural language goal
    enriched_intent: AMLIntent             # Structured, clarified intent
  
    # === SQL Bridge ===
    raw_sql: Optional[str]                 # SQL from PioTech AI agent
    sql_metadata: Optional[SQLMetadata]   # Tables, columns, operators extracted
  
    # === Decomposition ===
    scenario_parameters: Optional[ScenarioParameters]  # QB-ready payload
    decomposition_confidence: float        # 0.0 – 1.0 self-assessment score
  
    # === QB Execution ===
    scenario_id: Optional[str]            # ID of created scenario in AML DB
    scenario_status: Optional[str]        # DRAFT | ACTIVE | ERROR
  
    # === Validation ===
    validation_result: Optional[ValidationResult]  # Alert counts, sample data
  
    # === Control ===
    iteration_count: int
    next_action: str                       # Router signal
    error_log: list[str]
```

---

## Node Breakdown — What Each Node Does

### 1. ORCHESTRATOR NODE (Supervisor)

The router and state manager. It:

- Reads the current `next_action` signal
- Routes to the correct next node
- Enforces the lifecycle: `INTENT → SQL → DECOMPOSE → WRITE → VALIDATE`
- Detects loops and applies correction strategies
- Owns all user-facing communication (never lets sub-nodes talk to the user directly)

**This is the same pattern the existing Supervisor uses, generalized for the AML scenario lifecycle.**

---

### 2. INTENT ANALYST NODE

**Goal:** Convert a vague manager sentence into a *grounded, unambiguous AML intent object*.

```python
class AMLIntent(BaseModel):
    scenario_name: str                    # e.g., "Large Cash Transactions - Retail"
    scenario_type: str                    # "TRANSACTION" | "ACCOUNT" | "CUSTOMER"
  
    primary_entity: str                   # The main table domain (transactions, accounts)
    detection_logic: str                  # Business logic in plain English
  
    thresholds: list[Threshold]           # [{field: "amount", op: ">", value: 50000}]
    time_window: Optional[TimeWindow]     # {unit: "days", value: 30}
    customer_segments: Optional[list]     # ["RETAIL", "CORPORATE"]
  
    exclusions: Optional[list[str]]       # What to explicitly NOT include
    clarification_needed: bool
    clarification_questions: list[str]    # Business questions (NOT technical)
```

**Protocol:** Uses the same "Domain Expert Clarification" pattern from the existing supervisor — never asks technical questions, always maps uncertainty to business choices.

**Example:**

```
User: "Flag suspicious cash activity"

Intent Analyst detects:
  - "suspicious" → UNDEFINED threshold → ask user
  - "cash" → structured (cash transactions only, no wire?)
  - No time window → ask user

Output to user:
"To build this scenario precisely, I need 2 quick inputs:
  1. What transaction amount should trigger a flag? (e.g., above JD 10,000?)
  2. Should this look at a rolling period — say last 30 days — or all-time?"
```

---

### 3. SQL BRIDGE NODE

**Goal:** Take the enriched intent and get battle-tested SQL that represents the detection logic.

This node calls **PioTech AI** (the existing text-to-SQL agent) as an **external sub-agent** via HTTP. It does NOT rebuild the SQL stack — it delegates.

```python
class SQLBridgeProtocol:
    """How we communicate with PioTech AI agent."""
  
    endpoint: str = "http://piotech-ai-service/chat/stream"
  
    # The constructed prompt we send
    prompt_template = """
    <AML_SCENARIO_REQUEST>
    SCENARIO_NAME: {scenario_name}
    DETECTION_LOGIC: {detection_logic}
    THRESHOLDS: {thresholds}
    TIME_WINDOW: {time_window}
    CUSTOMER_SEGMENTS: {segments}
  
    TASK: Write a SQL SELECT query that identifies records matching this AML scenario.
    Return ONLY customers/accounts/transactions that should trigger an alert.
    Include the key fields needed to populate the scenario parameters.
    RETURN_FORMAT: SQL only — no explanation.
    </AML_SCENARIO_REQUEST>
    """
  
    # What we extract from the response
    output: SQLMetadata  # tables, columns, operators, filters used
```

**Key insight:** PioTech AI already knows the DWH schema, join dictionaries, and business codes. We are not re-inventing that. We are *consuming* it as a capability.

---

### 4. DECOMPOSER NODE — The Core Brain

**Goal:** Reverse-engineer the SQL into Query Builder parameters.

This is the most intellectually complex node. It must understand the **semantic mapping** between SQL constructs and QB parameter tables.

#### The Decomposition Contract

The Query Builder has specific parameter tables (exact schema TBD per open questions below). The mapping is:

```
SQL Construct              →  QB Parameter Table
─────────────────────────────────────────────────
FROM BI_DWH.PIO_TXNS      →  QB_SCENARIO_TABLES.TABLE_REF
WHERE TXN_AMT > 50000     →  QB_CONDITIONS: field=TXN_AMT, op=">", val=50000
AND TXN_DATE >= SYSDATE-30 →  QB_TIME_FILTERS: window_days=30
JOIN BI_DWH.PIO_CUSTOMERS  →  QB_ENTITY_JOIN: join_table=PIO_CUSTOMERS
GROUP BY CUS_NUM           →  QB_AGGREGATION: group_field=CUS_NUM
HAVING COUNT(*) > 3        →  QB_AGGREGATION: having_op=">", having_val=3
```

**Decomposition Algorithm:**

```python
class SQLDecomposer:
  
    def decompose(self, sql: str, intent: AMLIntent) -> ScenarioParameters:
        # Step 1: AST Parse the SQL (using sqlglot — Oracle dialect)
        ast = parse_sql(sql)
      
        # Step 2: Extract each QB-mappable construct
        tables     = self._extract_tables(ast)        # FROM + JOINs
        conditions = self._extract_conditions(ast)    # WHERE clauses
        time_filters = self._extract_time(ast)        # Date-based conditions
        aggregations = self._extract_aggregations(ast) # GROUP BY + HAVING
      
        # Step 3: Map to QB parameter objects
        qb_tables     = [self._map_table_to_qb(t) for t in tables]
        qb_conditions = [self._map_condition_to_qb(c) for c in conditions]
      
        # Step 4: Validate completeness
        confidence = self._assess_confidence(qb_tables, qb_conditions, intent)
      
        return ScenarioParameters(
            tables=qb_tables,
            conditions=qb_conditions,
            time_filters=time_filters,
            aggregations=aggregations,
            confidence=confidence
        )
  
    def _assess_confidence(self, ...):
        """Self-assess how well the decomposition covers the intent.
        If < 0.8, trigger a re-query or clarification cycle."""
```

**This is the "human implementation team" knowledge encoded as an algorithm.** Every rule they follow manually when filling the QB dropdowns becomes a mapping function here.

> **BLOCKER — Q1:** We need the exact Oracle table definitions for the QB parameter tables (QB_SCENARIO_TABLES, QB_CONDITIONS, or their real equivalents). This is the #1 prerequisite for implementing this node.

---

### 5. QB WRITER NODE

**Goal:** Call the legacy Oracle Query Builder procedure with the decomposed parameters.

```python
class QBWriterNode:
  
    def execute(self, params: ScenarioParameters) -> str:
        """
        Calls the existing Oracle QB engine.
        Two modes depending on how QB is invoked:
          A) If QB has a stored procedure API → call it directly
          B) If QB reads from parameter tables → INSERT then trigger
        """
      
        # Mode A: Stored procedure call
        conn.execute("""
            BEGIN
                PKG_QUERY_BUILDER.CREATE_SCENARIO(
                    p_name       => :name,
                    p_table_ref  => :table_ref,
                    p_conditions => :conditions_json,
                    p_status     => 'DRAFT'
                );
            END;
        """, params.to_oracle_bindings())
      
        # Mode B: Table-based
        # INSERT INTO QB_SCENARIOS (...)
        # INSERT INTO QB_CONDITIONS (...) for each condition
        # Then trigger or call activation procedure
      
        return scenario_id
```

> **BLOCKER — Q2:** We need to know how the QB engine is invoked (stored procedure vs. direct table inserts). This determines the entire implementation of this node.

---

### 6. SCENARIO VALIDATOR NODE

**Goal:** Agentic self-verification — did the scenario actually work?

This is what separates this system from anything in the market. After writing the scenario, the agent **hunts its own output**.

```python
class ScenarioValidatorNode:
  
    def validate(self, scenario_id: str, intent: AMLIntent) -> ValidationResult:
      
        # STEP 1: Check if scenario is ACTIVE (not in error state)
        status = self._check_scenario_status(scenario_id)
        if status != "ACTIVE":
            return ValidationResult(success=False, reason="QB failed to activate scenario")
      
        # STEP 2: Run the detection query independently
        # Call PioTech AI: "how many alerts does scenario X produce?"
        alert_count_sql = self._build_alert_count_query(scenario_id)
        alert_count = self._execute_via_piotech_ai(alert_count_sql)
      
        # STEP 3: Sanity-check the alert count
        if alert_count == 0:
            # Could mean: wrong thresholds, wrong table, wrong time window
            # → Trigger re-decomposition with adjusted parameters
            return ValidationResult(
                success=False, 
                alert_count=0,
                diagnosis="Zero alerts. Likely threshold too restrictive or wrong time window.",
                suggested_fix=self._diagnose_zero_alerts(intent)
            )
      
        if alert_count > intent.expected_max_alerts:
            # Threshold too permissive → would flood compliance team
            return ValidationResult(
                success=False,
                alert_count=alert_count,
                diagnosis=f"Alert volume ({alert_count}) exceeds expected range. Scenario may be too broad.",
                suggested_fix="Tighten threshold or add exclusion criteria."
            )
      
        # STEP 4: Pull sample alerts for human review
        sample_alerts = self._pull_sample_alerts(scenario_id, limit=5)
      
        return ValidationResult(
            success=True,
            alert_count=alert_count,
            sample_alerts=sample_alerts,
            confidence_score=self._calculate_quality_score(alert_count, intent)
        )
```

**The self-correction loop:**

```
Validate → FAIL (0 alerts) → Feed diagnosis back to Decomposer →
Decomposer adjusts parameters → QB Writer re-writes → Validate again
Max 3 correction cycles before escalating to user.
```

---

## The Control Flow — Full Lifecycle Example

```
User: "Flag customers doing more than 5 cash transactions > JD 5,000 in 30 days"
        │
        ▼
[INTENT ANALYST]
  → Parses: entity=CUSTOMER, trx_type=CASH, count_op=">", count_val=5,
            amount_op=">", amount_val=5000, time_window=30 days
  → Confidence: HIGH (no clarification needed)
        │
        ▼
[SQL BRIDGE] → Calls PioTech AI →
  → Returns: SELECT CUS_NUM FROM BI_DWH.PIO_TXN_CASH
             WHERE TXN_AMT > 5000
             AND TXN_DATE >= SYSDATE - 30
             GROUP BY CUS_NUM
             HAVING COUNT(*) > 5
        │
        ▼
[DECOMPOSER]
  → tables:     [{table: "PIO_TXN_CASH", alias: "T1", role: "PRIMARY"}]
  → conditions: [{field: "TXN_AMT", op: ">", val: 5000, type: "FILTER"},
                 {field: "CUS_NUM", op: "GROUP_BY", type: "AGGREGATION"},
                 {field: "COUNT(*)", op: ">", val: 5, type: "HAVING"}]
  → time:       [{field: "TXN_DATE", window: 30, unit: "DAYS"}]
  → confidence: 0.94
        │
        ▼
[QB WRITER]
  → Calls PKG_QUERY_BUILDER.CREATE_SCENARIO(...)
  → scenario_id = "SCN-20260617-004"
        │
        ▼
[VALIDATOR]
  → Status: ACTIVE ✓
  → Alert count: 23 customers
  → Sample: [CUS_001, CUS_045, CUS_112, ...]
  → Confidence: HIGH (23 is a reasonable and actionable volume)
        │
        ▼
[ORCHESTRATOR → USER]
  "✅ Scenario 'Cash Velocity - 30 Day Window' is live.

   📊 Current Impact:
   - 23 customers match this scenario
   - Sample: Customer 001 (7 transactions, JD 67,400 total), ...

   The scenario has been activated in the AML module.
   Would you like to adjust the threshold or review the full alert list?"
```

---

## Integration Architecture — How It Sits Next to PioTech AI

```
┌─────────────────────────────────────────────────────────────────┐
│                      PioTech AI Platform                        │
│                                                                 │
│  ┌─────────────────┐         ┌──────────────────────────────┐  │
│  │  DWH Agent      │         │  AML SCENARIO AGENT (NEW)    │  │
│  │  (Text-to-SQL)  │◄────────│  Port: 8005                  │  │
│  │  Port: 8001     │  HTTP   │  Module: services/aml_builder│  │
│  └─────────────────┘         └──────────────────────────────┘  │
│                                         │                       │
│  ┌─────────────────┐                   │ Oracle cx_Oracle       │
│  │  AML Agent      │                   ▼                       │
│  │  (Reporting)    │         ┌──────────────────────────────┐  │
│  │  Port: 8002     │         │  PioTech AML Oracle DB       │  │
│  └─────────────────┘         │  - QB_SCENARIOS (TBD)        │  │
│                               │  - QB_CONDITIONS (TBD)       │  │
│  ┌─────────────────┐         │  - PKG_QUERY_BUILDER (TBD)   │  │
│  │  Shared Infra   │         └──────────────────────────────┘  │
│  │  Redis, Neo4j   │                                            │
│  │  ChromaDB       │                                            │
│  │  SQLite (ckpt)  │                                            │
│  └─────────────────┘                                            │
└─────────────────────────────────────────────────────────────────┘
```

The new AML Scenario Agent is **a new microservice inside the existing PioTech monorepo** (`services/aml_builder/`), following the exact same patterns as `services/aml/` and `services/dwh/`.

---

## Technology Decisions

| Concern                          | Decision                         | Rationale                                         |
| -------------------------------- | -------------------------------- | ------------------------------------------------- |
| **Orchestration**          | LangGraph StateGraph             | Same as existing system — no new infra           |
| **SQL Decomposition**      | `sqlglot` (Python AST library) | Production-grade, handles Oracle dialect natively |
| **PioTech AI Integration** | HTTP/SSE streaming client        | Reuse existing `/chat/stream` endpoint          |
| **QB Integration**         | `cx_Oracle` direct call        | Same Oracle pool already in the codebase          |
| **State Persistence**      | SQLite Checkpointer              | Same as existing agents                           |
| **API**                    | FastAPI + SSE streaming          | Identical to existing services                    |
| **Memory Pattern**         | Trajectory Compression           | Proven pattern documented in `doc/Notes.md`     |
| **LLM**                    | Configurable (OpenAI / LMStudio) | Same `settings.py` pattern as all other modules |

---

## Proposed Module Structure

```
services/
└── aml_builder/                        ← NEW MODULE
    ├── __init__.py
    ├── web/
    │   ├── api/
    │   │   └── main.py                 ← FastAPI entrypoint (port 8005)
    │   └── services/
    │       ├── agent.py                ← LangGraph StateGraph (main brain)
    │       ├── intent_analyst.py       ← Intent parsing & clarification
    │       ├── sql_bridge.py           ← PioTech AI HTTP/SSE client
    │       ├── decomposer.py           ← SQL → QB parameter mapping
    │       ├── qb_writer.py            ← Oracle QB procedure/table writer
    │       ├── validator.py            ← Agentic self-validation loop
    │       ├── schemas.py              ← All Pydantic contracts
    │       ├── settings.py             ← Config (inherits shared pattern)
    │       └── prompts/
    │           ├── orchestrator_system.md
    │           ├── intent_analyst_system.md
    │           └── validator_system.md
    ├── config/
    │   └── qb_field_mappings.json      ← Table/field → QB dropdown mapping
    └── tests/
        └── test_full_scenario_lifecycle.py
```

---

## The QB Field Mapping Config

A key static artifact: `qb_field_mappings.json` — a declarative map of how SQL constructs translate to QB UI dropdowns. This will be populated once the QB schema is confirmed.

```json
{
  "tables": {
    "BI_DWH.PIO_TXN_CASH": {
      "qb_table_id": "T_CASH_TXN",
      "qb_display_name": "Cash Transactions",
      "default_role": "PRIMARY"
    },
    "BI_DWH.PIO_CUSTOMERS": {
      "qb_table_id": "T_CUSTOMERS",
      "qb_display_name": "Customer Master",
      "default_role": "DIMENSION"
    }
  },
  "operators": {
    ">":             {"qb_op_code": "GT",      "qb_display": "Greater than"},
    "<":             {"qb_op_code": "LT",      "qb_display": "Less than"},
    "=":             {"qb_op_code": "EQ",      "qb_display": "Equals"},
    "BETWEEN":       {"qb_op_code": "BTW",     "qb_display": "Between"},
    "IN":            {"qb_op_code": "IN",      "qb_display": "In list"},
    "GROUP BY":      {"qb_op_code": "GRP",     "qb_display": "Group by"},
    "HAVING COUNT(*)": {"qb_op_code": "HAV_CNT", "qb_display": "Having count"}
  },
  "time_patterns": {
    "SYSDATE - {N}":          {"qb_type": "ROLLING_WINDOW", "unit": "DAYS"},
    "TRUNC(SYSDATE, 'MM')":   {"qb_type": "MONTH_START"}
  }
}
```

---

## Open Questions — Blockers Before Final Implementation

> **These must be answered before the Decomposer and QB Writer can be finalized.**

### Q1: Query Builder Parameter Table Schema

What are the exact Oracle table names and column definitions for:

- The **scenario header table** (name, type, status, created_by)?
- The **conditions table** (field reference, operator code, value)?
- The **time window table** (rolling vs. fixed, unit, value)?
- The **aggregation/grouping table** (GROUP BY field, HAVING operator, HAVING value)?

### Q2: QB Engine Invocation Method

How does the current human implementation team commit a scenario to the engine?

- Is there an Oracle stored procedure? (If yes: package name, procedure name, parameter list)
- Do they INSERT directly into QB tables, then call a separate activation/build procedure?
- Is there a REST API on the PioTech AML backend that wraps the QB engine?

### Q3: QB Table / Field Catalog

Does the QB engine maintain a catalog of allowed tables and fields?

- Is there a lookup table listing valid QB table references with their internal IDs?
- Or does the QB engine accept raw Oracle schema-qualified table names directly?

### Q4: Alert Output Table

When a scenario runs and produces alerts:

- What is the Oracle table where alerts are stored?
- What column links an alert row to its source scenario (`SCENARIO_ID` or equivalent)?
- What query would return `COUNT(*) of alerts for scenario X`?

### Q5: PioTech AI Internal Endpoint

- What is the internal base URL of the DWH/AML text-to-SQL agent (e.g., `http://localhost:8001`)?
- Which specific service should the SQL Bridge call — DWH (general) or AML (domain-tuned)?

---

## What Makes This Different

Most "AI AML tools" in the market:

- ❌ Generate SQL and stop — no QB integration, still requires human translation
- ❌ Require a technical intermediary between AI output and the product
- ❌ Have no self-validation loop — no way to know if the scenario even fires
- ❌ Are deterministic pipelines dressed up as "AI" — capped at simple scenarios
- ❌ Cannot handle the long-tail of ambiguous, real-world compliance language

This system:

- ✅ Completes the **full lifecycle** autonomously — intent to live scenario
- ✅ Uses **PioTech AI** as a grounded sub-capability — no hallucinated SQL
- ✅ **Self-validates** its own output against live Oracle data
- ✅ **Self-corrects** via a structured diagnosis loop (max 3 cycles before escalation)
- ✅ Handles **ambiguity** with domain-expert clarification — never technical questions
- ✅ Is **production-grade** — same LangGraph / FastAPI / Oracle stack already running
- ✅ Lives **inside the monorepo** — no foreign system integration cost

---

## Phased Delivery

### Phase 1 — Foundation (Week 1–2)

- [ ] Module skeleton (`services/aml_builder/`)
- [ ] State schema & all Pydantic contracts (`schemas.py`)
- [ ] Orchestrator node + routing logic
- [ ] Intent Analyst node + clarification protocol
- [ ] FastAPI streaming endpoint (`main.py`)

### Phase 2 — SQL Bridge (Week 2–3)

- [ ] PioTech AI HTTP/SSE streaming client (`sql_bridge.py`)
- [ ] SQL metadata extractor (tables, columns, operators from response)
- [ ] QB field mapping config skeleton (`qb_field_mappings.json`)

### Phase 3 — Decomposer + QB Writer (Week 3–5)

- [ ] `sqlglot`-based SQL AST decomposer (`decomposer.py`)
- [ ] QB parameter mapping engine (requires Q1–Q3 answers)
- [ ] Oracle QB procedure / table writer (`qb_writer.py`)

### Phase 4 — Validator (Week 5–6)

- [ ] Alert count query builder
- [ ] Self-correction loop (diagnosis → adjust → re-write)
- [ ] Diagnosis engine for zero-alert and over-alert cases

### Phase 5 — Polish & Hardening (Week 6–7)

- [ ] Full lifecycle test harness
- [ ] Error recovery & user escalation paths
- [ ] Deployment config (`docker-compose` entry, `.env` vars)
- [ ] README and handover documentation

---

*Document status: **UNDER REVIEW** — awaiting critique and open question answers before coding begins.*
