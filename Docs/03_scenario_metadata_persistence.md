# 03 — Scenario Governance Metadata & Atomic Dual-Write Persistence

> Phase 2 of the Production Scenario Registry. Companion to
> [`PIO_AML_SCENARIO_schema_guide.md`](PIO_AML_SCENARIO_schema_guide.md) (the business/architecture
> rationale) and [`01_architecture.md`](01_architecture.md) (overall system architecture).
> Phase 3 (the standalone daily batch alert engine that populates `PIO_AML_CUSTOMERS`/
> `PIO_AML_CUSTOMERS_DET`) is out of scope here — a separate follow-up task.

---

## 1. What & why

Before this change, `persist_and_validate_scenario_in_dwh` wrote only `PIO_AML_PRODUCTION_SCENARIOS`
(the raw SQL consumed by the daily ETL runner). But `PIO_AML_SCENARIO` — business metadata (risk
degree, category, violation level, period) — is what downstream compliance UI/workflow modules
actually join against to display and route alerts. It remains mandatory even though this system
bypasses the legacy QB-engine SQL-building tables entirely.

The governing rule from day one of this design: **every governed field's value must come from a real
Oracle lookup table (or a small fixed enum) — the LLM may present options, but must never invent or
guess a value.** This is a compliance requirement, not a UX preference.

## 2. How it works

### Field registry (`web/services/scenario_metadata.py`)

Every `PIO_AML_SCENARIO` column is one `FieldSpec` entry in `SCENARIO_METADATA_FIELDS`, tagged with a
`kind`:

| Kind | Meaning | Example fields |
|---|---|---|
| `system_default` | Filled from settings, officer never sees it | `COUNTRY_CODE`, `INST_CODE`, `CREATED_BY`, `VERSION_NUM` |
| `derived` | Computed deterministically from `AMLIntent`, no LLM call | `SCENARIO_DESC`, `PERIOD_NUM` |
| `static_enum` | Small fixed set of allowed values, no live query | `SCENARIO_STATE`, `PERIOD_TYPE`, `VIOLATION_LEVEL`, `ACTIVE_FLAG` |
| `oracle_lookup` | Values come from a live Oracle table — the only source of truth | `CATEG_CODE` ← `PIO_AML_CATEGORY`, `RISK_DEGREE` ← `PIO_AML_DEGREE_RISK` |

Adding a new column is one new `FieldSpec` entry — nothing else in the module changes.

### The flow

1. **Turn 1 — seeding.** `analyze_intent_and_discover_explanation_codes` calls
   `seed_scenario_metadata(intent, existing)` after building `AMLIntent`. It maps
   `time_window.unit/value → PERIOD_TYPE/PERIOD_NUM` (weeks normalize to a day count — `PERIOD_TYPE`
   only supports D/M/Y), `detection_logic → SCENARIO_DESC` (400-char cap), and fills the system
   defaults. **It never overwrites a value already present** in `existing` — side-panel clicks and
   prior-turn values always win. The result is returned as `scenario_metadata` in the tool's JSON output.
2. **Step 4 — catalog.** Once the shadow test is approved, the agent calls
   `prepare_scenario_metadata_for_persistence(metadata_json)`. It calls `build_metadata_catalog()`,
   which attaches the currently valid options to every `static_enum`/`oracle_lookup` field (live Oracle
   query for the latter, filtered by `COUNTRY_CODE`/`INST_CODE`), and reports `missing_mandatory_fields`
   + `ready_for_persistence`. This is streamed to the frontend as a new `scenario_metadata_catalog` SSE
   event for the side panel; the agent also relays any missing fields in chat as a fallback to clicking
   the panel — matching the officer's two input paths (chat or click) described in the design discussion.
3. **Step 5 — persist.** `persist_and_validate_scenario_in_dwh` calls
   `validate_scenario_metadata()` first. If anything fails — a mandatory field is still null, or a
   value doesn't match its live/static allowed-value set — **nothing is written to either table**, and
   the tool returns `write_success: false` with a `validation_errors` list for the agent to relay.
   Only on success does it call the atomic dual-write.

### Atomic dual-write (`production_registry.py`)

`save_production_scenario()` now writes `PIO_AML_PRODUCTION_SCENARIOS` and `PIO_AML_SCENARIO` on one
shared cursor inside `oracle.atomic_connection()` — a single commit, full rollback on either failing.
Both tables share the same `SCENARIO_CODE`/`SCENARIO_ID` value (`PRD_{uuid8}`, unchanged format).
Re-persisting an existing code updates both rows in place and increments `PIO_AML_SCENARIO.VERSION_NUM`.

## 3. Backend/frontend split

This repository is backend-only. It provides:
1. The structured metadata contract (`scenario_metadata` dict shape, `FieldSpec` catalog).
2. The live lookup-fetching mechanism (`fetch_lookup_options`).
3. The `scenario_metadata_catalog` SSE event carrying the catalog to the frontend.
4. The validation guard-rail and atomic write.

Actual side-panel rendering (dropdowns, click-to-select) is implemented in the frontend repository,
which is not part of this codebase.

## 4. Known placeholders — need compliance confirmation

`scenario_metadata.py` has two `LookupTableConfig` constants marked `TODO(compliance)`:

```python
LOOKUP_CATEG_CODE = LookupTableConfig(table="PIO_AML_CATEGORY", code_column="CATEG_CODE", desc_column="DESC_ENG")
LOOKUP_RISK_DEGREE = LookupTableConfig(table="PIO_AML_DEGREE_RISK", code_column="DEGREE_CODE", desc_column="DESC_ENG")
```

These are best-guess column names (following the `PIO_EXPLANATION_CODE` naming convention: a `CODE`
column + a `DESC_ENG` column) — **not yet verified against the live schema.** Until confirmed,
`fetch_lookup_options()` will log an error and return an empty options list for these two fields,
which `validate_scenario_metadata()` correctly treats as "cannot verify" (blocking persistence) rather
than silently accepting anything. Fixing them is a one-line edit to the two constants above — no other
code changes needed.

`VIOLATION_LEVEL` is implemented as a static `HIGH`/`MEDIUM`/`LOW` enum, per
`PIO_AML_SCENARIO_schema_guide.md` (which names no live Oracle table for it). If compliance says
otherwise later, switching it to an `oracle_lookup` kind is also a one-line `FieldSpec` change.

## 5. How to run / verify

Same run commands as always (`.\run_dev.ps1` or `uvicorn app:app`). No new environment variables were
introduced by this phase.

Verified this pass: `py_compile` on every new/modified file; full `import app` with dummy credentials;
direct pure-Python invocation of `seed_scenario_metadata` → `prepare_scenario_metadata_for_persistence`
→ `persist_and_validate_scenario_in_dwh`, confirming the validation gate correctly blocks writes when
lookup values can't be verified (no live Oracle available in this environment) while the static-enum
field (`VIOLATION_LEVEL`) validates correctly with zero Oracle calls. **Not yet tested against a real
Oracle instance** — that requires the lookup table column names above to be confirmed first.

## 6. Remaining work (explicitly out of scope this pass)

- Confirm `PIO_AML_CATEGORY`/`PIO_AML_DEGREE_RISK` column names with the compliance team.
- Phase 3: the standalone daily batch engine that runs persisted `RAW_SQL` and populates
  `PIO_AML_CUSTOMERS`/`PIO_AML_CUSTOMERS_DET` — a separate task per user decision.
- A future agent tool to list/modify/update/delete already-persisted scenarios.
