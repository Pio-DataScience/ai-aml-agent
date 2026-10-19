# AML Scenario Builder: Refactor and Feature Plan

Ordered update packages for the IDE agent. Repos: **A** = `ai-aml-agent` (Service A), **UI** = `AI_AML_INTERFACE`, **B** = `PioTech-AI` (Service B, **not touched in this plan**).

---

## 0. How to use this file

1. Hand the IDE agent one package at a time, in order. Each package has a **Depends on** line, a **Work** list, **Acceptance tests**, and a ready-to-paste **IDE prompt**.
2. Every IDE prompt starts with the global rules below. Paste them once per session.
3. Decisions tagged `demo` are defaults chosen to get started. They live in catalogs/config, are editable without code, and are tracked in section 8 until the responsible people (compliance/business owners) confirm them.

### Global rules (prepend to every package prompt)

```
- Work only in the repos named in the package. NEVER modify PioTech-AI (Service B).
- Do not call live LLM, Oracle, or Service B in tests. Use fakes/fixtures.
- NEVER drop, delete or truncate any database rows or tables. P4 may update only
  the exact parent version's status/IS_ACTIVE and its matching PIO_AML_AGENT_SCENARIOS
  ACTIVE_FLAG in the same transaction as the child insert and audit row. Never
  write other automatic row updates. Inspect read-only (SELECT only). Where
  data must be checked, archived or removed, WRITE the SQL as text files for the
  owner to review and run in TOAD. Local file moves (sqlite state) are also
  owner-run, and nothing is ever deleted from disk.
- Keep existing tool names, SSE event names and UI behavior unchanged unless the
  package says otherwise.
- Prefer pure functions in domain/ with table-driven pytest tests.
- When done: run `python -m compileall`, `pytest -q` (new tests), and for UI
  `npm run lint` and `npm run build`. Report: files changed, tests added,
  deviations from the package, and anything that contradicts this plan.
- If something in the package is impossible or ambiguous, stop and report it
  instead of guessing.
```

---

## 1. Principles

1. One ReAct orchestrator, no new agents. The LLM does conversation and extraction; code owns every rule that must hold.
2. Typed per-thread workflow state is the source of truth, not the prompt and not ToolMessage scanning.
3. Approvals and confirmations are typed UI/API actions, never LLM-callable.
4. Business rules live in editable catalogs (YAML) with a `status: demo | confirmed` flag.
5. Everything deterministic is a pure function with table tests.
6. Fresh start: legacy chats, checkpoints and scenarios are archived, not migrated.

---

## 2. Decisions register

| # | Decision | Status | Configured in |
|---|---|---|---|
| D1 | One scenario per chat. After deploy the chat is **locked**: view, investigate, ask, registry query only. Tip popups point to a new chat. | decided | state `locked`, `ui_copy.yaml` |
| D2 | Changed scenarios are **new versions**, never in-place updates. Versions carry parent link, status, audit trail, rollback. | decided | registry v2 schema |
| D3 | Old scenarios are disposable: archive to file/backup tables, then the owner removes the rows (or renames the old production table) in TOAD using the generated runbook. No code deletes data. | decided | `scripts/archive_legacy.py`, `scripts/sql/` |
| D4 | Legacy chats/checkpoints/sidebar sqlite move to `old_data/<timestamp>/`; brand-new sqlite files for the new system. | decided | `STATE_DIR` setting |
| D5 | Dev creator ID is `999` (admin sign-in via UI). Temporary. | decided | `policy.yaml` users map |
| D6 | Service B changes are deferred. Service A compensates (explicit filter instructions in the intent, A-side SQL recheck). | decided | section 7 |
| D7 | Mock roles: `viewer` (read), `creator` (read + create), `manager` (read + create + modify + delete). | decided (mock) | `policy.yaml` |
| D8 | **Block** when (a) the same chat tries to deploy again, or (b) the new scenario is identical to an active one in everything but the ID. All other matches: notify, show the comparison, ask the user to confirm, cancel, or change params. | decided | `policy.yaml` `relations` |
| D9 | "Mandatory" params and filter dimensions are configurable; initial values below follow common transaction-monitoring practice. | demo | `params.yaml`, `filters.yaml` |
| D10 | Chat may NOT deploy to production by default. Deployment happens through the typed deploy action (button). Chat only prepares metadata. | demo | `policy.yaml` `chat_may_deploy: false` |
| D11 | Time window is mandatory; all-time is rejected; max window 365 days. | demo | `params.yaml` |
| D12 | Viewers read only their own chats plus any deployed scenario. No cross-user chat reading. | demo | `policy.yaml` `cross_user_chat_read: false` |
| D13 | Filter dimensions are applied whenever mentioned. A dimension may additionally be `required` (always asked). | demo | `filters.yaml` |
| PC1 | Card content: missing values first, then assumed defaults below (each editable). One click confirms everything. Confirm enables once all required params are resolved. | confirmed by user | `params.yaml` `card`, P6 |
| PC2 | Card and plan stay separate: confirm card, plan is generated, user presses **Proceed** (= plan approval). Never merged. | confirmed by user | P5, P6 |
| PC3 | A pending card stays pending indefinitely (no expiry) and reappears when the chat is reopened or reloaded. | confirmed by user | `pending_card_json`, P5, P6 |
| PC4 | Revision model: after the first card is confirmed and its first plan generated, any card change triggers a NEW plan. Each revision is a "trio" (card + plan), (SQL shadow test), (governance metadata) under one `revision_id`. The previous undeployed trio moves into a capped history; a deployed trio is never evicted or altered. First demo scope: revision data model + history storage/eviction + auto re-plan. History picker UI later, but model and API are designed for it. | confirmed by user | sections 3.2, 4.1, P3, P6b |
| PC5 | Non-UI callers (script/direct API) with missing required values are refused with a structured list of missing/unresolved params. Guessed values are never filled in. | confirmed by user | `confirm_params`, P6 |
| PC6 | Permanent application-level record per card: request/revision id, params asked, recommended values, user choice (`accepted_default` vs `user_input`), user, timestamps. In addition to LangSmith tracing. | confirmed by user | `param_card_audit`, P3, P6 |
| PC7 | Ambiguity detection is hybrid: the LLM flags unclear values; code validates required params against the catalog and blocks handoff if any are unresolved. | confirmed by user | P6 |
| PC8 | History cap default 5 (configurable); one pending card per chat, stale `request_id` rejected; typed input controls with one-click accept-recommended. | demo assumption | `policy.yaml` `revisions`, P6 |
| RV1 | Terminology: **revision** = a draft trio inside one chat (before deploy); **version** = a deployed scenario in the registry (P9). The P9 action is now called "Create new version". | demo assumption | glossary, P9 |
| RV2 | Plan (re)generation after a card confirmation is a deterministic server step (existing renderer), not an LLM turn. The chat gets a short action message so the assistant can acknowledge it. | demo assumption | P6, P6b |
| RV3 | Approvals (Proceed, result approval) never carry across revisions; each revision is approved by its own explicit actions. | demo assumption | P3, P6b |
| RV4 | Eviction keeps a tombstone row (ids, hashes, timestamps) and nulls the heavy payloads. Card audit rows are never evicted. | demo assumption | `scenario_revision.evicted_at` |
| RV5 | While a card is pending, Proceed, shadow test, result approval and deploy on the current revision are refused (`CARD_PENDING`) until the card is confirmed or discarded (`discard_card`). | demo assumption | guards |
| RV6 | "Pair a historical trio with the chat" (later phase) = copy-forward into a new current revision (`restored_from_revision_id`); the displaced current revision goes to history; approvals are cleared and the conflict check re-runs. The LangGraph checkpoint is never rewound. | demo assumption | `docs/revisions_api.md` |
| RV7 | Governance metadata edits stay in place on the current revision (no new revision). On a new revision, metadata is re-seeded and the fields the user edited are carried over and re-validated. | demo assumption | P6b |

