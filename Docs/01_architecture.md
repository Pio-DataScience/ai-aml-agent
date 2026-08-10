# 01 — Architecture Overview

> Complete technical architecture specification for the AI AML Scenario Builder platform.

---

## 1. System Overview

The **AI AML Scenario Builder** is an agentic AI solution for enterprise financial crime compliance. It enables AML compliance officers to define detection scenarios in plain English, translates them into structured Pydantic contracts (`AMLIntent`), performs live DWH shadow testing, and persists validated scenarios directly into the production Oracle database (`BI_DWH`).

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                             USER INTERFACE                                  │
│                 React Chat UI + Side Panel (Plan Renderer)                  │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │ SSE Stream
┌──────────────────────────────────────▼──────────────────────────────────────┐
│                    SERVICE A — AML SCENARIO BUILDER                         │
│               FastAPI + Autonomous ReAct Tool-Driven Agent                   │
│                                                                             │
│   Tools:                                                                    │
│   1. analyze_intent_and_discover_explanation_codes                          │
│   2. generate_scenario_execution_plan                                       │
│   3. execute_oracle_dwh_shadow_test                                         │
│   4. persist_and_validate_scenario_in_dwh                                  │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │ HTTP SSE (Intent Payload)
┌──────────────────────────────────────▼──────────────────────────────────────┐
│                    SERVICE B — PIOTECH AI SQL ENGINE                        │
│           Oracle Text-to-SQL Engine (Grounds against BI_DWH)                │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │ Read / Write
┌──────────────────────────────────────▼──────────────────────────────────────┐
│                            ORACLE DWH (BI_DWH)                              │
│   - Live Financial Transaction Data (Shadow Testing)                        │
│   - PIO_EXPLANATION_CODE (Vector Embedding Search Catalog)                  │
│   - PIO_AML_PRODUCTION_SCENARIOS (Production Persistence Table)             │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Core Architectural Principles

1. **Zero Multi-Node Routing Complexity**:
   Replaced rigid multi-node state graphs with a single autonomous **LangGraph ReAct agent** (`agent_tool_driven.py`). The LLM autonomously chooses tool invocation sequences based on prompt goal instructions.

2. **Single Source of Truth (`AMLIntent`)**:
   All communication between agents and Service B relies exclusively on `AMLIntent` Pydantic contracts (`web/services/schemas.py`). No ambiguous prose is passed between nodes.

3. **Deterministic Implementation Plan Rendering**:
   Plan generation (`generate_scenario_execution_plan`) uses a deterministic Python renderer (`_build_plan_markdown`). Zero LLM involvement in plan formatting guarantees structured, error-free side-panel rendering.

4. **Production Table Registry**:
   Validated scenarios are persisted directly into `PIO_AML_PRODUCTION_SCENARIOS` via `production_registry.py` for automated daily ETL execution.

---

## 3. Tool Pipeline Specification

### Tool 1: `analyze_intent_and_discover_explanation_codes`
- **Extracts**: Translates user prompt into `AMLIntent` using 11 Extraction Laws in `system_instruction`.
- **Coerces**: Coerces numeric percentage strings (`"300%"` → `3.0`, `"90%"` → `0.90`) via `@field_validator`.
- **Deduplicates**: Purges redundant `DETAIL` thresholds on cumulative `SUM`/`COUNT` scenarios via `@model_validator`.
- **Vector Search**: Performs cosine similarity search over `PIO_EXPLANATION_CODE` (6,900+ rows) using OpenAI embeddings (`text-embedding-3-small`) to discover domain explanation codes.

### Tool 2: `generate_scenario_execution_plan`
- **Renders**: Produces a 11-section markdown implementation plan artifact for the side panel UI.
- **Structure**:
  - Banner: Status (`Ready for Handoff` vs `Clarification Required`)
  - 1. Overview (Scenario Name, Type, Customer Segments, Evaluation Mode)
  - 2. Detection Logic
  - 3. Observation & Baseline Windows
  - 4. Aggregation Profile (Metric, Function, Grain)
  - 5. Thresholds (`WHERE` Detail vs `HAVING` Aggregate tables)
  - 6. Behavioral & Semantic Rules (Typed table)
  - 7. Exclusion Rules
  - 8. Explanation Codes
  - 9. Applied Defaults
  - 10. Open Clarifications
  - 11. Shadow Testing Scope

### Tool 3: `execute_oracle_dwh_shadow_test`
- **Delegates**: Calls Service B (PioTech AI SSE service) with `AMLIntent` payload to generate production Oracle SQL.
- **Shadow Tests**: Wraps SQL in `SELECT COUNT(*) FROM (...)` and executes against `BI_DWH` via `oracle.py`.
- **Metrics**: Computes `header_alert_count`, `transaction_detail_count`, and `alert_density_ratio`.

### Tool 4: `persist_and_validate_scenario_in_dwh`
- **Validates**: Ensures SQL, scenario name, and explanation codes are valid.
- **Persists**: Inserts the scenario record into `PIO_AML_PRODUCTION_SCENARIOS`.

---

## 4. Key Contracts & Schemas

### `AMLIntent` Schema (`web/services/schemas.py`)
- `scenario_name`: str
- `scenario_type`: Literal["CUSTOMER", "TRANSACTION", "ACCOUNT"]
- `transaction_type`: Optional[str]
- `thresholds`: List[Threshold]
- `time_window`: Optional[TimeWindow]
- `baseline_window`: Optional[BaselineWindow]
- `aggregation`: Optional[AggregationProfile]
- `customer_segments`: Optional[List[str]]
- `exclusions`: Optional[List[str]]
- `semantic_conditions`: List[SemanticCondition]
- `anchor_date`: Optional[str] (`YYYY-MM-DD` for historical shadow test)
- `explanation_codes`: Optional[List[str]]

---

## 5. File & Directory Layout

```text
services/aml_builder/
├── requirements.txt
└── web/
    ├── api/
    │   └── main.py                   # FastAPI app, SSE streaming & chat history endpoints
    └── services/
        ├── agent_tool_driven.py      # Core ReAct agent & 4 autonomous tools
        ├── schemas.py                # Pydantic data contracts (AMLIntent, Threshold, etc.)
        ├── explanation_code_search.py# Vector similarity RAG search over DWH codes
        ├── oracle.py                 # Read-only and read-write Oracle DB connection pool
        ├── production_registry.py    # Database writer for PIO_AML_PRODUCTION_SCENARIOS
        ├── settings.py               # Environment configuration loader
        ├── logging_config.py         # Structured logging configuration
        └── prompts/
            └── tool_driven_system.md # Autonomous agent system prompt
```
