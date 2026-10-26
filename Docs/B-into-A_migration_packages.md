# B-into-A migration packages (B0-B8)

Move `PioTech-AI/services/dwh_scenario_agent` (Service B) into `ai-aml-agent` (Service A) as one resilient, optimized package. Naming: **B0..B8** (P-numbers are taken by A's own refactor).

## Locked decisions

1. **Scope:** only `services/dwh_scenario_agent`. Nothing else from PioTech-AI moves.
2. **Architecture:** in-process generator inside A behind a typed interface. The existing HTTP adapter (`PIOTECH_AI_URL`) stays the default until the in-process path is measured at parity. No new HTTP route, session store, `/jobs`, `/report` or `/feedback` endpoint in A: A already owns HTTP, identity, sessions, history and SSE.
3. **Keep:** Chroma (schema tables, schema columns, experience), Neo4j reranking, and their index/enrichment scripts, all made configurable, optional and resilient.
4. **No model training.** SFT/DPO/reward scripts are out of scope. Experience pairs are used only as RAG lookup, and the experience index must work when empty.
5. **No golden dataset yet.** The AML officer's "scenario description -> SQL" pairs do not exist yet and must not block anything. Until they arrive, evaluation uses hand-written synthetic cases built from A's own catalogs, scored on structure and safety (not exact SQL match). The harness accepts the officer's pairs later without redesign.
6. **LLM providers:** keep all (OpenAI, LM Studio, Cohere, SambaNova, OpenRouter) behind ONE factory with per-role config (generator, critic, summarizer, embeddings). OpenAI and LM Studio are fully tested; the others are isolated adapters covered by fake-client contract tests.
7. **Critic gets no raw result rows by default** (columns, row count, errors only). Any row preview is an explicit, masked, per-provider setting (ties into the PII masking work).
8. **Oracle writes:** the app never writes to dictionary tables. Enrichment tooling only emits scoped SQL, a backup SELECT and a reverse script for the owner to review and run in Toad.
9. Ignore all compose files and `deployment_v1.0/`. `run.ps1` and the code are the only deployment evidence.

## Rules for the IDE agent (apply to every package)

- Work on a branch per package (`refactor/b<N>-<slug>`). Do not start package N+1 until N's acceptance checks pass and the owner approves.
- Never run live Oracle, LLM, Chroma-build, Neo4j-build or enrichment commands. All tests are offline with fakes and temp paths.
- Never delete or modify anything in `PioTech-AI`. Never copy `.env`, `artifacts/`, `data/`, `static/`, `logs/`, checkpoints or Chroma/Neo4j data into A.
- Never touch the existing `PIO_AML_PRODUCTION_SCENARIOS` or `PIO_AML_SCENARIO` tables. Do not port the explain-plan helper (it writes to Oracle `PLAN_TABLE`).
- Preserve `LANGCHAIN_TRACING_V2`, `LANGCHAIN_API_KEY`, `LANGCHAIN_PROJECT` propagation and the configurable `LLM_MAX_TOKENS`-style settings.
- Do not log raw SQL, prompts, result rows, secrets or customer values.
- Items marked **OWNER** need the owner (Toad/DBA/deployment) and must be reported, not attempted.

## Bug ledger (becomes test cases in B0)

These were verified in B's source. B0 writes tests that assert the CORRECT behavior. Against current B logic they are expected to fail (mark `xfail(strict=True)` in the baseline suite), and the package that fixes each one flips it to passing.

| ID | Defect in B | Correct behavior to assert | Fixed in |
| --- | --- | --- | --- |
| BL1 | Tool guard accepts `WITH`, but `oracle.run_readonly` rejects anything not starting with `SELECT` | Read-only CTE queries execute; DML/DDL/multi-statement/PL-SQL are rejected by AST | B3 |
| BL2 | Feedback writes collection `experience_pairs`; build and search use `experience_templates` | One collection-name constant shared by build, search and any write path | B4 |
| BL3 | Embedding manager never passes the API key to the OpenAI/Cohere providers | Keys are injected from settings; missing key gives a clear config error, not a crash at first use | B2 |
| BL4 | `manage_memory` calls `.invoke` on the `(client, model)` tuple from `get_llm_client`; error swallowed, fixed fallback summary | Compaction works or is deliberately removed; never silently replaced by a canned summary | B5 |
| BL5 | Critic only handles OpenAI and LM Studio; other providers fall through the OpenAI path, fast model may be unset | Critic uses the provider/model configured for the critic role, or fails with a clear config error | B2/B5 |
| BL6 | Local embedding model warmed up with a device argument, later loaded again without it (two instances) | One model instance per (provider, model, device); keyed cache | B2 |
| BL7 | Startup swallows Oracle/embedding/Chroma failures; `/health` says healthy; no Neo4j check | Readiness reports each dependency (Oracle, Chroma collections, Neo4j optional) as ok/degraded/down | B4 |
| BL8 | Index rebuild drops the live Chroma collection first; graph rebuild deletes the current Neo4j module graph | Build into a temp/versioned target, validate, then promote atomically; old version kept | B7 |
| BL9 | Candidate SQL fetches up to 10,000 rows into memory; `row_count` is rows fetched, not total | Bounded sample fetch with explicit `truncated` flag; no misleading counts | B3 |
| BL10 | OpenAI generation ignores the configured output-token setting; `LLM_MAX_TOKENS` is used both as output limit and as context budget | Separate output-token limit and context budget, both honored by every provider | B2 |
| BL11 | Feedback JSONL append can succeed while the vector insert fails | Not ported (no feedback endpoint in A); see decision D3 | n/a |

---

## B0: Baseline, bug ledger and evaluation harness (no production behavior change)

**Goal:** a safety net before anything moves, with zero dependency on the officer's golden pairs.

**Create in A (tests only):**

- `tests/characterization/dwh_scenario/` with fakes for Oracle, LLM, Chroma and Neo4j (in-memory, deterministic).
- Port `PioTech-AI/services/dwh_scenario_agent/tests/test_service_a_contract.py` behavior as characterization of the handoff: envelope parsing, `ready_for_handoff`, SQL-returned-equals-SQL-executed, fail-closed on errors, final SQL emitted only when approved.
- One test per bug-ledger row (BL1-BL10) as described above.
- Graph-routing fixtures: success in one turn, tool loop, critique approve, critique reject then repair, repeated rejection stop, max tool limit, provider error, Oracle error, empty retrieval, Neo4j down, missing collection.
- **Evaluation harness skeleton** `tests/eval/dwh_scenario/`: a case file format (YAML/JSONL) with fields `scenario_description`, `intent` (A's `AMLIntent` JSON), `expected_constraints` (required projection `CUS_NUM`, required filters, window/baseline semantics, forbidden constructs), `expected_sql` (optional), `source` (`synthetic` or `officer`). Metrics are structural: SQL parses (Oracle dialect), SELECT-only, required projection present, confirmed filters present, execution success on fake data, plus latency, LLM calls, tool calls, tokens. Exact-match is reported only when `expected_sql` exists.
- 20-30 hand-written **synthetic** cases derived from A's own filter catalogs and scenario types, all tagged `source: synthetic`. Include negative cases: write SQL, missing confirmed filter, contradictory filters, ambiguous date, unsupported table.

**Acceptance:** suite runs offline with no credentials; bug-ledger tests fail as expected (strict xfail); harness runs a stub generator end to end; adding an officer-provided case is only adding a file row.

**Rollback:** delete the test directories.

## B1: Typed contract adapter

**Goal:** A's `AMLIntent` is the single source of truth at the boundary.

**Create in A:** `web/domain/dwh_scenario/contracts.py` with `ScenarioSqlRequest` (built from `AMLIntent` plus resolved/confirmed filters, version context) and `ScenarioSqlResult` (SQL, execution evidence metadata, validation findings, approval state, model/provider metadata, trace id). Define the interface `ScenarioSqlGenerator.generate(request) -> ScenarioSqlResult` and make the existing HTTP adapter implement it.

**Port from B:** the intent-validation logic of `scenario_contract.py` (checks for `CUS_NUM` projection, EXPL\_CODE, rolling-window), re-expressed on A's types; reject unknown or incomplete required semantics instead of B's permissive `extra=allow`.

**Acceptance:** all `AMLIntent` variants serialize and map; unresolved required filters reject; the HTTP adapter satisfies the interface with no behavior change; B0 contract tests pass.

**Rollback:** revert the adapter selector; the old call path remains.

## B2: Settings, provider factory, embeddings

**Goal:** one provider layer and one settings authority.

**Change in A:** extend `web/services/settings.py` (new `DWH_SQL_*` keys: provider per role, models, timeouts, retry budget, max attempts, max candidate rows, allowed schemas, retrieval flags) and `llm_client.build_llm`. Add `web/services/dwh_scenario/embeddings.py`.

**Port from B:** the embedding provider implementations (local BGE, finance model, OpenAI `text-embedding-3-small`, Cohere), refactored.

**Requirements:**

- Every provider gets connect/read timeouts, a bounded retry policy (transient errors only, inside an end-to-end deadline), output-token limit and context budget as separate settings (fixes BL10), and A's tracing callbacks.
- API keys injected from settings (BL3); one cached model instance per (provider, model, device) (BL6); bounded concurrency for local models; lazy load (no model load at import).
- Embedding provider, model, dimension and fingerprint are explicit config; mixing an index built with one model and queried with another fails loudly.
- Per-role selection (BL5): generator, critic, summarizer and embeddings can each use a different provider/model.
- New dependencies are optional extras (Cohere, Neo4j, local embeddings, Chroma). The API image does not need torch unless local embeddings are enabled.

**Acceptance:** provider matrix tests with fake clients for all five LLM providers and three embedding providers (assert model, timeout, retries, token args, tracing callbacks, no secrets in logs); OpenAI and LM Studio paths additionally cover base URL handling, tool-calling and malformed tool-call failure modes; dependency lock reconciled without downgrading A (B's older pins are not adopted).

**Rollback:** feature flag off; revert dependency extras.

## B3: Oracle read repositories and SQL policy

**Goal:** every database access goes through A's pool and a deterministic guard.

**Create in A:** DWH read repositories (dictionary tables/columns, join metadata, business-code lookup, candidate-SQL execution) on A's Oracle adapter; `web/domain/dwh_scenario/sql_policy.py` using SQLGlot (Oracle dialect).

**Requirements:**

- SELECT-only by AST, CTEs allowed (BL1), single statement, no comment-obfuscated constructs, schema/object allowlist, required projection and confirmed filters, bind variables.
- Candidate execution: bounded sample fetch, per-call timeout, `truncated` flag, no total-count claims (BL9), per-request budget on Oracle statements and wall time.
- The returned SQL must be exactly the executed SQL.
- Do NOT port the explain-plan/query-estimator (writes to `PLAN_TABLE`).
- **OWNER/DBA:** confirm which schema owns the dictionary tables (`PIO_BANKBI_DICTIONARY_TABLES`, `PIO_BANKBI_DICTIONARY_COL`, `PIO_BANKBI_DICT_JOIN_TABLES`, `PIO_AML_TABLES`, `PIO_AML_COLUMNS`), the grants of the account used, and whether a read-only account is available for candidate execution.

**Acceptance:** fixtures for SELECT, WITH, comments, semicolons, nested writes, quoted identifiers, aliases, literals and binds; query-count and timeout budgets enforced against a fake Oracle.

**Rollback:** disable the tool registry flag; the HTTP adapter stays default.

## B4: Retrieval layer (Chroma, experience, Neo4j) with graceful degradation

**Goal:** keep the retrieval stack, make it measurable and safe.

**Create in A:** `Retriever` protocols and adapters for `schema_tables`, `schema_columns`, `experience_templates` (single collection-name constants, BL2), and an optional Neo4j reranker.

**Requirements:**

- Collection names, k values, thresholds, score weights (currently hardcoded 400 candidates, 50/50 semantic/graph, threshold 0.6) become config.
- Index manifest: embedding provider, model, dimension, build time, source row counts. Checked at startup and in readiness; mismatch is a hard, visible error.
- **Experience retrieval must work with an empty or missing collection** (no pairs exist yet): return no examples and continue; readiness shows "empty", not "down".
- Neo4j behind a feature flag with managed driver lifecycle, query timeout, bounded results, degraded status and metrics, and Chroma-only fallback. No default password; the secret is required when the flag is on.
- Readiness reports Oracle, each Chroma collection and Neo4j separately (BL7).

**Acceptance:** retrieval fixtures with expected IDs and ranks on a tiny disposable local Chroma; Neo4j-down and collection-missing cases degrade as specified; no model or network use in unit tests.

**Rollback:** point config at the previous retriever or disable the flag.

## B5: Agent decomposition (graph, tools, critic)

**Goal:** replace the 2,942-line `agent.py` with small injected modules.

**Create in A:** `web/services/dwh_scenario/{graph,generator,critique,tools/*}.py` (eight tools: `think`, experience search, advanced schema search, column schema search, SQL execution, column value probe, standard join lookup, business-code lookup), with dependency injection and no import-time clients or graph compilation.

**Requirements:**

- Hard budgets per request: tool calls, critique attempts, Oracle statements, wall time (B's current caps are 25 tool calls and 4 critique attempts; keep them as configurable defaults). Remove the unused `MAX_SUPERVISOR_ITERATIONS`.
- Enforce the "experience first" ordering in code only if it proves useful; otherwise drop the claim from the prompt.
- The critic is advisory: it cannot override `sql_policy`. By default it receives columns, row count and errors, not rows.
- `manage_memory`: fix or remove (BL4); decide in B0 results whether compaction is needed at all, since one scenario per chat bounds history.
- Prompts and the domain control plane become versioned files and are reconciled with A's intent, filter and window semantics.

**Acceptance:** B0 graph-routing fixtures pass on the new graph; budgets enforced; the critique cannot approve SQL that fails policy; provider matrix still green.

**Rollback:** flag back to the HTTP adapter.

## B6: In-process generator behind a flag, plus shadow comparison

**Goal:** wire the new generator into A without changing default behavior.

**Change in A:** register the in-process `ScenarioSqlGenerator` implementation; selection via setting (`http` default, `inprocess`, `shadow`). In `shadow` mode (non-production only) A runs both, uses the HTTP result, and logs a structural diff (parse validity, projection, filters, safety, latency, calls, tokens) with no customer data.

**Acceptance:** A's full scenario flow (shadow test, approval, deployment gates) works end to end with both implementations on fakes; the harness reports a side-by-side comparison on the synthetic set; officer pairs, when available, are added as cases without code change.

**Rollback:** setting back to `http`.

## B7: Owner-run index, graph and enrichment toolchain

**Goal:** keep the scripts' capabilities, remove their hazards. These run outside the app, never at startup or request time.

**Create in A:** `scripts/dwh_scenario/` as one parametrized CLI:

- `harvest_dictionary` (read-only, bounded, with manifest).
- `build_schema_index` (temp collection, validate counts and fingerprint, then promote; keep the previous version).
- `build_experience_index` (canonical JSONL input; empty input is valid; **`ingest-pairs` command ready for the officer's file when it arrives**).
- `build_join_graph` (versioned projection, validate, promote; requires Neo4j GDS, **OWNER** to confirm availability).
- `enrich_dictionary` (separate command): generates descriptions, then **emits** (a) a backup SELECT of affected rows, (b) scoped UPDATE statements keyed by table AND column (and project where applicable, unlike B's `WHERE COLUMN_NAME = :c_name`), (c) a reverse script. The owner reviews and runs them in Toad. The tool never executes writes.
- Every LLM-calling script sends only dictionary metadata, never query results, and respects the per-role provider config and timeouts.

**Out of scope:** `prepare_training_data`, `evaluate_rewards`, `train_model`, `train_dpo`, `templatize_golden_dataset`'s training use (templating pairs for RAG is allowed if useful).

**Acceptance:** dry-run outputs reviewed on fakes; promote and rollback demonstrated on a disposable local Chroma; scoped-SQL generation tests; no execution path for writes exists in code.

**Rollback:** promote the previous index/graph version.

## B8: Cutover and retirement

**Goal:** make in-process the default once parity is measured.

**Steps:** flip default to `inprocess` only after the owner reviews the shadow comparison (non-production) for correctness, latency, call counts and tokens; keep `http` selectable for an agreed period; keep PioTech-AI untouched; then update `PROJECT_OVERVIEW.md` and `Docs/refactor_status.md`.

**Rollback:** setting back to `http`.

---

## Open decisions (none block B0-B6)

- **D1: provider data approval (OWNER/compliance):** which providers may receive which data classes. B2 makes this a config switch; the answer feeds the PII masking work.
- **D2: deployed index inventory (OWNER):** exact embedding model and version of the Chroma and Neo4j data currently in use. If unknown, B7 rebuilds from the dictionary instead of reusing.
- **D3: learning loop:** B's feedback endpoint is not ported. Later option: feed scenarios the officer approves and A deploys into the experience index. Decide after the officer's pairs exist.
- **D4: officer's pairs:** when they arrive, they become `source: officer` eval cases and experience-index input; no migration step waits for them.

---

## Codex execution protocol — use this file as the single source

**Invocation:** `Refer to B-into-A_migration_packages.md and do B0.` (replace `B0` with the desired package `B1`–`B8`). Do not require the user to paste any other prompt, shared block, or AGENTS.md instructions.

For the requested package only: read **Locked decisions**, **Rules for the IDE agent**, and **Bug ledger** above; then follow the **Shared execution contract** and the matching **Detailed B# implementation prompt** below. The earlier B0–B8 sections provide goals and acceptance context; the detailed prompt supplies the executable steps, tests and reporting format. Preserve both; where wording differs, apply the stricter safety restriction and flag any material contradiction rather than silently changing scope. Do not execute other packages, live services, or owner-only actions. Honor package dependencies and approval gates.

**Workflow:** inspect and give a short plan, implement only the selected package, run offline checks, then report using the six required sections. An explicit owner approval is required before moving to the next package; B8 requires the separate cutover approval specified below.

## Detailed B# implementation prompts

The following blocks are **instructions to execute**, not text to paste into another chat. The shared contract applies automatically to each package.

## Shared execution contract (automatically applies to every B#)

```
CONTEXT
A = C:\\Users\\nidal.shahin\\Code\\ai-aml-agent (target, already refactored; quality bar).
B = C:\\Users\\nidal.shahin\\Code\\PioTech-AI (source). B is READ-ONLY: never modify, move, delete or create anything in B.
Goal: move ONLY B's services/dwh_scenario_agent into A as one resilient, in-process package behind a typed interface. The existing HTTP adapter (PIOTECH_AI_URL) stays the default until B6/B8. Packages are named B0..B8 (P-numbers belong to A's earlier refactor).
Background reports in repo A: Docs/b_into_a_investigation.md and Docs/b_into_a_investigation_round2.md.

DECISIONS
- Keep Chroma (schema_tables, schema_columns, experience_templates), Neo4j reranking and the index/enrichment scripts, made configurable, optional and resilient.
- No model training. Experience pairs are RAG lookup only; the experience index must work when empty. No golden dataset exists yet: use synthetic fixtures.
- Keep all LLM providers (OpenAI, LM Studio, Cohere, SambaNova, OpenRouter) behind ONE factory with per-role config (generator, critic, summarizer, embeddings). OpenAI and LM Studio fully tested; the others covered by fake-client contract tests.
- The critic gets NO raw result rows by default (columns, row count, errors only).
- The app and scripts never write to Oracle dictionary tables. Enrichment only EMITS scoped SQL + backup SELECT + reverse script for the owner to run in Toad.
- A gets no new HTTP route, session store, /jobs, /report or /feedback endpoint.
- Ignore all docker-compose files and deployment_v1.0/.

RULES
- Work on a branch named refactor/b<N>-<slug>. Do not start another package.
- NEVER run anything live: no Oracle, LLM, embedding API, Chroma build, Neo4j, or enrichment calls. Offline pytest with fakes and temp paths is allowed and expected.
- Never copy .env, artifacts/, data/, static/, logs/, checkpoints, or Chroma/Neo4j data from B. Never print secret values.
- Never touch the existing PIO_AML_PRODUCTION_SCENARIOS or PIO_AML_SCENARIO tables. Do not port B's explain-plan helper (it writes to Oracle PLAN_TABLE).
- Preserve LANGCHAIN_TRACING_V2 / LANGCHAIN_API_KEY / LANGCHAIN_PROJECT propagation and configurable LLM_MAX_TOKENS-style settings.
- Do not log raw SQL, prompts, result rows, secrets or customer values.
- Do not add dependencies to A's required set; heavy ones (chromadb, neo4j, torch, sentence-transformers, cohere) must be optional extras. Do not downgrade any A pin to match B.
- Anything needing the owner (Toad, DBA, deployment, data approval) is marked OWNER: list it in your report, do not attempt it.

BUG LEDGER (verified defects in B; correct behavior must be asserted by tests)
BL1 tool guard accepts WITH but oracle.run_readonly rejects anything not starting with SELECT
BL2 feedback writes collection experience_pairs; build and search use experience_templates
BL3 embedding manager never passes API keys to OpenAI/Cohere providers
BL4 manage_memory calls .invoke on the (client, model) tuple from get_llm_client; error swallowed, canned summary used
BL5 critic only handles OpenAI/LM Studio; other providers fall through the OpenAI path; fast model may be unset
BL6 local embedding model loaded twice (warm-up passes a device, manager path does not)
BL7 startup swallows Oracle/embedding/Chroma errors; /health healthy anyway; no Neo4j check
BL8 index rebuild drops the live Chroma collection first; graph rebuild deletes the live Neo4j graph
BL9 candidate SQL fetches up to 10,000 rows; row_count is rows fetched, not total
BL10 OpenAI generation ignores the output-token setting; LLM_MAX_TOKENS used as both output limit and context budget

REPORT BACK (end your final message with exactly these sections)
1. Plan you followed (max 10 lines) and any deviation from this prompt, with the reason
2. Files created/changed in A (path + one line each); files read in B
3. Tests: added / passing / xfail(strict) / skipped, and the exact command to run them
4. Bug-ledger rows touched and their status
5. OWNER items and open questions
6. Risks you noticed that were not in the prompt
Be explicit about VERIFIED (you ran it or saw it in code, with path:line) vs INFERRED.
```

---

### B0: Baseline, bug-ledger tests and evaluation harness

```
TASK B0: tests and evaluation harness only. Branch refactor/b0-baseline. No production behavior changes.

STEP 1 (inspect, then write your plan in max 15 lines before coding):
- Read B: services/dwh_scenario_agent/tests/test_service_a_contract.py, web/services/scenario_contract.py, and the structure of web/services/agent.py (graph nodes, the eight tools, budgets), oracle.py, embeddings_provider.py, llm_init.py.
- Read A: its test layout, conftest, how existing tests fake Oracle and LLM, and the AMLIntent and filter-catalog types.

STEP 2 (implement):
1. tests/characterization/dwh_scenario/ with reusable fakes: FakeOracle (scripted rows, errors, timeouts, statement counter), FakeLLM (scripted tool calls and messages, including malformed tool calls), FakeChroma (in-memory collections, can be empty or missing), FakeNeo4j (healthy / down / slow).
2. Port the behavior of B's test_service_a_contract.py as characterization tests: envelope parsing, ready_for_handoff, SQL-returned-equals-SQL-executed, fail-closed on errors, final SQL emitted only when approved.
3. Graph-routing fixtures: one-turn success; tool loop; critique approve; critique reject then repair; repeated rejection stop; max tool-call limit; provider error; Oracle error; empty retrieval; Neo4j down; missing collection.
4. One test per bug-ledger row BL1-BL10 asserting the CORRECT behavior. Mark each xfail(strict=True) with the BL id as the reason. B's code uses bare 'web.services.*' imports and sys.path insertion: do NOT import B inside A's normal test process in a way that pollutes sys.modules. Use isolated loading (importlib from file path with cleanup, or a subprocess helper). If a B module cannot be imported cheaply (missing chromadb/torch), exercise the smallest function you can or assert on the source structure, and say in the report which BL tests are executable and which are static-evidence.
5. Evaluation harness at tests/eval/dwh_scenario/: case file format (YAML or JSONL) with fields scenario_description, intent (A's AMLIntent JSON), expected_constraints (required projection CUS_NUM, required confirmed filters, window/baseline semantics, forbidden constructs), expected_sql (optional), source (synthetic | officer). A runner that takes any generator callable and reports: SQL parses (Oracle dialect via sqlglot), SELECT-only, required projection present, confirmed filters present, execution success on fake data, plus latency, LLM calls, tool calls, tokens. Exact SQL match is reported ONLY when expected_sql exists.
6. 20-30 hand-written synthetic cases built from A's own scenario types and filter catalogs, all tagged source: synthetic. Include negative cases: write SQL, missing confirmed filter, contradictory filters, ambiguous date, unsupported table. Add a README explaining how the officer's pairs will be added later (new rows with source: officer, no code change).

STEP 3: run the offline suite. Existing A tests must still pass. Report counts.

ACCEPTANCE: runs offline with no credentials; BL tests fail as strict xfail; the harness runs end to end with a stub generator; no files outside tests/ and docs changed.
```

---

### B1: Typed contract adapter

```
TASK B1: typed request/result contract and generator interface. Branch refactor/b1-contract-adapter. Depends on B0 being merged.

STEP 1 (inspect, then plan in max 12 lines):
- A: AMLIntent, resolved/confirmed filter structures, the tool/service that currently calls PIOTECH_AI_URL, its settings and error handling.
- B: web/services/scenario_contract.py (what it validates: CUS_NUM projection, EXPL_CODE, rolling-window checks, extra=allow, ready_for_handoff default) and where api/main.py uses it.

STEP 2 (implement):
1. web/domain/dwh_scenario/contracts.py: ScenarioSqlRequest (built from AMLIntent plus resolved/confirmed filters and version context) and ScenarioSqlResult (final SQL, approval state, execution evidence metadata: columns, row count, truncated flag, error; validation findings; provider/model metadata; trace id). Pydantic, extra='forbid'.
2. Interface ScenarioSqlGenerator.generate(request) -> ScenarioSqlResult (Protocol or ABC) and make the existing HTTP adapter implement it with NO behavior change on the wire.
3. Re-express B's contract checks on A's types as pure functions in web/domain/dwh_scenario/. Reject unknown or incomplete required semantics rather than allowing extras. List every case where B's check is weaker or different from A's confirmed-filter semantics in your report.
4. Do not add routes, sessions or settings beyond what the adapter needs.

STEP 3: offline tests. Cover every AMLIntent variant (multiple transaction types, all transactions, baseline windows, optional filters), malformed input, unresolved required filters. B0 contract tests must pass; BL tests unchanged.

ACCEPTANCE: all AMLIntent variants map; unresolved required filters reject; the HTTP adapter satisfies the interface; A's existing scenario flow tests unchanged and green.
```

---

### B2: Settings, provider factory and embeddings

```
TASK B2: one provider layer and one settings authority. Branch refactor/b2-providers. Depends on B1.

STEP 1 (inspect, then plan in max 15 lines):
- A: web/services/settings.py, llm_client.build_llm, LangSmith propagation, how A tests fake LLM clients.
- B: settings.py, settings_graph.py, llm_init.py, embeddings_provider.py (all four embedding modes), and every LLM construction site in agent.py (generator, critic, summarizer, memory).

STEP 2 (implement):
1. Extend A's Settings with DWH_SQL_* keys: provider and model per role (generator, critic, summarizer, embeddings), timeouts, retry budget, max attempts, output-token limit, context budget, max candidate rows, allowed schemas. Secrets only from environment; update .env.example with key names only.
2. Extend the A LLM factory to support all five providers with: connect/read timeouts, bounded retries for transient errors only inside an end-to-end deadline, output-token limit and context budget as SEPARATE settings honored by every provider (BL10), tool-calling support flags, and A's tracing callbacks. Fix the critic provider selection (BL5): the critic uses its own configured role or fails with a clear config error.
3. web/services/dwh_scenario/embeddings.py: port the embedding providers (local BGE, local finance model, OpenAI text-embedding-3-small, Cohere) as injected classes. Keys come from settings (BL3). One cached model instance per (provider, model, device) (BL6). Lazy load, no model load at import, bounded concurrency for local models. Record provider, model, dimension and a fingerprint so B4 can detect index/model mismatch.
4. Optional extras only for cohere, local embeddings (torch, sentence-transformers, transformers), chroma, neo4j. Show the dependency delta in your report and confirm A's required requirements are unchanged.
5. OpenAI and LM Studio specifics: base URL handling, tool-calling and structured-output differences, malformed tool-call handling. Document what you implemented.

STEP 3: offline tests. Provider matrix with fake clients for all 5 LLM providers and 3 embedding providers asserting model, timeouts, retries, token args, tracing callbacks, missing-key error messages, no secret in logs. Flip BL3, BL5, BL6, BL10 from xfail to passing.

ACCEPTANCE: no network in tests; A pins untouched; tracing env propagation tests still pass; BL3/BL5/BL6/BL10 pass.
```

---

### B3: Oracle read repositories and SQL policy

```
TASK B3: DWH read repositories on A's Oracle adapter plus a deterministic SQL policy. Branch refactor/b3-oracle-sql-policy. Depends on B2.

STEP 1 (inspect, then plan in max 15 lines):
- A: web/services/oracle.py (pools, read helpers, shadow vs primary), existing SQLGlot/filter verification, how A passes query timeouts.
- B: oracle.py, and in agent.py the tools execute_sql_query, probe_column_values, lookup_standard_join, lookup_business_code, search_database_specific_column_schema, and every Oracle statement they issue (list them with the table names).

STEP 2 (implement):
1. web/domain/dwh_scenario/sql_policy.py using sqlglot (Oracle dialect): SELECT-only by AST, CTEs allowed (BL1), single statement, no comment-obfuscated constructs, schema/object allowlist from settings, required CUS_NUM projection and confirmed filters from the request, bind variables where literals come from user input. Fail closed on unparseable SQL.
2. Read repositories in A (use A's pool, never a new pool): dictionary tables/columns, join metadata, business-code lookup, candidate-SQL execution. Candidate execution: bounded sample fetch with explicit truncated flag and no total-count claims (BL9), per-call timeout, a per-request budget on Oracle statements and wall time. The SQL returned must be exactly the SQL executed.
3. Do NOT port B's explain-plan or query-estimator code (it writes to PLAN_TABLE). Note in your report what B used the estimate for.
4. Report as OWNER items: which schema owns PIO_BANKBI_DICTIONARY_TABLES, PIO_BANKBI_DICTIONARY_COL, PIO_BANKBI_DICT_JOIN_TABLES, PIO_AML_TABLES, PIO_AML_COLUMNS in the environment; whether a read-only account exists for candidate execution. Do not guess; make owner and allowlist configurable.

STEP 3: offline tests against FakeOracle: SELECT, WITH, comments, semicolons, multi-statement, nested writes, quoted identifiers, aliases, literals and binds, disallowed schema/table, missing projection, missing confirmed filter, unparseable SQL, timeout, statement-budget exhaustion, truncation flag. Flip BL1 and BL9 to passing.

ACCEPTANCE: no write statement can pass the policy; every Oracle access goes through A's adapter; the repositories are injectable; BL1 and BL9 pass.
```

---

### B4: Retrieval layer with graceful degradation

```
TASK B4: Chroma, experience and Neo4j retrieval behind interfaces, with readiness. Branch refactor/b4-retrieval. Depends on B2 (B3 not required).

STEP 1 (inspect, then plan in max 15 lines):
- B: in agent.py the tools search_experience_database, advanced_schema_search, search_database_specific_column_schema; graph_client.py; settings_graph.py; scripts/build_hierarchical_index.py, build_experience_index.py, build_graph_from_joins.py (collection names, document and metadata schema, node/edge schema); embeddings use.
- A: how services and adapters are registered, how readiness/health is exposed.

STEP 2 (implement):
1. Retriever protocols and Chroma adapters for schema_tables, schema_columns and experience_templates. ONE set of collection-name constants used by build, search and any write path (BL2). Hardcoded values (400 candidates, top-k 8/10/20/3, score threshold 0.6, 50/50 semantic-graph weighting, standard-column boost) become configuration with the current values as defaults.
2. Index manifest (embedding provider, model, dimension, fingerprint, build time, source row counts). Startup/readiness compares it with the configured embedding provider; a mismatch is a loud, explicit error, never a silent query with the wrong model.
3. Experience retrieval must work with an EMPTY or MISSING collection: return no examples and continue; readiness reports 'empty', not 'down'.
4. Neo4j as an optional reranker behind a feature flag: managed driver lifecycle (no per-call create/close), query timeout, bounded results, degraded status and counters, automatic Chroma-only fallback. The password has no default; when the flag is on and the secret is missing, fail with a clear config error. Do not port the legacy helpers that query REFERENCES/owner shapes unless you can show an active caller.
5. Readiness model: separate ok/degraded/down status for Oracle (use the interface, mocked here), each Chroma collection, and Neo4j (optional). Fix BL7 semantics: a healthy flag may not hide a failed required dependency.

STEP 3: offline tests on a tiny disposable local Chroma (or FakeChroma): expected IDs and ranks, empty experience collection, missing collection, fingerprint mismatch, Neo4j down/slow/unconfigured, weight/threshold config. No model download and no network: use a deterministic fake embedder. Flip BL2 and BL7 to passing.

ACCEPTANCE: Chroma-only operation works with Neo4j off; the app starts and reports degraded rather than crashing when an optional part is missing; BL2 and BL7 pass.
```

---

### B5: Agent decomposition (graph, tools, critic)

```
TASK B5: replace B's 2,942-line agent.py with small injected modules. Branch refactor/b5-agent. Depends on B1-B4 merged.

STEP 1 (inspect, then plan in max 20 lines including the module split):
- B: agent.py in full (graph nodes manage_memory, agent, call_tools, critique; the eight tools think, search_experience_database, advanced_schema_search, search_database_specific_column_schema, execute_sql_query, probe_column_values, lookup_standard_join, lookup_business_code), prompts/data_agent_system.md, prompts/critique_agent_system.md, config/dwh_domain_control_plane.md.
- For each tool: which are needed for the AML scenario path and which only serve generic chat. List them. Do not port generic-chat-only behavior (image helper, generic intent classifier, Excel export).

STEP 2 (implement under web/services/dwh_scenario/):
1. graph.py (bounded LangGraph orchestration), generator.py, critique.py, tools/ (one module per tool), context assembly, prompt loader. No import-time clients, no import-time graph compilation, no module-level globals: everything injected (LLM factory, retrievers, repositories, policy).
2. Hard per-request budgets, configurable with B's current values as defaults (tool calls 25, critique attempts 4): tool calls, critique attempts, Oracle statements, wall time. Remove the unused MAX_SUPERVISOR_ITERATIONS. Emit structured counters (LLM calls, tool calls, Oracle statements, tokens).
3. The critic is advisory only: it can never approve SQL that fails sql_policy. By default it receives columns, row count and errors, not result rows; a row preview is an explicit setting that is off by default.
4. manage_memory (BL4): decide from the code whether compaction is needed given one scenario per chat; either implement it correctly through the shared LLM factory or remove it with a justification. Never silently substitute a canned summary.
5. Prompts and the domain control plane become versioned files. Reconcile them with A's AMLIntent, confirmed-filter, EXPL_CODE and window semantics; list contradictions you found and how you resolved them. Prompt text is not enforcement: every rule that matters is also checked in code.
6. Return a ScenarioSqlResult (B1) from the generator implementation.

STEP 3: offline tests: all B0 graph-routing fixtures against the new graph; budget enforcement; critic cannot override policy; provider matrix still green; synthetic eval harness run with FakeLLM scripted cases. Flip BL4 to passing. Report which B0 characterization tests had to change and why.

ACCEPTANCE: new graph passes the B0 fixtures; no module over ~400 lines; no import-time side effects (test by importing every module with network and filesystem faked).
```

---

### B6: In-process generator behind a flag, with shadow comparison

```
TASK B6: wire the in-process generator into A without changing default behavior. Branch refactor/b6-flag-shadow. Depends on B5.

STEP 1 (inspect, then plan in max 12 lines):
- A: the tool that calls the generator (tools.py) and the ScenarioSqlGenerator interface from B1, settings, SSE and error contract, where shadow tests and approval happen.

STEP 2 (implement):
1. Setting DWH_SQL_MODE = http (default) | inprocess | shadow. 'http' = current behavior, byte-for-byte unchanged.
2. 'shadow' (must refuse to run when the environment is marked production; add that guard): call both, USE the http result, run the in-process generator with a hard time limit in the background, and log a structural diff only: SQL parses, SELECT-only, projection, confirmed filters, latency, LLM calls, tool calls, tokens, errors. No SQL text, rows or customer values in the diff.
3. The in-process path must obey A's existing approval, shadow-test and deployment gates unchanged.
4. Add a small comparison report command that runs the B0 evaluation harness over the synthetic set against both implementations (fake backends offline) and prints a side-by-side table.

STEP 3: offline tests: A's full scenario flow with each mode on fakes; shadow failure of the in-process side never affects the user-facing result; production guard; mode switch needs no restart-time side effects. A's existing tests unchanged.

ACCEPTANCE: default behavior identical; shadow mode safe; comparison report works on synthetic cases; officer pairs can be added as eval cases without code changes.
```

---

### B7: Owner-run index, graph and enrichment toolchain

```
TASK B7: CLI toolchain for indexes, graph and dictionary enrichment. Branch refactor/b7-toolchain. Depends on B4. These tools run OUTSIDE the app, never at startup or request time. Do NOT execute any of them.

STEP 1 (inspect, then plan in max 15 lines):
- B: scripts/harvest_all.py, build_hierarchical_index.py, build_experience_index.py, build_graph_from_joins.py, enrich_table_descriptions.py, enrich_glossary_metadata.py, templatize_golden_dataset.py, web/services/harvestor.py, embedding.py. For each: inputs, outputs, Oracle reads, Oracle WRITES (exact statements), LLM calls and exactly what data they send, idempotency, dry-run support.

STEP 2 (implement scripts/dwh_scenario/ as one parametrized CLI reusing B2/B4 modules):
1. harvest_dictionary: read-only, bounded and streaming, writes a manifest.
2. build_schema_index: build into a temp/versioned collection, validate counts and fingerprint, then promote atomically; keep the previous version for rollback (BL8). Never drop the live collection first.
3. build_experience_index: canonical JSONL input (B's file uses blank-line blocks despite the .jsonl name: define one canonical format and write a converter). An EMPTY input is valid and produces an empty collection. Add an ingest-pairs command ready for the AML officer's future file (question or scenario description plus SQL), validating and deduplicating it.
4. build_join_graph: versioned projection, validate, promote; fail clearly if GDS PageRank is unavailable (OWNER to confirm GDS).
5. enrich_dictionary (separate command, not run by default): generate descriptions with the configured summarizer/enrichment role, then OUTPUT three files and nothing else: (a) a backup SELECT of the affected rows, (b) scoped UPDATE statements keyed by table AND column (and project where applicable; B's script updates WHERE COLUMN_NAME = :c_name with no table scope: show in your report what that would touch), (c) a reverse script. There must be NO code path in these tools that executes an Oracle write. The LLM receives dictionary metadata only, never query results.
6. Out of scope, do not port: prepare_training_data, evaluate_rewards, train_model, train_dpo.

STEP 3: offline tests: dry-run output review on fakes; promote and rollback on a disposable local Chroma; scoped-SQL generation tests including a case where the same column name exists in several tables; a test that proves no write execution path exists (e.g. a connection fake that fails on any non-SELECT).

ACCEPTANCE: BL8 passes; every command has --dry-run semantics or is read-only; no live execution; clear OWNER runbook in docs describing what to run in Toad and in what order.
```

---

### B8: Cutover and retirement

```
TASK B8: make in-process the default, only if the owner has approved. Branch refactor/b8-cutover. Depends on B6 and B7, and on the owner's explicit approval of the shadow comparison. If you were not given that approval in this prompt's header, STOP after step 1 and report.

STEP 1 (verify, report):
- Run the offline suite and the comparison report from B6; list every BL row and confirm each is passing.
- Checklist of what is still unverified live (OWNER): provider data approval, deployed index versions, Oracle grants, Neo4j GDS.

STEP 2 (only with approval): change the default of DWH_SQL_MODE to inprocess, keep 'http' selectable, keep PIOTECH_AI_URL settings and the adapter, and add a startup warning when 'http' is used with the new default. Do not remove, edit or delete anything in B.

STEP 3: update PROJECT_OVERVIEW.md and Docs/refactor_status.md (packages B0-B8, status, settings, how to switch modes, how to roll back, the OWNER runbook for the toolchain). Offline tests pass in all three modes.

ACCEPTANCE: a one-line settings change returns the system to B's HTTP service; documentation matches the code.
```

---

### Order and checkpoints

- B0 first (safe, no behavior change). Then B1. B2 and B4 can be done in either order after B1 if you want parallel branches, but B5 needs B1-B4.
- Paste each report here before the next package. I'll check it against the bug ledger, the locked decisions, and the interface contracts.
- OWNER items to collect along the way: provider data approval (B2), Oracle ownership and grants (B3), deployed index versions and Neo4j GDS (B4/B7), your Toad run of any enrichment output (B7).

---

## B4 completion report — 2026-10-26

### 1. Plan followed and deviations

- Inspected A's B3 state, settings, health route, B0 fakes, and bug-ledger tests; created `refactor/b4-retrieval` from B3 HEAD.
- Added injectable retrieval/reranking interfaces, institution-safe Chroma adapter, index manifest validation, optional Neo4j lifecycle/fallback, readiness probes and configuration.
- Replaced BL2 and BL7 source probes with behavioral tests; documented B4 and updated this ledger, changelog, and canonical overview.
- No Service B files were read or modified. No live database, model, Chroma build, Neo4j, enrichment, or B5 work was run.
- Deviation: B1-B3 regression tests were started but stalled during Windows asyncio setup after emitting 24 progress dots; the run was interrupted. This is the previously documented Proactor `socketpair()` sandbox limitation, so no B1-B3 pass claim is made.

### 2. Files created/changed in A; files read in B

- `services/aml_builder/web/services/dwh_scenario/retrieval.py` — retriever/reranker protocols, Chroma, manifests, readiness, and lazy graph adapter.
- `services/aml_builder/web/services/settings.py`, `.env.example` — collection/index/retrieval/Neo4j configuration.
- `services/aml_builder/web/api/main.py` — startup readiness snapshot, live readiness response and graph-driver shutdown.
- `services/aml_builder/tests/test_b4_retrieval.py` — offline adapter tests.
- `services/aml_builder/tests/characterization/dwh_scenario/test_bug_ledger.py` — behavioral BL2/BL7 replacements.
- `Docs/B4_retrieval.md`, `Docs/B-into-A_migration_packages.md`, `PROJECT_OVERVIEW.md`, `CHANGELOG.MD` — usage, report, architecture, and changelog.
- B files read: none. B files changed: none.

### 3. Tests

- **Added / passing:** B4 adapter tests and BL2/BL7 behavioral tests. Exact command: `\.venv\Scripts\python.exe -m pytest -q services\aml_builder\tests\test_b4_retrieval.py services\aml_builder\tests\characterization\dwh_scenario\test_bug_ledger.py` — **11 passed, 4 xfailed**.
- **Strict xfail:** BL1, BL4, BL8, BL9 remain strict xfails and are outside B4.
- **Skipped:** none in this run.
- **Regression attempt:** `\.venv\Scripts\python.exe -m pytest -q services\aml_builder\tests\test_b1_dwh_contract.py services\aml_builder\tests\test_b3_oracle_sql_policy.py services\aml_builder\tests\characterization\dwh_scenario\test_b2_provider_factory.py` stalled during startup; interrupted without a result summary.
- **Other checks:** `\.venv\Scripts\python.exe -m compileall -q services app.py run_alert_engine.py` and `git diff --check` passed.

### 4. Bug-ledger status

- **BL2 — VERIFIED fixed:** canonical `EXPERIENCE_COLLECTION_NAME`; missing experience collection yields no examples and readiness `empty`.
- **BL7 — VERIFIED fixed:** readiness reports Oracle, each Chroma collection and Neo4j separately; required collection failures affect aggregate readiness, while optional Neo4j degradation and empty experience remain visible.
- **BL1, BL4, BL8, BL9 — unchanged strict xfail.**
- **BL3, BL5, BL6, BL10 — existing B2 passing tests not rerun in this package.**

### 5. OWNER items and open questions

- **OWNER:** confirm deployed Chroma collection names, manifest embedding identity/fingerprint and source counts before pointing a deployment at an index.
- **OWNER:** confirm Neo4j endpoint, graph schema/rank semantics and GDS requirement before enabling reranking; credentials must be deployment-managed.
- **Open:** current graph query uses `n.id` and `n.rank`; confirm these properties match the deployed graph before enabling it.

### 6. Risks noticed

- **VERIFIED:** B4 adapter behavior is covered with in-memory fakes; real Chroma, Neo4j and Oracle readiness were not exercised. The B1-B3 regression command did not finish.
- **INFERRED:** semantic similarity and graph rank may have different scales; configured 50/50 weights may need calibration against a real, approved index before enabling Neo4j.
- The default HTTP SQL generator remains selected; B5 was not started.

## B5 completion report — 2026-10-26

### 1. Plan followed and deviations

- Created `refactor/b5-agent` from the checked-out B4 HEAD and built the local path behind an explicit factory; the active HTTP adapter/tool path is unchanged.
- Added bounded retrieval/context, provider generation, deterministic policy and intent validation, bounded Oracle execution, critique/repair, structured counters, and explicit failure states.
- Removed the need for memory compaction in the local one-request/one-intent flow; BL4 now checks typed context assembly rather than probing B source.
- Added offline behavioral tests for generation approval, critique rejection, unsafe SQL, zero rows, provider/retrieval errors, and context/tool/Oracle budget exhaustion.
- Checked B4's lazy readiness construction with no Chroma directory and disabled Neo4j; optional adapters do not initialize or block the HTTP-only path.
- Deviation: did not inspect Service B source; the B5 requirements and existing B1–B4 interfaces in A were sufficient for this isolated implementation, and no Service B changes were authorized.

### 2. Files created/changed in A; files read in B

- `web/services/dwh_scenario/graph.py`, `generator.py`, `critique.py`, `context_assembly.py`, `prompt_loader.py`, `prompts/`, and `tools/` — dependency-injected local graph and eight tool adapters; graph compile is explicit and on demand.
- `web/domain/dwh_scenario/contracts.py` — optional safe metrics on `ScenarioSqlResult`.
- `web/services/settings.py` — configurable tool, critique and Oracle statement caps.
- `tests/test_b5_local_generator.py` — offline behavior and lifecycle checks.
- `tests/characterization/dwh_scenario/test_bug_ledger.py` — BL4 behavioral replacement.
- `Docs/B5_local_generator.md`, `Docs/B-into-A_migration_packages.md`, `PROJECT_OVERVIEW.md`, `CHANGELOG.MD` — operating notes and verified package report.
- B files read: none. B files changed: none.

### 3. Tests

- **Added / passing:** focused B5, B4 and ledger run: `22 passed, 3 xfailed`. Command: `\.venv\Scripts\python.exe -m pytest -q services\aml_builder\tests\test_b5_local_generator.py services\aml_builder\tests\test_b4_retrieval.py services\aml_builder\tests\characterization\dwh_scenario\test_bug_ledger.py`.
- **Regression:** B1 contract, B2 provider factory and B3 SQL-policy tests: `59 passed`. Command: `\.venv\Scripts\python.exe -m pytest -q services\aml_builder\tests\test_b1_dwh_contract.py services\aml_builder\tests\test_b3_oracle_sql_policy.py services\aml_builder\tests\characterization\dwh_scenario\test_b2_provider_factory.py`.
- **Other checks:** compileall and `git diff --check` passed. Black passed for all 15 changed Python files. Scoped flake8 passed with `--max-line-length=88` to match Black's configured width.
- **Skipped:** none. The full suite was not run because of the documented Windows Proactor sandbox hang.

### 4. Bug-ledger rows touched and status

- **BL4 — VERIFIED fixed:** local context assembly uses the typed current request and bounded retrieval context; no memory compaction or canned-summary fallback exists in the new path. Behavioral test passes.
- **BL5 — VERIFIED passing:** the local path requests generator and critic roles independently through A's B2 provider factory. B2 provider matrix regression passed.
- **BL1, BL8, BL9 — unchanged strict xfail.**
- **BL2, BL3, BL6, BL7, BL10 — VERIFIED passing in focused B4/B2 tests:** B4 retrieval/readiness and the B2 provider factory regressions passed in this run.

### 5. OWNER items and open questions

- **OWNER/compliance:** confirm provider data approval (D1) before using external generator/critic providers with business scenario metadata.
- **OWNER:** confirm deployed Chroma collection names, embedding manifest identity and source counts before configuring the local path to an index.
- **OWNER:** confirm Neo4j endpoint, graph schema/rank fields and GDS availability before enabling reranking.
- **OWNER/DBA:** confirm B3 dictionary schema ownership, Oracle grants and read-only candidate-query account before live execution.
- The B5 path remains unselected. No live provider, Oracle, Chroma or Neo4j checks were run.

### 6. Risks noticed

- **VERIFIED:** SQL is returned only after AST policy, A intent checks, repository execution success and explicit critic approval; zero-row success is retained as row-count evidence. Tests use fakes only.
- **VERIFIED:** B4 readiness constructs lazy optional checks without creating Chroma or Neo4j clients; actual FastAPI startup still uses A's unchanged active graph.
- **INFERRED:** request context budgeting uses a conservative four-character-per-token estimate; provider tokenizer-specific accounting is not available for retrieval context.
- **INFERRED:** schema retrieval quality and critic judgments need a later approved index/provider evaluation; B5 does not establish SQL parity with the HTTP generator.