Status legend: **decided / confirmed by user** = settled; **demo / demo assumption** = default chosen to get started, tracked in section 8 until the owners confirm.

---

## 3. Target architecture

### 3.1 Layout

```
services/aml_builder/
  domain/       pure functions: intent normalization + provenance, code binding,
                filter resolution, signature builder, relation comparator,
                plan renderer, SQL extraction + filter-applied check,
                hashing, window math
  catalogs/     params.yaml, filters.yaml, policy.yaml, ui_copy.yaml
  workflow/     ScenarioState, state machine, guards, WorkflowRepo (sqlite)
  adapters/     ServiceBClient, OracleRepo (lookups, shadow read-only, registry),
                CodeIndex (embeddings + rerank), LLM registry
  tools/        thin LangGraph wrappers (names unchanged)
  api/          routes, SSE mapper, tool-result codec, action endpoints, auth deps
```

### 3.2 Workflow stages and revisions

**Glossary.** A *revision* is a draft trio inside one chat: (card + plan), (SQL shadow test), (governance metadata), all tied to one `revision_id` and a running `rev_no`. A *version* is a deployed scenario in the registry (P9). One revision per chat is `current`.

Stages are tracked **per revision**:

```
EMPTY -> INTAKE -> PARAMS_PENDING -> PARAMS_CONFIRMED -> CONFLICT_CHECKED
      -> PLAN_PENDING -> PLAN_APPROVED -> SHADOW_PASSED -> RESULT_APPROVED
      -> METADATA_READY -> DEPLOYED (locked)
```

`PARAMS_CONFIRMED -> CONFLICT_CHECKED -> PLAN_PENDING` is one server step: confirming the card runs the conflict check and renders the plan automatically (RV2). It pauses only if a conflict blocks or needs acknowledgement. "Proceed" is the `approve_plan` action.

**Revision rules**
- The first confirmed card creates revision 1 (current).
- **Before the first plan exists**, a card change just replaces the card of revision 1.
- **After the first plan exists**, any confirmed card change creates revision N+1: a copy of the current revision with the new card, a new plan generated automatically, and empty shadow result and approvals. Revision N moves to `history`. Nothing in it is cleared (its shadow result and metadata stay with it).
- Governance-metadata-only edits happen in place on the current revision, clear `METADATA_READY` and later, and create no new revision (RV7).
- **History cap** (`policy.yaml: revisions.history_cap`, default 5): history counts undeployed, non-current revisions. When a new revision pushes history over the cap, the oldest is evicted: its payloads are nulled and a tombstone row stays (`evicted_at`). A deployed revision is never evicted or altered. Card audit rows are never evicted.
- **Pending card:** at most one per chat, stored in `pending_card_json` with a `request_id`. While it exists, `approve_plan`, shadow test, `approve_result` and `deploy` are refused with `CARD_PENDING` until the card is confirmed or discarded.
- A failed shadow test returns the revision to `PLAN_APPROVED`. A failed deploy stays in `METADATA_READY`.
- `DEPLOYED` is terminal for the chat: the chat is locked, `deployed_revision_id` is recorded, history stays readable and cannot be restored or edited. Changes happen through "Create new version" in a **new chat** (P9).
- **Restore from history (later phase, reserved):** copy-forward into a new current revision (RV6). No LangGraph checkpoint rewind: the workflow store is the source of truth, and the checkpoint holds only the conversation narrative.

### 3.3 Tool and action gating

| Tool / action | Allowed stages | Notes |
|---|---|---|
| `analyze_intent_and_discover_explanation_codes` | any stage before DEPLOYED | Produces a pending card (replaces an older pending card). Never changes the current revision by itself. Denied if locked. |
| `confirm_params` (action) | a pending card exists | Body: `request_id` + values/choices. Refuses with `CARD_STALE` (old `request_id`) or `PARAMS_INCOMPLETE` (missing/unresolved list). Creates/updates the revision, writes the card audit row, runs the conflict check, renders the plan. |
| `discard_card` (action) | a pending card exists | Drops the pending card, audit outcome `discarded`. |
| `acknowledge_conflicts` (action) | PARAMS_CONFIRMED | Needed only for warn-level relations. |
| `generate_scenario_execution_plan` | CONFLICT_CHECKED, PLAN_PENDING | Returns the current revision's plan (renders it if missing). Sole owner of plan rendering. Writes `plan_hash`. |
| `approve_plan` (action, label **Proceed**) | PLAN_PENDING | Must send `revision_id` and the expected `plan_hash`. |
| `execute_oracle_dwh_shadow_test` | PLAN_APPROVED | Stores typed `ShadowResult` with `sql_hash`. |
| `approve_result` (action) | SHADOW_PASSED | Must send expected `sql_hash`. Only if the shadow test passed. |
| `prepare_scenario_metadata_for_persistence` | RESULT_APPROVED, METADATA_READY | |
| `deploy` (action) | METADATA_READY | Goes through `DeployService`. |
| `persist_and_validate_scenario_in_dwh` (tool) | same as deploy | Removed from the agent's toolset while `chat_may_deploy: false`. |
| `query_scenario_registry_analytics` (name as in current code) | any stage, including DEPLOYED | Read-only. |

Every action carries the `revision_id` (and `request_id` for cards) it was shown for. Guard failures return a typed error (`STAGE_NOT_ALLOWED`, `CHAT_LOCKED_DEPLOYED`, `PARAMS_UNCONFIRMED`, `PARAMS_INCOMPLETE`, `CARD_STALE`, `CARD_PENDING`, `REVISION_STALE`, `PLAN_NOT_APPROVED`, `SHADOW_NOT_PASSED`, `HASH_MISMATCH`, `PERMISSION_DENIED`) that the LLM relays in plain language.

### 3.4 Hashes and receipt

- All hashes below are **per revision**.
- `params_hash`: stable JSON hash of confirmed params + resolved filters.
- `plan_hash`: hash of the rendered plan text.
- `sql_hash`: hash of normalized SQL (strip comments, collapse whitespace, case-fold outside string literals).
- `metadata_hash`: hash of validated governance metadata.
- `signature_hash`: hash of the canonical scenario signature (section 5, P8). **Excludes** name, IDs, governance metadata.
- Deployment receipt (JSON stored on the scenario row and in the audit table): thread, **`revision_id`/`rev_no`**, scenario ID/key/version, parent, **card audit request IDs**, all hashes, who/when for params confirmation, plan approval, result approval and deploy, shadow summary, conflict acknowledgements and overrides.

### 3.5 Tool-result envelope

`{schema_version, status: ok|error|blocked, error_code, payload}` through one codec used by history, SSE mapper, deploy and tools. Existing top-level keys the UI/history rely on (`plan_artifact`, `fields`, `scenario_metadata`, `write_success`, `header_alert_count`, `raw_sql`, `validation_success`) stay inside `payload` until P5 moves the UI to state events.

