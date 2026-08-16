# PIO_AML_SCENARIO Metadata Schema & Next-Phase Engine Blueprint

> **Purpose**: Technical specification of mandatory columns in `PIO_AML_SCENARIO`, mapping rules to downstream AML workflow tables (`PIO_AML_CUSTOMERS`, `PIO_AML_CUSTOMERS_DET`), and architectural review of the Phase 2/3 metadata side-panel & standalone alert execution engine.

---

## 1. Executive Summary & Context

### Why are we still populating `PIO_AML_SCENARIO`?

While our new AI Scenario Builder generates raw Oracle SQL (`RAW_SQL`) and stores it in `PIO_AML_PRODUCTION_SCENARIOS` (bypassing the legacy query builder tables like `PIO_AML_RULES` and `PIO_AML_RULES_DETAILS`), **`PIO_AML_SCENARIO` remains mandatory**.

`PIO_AML_SCENARIO` is **not** used for SQL generation — it serves as the **central business metadata registry** for the entire enterprise AML platform:

1. **Downstream Alert UI Dashboards**: Compliance investigation screens join `PIO_AML_CUSTOMERS` alerts back to `PIO_AML_SCENARIO` to display risk levels (`RISK_DEGREE`), scenario descriptions (`SCENARIO_DESC`), categories (`CATEG_CODE`), and breach severity (`VIOLATION_LEVEL`).
2. **AML Workflow & Case Routing**: Investigator queues, automated case creation (`WORKCASE`), and alert scoring engines rely on `PIO_AML_SCENARIO` metadata fields to route alerts to junior vs senior compliance analysts.
3. **Audit & Governance**: Versioning (`VERSION_NUM`), authoring (`CREATED_BY`, `USER_NAME`), and scenario status flags (`ACTIVE_FLAG`, `SCENARIO_STATE`).

---

## 2. Mandatory Columns in `PIO_AML_SCENARIO`

Below is the complete field specification for `PIO_AML_SCENARIO`, detailing which columns are mandatory, their data types, lookup constraints, and population source.

| Column Name         | Data Type         | Nullable            | Source / Governance      | Lookup Table / Allowed Values                         | Description & Downstream Purpose                                                                                                        |
| ------------------- | ----------------- | ------------------- | ------------------------ | ----------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| `COUNTRY_CODE`    | `NUMBER`        | **NO**        | System Default           | Config (`AML_COUNTRY_CODE`, e.g. `400`)           | Country identifier (Jordan = 400).                                                                                                      |
| `INST_CODE`       | `NUMBER`        | **NO**        | System Default           | Config (`AML_INST_CODE`, e.g. `1`)                | Institution identifier code.                                                                                                            |
| `SCENARIO_CODE`   | `VARCHAR2(40)`  | **NO**        | System Auto-Gen          | Shared PK string (`SCEN_XXXX` / `PRD_XXXX`)       | Shared primary key linking`PIO_AML_SCENARIO`, `PIO_AML_PRODUCTION_SCENARIOS`, `PIO_AML_CUSTOMERS`, and `PIO_AML_CUSTOMERS_DET`. |
| `SCENARIO_DESC`   | `VARCHAR2(400)` | YES                 | User / Agent             | Plain English summary (max 400 chars)                 | Executive scenario title & description displayed on investigator alert screens.                                                         |
| `CATEG_CODE`      | `VARCHAR2(40)`  | **MANDATORY** | Compliance / UI          | `PIO_AML_CATEGORY` (e.g. `'1'`, `'2'`, `'3'`) | Typology classification (e.g. Structuring, Velocity, Profile Mismatch).                                                                 |
| `SCENARIO_STATE`  | `VARCHAR2(40)`  | **MANDATORY** | System Default           | `'1'` (Active), `'0'` (Draft/Inactive)            | Lifecycle state of scenario.                                                                                                            |
| `PERIOD_TYPE`     | `VARCHAR2(40)`  | **MANDATORY** | Mapped from`AMLIntent` | `'D'` (Days), `'M'` (Months), `'Y'` (Years)     | Time unit of observation window (from`time_window.unit`).                                                                             |
| `PERIOD_NUM`      | `NUMBER`        | **MANDATORY** | Mapped from`AMLIntent` | Numeric integer (e.g.`1`, `7`, `30`, `180`)   | Time window duration (from`time_window.value`).                                                                                       |
| `RISK_DEGREE`     | `VARCHAR2(40)`  | **MANDATORY** | Compliance / UI          | `PIO_AML_DEGREE_RISK` (`'H'`, `'M'`, `'L'`)   | Risk degree assigned to alerts fired by this scenario. Drives investigator triage priority.                                             |
| `VIOLATION_LEVEL` | `VARCHAR2(40)`  | **MANDATORY** | Compliance / UI          | `'HIGH'`, `'MEDIUM'`, `'LOW'`                   | Breach severity level. Used for automated escalation workflows.                                                                         |
| `CREATED_BY`      | `NUMBER`        | **MANDATORY** | Session / Auth           | Numeric User ID (e.g.`999`)                         | Author user ID.                                                                                                                         |
| `ACTIVE_FLAG`     | `VARCHAR2(40)`  | **MANDATORY** | UI / Default             | `'1'` (Active), `'0'` (Inactive)                  | Enables/disables scenario execution in daily batch runs.                                                                                |
| `SYS_DATE`        | `DATE`          | **MANDATORY** | System                   | `SYSDATE`                                           | Creation/modification timestamp.                                                                                                        |
| `VERSION_NUM`     | `NUMBER`        | **MANDATORY** | System Default           | Integer (Default`1`)                                | Version tracking number.                                                                                                                |
| `USER_NAME`       | `VARCHAR2(400)` | YES                 | Session / Auth           | User string (e.g.`'anas@pio-tech'`)                 | Human username of author/modifier.                                                                                                      |