---

## 4. Data model

### 4.1 `app_state.sqlite` (new, under `STATE_DIR`)

```
scenario_workflow(
  thread_id TEXT PK, project_id, chat_id, user_id,
  stage TEXT,                                      -- mirrors the current revision, or DEPLOYED
  locked INTEGER, version INTEGER,                 -- optimistic lock
  scenario_id TEXT, scenario_key TEXT,             -- allocated at creation
  parent_scenario_id TEXT, parent_version INTEGER, -- set for new-version chats (P9)
  current_revision_id TEXT, deployed_revision_id TEXT,
  pending_card_json TEXT,                          -- {request_id, rows, base_revision_id, created_at} or NULL
  created_at, updated_at)
scenario_revision(                                 -- one row per draft trio; UNIQUE (thread_id, rev_no)
  revision_id TEXT PK, thread_id, rev_no INTEGER,
  status TEXT,                                     -- current | history | deployed | evicted
  stage TEXT,
  parent_revision_id, restored_from_revision_id,   -- restore reserved (RV6)
  card_json, params_hash, signature_json, signature_hash, conflict_json,
  plan_md, plan_hash, plan_approved_at, plan_approved_by,
  shadow_json, sql_hash, result_approved_at, result_approved_by,
  metadata_json, metadata_hash,
  created_at, created_by, superseded_at, superseded_by_revision_id, evicted_at)
param_card_audit(                                  -- permanent, append-only, never evicted
  request_id TEXT PK, thread_id, revision_id, user_id, user_role,
  source TEXT,                                     -- ui | api | system
  shown_at, resolved_at,
  outcome TEXT,                                    -- confirmed | auto_confirmed | discarded | superseded | refused
  rows_json)                                       -- per param: key, ask_status (missing|defaulted|ambiguous|provided),
                                                   -- recommended_value, choice (accepted_default|user_input|provided|system_default),
                                                   -- final_value, provenance
workflow_events(id PK, thread_id, ts, actor, action, from_stage, to_stage, detail_json)
chat_sessions(project_id, chat_id, user_id, title, pinned, deleted, created_at, updated_at,
              PRIMARY KEY (project_id, chat_id, user_id))
```

Checkpointer uses its own new file (`STATE_DIR/checkpoints.sqlite`). Put all behind `WorkflowRepo` and `CardAuditRepo` interfaces so they can move to Oracle later (the card audit is meant to be permanent, so decide its production home under C14). The checkpoint holds only the conversation; revision content lives in `scenario_revision`.

### 4.2 Oracle registry v2 (app-created, so redesign freely)

`PIO_AML_AGENT_PRODUCTION_SCENARIOS` (created if missing by `ensure_schema()`; existing objects are never altered; **keep** the columns the alert engine reads: `SCENARIO_ID, SCENARIO_NAME, SCENARIO_TYPE, RAW_SQL, TIME_WINDOW_DAYS`, and keep `IS_ACTIVE` in sync with `STATUS`):

| Column | Type | Purpose |
|---|---|---|
| `SCENARIO_ID` | VARCHAR2(50) PK | Version-specific ID, e.g. `PRD_AB12CD34_V1`. Referenced by alerts and the metadata table. |
| `SCENARIO_KEY` | VARCHAR2(50) NOT NULL | Stable across versions, e.g. `PRD_AB12CD34`. |
| `VERSION_NO` | NUMBER NOT NULL | 1, 2, 3... |
| `PARENT_SCENARIO_ID` | VARCHAR2(50) | Version this one was revised from. |
| `STATUS` | VARCHAR2(20) | `ACTIVE`, `SUPERSEDED`, `RETIRED`. |
| `IS_ACTIVE` | NUMBER(1) | 1 only when `STATUS='ACTIVE'`. |
| `THREAD_ID` | VARCHAR2(200) | Chat that created this version. |
| `REVISION_ID`, `REV_NO` | VARCHAR2(64), NUMBER | The draft revision (trio) that was deployed. |
| `SIGNATURE_HASH`, `PARAMS_HASH`, `PLAN_HASH`, `SQL_HASH`, `METADATA_HASH` | VARCHAR2(64) | |
| `SIGNATURE_JSON`, `PARAMS_JSON`, `FILTERS_JSON`, `METADATA_JSON`, `RECEIPT_JSON` | CLOB | Structured, comparable content. |
| `CHANGE_REASON` | VARCHAR2(500) | Required for versions > 1. |
| existing | | `SCENARIO_NAME`, `SCENARIO_TYPE`, `DETECTION_LOGIC`, `RAW_SQL`, `TIME_WINDOW_DAYS`, `CREATED_BY` (VARCHAR2), `CREATED_AT` |
| `UPDATED_BY`, `UPDATED_AT` | | Status changes. |

Constraints and indexes:
- `UNIQUE (SCENARIO_KEY, VERSION_NO)`
- `UNIQUE (THREAD_ID)`: one version per chat.
- Function-based unique index on `CASE WHEN STATUS='ACTIVE' THEN SIGNATURE_HASH END`: identical active scenarios cannot coexist even if the app check is bypassed.
- Index on `(SCENARIO_KEY, STATUS)`.

`PIO_AML_AGENT_SCENARIO_AUDIT(AUDIT_ID PK, SCENARIO_ID, SCENARIO_KEY, ACTION, ACTOR, ACTOR_ROLE, TS, FROM_STATUS, TO_STATUS, REASON, DIFF_JSON)`: append-only. Actions: `CREATE`, `REVISE`, `SUPERSEDE`, `ROLLBACK`, `RETIRE`, `OVERRIDE_CONFLICT`.

`PIO_AML_AGENT_SCHEMA_VERSION(COMPONENT, VERSION, APPLIED_AT)`: lets `ensure_schema()` upgrade safely later.

`PIO_AML_AGENT_SCENARIOS` (agent-owned governance/metadata table): mirror the minimum compatible metadata columns; `SCENARIO_CODE` = `SCENARIO_ID`. Metadata rows for superseded versions stay, with `ACTIVE_FLAG` updated in the same transaction as the production row. The existing shared `PIO_AML_SCENARIO` and `PIO_AML_PRODUCTION_SCENARIOS` remain untouched; the app never changes them.

---

## 5. Packages

### P0. Foundations, archive, config loader, spikes

**Goal:** clean base before any behavior change.
**Depends on:** none. **Repos:** A.
**Work**
1. **Legacy archive tooling (read-only; the agent never changes any data).**
   - `scripts/archive_legacy.py`: SELECT-only. `--dry-run` (default) prints what exists: row counts, the `PRD_` IDs found in `PIO_AML_PRODUCTION_SCENARIOS`, how many `PIO_AML_SCENARIO` rows match those IDs (by `SCENARIO_CODE`), how many alert rows reference them, and the local sqlite files that would move. `--export` writes read-only extracts to `old_data/<ts>/` (`.csv` + `.json`) for exactly the matching rows. `--move-local-state` moves the single checkpoint/sidebar sqlite (plus `-wal`/`-shm`) into `old_data/<ts>/`; it never deletes anything. All three are run by the owner, not by the IDE agent.
   - **`PIO_AML_SCENARIO` is a large shared table. It is never dropped, truncated or bulk-deleted.** Only rows whose `SCENARIO_CODE` matches a legacy production scenario ID are in scope.
   - The agent also writes a TOAD runbook under `scripts/sql/` for the owner to review and run step by step (no step is run by code):
     - `01_check.sql`: row counts; list of matching `PRD_` IDs; matching rows in `PIO_AML_SCENARIO`; alert rows referencing them; foreign keys and child tables referencing either table (`ALL_CONSTRAINTS`/`ALL_CONS_COLUMNS`); triggers on both tables; rows with unexpected ID patterns that should NOT be touched.
     - `02_archive.sql`: backup copies (`CREATE TABLE ..._BAK_<ts> AS SELECT ...` limited to the matching rows) plus the `SELECT` statements to export to CSV from TOAD.
     - `03_remove.sql`: `DELETE` statements limited to exactly those IDs, no `COMMIT` included, with comments to compare row counts against `01_check.sql` before committing. For the old production scenarios table the runbook offers either the same row-delete or a rename-to-`_OLD_<ts>` option so P4 can recreate the new schema.
     - `04_restore.sql`: how to put archived rows back.
2. Settings (`settings.py`): add `STATE_DIR` (default `artifacts/v2`), `CHECKPOINT_DB_PATH`, `APP_STATE_DB_PATH`, `CATALOG_DIR` (default `config/aml_catalogs`), `AGENT_RECURSION_LIMIT` (default 25, `demo`). Route reads the recursion limit from settings. **Keep** `LANGCHAIN_*` (LangSmith tracing is in use), `LLM_MAX_TOKENS`, `MAX_AGENT_ITERATIONS` and `MAX_VALIDATION_RETRIES` as configurable settings. Wire them only where default behavior stays identical (for example `LLM_MAX_TOKENS` unset means provider default, and check reasoning models that need a different parameter name). If one conflicts with another setting (`MAX_AGENT_ITERATIONS` vs `AGENT_RECURSION_LIMIT`), keep both fields, make one authoritative, document the other as reserved, and report it. Verify tracing end to end without printing keys: LangChain reads `LANGCHAIN_*`/`LANGSMITH_*` from process environment, so confirm that values loaded only via `.env` into `BaseSettings` actually reach it; if not, export them to `os.environ` at startup when unset.
3. `config_loader.py`: load YAML catalogs with Pydantic models, mtime-based reload, startup fails with a clear message on invalid catalogs. Create empty/valid skeletons for the four catalogs.
4. Remove `final_answer` and `escalation_report` from Service A's `SSEEvent` (declared, never emitted). Leave UI handlers alone. Keep `reasoning_mode` accepted in `ChatRequest` but documented as ignored.
5. Spikes, results written to `docs/spikes.md`:
   - S1: In langgraph 1.2.5, does `create_react_agent` emit a deprecation warning? Does `ToolNode` support tools returning `Command`? Does `pre_model_hook` allow returning a system message that is not persisted into `messages`?
   - S2: Is `sqlglot` installed or installable (pin version) with an Oracle dialect that parses typical `SELECT/WITH` statements? Test on 5 sample SQLs from the repo.
   - S3: Read-only: how does B's `scenario_contract.py` verify explicit codes appear in SQL? Summarize the approach so A can mirror it.

**Acceptance:** app boots with a fresh `STATE_DIR`; archive script dry-run output reviewed; loader tests (valid, invalid, reload); `compileall`; spikes documented.

**IDE prompt**
```
[global rules]
Implement package P0 of aml_builder_refactor_plan.md in ai-aml-agent only.
Do items 1-5 exactly as written. For item 1 implement the script but do NOT run
--export or --move-local-state, and never write code that deletes, drops or updates
data. Also write scripts/sql/01_check.sql..04_restore.sql as TOAD runbooks for me to
run. Keep LANGCHAIN_* and the token/iteration settings (see item 2). For spikes S1-S3 run only local, offline checks and
write findings to docs/spikes.md with file:line citations.
```

---

### P1. Typed envelope, codec, domain extraction

**Goal:** make tools thin and testable; one contract for tool outputs.
**Depends on:** P0.
**Work**
1. `api/codec.py`: `ToolResult` model and `encode/decode`; typed payload models per tool. History, SSE mapper and deploy route decode through the codec. Mirror the legacy top-level keys inside `payload`.
2. Extract pure modules from `tools.py`:
   - `domain/intent_normalize.py` (current lines 222-345 behavior). Every default injected gets `provenance="default"` and an entry in `applied_defaults`. Add `provenance` to `BaselineWindow`.
   - `domain/code_binding.py` (lines 59-160 behavior).
   - `domain/sql_extract.py` (move `sql_extraction.py` logic), `domain/metadata_rules.py`, `domain/window.py`, `domain/hashing.py` (`normalize_sql`, `stable_hash`).
3. `analyze_intent...` no longer renders the plan. The plan tool is the single plan owner. Update `tool_driven_system.md` accordingly.
4. SSE mapper moves to `api/sse_mapper.py`; behavior unchanged.

**Acceptance:** golden tests: captured intent fixtures produce identical normalized output before/after; codec round trip; history endpoint works on a synthetic message list; SSE mapper snapshot test; no new tool names.

**IDE prompt**
```
[global rules]
Implement P1. First write golden fixtures by running the CURRENT normalization
functions on at least 10 intent samples (pure functions only, no LLM). Then extract
modules so outputs stay identical, except: defaults now carry provenance and are
listed in applied_defaults. Introduce the ToolResult codec and switch history/deploy/
SSE mapping to it. Do not change tool names or UI-visible SSE events.
```

---

### P2. Identity and mock roles

**Goal:** trusted identity and a permission layer, so later packages can enforce roles.
**Depends on:** P0. **Repos:** A, UI.
**Work**
1. UI: `middleware.ts` matcher extended to `/api/:path*` (excluding login). BFF routes (`api/chat`, `api/chat/deploy`, history, sessions) take the user from the verified `pio_session` JWT, never from the request body. Add `AUTH_MODE=dev` fallback to user `999`. BFF forwards `X-User-Id` and `X-Service-Token`.
2. A: dependency `require_identity` validates the service token and reads user/project from headers. Reject (403) when body/path IDs disagree. CORS restricted to configured origins.
3. `policy.yaml` roles and users (draft in section 6). `authorize(user_id, action, resource)` in `domain/policy.py`. Actions: `chat.read`, `scenario.create`, `scenario.revise`, `scenario.rollback`, `scenario.delete`.
4. `GET /me/permissions` returns role and allowed actions. UI loads it into a context; buttons are disabled with a tooltip when not allowed.
5. Thread ownership: routes verify the thread's `user_id` matches the caller (viewer rule D12).

**Acceptance:** authorize matrix table test; forged identity rejected; missing token rejected; UI lint/build; manual note on how to test each role.

**IDE prompt**
```
[global rules]
Implement P2 across ai-aml-agent and AI_AML_INTERFACE. Identity must come only from
the verified session on the BFF and from signed headers on Service A. Use the
policy.yaml draft from section 6 as-is. Do not add features beyond the actions
listed.
```

---

### P3. Workflow store, revisions, state machine, guards