---

## 3. Data Flow Architecture: Service A → Service B → Alert Tables

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                      PHASE 2: INTENT & METADATA BUILD                       │
│                                                                             │
│  User Chat Prompt + Side Panel Clicks                                       │
│    ├── Intent Agent extracts AMLIntent JSON                                 │
│    ├── Side Panel auto-populates metadata JSON (RISK_DEGREE, CATEG_CODE...) │
│    └── Persist Tool validates & executes ATOMIC INSERT into:                │
│         1. PIO_AML_PRODUCTION_SCENARIOS (RAW_SQL + INTENT JSON)            │
│         2. PIO_AML_SCENARIO (Business Metadata for UI/Workflows)           │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                      PHASE 3: STANDALONE ALERT ENGINE                       │
│                                                                             │
│  Daily Batch Runner (Cron / Task Scheduler)                                 │
│    1. Fetches active scenarios from PIO_AML_PRODUCTION_SCENARIOS            │
│    2. Executes RAW_SQL against BI_DWH                                       │
│    3. Joins flagged records with PIO_AML_SCENARIO metadata                  │
│    4. Inserts results into Alert Tables:                                    │
│         ├── PIO_AML_CUSTOMERS     (Alert Header per Customer/Date)          │
│         └── PIO_AML_CUSTOMERS_DET (Transaction Details from PIO_TRANSACTIONS)│
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 4. Operational Mapping to Alert Tables

When the Phase 3 Standalone Engine runs a scenario's `RAW_SQL`, it populates `PIO_AML_CUSTOMERS` and `PIO_AML_CUSTOMERS_DET` by mapping fields from `PIO_AML_SCENARIO`, `PIO_TRANSACTIONS`, and the query results:

### Header Alert Mapping (`PIO_AML_CUSTOMERS`)

- `DAY_DATE`: Batch run evaluation date (`TRUNC(SYSDATE)` or `anchor_date`).
- `COUNTRY_CODE`: `PIO_AML_SCENARIO.COUNTRY_CODE`
- `INST_CODE`: `PIO_AML_SCENARIO.INST_CODE`
- `CUS_NUM`: Customer number returned by `RAW_SQL`.
- `AML_SCENARIO_CODE`: `PIO_AML_SCENARIO.SCENARIO_CODE`
- `RISK_DEGREE`: `PIO_AML_SCENARIO.RISK_DEGREE`
- `CUS_NAME` / `CIF` / `BRA_CODE` / `MOBILE_NO` / `ID_NUMBER`: Fetched from customer master table (`PIO_CUSTOMER`) during alert enrichment.
- `INITIAL_STATUS` / `CURRENT_STATUS`: Initial triage status (e.g. `'0'`, `'0'`).
- `SEQ`: Auto-incrementing sequence integer per `(DAY_DATE, CUS_NUM, AML_SCENARIO_CODE)`.

### Detail Alert Mapping (`PIO_AML_CUSTOMERS_DET`)