**Goal:** server-enforced stages, with the revision (trio) model and history from the start.
**Depends on:** P1, P2.
**Work**
1. New `app_state.sqlite` and `WorkflowRepo` + `CardAuditRepo` (section 4.1): `scenario_workflow`, `scenario_revision`, `param_card_audit`, `workflow_events`, `chat_sessions`. Migrate `session_store.py` to the composite key.
2. `workflow/models.py` (`WorkflowRow`, `Revision`, `PendingCard`), `workflow/machine.py` (per-revision transitions per 3.2), `workflow/revisions.py` (`create_revision`, `supersede_to_history`, `evict_over_cap`, `current()`, `list_history()`), `workflow/guards.py` (table 3.3, including `CARD_PENDING`, `CARD_STALE`, `REVISION_STALE`).
3. Tools read `thread_id` from `config["configurable"]` and call `guard(...)` first. Allocate `scenario_id/key` at workflow-row creation.
4. Codec: add an optional `revision_id` to the `ToolResult` envelope (backward compatible) so every tool output is tied to its revision.
5. Prompt: `pre_model_hook` (if S1 confirms) injects a short "current stage / allowed actions / current revision summary" message each turn **and** replaces ToolMessage contents that belong to superseded revisions with a short stub (`[superseded by revision N]`) in the messages sent to the model only; stored checkpoint state is untouched. If S1 says this is not possible, render the current revision into the system prompt and instruct the model that older tool outputs are outdated.
6. Endpoints: `GET /workflow/{project}/{chat}/{user}/state` (stage, current revision summary, pending card, history summaries without payloads), `GET .../revisions`, `GET .../revisions/{revision_id}`. Chat route emits `stage_changed` (with `revision_id`) after each tool round. Document the reserved `POST .../revisions/{revision_id}/restore` contract in `docs/revisions_api.md` (copy-forward semantics, RV6); do not implement it.
7. Optimistic locking on `version`; concurrent writes return a typed conflict. Every action endpoint rejects a stale `revision_id`.

**Acceptance:** transition table tests per revision; revision creation/copy rules; eviction tests (cap 5: the 6th undeployed history revision evicts the oldest, a tombstone remains, payload nulled, a deployed revision is never evicted even beyond the cap, audit rows untouched; cap configurable); superseded-stub tests; stale `revision_id`/`request_id` tests; guard tests per tool; concurrent write test; `stage_changed` in an SSE mapper test.

**IDE prompt**
```
[global rules]
Implement P3. State lives in its own sqlite behind WorkflowRepo/CardAuditRepo,
including the revision (trio) tables and history/eviction in section 3.2 and 4.1.
Guards return typed errors from section 3.3 inside the ToolResult envelope. Use
docs/spikes.md S1 to choose pre_model_hook vs prompt rendering for the current-revision
context and superseded stubs. Do NOT implement the restore endpoint; only document its
contract. Write the transition, revision and eviction tests first.
```

---

### P4. Registry v2, DeployService, audit

**Goal:** idempotent, versioned, auditable deployment.
**Depends on:** P3.
**Work**
1. `ensure_schema()` creates the agent-owned tables in 4.2 (create-if-missing, versioned by `PIO_AML_AGENT_SCHEMA_VERSION`). Existing shared registry tables stay untouched. No destructive DDL inside the app.
2. `adapters/oracle_repo.py`: `insert_version`, `supersede`, `get_active_by_key`, `list_active_signatures`, `set_status`, `append_audit`. Single transaction for create/revise: insert new row (+ metadata row), mark parent `SUPERSEDED` (`IS_ACTIVE=0`), append audit.
3. `domain/deploy_service.py`: `deploy(workflow, revision_id, actor)` requires that `revision_id` is the current revision (`REVISION_STALE` otherwise), no pending card (`CARD_PENDING`), and approvals belonging to that revision; it checks stage `METADATA_READY`, shadow passed for the same `sql_hash`, plan approved for the same `plan_hash`, metadata valid, permission `scenario.create` (or `scenario.revise` for revise chats), parent still the latest (`REVISE_STALE` otherwise), conflict acknowledgements recorded (P8). It uses the pre-allocated `scenario_id`; a repeat call returns the existing deployment (idempotent).
4. Both the `/scenario/deploy` route and the persist tool call `DeployService`. `deploy.py` no longer scans checkpoints.
5. Return value includes the receipt (with `revision_id`, `rev_no` and the card audit request IDs). Set workflow `locked=1`, stage `DEPLOYED`, `deployed_revision_id`, and mark that revision `deployed`. Write `REVISION_ID`/`REV_NO` on the registry row.
6. `CREATED_BY` receives the authenticated user ID as text. The metadata table's numeric `CREATED_BY` receives the numeric user ID if parseable, else a configured default (document this).

**Acceptance (fake repo):** rejects failed shadow, `sql_hash` mismatch, unapproved plan, wrong stage, locked chat, missing permission; retry returns same ID; superseding is atomic; audit rows written; unique-index violation maps to a typed `DUPLICATE_ACTIVE_SIGNATURE` error. Owner runs one DDL smoke test against dev Oracle.

**IDE prompt**
```
[global rules]
Implement P4 per section 4.2. Do not run DDL against any real database; test with an
in-memory fake repo, and give me the DDL as a single reviewed script
(scripts/registry_v2.sql) that ensure_schema() also embeds. Keep the columns the
alert engine reads unchanged in name and type.
```

---

### P5. UI: server-driven state, approvals, lock tips

**Goal:** UI reflects the real workflow; fixes the reset-on-tab-change and shadow-view-cleared bugs.
**Depends on:** P3, P4. **Repos:** A, UI.
**Work**
1. A: endpoints `approve_plan` and `approve_result` (body carries expected hash; reject on mismatch), `deploy` (existing route, now via `DeployService`).
2. UI hook handles `stage_changed`; a `WorkflowContext` fetches state on mount and after each event. Deploy success, approvals and lock come from this context, not component state, so tab changes and panel close no longer reset them.
3. Buttons: **Proceed** (Plan tab; this is plan approval, label from `ui_copy.yaml`), **Approve result** (Shadow tab, disabled unless shadow passed), **Deploy** (enabled only when state says `can_deploy`).
4. Chat deployment (if enabled) must not clear the shadow view; keep artifacts keyed by stage.
5. When an action succeeds the UI sends a short deterministic "action message" through the chat endpoint (e.g. `[ACTION] plan_approved`) so the LLM continues. The route passes it as a normal user message flagged in metadata.
6. Lock UX (copy from `ui_copy.yaml`): tip popup when `DEPLOYED`, composer placeholder changes, **New chat** button. Show a disabled "Create new version" until P9.
7. `chat_may_deploy: false` removes the persist tool from the graph.
8. **Artifacts come from the current revision** (`GET state`), not from checkpoint messages: Plan, Shadow and Governance tabs show the current revision; the history endpoint stops rebuilding artifacts from old ToolMessages (it keeps the chat text). Show a "Revision N" badge.
9. A pending card is restored from state on mount/reload (no expiry). Actions send `revision_id`/`request_id`; a stale response (409) refreshes state and shows the stale notice from `ui_copy.yaml`.

**Acceptance:** UI lint/build; component tests if a test runner exists (none today, so list manual test steps in the PR, including: reload with a pending card, edit after plan, stale tab); A endpoint tests for hash mismatch, stale revision and permission denial.

**IDE prompt**
```
[global rules]
Implement P5. UI state for approvals/deploy/lock must come from the server workflow
state, not from local component state. Keep existing SSE handlers; add stage_changed.
Add manual test steps to the PR description (no UI test runner exists today).
```

---

### P6. Param catalog, gap detection, confirmation card

**Goal:** a compliance officer confirms or edits the parameters in one click before any planning; the card survives reloads and is audited.
**Depends on:** P3, P5.
**Work**
1. `params.yaml` loader (draft in section 6). Each param: key, label, required, input type, options, default, validation, `required_if`, prompt text, `status`. Plus the `card` and `ambiguity` settings.
2. `domain/param_gaps.py`: from the normalized `AMLIntent` produce rows with status `provided | defaulted | inferred | missing | ambiguous`, recommended value, input type and prompt. Time window rule: missing or all-time is `missing` and cannot be defaulted.
3. **Hybrid ambiguity (PC7).** The LLM may flag unclear values (reuse the existing `Clarification` model) and can only **add** rows or mark them ambiguous. Code validates required params against the catalog and computes `ready_for_handoff`; the LLM can never clear a code-detected gap. A required param that is missing or ambiguous blocks; an ambiguous optional param is shown but does not block (`ambiguity.block_on_optional: false`).
4. **Pending card.** `analyze_intent` stores `pending_card_json` (`request_id`, rows, base revision) and emits the `parameter_card` SSE event. One pending card per chat: a new analysis replaces the old one, whose `request_id` becomes stale (audit outcome `superseded`). The card stays pending with no expiry (PC3).
5. **`confirm_params` action** (body: `request_id`, per-row `value` + `choice`, or `accept_defaults`): 409 `CARD_STALE` on an old `request_id`; 422 `PARAMS_INCOMPLETE` with `missing[]`, `unresolved[]` and the `request_id` when required values are absent (PC5). Script/API callers must send explicit values or explicitly list the defaults they accept; nothing is ever guessed. On success it writes the audit row, stores params, creates/updates the revision (P6b rules), runs the conflict check (when P8 exists) and renders the plan automatically.
6. `skip_card_when_complete` (default true): when nothing is missing or ambiguous the server auto-confirms with `source=system`, writes an audit row with outcome `auto_confirmed`, and shows an info notice.
7. **`discard_card` action:** drops the pending card (audit outcome `discarded`).
8. **UI card (PC1):** section "Needs your input" first (missing/ambiguous), then "Assumed defaults", each row editable with the typed control and ghost-text recommended value; one **Confirm** button, enabled once all required rows are resolved, that confirms everything at once. Defaults the officer leaves untouched are recorded as `accepted_default`, typed values as `user_input`. The pending card is restored from state on reload.
9. **Audit (PC6):** every card writes one `param_card_audit` row (request, revision, params asked, recommended values, choices, user, role, source, timestamps, outcome) through `CardAuditRepo`. LangSmith tracing is unchanged and additional.
10. Keep card and plan separate (PC2): the card never shows plan text; **Proceed** (plan approval) never edits params.

**Acceptance:** table tests for gap detection (short request "customers with high-value transactions" yields customer OK, "high-value" ambiguous, window missing); LLM says "all clear" while the window is missing, still blocked; LLM-flagged ambiguity on an optional param is shown but not blocking; catalog validation tests; `confirm_params` refusal tests for API callers (422 with lists, nothing guessed, audit outcome `refused`); stale `request_id` rejected; one pending card per chat; audit row content per choice type; auto-confirm audit; pending card returned by `GET state` after a simulated reload.

**IDE prompt**
```
[global rules]
Implement P6 using the params.yaml draft in section 6 and decisions PC1-PC8 in
section 2. Required/optional/default logic comes only from the catalog; the LLM may
flag ambiguity but can never clear a code-detected gap. Write the param_gaps,
refusal and audit tests before the UI work. Reuse the existing Clarification and
applied_defaults fields instead of adding parallel structures. Never fill in a value
for a script/API caller.
```

---

### P6b. Revision flow and history (first demo scope)

**Goal:** card changes after the first plan create a new revision with an automatically regenerated plan; older undeployed trios are kept in a capped history. The history picker UI is NOT built now.
**Depends on:** P3, P4, P6.
**Work**
1. **Creation rules** exactly as section 3.2: first confirm creates revision 1; a card change before the first plan replaces the card; after the first plan a confirmed card change creates revision N+1 (copy of the current one with the new card), and revision N goes to history with its shadow result and metadata intact.
2. **Auto re-plan (RV2):** the plan is rendered by the deterministic renderer inside `confirm_params`, stored on the new revision with a new `plan_hash`, and returned in the response with `stage_changed` (`revision_id`). The chat receives a short action message (`[ACTION] card_confirmed revision N`) so the assistant can acknowledge it; the LLM does not produce the plan.
3. **Supersession:** the new revision starts with no shadow result and no approvals (RV3). Governance metadata is re-seeded; fields the user edited are carried over and re-validated (RV7). Metadata edits alone never create a revision.
4. **History:** cap from `policy.yaml revisions.history_cap` (default 5), eviction with tombstones (RV4) at revision creation; a deployed revision is never evicted or altered.
5. **Interactions:** one scenario per chat still holds, because only one revision can be deployed and deploying locks the chat; a pending card blocks approvals and deploy (RV5); the conflict check (P8) re-runs for every new revision and again at deploy; the registry row stores the deployed `REVISION_ID`.
6. **API for later:** list and read endpoints exist (P3); the restore contract is documented in `docs/revisions_api.md` and not implemented. The only UI added now is the "Revision N" badge from P5.

**Acceptance:** a card change after the first plan creates revision 2 with a new plan and no carried approvals; revision 1 keeps its shadow result and metadata in history; a card change before any plan leaves a single revision; six revisions with cap 5 evict the oldest with a tombstone while a deployed revision survives beyond the cap; stale `revision_id` rejected on approve/deploy; metadata carry-over test; pending card blocks Proceed; no LLM call is made during re-plan (assert with a fake LLM that fails if invoked).

**IDE prompt**
```
[global rules]
Implement P6b per sections 3.2 and 4.1 and decisions PC4, RV1-RV7. Re-plan is
deterministic server code, not an LLM turn (the fake LLM in tests must fail if called).
Do not build the history picker UI or the restore endpoint. Eviction keeps tombstones
and never touches deployed revisions or card audit rows.
```

---

### P7. Filter catalog, resolution, A-side filter check

**Goal:** advisor-provided filter dimensions applied deterministically; explanation codes become the first dimension.
**Depends on:** P6.
**Work**
1. `filters.yaml` loader (draft in section 6): per dimension `label, level (customer|account|transaction), target {table, column}, join_hint, resolver (lookup|enum|semantic), value_source, match (any_of|all_of), polarity_allowed, required, status`.
2. Intent schema gets a generic `filters: [{dimension, raw_mention, polarity}]`. The intent prompt's "dimensions you may extract" section is generated from the catalog. The LLM never sees column names.
3. `domain/filter_resolution.py`: run each resolver, produce `ResolvedFilter(dimension, target, values, polarity, status resolved|ambiguous|unresolved, confidence, provenance)`. Wrap the existing explanation-code search behind the same interface. Failed lookups return `lookup_failed`, not an empty list.
4. Ambiguous/unresolved filters become card rows (P6) with the lookup values as options.
5. Plan renderer shows resolved filters explicitly; `params_hash` includes them.
6. **Service B stopgap (D6):** send resolved filters inside `intent` as `resolved_filters` plus a plain-language `filter_instructions` block naming target table/column/join hint. B's models allow extra fields, so they pass through into its prompt. Do not edit B.
7. A-side recheck after SQL is returned: parse SQL (sqlglot per S2, else conservative regex) and verify each resolved filter's target column appears in a predicate with a compatible value set. Failure sets shadow `validation_success=false` with `FILTER_NOT_APPLIED` and lists the dropped filters.
8. Level handling: a customer-level filter plus transaction-level threshold produces an info note in the plan about baseline semantics.