- `DAY_DATE`: Batch run snapshot date.
- `TRA_DAY_DATE`: `PIO_TRANSACTIONS.TRA_DAY_DATE`
- `COUNTRY_CODE` / `INST_CODE`: `PIO_AML_SCENARIO` identifiers.
- `TRA_DATE`, `TRA_SEQ1`, `TRA_SEQ2`, `BRA_CODE`, `CUS_NUM`, `CUR_CODE`, `LED_CODE`, `SUB_ACCT_CODE`, `ACCOUNT_NUMBER`, `TRA_AMT`, `EQU_TRA_AMT`, `EXPL_CODE`: **Selected directly from `PIO_TRANSACTIONS`** for the specific transactions that breached the scenario thresholds.
- `AML_SCENARIO_CODE`: `PIO_AML_SCENARIO.SCENARIO_CODE`
- `AML_RULE_CODE`: `PIO_AML_SCENARIO.SCENARIO_CODE` (or default rule ID).
- `LINK_CUS_NUM` / `REL_TYPE` / `ENTITIY_SIGNATORY`: Defaults (`'N/A'` / `'0'`) if not applicable, ensuring non-null DDL constraints pass cleanly.
- `SEQ`: Detail sequence counter per customer alert.

---

## 5. Architectural Recommendations & Missing Pieces Checklist

Before proceeding to code Phase 2 & Phase 3, review these 5 critical architectural recommendations:

### 1. Dual-Table Atomic Transaction (Persistence Tool)

- **Constraint**: `save_production_scenario()` in `production_registry.py` must write to **BOTH** `PIO_AML_PRODUCTION_SCENARIOS` and `PIO_AML_SCENARIO` in a **single atomic Oracle transaction** (`conn.commit()`).
- **Why**: If `PIO_AML_PRODUCTION_SCENARIOS` gets the SQL but `PIO_AML_SCENARIO` fails (or vice versa), the scenario will either fail to display in compliance investigation UIs or fail to run in the alert engine.

### 2. Standalone Engine Transaction-Detail Output Contract

- **Constraint**: When Service B (PioTech AI) generates `RAW_SQL` for transaction-level or daily cumulative scenarios, the SQL must select standard transaction key columns (`TRA_DAY_DATE`, `TRA_DATE`, `TRA_SEQ1`, `TRA_SEQ2`, `BRA_CODE`, `CUS_NUM`, `CUR_CODE`, `LED_CODE`, `SUB_ACCT_CODE`, `ACCOUNT_NUMBER`, `TRA_AMT`, `EQU_TRA_AMT`, `EXPL_CODE`).
- **Why**: The standalone engine needs these columns to execute a clean `INSERT INTO PIO_AML_CUSTOMERS_DET ... SELECT ... FROM PIO_TRANSACTIONS WHERE ...`.

### 3. Sequence (`SEQ`) Generation Safety in Alert Populator

- **Constraint**: In `PIO_AML_CUSTOMERS` and `PIO_AML_CUSTOMERS_DET`, `SEQ` is part of the Primary Key.
- **Why**: The standalone populator engine must generate `SEQ` using `ROW_NUMBER() OVER (PARTITION BY CUS_NUM ORDER BY TRA_DATE)` or DB sequences to avoid `ORA-00001: unique constraint violated` when multiple transactions flag for the same customer on the same day.

### 4. Non-Null Fallbacks for Mandatory DDL Columns in `PIO_AML_CUSTOMERS_DET`

- **Constraint**: Columns like `LINK_CUS_NUM`, `REL_TYPE`, and `ENTITIY_SIGNATORY` in `PIO_AML_CUSTOMERS_DET` are defined as `NOT NULL` in Oracle DDL.
- **Why**: Non-counterparty scenarios (e.g. single large cash deposit) won't have a linked customer. The standalone engine must supply standard fallbacks (`LINK_CUS_NUM = CUS_NUM`, `REL_TYPE = 'SELF'`, `ENTITIY_SIGNATORY = 'N/A'`) so the INSERT statement doesn't crash with `ORA-01400: cannot insert NULL`.

### 5. Auto-Mapping `AMLIntent` to Side-Panel Metadata JSON

- **Constraint**: When the user describes a scenario in chat, `analyze_intent_and_discover_explanation_codes` extracts `AMLIntent`.
- **Optimization**: The backend should pre-fill the side panel Metadata JSON from `AMLIntent` defaults:
  - `PERIOD_TYPE`: Mapped from `time_window.unit` (`DAYS` → `'D'`, `MONTHS` → `'M'`).
  - `PERIOD_NUM`: Mapped from `time_window.value`.
  - `SCENARIO_DESC`: Mapped from `intent.detection_logic`.
- This minimizes manual clicks for the user while keeping the compliance officer in full control to override values (`RISK_DEGREE`, `CATEG_CODE`, `VIOLATION_LEVEL`) before clicking **Persist**.