**Acceptance:** resolver tests with a fake lookup repo; `lookup_failed` path; filter-applied check tests (applied, dropped, wrong values, negated); explanation-code behavior unchanged for existing fixtures.

**IDE prompt**
```
[global rules]
Implement P7. The advisor's column details (join paths, value sources) are not yet
known, so ship the catalog with explanation_code fully working and account_status /
customer_profession as entries marked status: needs_advisor_input that are loaded
but disabled. Do not modify PioTech-AI. The filter-applied check is the safety net
for B dropping a filter: write its tests first.
```

---

### P8. Signature, comparator, conflict report

**Goal:** tell the officer early about duplicates, similar scenarios and overlaps.
**Depends on:** P6, P7 (P4 for stored signatures).
**Work**
1. `domain/signature.py`: canonical JSON from confirmed params and resolved filters:
   ```
   scenario_type, grain, metric, aggregation, window{value, unit_days, kind},
   comparator, threshold{value|from/to, currency?}, baseline,
   transaction_scope{mode: specific|all, types, codes},
   filters{dimension: {values(sorted), match, polarity}}
   ```
   Normalization: units to days, values sorted, ALL/ANY scope canonicalized. `signature_hash` excludes name, IDs and governance metadata.
2. `domain/compare.py` returns `Relation` plus a per-dimension diff. Comparable core = same `scenario_type`, `grain`, `metric`, `aggregation`, comparator direction, window kind. If the core differs the relation is `RELATED` or none. Otherwise compare the rest:
   - Threshold intervals: containment test (`>10K` ⊂ `>5K`).
   - Window length: longer window with same threshold is not comparable by containment, so `OVERLAP` or `RELATED` per config.
   - Filters: set containment per dimension, respecting `match` and polarity. A missing filter means "all values".
   - New ⊆ existing every dimension: `COVERED`. New ⊇ existing: `COVERS`. Equal: `IDENTICAL`. Intersecting but neither: `OVERLAP`. Core differs only: `RELATED`.
3. Fallback `SIMILAR` from the existing semantic search (embedding score above `similar_min_score`, demo 0.85) for rows without a usable signature. Rows from the new schema always have a signature.
4. Similarity "%": weighted share of matching dimensions (core dimensions weighted higher; weights in `policy.yaml`), displayed with the diff, never raw cosine.
5. `conflict_check` service: fetch active signatures, filter by scenario type and grain, compare, return top N (demo 5) sorted by relation severity then score. Run: (a) early non-blocking hint inside the intent tool output when the comparable core is resolved; (b) authoritative at `confirm_params`; (c) again at deploy (registry may have changed).
6. The conflict result is stored on the revision (`conflict_json`) and re-run for every new revision and at deploy.
7. Policy actions per relation (section 6): `IDENTICAL` and same-chat redeploy **block**; `COVERED`, `COVERS`, `OVERLAP` require acknowledgement; `RELATED`, `SIMILAR` inform only.
8. SSE `conflict_report` and a UI panel: each match with relation, diff table, and actions **View scenario**, **Change params**, **Proceed anyway** (warn-level only), **Cancel**. Acknowledgements stored in state and the receipt, and written to audit as `OVERRIDE_CONFLICT`.
9. LLM role: none in deciding relations. It may phrase an explanation from the computed diff.

**Acceptance:** table tests for the comparator, including: (a) >5K monthly vs >10K monthly gives `COVERED`/`COVERS` both directions; (b) identical signature with different names gives `IDENTICAL`; (c) same core, different filter sets partly overlapping gives `OVERLAP`; (d) rolling vs calendar window gives `RELATED`; (e) relative baseline thresholds gives `RELATED`. Unique-index test via the fake repo. Re-check at deploy rejects when a new identical active scenario appeared meanwhile.

**IDE prompt**
```
[global rules]
Implement P8. Start with the comparator table tests listed in the package, then the
code. The comparator is deterministic Python; the LLM must not decide relations.
Weights, thresholds and relation->action mapping come from policy.yaml.
```

---

### P9. Versioning flows: new version, rollback, retire

**Goal:** professional change management with permissions and audit.
**Depends on:** P4, P5, P8, P2.
**Work**
1. **Create new version** (`scenario.revise`; draft "revisions" inside a chat are a different thing, see 3.2): action on a deployed scenario creates a new chat seeded with the stored intent/params/filters/metadata, with `parent_scenario_id` and `parent_version` set. The chat starts at `PARAMS_PENDING`. A change reason is required at deploy. Deploy supersedes the parent atomically; a stale parent returns `REVISE_STALE`.
2. Identical-to-parent new versions are blocked by the signature rule (no-op).
3. **Rollback** (`scenario.rollback`): reactivate a `SUPERSEDED` version and supersede the current active one, in one transaction with audit. Requires a reason.
4. **Retire/delete** (`scenario.delete`): soft delete to `RETIRED` (`IS_ACTIVE=0`), reason required, audit row. No hard deletes.
5. UI: scenario actions menu (permission-aware), reason dialog, version history list with status and who/when from audit.
6. Registry query tool gains version-aware modes (list versions, show diff between versions using stored JSON).

**Acceptance:** tests with the fake repo for revise (happy path, stale parent, identical no-op), rollback, retire, permission denial per role, audit completeness.

**IDE prompt**
```
[global rules]
Implement P9. Every state-changing action must (1) check authorize(), (2) run in one
transaction with its audit row, (3) require a reason where the plan says so. No hard
deletes anywhere.
```

---

### P10. Performance, context control, observability

**Goal:** measured improvements after correctness is in place.
**Depends on:** P1..P9.
**Work**
1. Context: keep only the latest artifact per type in agent context; summarize or drop older ToolMessage payloads (via the pre-model hook), extending the superseded-revision stubs from P3.
2. Async adapters: `httpx.AsyncClient` for B; Oracle through `oracledb` async pools or a bounded thread pool; sidebar/workflow writes off the event loop. Make the alert engine routes run in a worker.
3. Per-type code retrieval/rerank concurrently with a semaphore; vector cache with an invalidation key (catalog row count + max update time).
4. Stage metrics: latency per tool, token counts if available, error codes; a correlation ID (`thread_id` + request ID) passed to B in `metadata.chat_id` and logged. Verify LangSmith settings or remove them.
5. Replace the "estimated" detail-count metric: label as estimate in UI, or compute real counts if the contract allows.
6. Add a minimal CI workflow (compileall, pytest) for A.

**Acceptance:** before/after timings for a scripted scenario run using fakes; no regression in earlier packages' tests; CI config runs green.

**IDE prompt**
```
[global rules]
Implement P10 only after measuring: first add the stage timing instrumentation and
report baseline numbers from a fake-adapter run, then apply the optimizations one at
a time, re-measuring each. Do not change business behavior.
```

---

## 6. Catalog drafts (all `status: demo`)

### `policy.yaml`

```yaml
version: 1
status: demo
roles:
  viewer:  [chat.read]
  creator: [chat.read, scenario.create]
  manager: [chat.read, scenario.create, scenario.revise, scenario.rollback, scenario.delete]
users:                       # mock mapping; replace with real directory later
  "999":  manager
  "1001": creator
  "1002": viewer
default_role: viewer
cross_user_chat_read: false
chat_may_deploy: false
auth_mode: dev               # dev | jwt
relations:                   # relation -> action
  IDENTICAL:            block
  SAME_CHAT_REDEPLOY:   block
  COVERED:              warn_confirm
  COVERS:               warn_confirm
  OVERLAP:              warn_confirm
  RELATED:              info
  SIMILAR:              info
similar_min_score: 0.85
max_matches_shown: 5
dimension_weights:           # for the displayed "% match"
  scenario_type: 3
  grain: 3
  metric: 3
  aggregation: 2
  window: 2
  comparator: 2
  threshold: 2
  transaction_scope: 2
  filters: 1
reason_required_for: [revise, rollback, retire]
revisions:
  history_cap: 5               # undeployed history trios kept per chat (demo)
  evict: oldest_undeployed     # tombstone kept, payloads nulled
  carry_over_user_edited_metadata: true
  restore_from_history: reserved   # API contract documented, not implemented
card_audit:
  store: sqlite                # sqlite | oracle (see C14)
  retention: permanent
```

### `params.yaml`

```yaml
version: 1
status: demo
skip_card_when_complete: true
card:
  order: [needs_input, assumed_defaults]
  one_click_confirm: true
  pending_expiry: null         # never expires
  one_pending_per_chat: true
ambiguity:
  llm_may_flag: true
  block_on_required: true
  block_on_optional: false
params:
  scenario_type:   {required: true,  input: enum,   options: [TRANSACTION, ACCOUNT, CUSTOMER], default: null}
  grain:           {required: true,  input: enum,   options: [customer, account, transaction],  default: null}
  metric:          {required: true,  input: enum,   options: [transaction_amount, transaction_count], default: transaction_amount}
  aggregation:     {required: true,  input: enum,   options: [SUM, COUNT, MAX, AVG], default: SUM}
  comparator:      {required: true,  input: enum,   options: [">", ">=", "<", "<=", "between"], default: ">"}
  threshold:       {required: true,  input: number,  default: null}
  window:          {required: true,  input: duration, default: null, min_days: 1, max_days: 365, all_time_allowed: false}
  window_kind:     {required: true,  input: enum,   options: [rolling, calendar], default: rolling}
  transaction_scope: {required: true, input: enum,  options: [specific, all], default: all}
  baseline:        {required: false, input: duration, required_if: "threshold.relative == true"}
  exclusions:      {required: false, input: text}
  expected_alert_range: {required: false, input: range}
```

### `filters.yaml`

```yaml
version: 1
status: demo
dimensions:
  explanation_code:
    label: Explanation code
    level: transaction
    target: {table: PIO_TRANSACTIONS, column: EXPL_CODE}
    resolver: semantic          # existing embeddings + rerank
    match: any_of
    polarity_allowed: false
    required: false
    status: confirmed           # existing behavior
  account_status:
    label: Account status
    level: account
    target: {table: TBD, column: TBD}
    join_hint: TBD
    resolver: lookup
    value_source: TBD
    match: any_of
    polarity_allowed: true
    required: false
    status: needs_advisor_input  # loaded, disabled
  customer_profession:
    label: Customer profession
    level: customer
    target: {table: TBD, column: TBD}
    join_hint: TBD
    resolver: semantic
    value_source: TBD
    match: any_of
    polarity_allowed: true
    required: false
    status: needs_advisor_input
```

### `ui_copy.yaml` (tips and notices)

```yaml
locked_chat_tip: "This chat is now the record for scenario {scenario_id} (v{version}). You can view it, ask about it, or look up other scenarios. To create another scenario, start a new chat."
locked_composer_placeholder: "This scenario is deployed. Ask a question or start a new chat."
conflict_block: "An active scenario with the same detection logic already exists ({scenario_id})."
conflict_warn: "{n} similar scenario(s) found. Review before continuing."
card_pending_notice: "Confirm or discard the parameters card before continuing."
card_stale_notice: "This card was replaced by a newer one. Showing the latest."
revision_badge: "Revision {rev_no}"
proceed_button: "Proceed"
params_incomplete: "These values are still needed: {missing}."
```

---

## 7. Deferred: Service B (not in this plan)

Handle in a later effort, in this order:
1. Accept structured `resolved_filters` and `filter_instructions` as first-class intent fields in `scenario_contract.py`.
2. Add deterministic contract checks for each resolved filter (mirror A's check), so B refuses SQL that drops a filter.
3. Return richer final payloads (columns, assumptions, warnings) instead of SQL text only.
4. Schema/join knowledge for the new dimensions, driven by the same `filters.yaml`.
5. Add auth, rate limits, and restrict CORS.
6. Tests: extend `services/dwh_scenario_agent/tests/test_service_a_contract.py`.

Until then, Service A's filter instructions plus its own recheck (P7.7) are the safeguards.

---

## 8. Open confirmations (tracked, not blocking)

| # | Question | Owner | Current default |
|---|---|---|---|
| C1 | `PIO_AML_SCENARIO` is a large shared table: confirm no new columns are needed there (new data lives in the production scenarios table). Who holds DDL rights? | DBA/owner | Do not alter its columns; no deletes by app code |
| C2 | Advisor filter list: target table/column, join path, level and value source per dimension; `match` semantics (any_of vs all_of) | AML advisors | Entries disabled until supplied |
| C3 | Meaning of "mandatory" per dimension: apply-when-mentioned vs always-ask | Compliance | Apply when mentioned; `required: false` |
| C4 | Which relation types should block vs warn, and whether intentional tiering (5K medium, 10K high) is allowed | Compliance | Only IDENTICAL and same-chat redeploy block |
| C5 | May chat deploy to production, or only the button? | Product/compliance | Button only |
| C6 | Should viewers read other users' chats? | Product | No |
| C7 | Max window and default window kind | Compliance | 365 days, rolling |
| C8 | `CREATED_BY` mapping (text vs numeric user IDs) for the metadata table | Owner | Text on production table; numeric if parseable on metadata table |
| C9 | Retention for `SUPERSEDED` versions and audit rows | Compliance | Keep indefinitely |
| C10 | Production topology: number of Service A workers and where state sqlite lives | Ops | Single worker, local files |
| C11 | History cap size per chat | Product/compliance | 5 |
| C12 | When a card changes, should user-edited governance metadata carry over to the new revision? | Product | Yes, re-validated |
| C13 | Restoring an older trio (later phase): are approvals reset and shadow re-run? | Compliance | Approvals reset, conflict check re-run, shadow shown as "previous run" and re-run required |
| C14 | Where should the permanent card audit live in production (sqlite vs Oracle) and for how long? | Compliance/DBA | sqlite for the demo, permanent retention |
| C15 | While a card is pending, should Proceed/deploy of the current revision be blocked? | Product | Blocked until confirmed or discarded |

---

## 9. Dependency order

```
P0 -> P1 -> P2 -> P3 -> P4 -> P5 -> P6 -> P6b -> P7 -> P8 -> P9 -> P10
        \------------^
(P2 depends on P0 only; P3 on P1+P2; P6b on P3+P4+P6; P8 on P6+P7; P9 on P4+P5+P8+P2)
```

Smallest useful release after P5: locked chats, idempotent versioned deploy with receipt, server-driven approvals, roles. P6 and P6b deliver the card and revision flow; P7-P8 deliver the advisor features; P9 completes change management.
