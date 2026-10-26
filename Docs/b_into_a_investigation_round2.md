# PioTech-AI DWH Scenario Agent → ai-aml-agent: Investigation Round 2

**Scope and method.** This is a static, read-only source review of `PioTech-AI/services/dwh_scenario_agent` as a candidate integration into `ai-aml-agent`. Other PioTech-AI services were checked only for imports/shared modules. `run.ps1` and tracked source were inspected; no service, test, script, database, network/API, or model call was run. Secret values were not read or reproduced. Statements are tagged **VERIFIED** (visible in source/config), **INFERRED** (reasoned from source, not observed at runtime), or **UNKNOWN** (cannot be established without an owner-run check). Paths are repo-relative and line numbers refer to the inspected source snapshot.

## Corrections to round-one report

1. **Scope:** this report covers only `services/dwh_scenario_agent`, its scripts/assets, and the one shared module it imports. Compose files and other service applications are outside the migration design; startup conclusions follow the requested `run.ps1` path.
2. **Size:** replace the round-one rough “15–25 modules” estimate with a source-verified closure: **15 Python files / 7,873 lines** (14 under the DWH scenario agent and one B shared helper) plus **3 prompt/domain text files / 175 lines**. This is the Python runtime import closure, not the whole source-and-operations surface. The separate maintenance scripts listed below are not counted as runtime closure.
3. **Jobs:** `celery_worker.py` is not imported by the scenario API, and the `/jobs` code is report-file polling, not evidence of an enqueue/worker path. Do not migrate it as a background job system without discovering a real producer.
4. **Experience feedback:** feedback writes to Chroma collection `experience_pairs`, while retrieval/build use `experience_templates`; this is a concrete name mismatch.
5. **Shared classifier:** the optional B `shared.services.intent_library` import is reachable from the generic-intent branch, but `classify_intent` returns `None` before its model call. The scenario path supplies Service A intent and bypasses classification.
6. **Dependencies:** the shared-package mismatch count is **45**, not 39 as stated in round one.
7. **Graph topology:** generic chat and AML scenario generation use the same tool-driven graph and tool registry; there is no separate scenario subagent graph in this service.

## 1. Target architecture in A

### Layout and conventions

**VERIFIED.** A’s canonical map is `PROJECT_OVERVIEW.md`; source is rooted at `services/aml_builder`. The runtime is a FastAPI application with route modules, a LangGraph ReAct workflow, domain/workflow services, adapters for external systems, and repositories/data access behind explicit boundaries. `web/api/main.py` owns app construction/lifespan; routes are under `web/api/routes`; orchestration/tool contracts live in `web/services`; business validation and scenario/filter behavior are separated into domain/workflow modules; Oracle access is centralized in `web/services/oracle.py`. The alert engine also has a CLI entrypoint (`run_alert_engine.py`). The project overview is navigation, while implementation remains authoritative.

**VERIFIED.** A settings are Pydantic `BaseSettings` in `web/services/settings.py`. Provider/config choices belong there; secrets come from environment/`.env`, not source. The external DWH SQL generation seam is intentionally HTTP (`PIOTECH_AI_URL`), allowing DWH functionality to be consolidated without importing B’s app package or sharing its Python dependency namespace.

**VERIFIED.** A preserves LangChain/LangSmith tracing settings (`LANGCHAIN_*`), uses common LLM construction and parsing helpers, structured Pydantic contracts, logging, and typed error/result patterns. Oracle pools and read/write/atomic transaction operations are centralized. Current A source has SQL/filter safety and AML intent semantics, including confirmed/resolved filters; integration must not weaken those contracts. Tests currently have limited collection and a live/manual Oracle/OpenAI integration check; do not interpret collection as broad coverage.

**VERIFIED.** A dependency pins differ from B substantially. The inspected shared-package pins include A: FastAPI 0.137.2, LangChain 1.3.9, langchain-core 1.4.7, LangGraph 1.2.5, LangSmith 0.8.16, OpenAI 2.43.0, python-oracledb 4.0.1, Pydantic 2.13.4; B has 0.124.4, 1.2.0, 1.2.1, 1.0.5, 0.4.59, 2.12.0, 3.4.1, and 2.12.5 respectively. The verified overlap delta is 45 shared package entries; reconcile a single target lock rather than installing B’s requirements into A.

### B extension points and required integration pattern

| B capability | A extension point / contract | Required pattern |
|---|---|---|
| DWH scenario chat/SQL generation | A tool/service boundary currently calling `PIOTECH_AI_URL`; workflow/service layer | Replace the remote call behind an adapter only after the internal service exposes a typed request/result contract. Do not import B’s `web.*` modules into A. |
| Scenario intent handoff | A `AMLIntent`/scenario schema and route/service request contract | Preserve A’s resolved and confirmed filter structures end-to-end; validate once at the boundary and enforce deterministically before SQL execution. |
| Oracle dictionary access | A Oracle adapter/pool and repository/service methods | Use A-owned pool lifecycle, read-only methods, bounded queries, and explicit transactions for reviewed writes. No B-owned pool. |
| Chroma schema/experience retrieval | A service/retrieval adapter registry (or new DWH-specific adapter) | Preserve collections/providers as a selectable retrieval implementation; inject client/embeddings, version index metadata, use bounded retrieval and a fallback. |
| Neo4j join reranking | Optional graph adapter/registry | Keep behind `NEO4J_RERANKING_ENABLED`, timeout/health/fallback semantics, managed lifecycle; retain Chroma-only operation. Do not silently remove the requested graph feature. |
| LLM provider construction | A shared LLM factory/settings and callback/tracing config | One provider registry/factory for generator, critic, summarizer, and embeddings; provider-specific credentials/timeouts/retries/token caps; retain `LANGCHAIN_*` / LangSmith propagation. |
| Owner-run index/enrichment scripts | A CLI/maintenance command package, separate from API startup | Explicit read/write modes, preview/dry-run, durable manifests, bounded batches, atomic index swap. Never build indexes or mutate dictionary rows in web startup/request handlers. |

## 2. B service map and runtime closure

### Entrypoints and reachable API behavior

**VERIFIED.** Under the requested launcher, `run.ps1 -sb` starts A at port 8005 and this service from `services/dwh_scenario_agent` at port 8006; `run.ps1 -dwh_scenario` selects the DWH scenario service. The service is launched as `python -m uvicorn web.api.main:app` from its own directory. Do not rely on B’s package-qualified imports: runtime uses bare `web.services.*` imports and prepends this service directory to `sys.path`.

**VERIFIED.** `web/api/main.py` is the FastAPI entrypoint. It exposes health, streaming chat, session/history operations, feedback, report/job-status routes, and static/export behavior. The central streaming route accepts the last message and an optional Service A intent payload; `parse_service_a_intent` parses and rejects an intent not ready for handoff. For scenario requests it injects that intent into the prompt and calls the same graph as generic chat. Final SQL is emitted only when the graph reaches approved state and provides the final SQL. The graph’s “experience first” behavior is prompt guidance, not a route-level hard requirement.

**VERIFIED.** `main.py` import/startup is not side-effect free: module-level initialization creates/initializes local session state, `agent.py` initializes the execution log/checkpointer, and startup creates static/export directories and runs old-export cleanup. Lifespan attempts Oracle-pool initialization and embedding/Chroma warm-up, catches failures and continues; it does not perform a Neo4j readiness check. Thus process health may not mean retrieval/database readiness. Startup behavior and side effects are source-inspected only; they were not executed.

**VERIFIED.** `agent.py` compiles one cyclic graph: `manage_memory → agent → call_tools → agent`, with critique/approval/rejection routing. It registers eight tools: `think`, `search_experience_database`, `advanced_schema_search`, `search_database_specific_column_schema`, `execute_sql_query`, `probe_column_values`, `lookup_standard_join`, and `lookup_business_code`. `MAX_ITERATIONS=25`, `MAX_CRITIQUE_ATTEMPTS=4`; `MAX_SUPERVISOR_ITERATIONS=6` is defined but no supervisor graph/node uses it. API recursion limit is derived as `MAX_ITERATIONS * 3`. The graph/checkpointer uses relative `checkpoints.sqlite` paths, so cwd affects which local file is opened.

**VERIFIED.** `shared/services/intent_library.py` is imported via an inserted repository root and is the only runtime Python dependency outside this service subtree. No runtime imports from the other `services/aml`, `dwh`, `collection`, or `cockpit` applications were found in the scenario agent. This shared helper imports settings through the ambiguous `web.services.settings` namespace. Co-hosting/importing multiple services in one interpreter risks Python `web` module-cache collisions.

### Runtime files (verified inventory)

The following 15 Python files total 7,873 lines in the inspected snapshot. Counts are for scope/effort, not a quality score.

| File | Lines | Runtime role / verified reachability |
|---|---:|---|
| `services/dwh_scenario_agent/web/api/main.py` | 1,514 | FastAPI routes, startup/lifespan, session, chat, feedback; entrypoint. |
| `services/dwh_scenario_agent/web/services/agent.py` | 2,942 | Graph, eight tools, retrieval, Oracle queries, generation/critique, memory. |
| `services/dwh_scenario_agent/web/services/scenario_contract.py` | 161 | Service A intent parsing/validation and scenario handoff contract; imported by API. |
| `services/dwh_scenario_agent/web/services/settings.py` | 250 | B-specific settings/provider configuration; imported by runtime modules. |
| `services/dwh_scenario_agent/web/services/settings_graph.py` | 71 | Neo4j settings; imported by graph client. |
| `services/dwh_scenario_agent/web/services/logging_config.py` | 97 | Logging configuration; imported by entrypoint. |
| `services/dwh_scenario_agent/web/services/oracle.py` | 204 | Oracle pool/query helper; imported by agent. |
| `services/dwh_scenario_agent/web/services/embeddings_provider.py` | 835 | Local/OpenAI/Cohere embedding implementations and manager. |
| `services/dwh_scenario_agent/web/services/graph_client.py` | 335 | Neo4j driver/query wrapper; imported by schema tool. |
| `services/dwh_scenario_agent/web/services/llm_init.py` | 84 | Lazy provider clients; imported by memory/shared helper. |
| `services/dwh_scenario_agent/web/services/execution_log.py` | 166 | Local execution logging; imported by agent. |
| `services/dwh_scenario_agent/web/services/utils.py` | 25 | Small utility module; runtime-imported. |
| `services/dwh_scenario_agent/web/services/tools_base.py` | 168 | Tool base/helpers; runtime-imported. |
| `services/dwh_scenario_agent/web/services/excel_exporter.py` | 338 | `cleanup_old_exports` called by startup; export class itself not otherwise reached from reviewed API path. |
| `shared/services/intent_library.py` | 683 | Optional generic-intent classifier import; imported, but `classify_intent` returns `None` before model call. |

Three additional text files are loaded by runtime: `web/services/prompts/data_agent_system.md` (58 lines), `web/services/prompts/critique_agent_system.md` (49), and `config/dwh_domain_control_plane.md` (68). Together with Python closure: 8,048 lines. These counts exclude the 11 maintenance scripts, tests, datasets, model artifacts, and ignored/generated files.

**INFERRED.** A likely migration surface is larger than just 8,048 lines because behavior is encoded in requirements, database metadata, external Chroma/Neo4j state, generated golden data, model downloads, environment configuration, and operational knowledge not represented by the runtime import graph. Verify those before scoping an implementation estimate.

### Data flow by feature

| Feature | End-to-end flow (VERIFIED unless tagged otherwise) | Main quality/security boundary |
|---|---|---|
| Scenario SQL generation | Service A intent JSON/raw question → `scenario_contract.parse_service_a_intent` → system prompt/domain control plane + conversation → graph tool loop → retrieval/tools/Oracle execution → critique node → explicit approval → SSE final SQL. | B does not deterministically enforce all A-resolved filters; generated SQL is executed against B’s configured Oracle connection before approval. Critic sees SQL and result metadata/preview. |
| Generic DWH chat | User message → same graph; optional `shared.services.intent_library` call for generic intent classification, but classifier currently returns `None` → same tools and generator. | Generic path shares all tool permissions and model loop; no separate role/tool allowlist. |
| Schema table retrieval | Query → Chroma `schema_tables`, up to 400 candidates → dedupe and optional Neo4j table/PageRank/join-count score → 50/50 semantic/graph score → top 8 context. If Neo4j fails, semantic score remains but is halved. | Candidate count/score weights are hardcoded; graph fallback is not health/readiness surfaced. |
| Column retrieval | Query → Chroma `schema_columns` top 20, optionally retry qualified-table query top 20 → dedupe/standard-column boost → cap 10. Some query paths additionally fetch Oracle allowlist/flags. | No index-version/freshness check; result quality depends on embedding/index generation parity. |
| Experience retrieval | User ask → Chroma `experience_templates` top 3, threshold 0.6 → question/SQL examples in model context. | `/feedback` targets `experience_pairs`; retrieval/build target `experience_templates`. |
| Candidate SQL execution | Tool checks leading SQL token (`SELECT` or `WITH` per tool code), then calls `oracle.run_readonly` and fetches at most 10,000 rows; returns columns, capped row count, and preview. | Oracle helper only accepts strings beginning `SELECT`, so accepted `WITH` SQL is rejected. Read-only is enforced by string prefix, not parsed SQL/read-only transaction policy. |
| Business-code resolution | Query/schema context → dictionary lookup and Oracle metadata/value queries with country/institution scope → candidate values/descriptions returned to model. | Multiple sequential queries per tool; results may contain sensitive/business data; no query budget per request. |
| Feedback | Request includes question, generated SQL, optionally corrected SQL → append JSONL golden record → vector embedding attempt and add to `experience_pairs`. | The file append may succeed before vector insertion fails; JSONL can contain sensitive question/SQL. |
| Session/checkpoint | Conversation `thread_id` → SQLite LangGraph checkpointer at relative `checkpoints.sqlite`; session metadata also stored in SQLite. | Different launch cwd can create split state; SQLite operations are local and not coordinated across replicas. |

## 3. Dependencies, resources, configuration, and scripts

### External dependencies and configured resources

**VERIFIED.** B’s scenario requirements are a separate dependency set; the 45 shared-package version deltas noted above make direct merge unsafe. The runtime uses FastAPI/Starlette, LangChain/LangGraph/LangSmith, Pydantic settings, OpenAI-compatible clients, Oracle (`oracledb`), ChromaDB, sentence-transformers/transformers/Torch, Neo4j driver, SQLite, and export support. Optional provider integrations include Cohere, SambaNova, OpenRouter, LM Studio-compatible endpoints. Script-only dependencies include Unsloth, Transformers, TRL, Datasets, PEFT/quantization stack, and direct OpenAI client.

**VERIFIED.** LLM provider setting supports Cohere, OpenAI, LM Studio, SambaNova, OpenRouter (`settings.py`). The generator constructs a LangChain chat model per turn and binds the eight tools. OpenAI and LM Studio have explicit paths; Cohere uses `ChatCohere`; SambaNova/OpenRouter use OpenAI-compatible endpoints. OpenAI, Cohere, SambaNova generation have no explicit code-level timeout/retry; OpenRouter has explicit 60-second timeout and two retries; LM Studio has a local base URL. OpenAI generation does not pass the configured output-token setting; LM Studio uses `LLM_MAX_TOKENS` as output max. Critic construction only correctly handles OpenAI and LM Studio; other configured providers fall through the OpenAI path. Critique’s fast provider may be unset rather than falling back. Verify actual installed provider extras/requirements before enabling each provider.

**VERIFIED.** `llm_init.get_llm_client()` returns a `(client, model)` tuple; it creates provider-specific clients lazily. `manage_memory` assigns this tuple to `summarizer_llm` then calls `.invoke(...)`; that raises `AttributeError`, is caught, and produces a fixed fallback summary. The summary trigger uses an approximate `len(str(messages))/4` token count against `LLM_MAX_TOKENS`; this also conflates a generation output limit and context budget. This prevents the intended conversation compaction from working.

**VERIFIED.** Embeddings have four modes: local BAAI BGE large, local finance mode, OpenAI `text-embedding-3-small`, and Cohere `embed-english-v3.0`. `EmbeddingProviderManager.get_provider()` calls provider construction without passing the configured API key; OpenAI/Cohere constructors require a key and raise. Remote embedding configuration is therefore broken at this manager boundary unless a different code path injects credentials (not found). Local model/query cache keys include provider/model/device, but the warm-up path uses a device argument while the manager path omits it, allowing a second local model instance. Search query results are cached by exact string; in-memory caches do not show bounded cross-process/index version semantics.

**VERIFIED.** Chroma resources: `schema_tables` and `schema_columns` are built by `build_hierarchical_index.py`; `experience_templates` by `build_experience_index.py`. Table/column glossary source is Oracle `PIO_BANKBI_DICTIONARY_TABLES`, `PIO_BANKBI_DICTIONARY_COL` with common-project filtering. Experience source is `data/processed/golden_dataset_templates.jsonl`. `CHROMA_PATH` plus provider-specific local path mapping controls schema indexes; experience DB path is separately rooted under `artifacts/chroma_db_experience`. No freshness manifest/model fingerprint/atomic collection alias was found. Missing/corrupt collections are caught as tool errors; startup can still succeed.

**VERIFIED.** Neo4j is an optional reranker called from `advanced_schema_search`. `settings_graph.py` has a source literal default password `password`; replace with required secret configuration before any deployment. Driver config has connection timeout 30 seconds and pool lifetime/size settings, but query calls have no explicit query/transaction timeout. `GraphClient` creates/closes a driver around each search use; it is not lifecycle-managed by FastAPI. Graph builder uses nodes `Table{name,module,...}` and `JOINS_WITH` relationships; some unused GraphClient helpers query legacy `REFERENCES`/`owner`/`schemaGraph` shapes. The actual table search uses `module`, `name`, PageRank and join count.

**VERIFIED.** Oracle objects read by runtime include dictionary and code metadata (e.g. `PIO_BANKBI_DICTIONARY_TABLES`, `PIO_BANKBI_DICTIONARY_COL`, `PIO_BANKBI_DICT_JOIN_TABLES`, lookup/code tables selected through metadata), plus executing candidate SQL against the configured DWH schema. The B enrichment script writes `PIO_BANKBI_DICTIONARY_TABLES.DES_ENG` and `PIO_BANKBI_DICTIONARY_COL.COLUMN_DESCRIPTION_ENG`. Table and column dictionary objects may also be consumed by existing services: treat those updates as shared legacy-table writes. Runtime candidate SQL may read arbitrary objects available to the Oracle account; exact effective grants and data sensitivity are **UNKNOWN** without DBA review. No Oracle writes were found in normal runtime scenario tools; feedback writes local files/Chroma. Index scripts write Chroma/Neo4j, not Oracle, except explicit enrichment scripts.

**VERIFIED.** Config key names include Oracle credentials/DSN, OpenAI/Cohere/SambaNova/OpenRouter keys, LLM provider/model/fast model, LM Studio URL, embedding provider/model/device, Chroma path, Neo4j URI/user/password, context/token and pool settings, and export/session paths. No values are quoted here. Read `.env` key names only if needed; do not copy value-bearing lines into the report or logs. `LANGCHAIN_*` tracing settings must be forwarded through A’s shared runtime config; B does not define A’s tracing contract.

### LLM call sites and data leaving the process

| Call site | Provider/call | Data submitted (VERIFIED from code) | Concern |
|---|---|---|---|
| `agent.call_llm_reasoner` | Per-turn LangChain generation using selected provider; tool binding. | Raw user question or A intent JSON, scenario name/type/detection logic, codes/thresholds/date windows, system/domain prompt, prior messages, retrieved glossary/join/business-code context, and previous tool results/previews. | May transmit PII, customer samples, SQL, schema/business metadata to external provider. No explicit generation timeout/retry for several providers. |
| `agent.call_critique_llm_node` | Separate fast model/critic. | User request/intent, candidate SQL, execution success/row count/columns/error, first result rows, joins/context. | Provider mismatch breaks non-OpenAI critic settings; result preview can expose customer values. |
| `agent.manage_memory` | Intended summary call; currently errors before LLM because tuple `.invoke`. | If fixed: a slice of conversation messages, potentially SQL/results. Current source catches error and emits fixed summary. | Characterize intended behavior before changing; ensure redaction and correct client contract. |
| Embedding provider | Local or remote embedding calls. | Schema/column/experience strings and user search queries. | Remote provider path currently lacks key injection; external transfer requires data approval. |
| `scripts/enrich_glossary_metadata.py` | Direct OpenAI async/sync structured-output calls, hardcoded model in universal path. | Column name, existing descriptions, object name, table/project context, static AML prompt context. | No explicit client timeout; broad SQL update may impact rows beyond prompted table. |
| `scripts/enrich_table_descriptions.py` | OpenAI-compatible structured output using configured model/base URL. | Table name, project code, AML retrieval profile context. | One call per candidate table; then persistent dictionary update. |
| `scripts/templatize_golden_dataset.py` | OpenAI `gpt-4o`, JSON response format. | Raw user question and raw/golden SQL. | No explicit timeout/retry; potentially sensitive data leaves environment. |
| Shared intent library | Contains an LLM classification path, but `classify_intent` returns `None` first. | No LLM request from this function as currently reachable. | Dead intended path, not a functioning dependency. |

**UNKNOWN.** Whether user/customer data is approved for each provider, what Oracle grants permit, and whether traffic is logged by external vendors require security/data-owner confirmation. Inspect provider contracts, data classification, retention, and Oracle grant sets before enabling external embeddings/LLM calls with production data.

### Owner-run scripts and assets (not API runtime closure)

Scripts are classified individually below. “No Oracle write” means no write found in source; local artifacts/Chroma/Neo4j may still be modified. None were executed during this review.

| Script | Purpose / inputs / outputs | DB, LLM, mutation and quality notes |
|---|---|---|
| `scripts/harvest_all.py` | Extract dictionary rows to `data/raw` JSON and timestamped `data/processed/harvest` JSONL; `--limit`. | Reads Oracle `PIO_BANKBI_DICTIONARY_COL`; no LLM/Oracle write. Loads result sets in memory, no resume/manifest; pool close path should be exception-safe. **REFACTOR** before operationalizing. |
| `scripts/build_hierarchical_index.py` | Build table/column schema collections from common-project dictionary tables. | Oracle reads `PIO_BANKBI_DICTIONARY_TABLES` and `PIO_BANKBI_DICTIONARY_COL`; drops/recreates Chroma collections; batches embedding after `fetchall`; no dry-run/resume or atomic swap. **REFACTOR**, preserve both collections. |
| `scripts/build_experience_index.py` | Build experience Chroma collection from `golden_dataset_templates.jsonl`. | No Oracle writes/LLM text generation; embedding calls; drops/recreates `experience_templates`. Parser splits blank-line blocks despite `.jsonl` naming. **REFACTOR**, retain feature and validate canonical input format. |
| `scripts/build_graph_from_joins.py` | Build Neo4j table/relationship projection and PageRank from join metadata. | Oracle reads `PIO_BANKBI_DICT_JOIN_TABLES` and dictionary table metadata; deletes/recreates module nodes/relationships; computes GDS PageRank; no dry-run/atomic swap. **REFACTOR**, preserve Neo4j capability behind optional reranker. |
| `scripts/enrich_table_descriptions.py` | Enrich AML `DES_ENG` descriptions into structured table summaries/keywords/questions. | Reads then updates `PIO_BANKBI_DICTIONARY_TABLES.DES_ENG`; calls configured OpenAI-compatible model; has `--dry-run`, batch/limit but no reviewed diff and partial commits. **REFACTOR, high-risk write**; require scoped preview/approval and rollback snapshot. |
| `scripts/enrich_glossary_metadata.py` | Generate universal and table-contextual column descriptions. | Reads dictionary table/column rows; LLM calls; updates `PIO_BANKBI_DICTIONARY_COL.COLUMN_DESCRIPTION_ENG` using `WHERE COLUMN_NAME=:c_name` without table/project scope (also force path). `--dry-run` logs counts rather than exact SQL diff; concurrency/rate limiting exists but no explicit timeout. **REFACTOR, highest-risk write** due broad update predicate/shared table. |
| `scripts/templatize_golden_dataset.py` | Convert raw golden question/SQL examples into reusable templates in `golden_dataset_templates.jsonl`. | Direct OpenAI call; incremental read/rewrite; no Oracle write; catches per-record errors but no explicit timeout/retry, non-deterministic IDs. **REFACTOR**, keep only with consented/redacted corpus and evaluation. |
| `scripts/prepare_training_data.py` | Split golden records into seeded 90/10 ChatML train/validation JSONL. | Local read/write, no DB/LLM; overwrites outputs deterministically for same input/seed. PII may persist in training files. **REFACTOR** if model training is retained; otherwise DROP from runtime, not product capability. |
| `scripts/evaluate_rewards.py` | Evaluate a fixed SQL-pair reward/structure suite. | Pure local/static scoring; no DB/LLM; no measured connection to request quality or golden set. **REFACTOR/RETAIN as offline experiment** only if wired to a documented evaluation gate. |
| `scripts/train_model.py` | LoRA SFT of Mistral 7B 4-bit via Unsloth; consumes train JSONL, writes adapter directory. | Downloads/loads model dependencies and may require GPU; local writes under `models`; no Oracle/LLM API call in source, but model weights may be fetched remotely. Hard-coded model/hyperparameters, no validation evaluation gate. **DROP from service runtime; retain as optional offline MLOps only if owned.** |
| `scripts/train_dpo.py` | DPO LoRA from corrected/generated SQL pairs in golden dataset; writes policy adapter. | Local training/model download; no Oracle/API in code; assumes golden records have `generated_sql`, `golden_sql`, `user_question`; current feedback path must be checked to ensure it actually populates all fields. **DROP from service runtime; retain only with data governance and an evaluation/release workflow.** |

**VERIFIED.** `config/dwh_domain_control_plane.md` is runtime prompt/config. `config/aml_scenario_facts.json` was not tracked/referenced by the reviewed runtime; `golden_dataset_templates.jsonl` is a script input, not startup/request-path resource. `rag_adapter.py` is not imported from the actual application and imports sibling `prompt_composer`/`retriever` modules absent from the scenario package; classify as unused/unusable in this service snapshot, not as an active replacement architecture. Output directories/model files, ignored files, and deployed Chroma/Neo4j contents are **UNKNOWN** from tracked-source review.

## 4. Module and folder classification

Labels answer whether the component belongs in the target product, not whether it should be copied verbatim. “Evidence” gives paths and line references where practical; a module is marked used only where static import/call flow establishes reachability. For the full source review, “searched” means source/import references were checked, not runtime coverage.

| module | purpose | used by | label | evidence | notes |
|---|---|---|---|---|---|
| `web/api/main.py` | FastAPI lifecycle, chat/session/feedback/report API | `uvicorn web.api.main:app` from run.ps1 | REFACTOR | `main.py:~150–260` initialization/lifespan; `~700–780` graph chat; `~1460–1475` feedback Chroma | Move endpoints into A routes; remove import-time side effects; correct feedback/index contract; readiness should report actual dependencies. |
| `web/services/agent.py` | Graph, tools, LLM, retrieval, Oracle orchestration | API graph/checkpointer | REFACTOR | `agent.py:278–282`, `505`, `711`, `2021–2059`, `2248–2327`, `2687`, `2936–2937` | 2,942 LOC monolith; split typed graph, tools, retrieval, execution, critique. Preserve graph/Chroma/Neo4j features and bound calls. |
| `web/services/scenario_contract.py` | Validate/parse Service A intent handoff | `main.py` streaming path | REPLACE-WITH-A | `scenario_contract.py:~1–135`; A `services/aml_builder/web/services/schemas.py` AMLIntent | Replace with explicit shared A contract adapter; identify unknown/additional field semantics before eliminating B model. |
| `web/services/settings.py` | B runtime settings and providers | API, agent, embeddings, Oracle | REPLACE-WITH-A | `settings.py:~1–218`; A `services/aml_builder/web/services/settings.py` | Merge non-duplicate DWH fields into A settings; do not retain two settings/env namespaces. |
| `web/services/settings_graph.py` | Neo4j settings | `graph_client.py` | REFACTOR | `settings_graph.py:~1–59` | Fold into A settings; require secret; preserve optional graph flag and timeout. |
| `web/services/logging_config.py` | B logging setup | API startup | REPLACE-WITH-A | imported from `main.py`; A app logging/config modules in `PROJECT_OVERVIEW.md` | Use A logging and LangSmith tracing propagation. |
| `web/services/oracle.py` | B Oracle pool and SQL helper | B agent tools | REPLACE-WITH-A | `oracle.py:~1–161`, especially `152`, `187`; A `web/services/oracle.py` | Use A pool/read-only adapter; add approved B read methods behind repository interface; fix SELECT/WITH conflict as characterization case. |
| `web/services/embeddings_provider.py` | B local/remote embedding implementations/cache | Chroma search/index tools and scripts | REFACTOR | `embeddings_provider.py:273–395`, `623–692`, `804+` | Keep all providers and local finance model; inject credentials; timeout, concurrency, version and cache lifecycle; eliminate duplicate model warm-up. |
| `web/services/graph_client.py` | Neo4j driver and reranking queries | `advanced_schema_search` | REFACTOR | `agent.py:505+`; `graph_client.py:~1–282`; builder `build_graph_from_joins.py` | Keep Neo4j; adapt driver lifecycle, query timeout, result bounds, schema migration, fallback metrics. |
| `web/services/llm_init.py` | Lazy provider client builder | memory/shared classifier | REFACTOR | `llm_init.py:~1–66`; `agent.py:2248–2307` | Consolidate with A LLM factory; fix tuple/client contract and provider settings; tracing and token/timeouts. |
| `web/services/execution_log.py` | Local execution log | agent import/tool flow | REFACTOR | imported from `agent.py`; module initialization on import | Define structured, redacted, bounded logs; no import-time file writes. |
| `web/services/utils.py` | Small helpers | agent/tools | REPLACE-WITH-A | `utils.py:~1–17`; A shared utilities | Reuse A only after behavior comparison; avoid importing arbitrary helper bundle. |
| `web/services/tools_base.py` | Tool scaffolding/helpers | agent tools | REFACTOR | agent imports; `tools_base.py:~1–131` | Migrate only contracts actually used; use typed A service tools/result/error conventions. |
| `web/services/excel_exporter.py` | Excel export and old-export cleanup | startup cleanup; export path if enabled | REFACTOR | `main.py:162–164`; module `excel_exporter.py:1–265` | Only cleanup is confirmed startup use; audit actual export route reachability. Remove startup deletion and use A artifact/export policy. |
| `shared/services/intent_library.py` | Generic intent classifier | optional generic path | DROP | import in `agent.py`; `intent_library.py:~565` returns `None` before model logic | Dead behavior; AML scenario bypasses. Do not import B `shared` package into A. |
| `prompts/data_agent_system.md` | Generator prompt | `call_llm_reasoner` | REFACTOR | `agent.py:~2327+` prompt loader/use; 48-line file | Preserve useful SQL/schema rules as versioned prompt content; split system policy from feature content and add tests. |
| `prompts/critique_agent_system.md` | SQL critic prompt | critique node | REFACTOR | `agent.py:2687+`; 40-line file | Keep approval/rejection constraints but do not treat model critique as security enforcement. |
| `config/dwh_domain_control_plane.md` | DWH domain context | generator prompt composition | REFACTOR | loaded by agent; 49 lines | Preserve knowledge, establish owner/source/version and avoid duplicating A policy. |
| `config/aml_scenario_facts.json` | Apparent facts/config artifact | no reference found | DROP | not tracked by `git ls-files` and no runtime reference found | Generated/untracked status and external use unknown; verify deployment artifact owners before deleting upstream. |
| `scripts/harvest_all.py` + `harvestor.py` | Oracle dictionary extract | manually run maintenance | REFACTOR | script call path to `harvestor.py`; no API import | Keep extraction workflow as a controlled CLI; stream/batch and manifest. |
| `scripts/build_hierarchical_index.py` | Chroma table/column indexing | manual maintenance | REFACTOR | script DDL/build path; agent collection names | Keep both schema collections; build versioned temp index and atomically promote. |
| `scripts/build_experience_index.py` | Experience-template Chroma index | manual maintenance | REFACTOR | script collection `experience_templates`; agent search `agent.py:2058–2059` | Keep; correct JSONL parser and feedback collection name. |
| `scripts/build_graph_from_joins.py` | Neo4j graph build/PageRank | manual maintenance | REFACTOR | script: `main` at 268; GraphClient search from agent | Keep graph feature; version and atomically promote; destructive replacement currently. |
| `scripts/enrich_table_descriptions.py` | Oracle dictionary writes + LLM enrichment | manual, not request path | REFACTOR | script LLM and `UPDATE PIO_BANKBI_DICTIONARY_TABLES` | Shared metadata table; preview exact diff, table key scoping, approval/rollback. |
| `scripts/enrich_glossary_metadata.py` | Oracle column dictionary writes + LLM | manual, not request path | REFACTOR | script update `PIO_BANKBI_DICTIONARY_COL` by `COLUMN_NAME` | Highest-risk script: update predicate not scoped by table/project. Do not port until corrected and owner-approved. |
| `scripts/templatize_golden_dataset.py` | LLM-generated experience templates | manual, not request path | REFACTOR | script reads golden dataset, writes template JSONL | Keep optional corpus-preparation feature; sensitive input approval, timeout, deterministic output. |
| `scripts/prepare_training_data.py` | SFT data preparation | manual ML workflow | DROP (from service package) | standalone script, no app import | Only retain in a separately owned offline training repo/workflow if still used. |
| `scripts/evaluate_rewards.py` | Experimental static reward scoring | manual | DROP (unless adopted as evaluation gate) | no runtime import; no database/model call | Replace with golden-set semantic/SQL execution evaluation if training remains. |
| `scripts/train_model.py` | Local Unsloth SFT model training | manual GPU workflow | DROP (from service package) | `MODEL_NAME`, training/output constants; no app import | Move to MLOps/training project only if model lifecycle is approved. |
| `scripts/train_dpo.py` | Local DPO policy training | manual GPU workflow | DROP (from service package) | reads generated/corrected SQL pairs, no app import | Feedback data completeness and evaluation/release process are unverified. |
| `rag_adapter.py`, `prompt_composer.py`, `retriever.py` | Alternative RAG composition path | no API import; sibling modules absent in scenario package | DROP | import/reference search in scenario service | May be abandoned experiment; check git history/external entrypoints before deleting source B. |
| `celery_worker.py` / `/jobs` status code | Possible async job path | no app producer/import found; route polls report files | DROP (until producer proven) | app import/reference search; route/status source | No verified queue or enqueue flow. |
| `tests/` | Test code | manually invoked only | REFACTOR | test tree inspected; app source not importing tests | Add hermetic contract/graph/retrieval tests; existing tests may require live service/Oracle and must not run in this review. |
| `data/`, `artifacts/`, `models/`, `static/` | Corpora/indexes/checkpoints/exports/model outputs | script/runtime files may read/write | UNKNOWN | tracked source references; ignored/generated data not inspected for secrets/content | Inventory with owner in controlled environment; do not copy blindly. |

## 5. Overlap, conflicts, and high-risk writes

### Duplicated capabilities and conflicts

| Concern | A | B | Integration decision |
|---|---|---|---|
| API/framework and route namespace | FastAPI routes split by domain | One large FastAPI module with chat/session/feedback/report routes | A owns the app/routes; migrate behavior into A route/service modules, not B app initialization. |
| Graph/agent orchestration | A LangGraph tool-driven AML scenario workflow | B separate LangGraph graph with SQL retrieval/execution/critique | Keep B’s DWH graph behavior as services/tools or an explicitly bounded subgraph; don’t create a second app or duplicate intent orchestration. |
| Settings/secrets | One Pydantic settings module | B settings + graph settings + dotenv behavior | Consolidate into A settings; preserve provider fields and LangSmith `LANGCHAIN_*`; remove unsafe secret defaults. |
| Oracle | A pooled adapter with read/write/atomic/shadow pools | B’s own pool/helper | A owns lifecycle; route all B reads through A adapter and explicitly separate any reviewed dictionary writes from runtime. |
| LLM clients | A shared `build_llm` / safe parse | B per-turn constructors, direct clients, tuple-returning helper, mixed timeout behavior | One provider factory; all model calls have bounded timeout/retry/token policy and tracing; test each advertised provider path. |
| Chroma | A explanation-code search | B table/column and experience collections | Not equivalent: retain B schema/experience indexes behind retrieval adapter; align clients/settings/index versions. |
| Embeddings | A direct OpenAI embeddings in explanation search | B local BGE, finance, OpenAI, Cohere | Keep B provider range as requested, but unify credential, dimension, model fingerprint and privacy handling. |
| Domain contracts | A typed AMLIntent with resolved/confirmed filters | B permissive ServiceAIntent with extra fields and shallow checks | A contract is source of truth; explicit adapter maps fields and rejects unknown/incomplete required semantics. |
| Logging/tracing | A structured logging and LangSmith settings | B logging config/local execution log | A logger/tracing propagation; redact SQL parameters/results and sensitive rows. |
| SQLite/session | A checkpoint and sidebar session store | B relative `checkpoints.sqlite` tables/checkpointer | Decide whether B sessions are retained/migrated; use A lifecycle/path policy, no implicit shared filename. |
| Ports/routes | A 8005, `/chat/stream`, `/engine/*`, scenario deploy | B 8006, `/chat/stream` plus session/report/feedback routes | B app’s `/chat/stream` collides at HTTP path if merged. Keep one A route and dispatch by typed operation, or provide a distinct versioned subroute. |
| Oracle dictionary names | A AML scenario tables and explanation codes; B DWH dictionary objects | Both may use shared production Oracle schemas; B enrich scripts update dictionary descriptions | Verify ownership, grants, update triggers/consumers and rollback with DBA/data owner before enabling writes. |
| Dependencies | A newer pinned shared versions | B older pins plus optional providers/ML stack | Merge only actual runtime deps after compatibility checks; keep GPU training extras out of API installation. |

**VERIFIED.** B table/column metadata tables are shared-looking dictionary objects and B scripts update them. Exact schema ownership, other consumers and production impact are **UNKNOWN**. Treat both enrichment scripts as production-impacting writes until the DBA confirms otherwise. The glossary column update by name may change multiple table rows, so it is especially high risk.

**VERIFIED.** B’s feedback flow appends user question/generated/corrected SQL to local JSONL and attempts vector insertion. B’s generated SQL executor reads from the configured Oracle connection and returns up to 10k rows. The exact data contained in those results and current provider egress controls are unknown. Treat prompts, tool output previews, SQL, and golden feedback as potentially sensitive/PII-bearing.

## 6. Concrete “barely works” causes and resilience audit

The following are source-supported failure mechanisms; without executing against the configured environment, their incident frequency is **UNKNOWN**.

| Priority | Verified issue | Expected symptom / impact |
|---:|---|---|
| 1 | SQL tool admits `WITH`, but `oracle.run_readonly` rejects anything not starting `SELECT` (`agent.py:711+`, `oracle.py:152/187`). | CTE-based generated SQL fails at execution despite being accepted by tool guard/prompt. |
| 2 | Feedback writes `experience_pairs`; search and builder use `experience_templates` (`main.py:1467`, `agent.py:2058–2059`, builder collection). | Feedback can append to JSONL, then fail vector update; new correction is not retrievable. |
| 3 | Embedding manager omits API key while remote providers require one (`embeddings_provider.py:623–692`, manager at `804+`). | OpenAI/Cohere embedding choice fails at provider construction; schema/experience retrieval or startup warmup errors. |
| 4 | Memory compactor calls `.invoke` on `(client, model)` tuple returned by `get_llm_client` (`agent.py:2248–2307`, `llm_init.py`). | Compaction exception is swallowed; fallback loses prior conversation context when threshold is reached. |
| 5 | Critic handles OpenAI/LM Studio but routes other configured providers through OpenAI; fast provider may be unset (`agent.py:2687–2745`). | Cohere/SambaNova/OpenRouter generator may work while approval critic fails or uses wrong credentials/model. |
| 6 | Startup continues after Oracle/embedding/Chroma errors and health is not dependency-aware; no Neo4j readiness check. | App may look alive while required-first retrieval/database features fail per request. |
| 7 | Startup warmup and later embedding manager use different device/cache key paths. | Large BGE local model can be loaded twice, increasing startup time and RAM/GPU pressure. |
| 8 | Chroma rebuild drops active collection before successful replacement; Neo4j rebuild deletes current module graph. | Interrupted build leaves no/partial production index/graph; no atomic rollback. |
| 9 | Generator/critic/tool paths synchronous and several provider calls/query calls lack explicit timeouts/retries. | Event-loop blocking, long-tail request latency, hung requests, provider transient failures; concurrency impact inferred. |
| 10 | Tool loop permits up to 25 tool calls, each may execute multiple sequential Oracle/Chroma/Neo4j operations; no aggregate SQL/query budget. | Worst-case database/retrieval load per request can grow substantially. **INFERRED:** rough code upper bound is around 150 Oracle statements if 25 calls each hit the most query-heavy business-code path; actual tool-call mixes and retries are not measured. |
| 11 | Candidate query fetches up to 10,000 rows into memory; count is only rows fetched, not database total. | High memory/latency and misleading `row_count`; export/result evidence truncated. |
| 12 | SQL safety uses leading-token prefix rather than parsed SQL/read-only session/allowlist. | Comments, multiple statements, nested writes, dialect edge cases may evade simplistic policy; exact Oracle driver behavior unknown. Use SQLGlot/A-owned query guard and read-only credentials/transaction. |
| 13 | Feedback and `execution_log` can persist raw question/SQL/result metadata locally; response critic receives preview rows. | PII/secrets may enter file logs, local JSONL, prompt history, or external LLM calls. Retention/access controls unknown. |
| 14 | Scenario contract’s `extra=allow`, `ready_for_handoff=True` default, and code/date checks are not equivalent to A’s confirmed resolved-filter enforcement. | Missing or unknown intent fields can pass boundary; filter fidelity depends on prompt/model/critic. |
| 15 | CUS_NUM is checked in projection and EXPL_CODE literal checks use regex; no SQL AST comparison to confirmed filters. | Aliasing, expressions, alternate predicate forms, negation, or unsupported date windows may escape/false-fail checks. |
| 16 | `MAX_SUPERVISOR_ITERATIONS` unused; model says use experience first but graph does not enforce ordering. | Claimed workflow controls are weaker than names/prompts suggest. |
| 17 | Relative SQLite path and module-global state; Oracle/Chroma state can vary by cwd/process. | Multiple replicas/worktrees can split state or race; lifecycle issue is inferred from local file semantics. |
| 18 | Image helper refers to a global `describe_image` that is not defined in this module; generic image path inserts a placeholder. | Image input is not meaningfully analyzed. AML scenario path does not require images, so this is not core migration scope. |
| 19 | Business-code/metadata tool composes several Oracle calls serially; wildcard/metadata fallbacks add queries. | Latency and database load hot spot. Add query metrics, per-request budget, and bounded caching only after freshness/tenant semantics defined. |
| 20 | No adequate golden-set evaluation gate is present in request workflow; reward script is a small static experiment. | Prompt/index/provider changes can regress SQL safety/accuracy undetected. |

### SQL/model call budget and graph/index quality

**INFERRED, not measured.** A typical scenario may take roughly 5–8 generator turns, one critic, and 4–7 tool invocations; model/tool behavior depends on prompt and input. The code cap is 25 tool invocations plus up to four critique decisions; API recursion limit is 75 graph steps. A tool call can perform multiple internal DB/retrieval operations. The query-heavy business-code lookup can issue roughly 4–6 Oracle statements from the reviewed fallback/metadata path; a theoretical upper envelope of about 150 Oracle statements for 25 such calls is a conservative multiplication, not an observed rate. Add telemetry and hard request-level query/call/time budgets rather than relying on this estimate.

**VERIFIED / UNKNOWN.** Chroma search uses hardcoded collection names, top-k, score threshold, and boost/rerank values; no index generation manifest or golden-set score was found. Neo4j reranking combines semantic and graph score 50/50 and can fall back, but no measured before/after retrieval quality was found. Preserve both stores as required, while making them separately observable and validating retrieval quality against a fixed gold set before changing embedding model, index schema, or rerank weights.

## 7. Integration design

### Proposed A tree

```text
services/aml_builder/web/
  api/
    routes/
      dwh_scenarios.py             # A-owned versioned route; no second FastAPI app
  domain/
    dwh_scenario/
      contracts.py                 # typed DWH request/result and explicit AMLIntent adapter
      sql_policy.py                # deterministic SQL/filter/result safety
  services/
    dwh_scenario/
      graph.py                     # bounded LangGraph orchestration/subgraph
      generator.py                 # shared A LLM factory + DWH tools
      critique.py                  # typed critique result, not security authority
      schema_retrieval.py          # table/column Chroma retrieval and reranking
      experience_retrieval.py      # experience Chroma search/feedback
      business_codes.py            # bounded lookup/code resolution
      joins.py                     # join search + optional Neo4j rerank
      index_contracts.py           # index versions/fingerprints/health
    adapters/
      chroma/                       # injected client/config, retained collections
      neo4j/                        # optional graph client with query timeout
      oracle/                       # existing A adapter/repositories only
    llm_client.py                  # sole provider factory; preserve LANGCHAIN_*
    settings.py                    # sole settings; DWH additions, secret required
scripts/
  dwh_scenario/
    harvest_dictionary.py          # read-only, bounded snapshot export
    build_schema_index.py           # temp Chroma build + validate + promote
    build_experience_index.py       # validated corpus -> temp collection + promote
    build_join_graph.py             # temp/versioned Neo4j projection + promote
    enrich_dictionary.py            # preview/approval-gated writes, separate command
tests/
  characterization/dwh_scenario/  # legacy behavior fixtures/golden corpus
  unit/dwh_scenario/               # policies, providers, adapters, graph routing
  integration/dwh_scenario/        # hermetic fakes/local stores; opt-in external checks
```

### Contracts and consolidation

1. **Boundary:** accept A `AMLIntent` plus DWH operation context; explicitly map scenario name/type/detection logic, threshold/code/date semantics, and confirmed `resolved_filters`. Reject a request whose required confirmed filters are unresolved. Return typed `ScenarioSqlResult` with candidate SQL, validation findings, execution evidence metadata, approval state and trace identifiers; do not return arbitrary tool state.
2. **SQL safety:** tool output is untrusted. Parse Oracle SQL with A’s SQLGlot/policy layer; enforce SELECT-only AST, no multiple statements, object/schema allowlist, required filters from A intent, bounded fetch, bind variables, and read-only connection. Preserve B business-code and join tool capabilities but make all queries repository calls with per-operation timeout and request budget.
3. **Retrieval:** keep `schema_tables`, `schema_columns`, and `experience_templates`; keep local BGE/finance, OpenAI, and Cohere embedding providers. Inject API keys, model/dimension/index fingerprint, timeout/retry, bounded concurrency/cache, and provider data-classification policy. Check the index fingerprint at startup/readiness. Do not silently fall back across incompatible dimensions/models.
4. **Neo4j:** preserve join/PageRank graph reranking behind config. `advanced_schema_search` must return useful Chroma results if Neo4j is unavailable, expose degraded status/metrics, bound candidate input/output, use managed driver lifecycle and query timeout, and use a versioned schema shared with the graph builder. Remove default credentials.
5. **LLM:** single factory for generator/critic/summary and structured parsing. Every provider gets explicit connection/read timeout, retry policy, max output tokens, context budget, and tracing callbacks/config. Keep `LANGCHAIN_*` and LangSmith behavior from A. Test generator and critic selection independently for every supported provider. Apply prompt/data redaction policy and exclude raw result rows from critic unless explicitly justified.
6. **Settings/dependencies:** extend A’s Pydantic settings, use one dependency lock, add optional extras for Cohere/Neo4j/local embeddings only where needed. GPU training packages do not belong in API dependencies. Preserve versions currently owned by A; run compatibility review and focused tests after implementation (not as part of this read-only investigation).
7. **Operational scripts:** convert destructive builds into versioned temp-build/validate/promote flows; no index drop before replacement. Enrichment writes require exact diff, scoped PK predicates, explicit `--apply`, transaction limits, backup and rollback. Keep training scripts outside the service unless an ML owner confirms ownership.

### Incremental migration packages

All acceptance checks below are proposed future checks, not run in this investigation. Each package should be a small PR and should not activate the next package until its checks pass.

| Package | Scope / likely files | Acceptance checks | Rollback |
|---|---|---|---|
| **B0 — behavior inventory and golden baseline** | Add no production behavior; prepare sanitized input/output fixtures, current route/graph contract inventory, SQL corpus, provider/index manifest templates. | Captured expected handoff parsing, tool schemas, retrieval result fixtures, SQL guard outcomes, and approved/rejected graph cases; no live customer values; checks reproducible offline. | Remove fixtures/branch; source B untouched. |
| **B1 — typed contract adapter** | A `domain/dwh_scenario/contracts.py`, mapping layer from `AMLIntent`, unit tests. | Valid/invalid A intent fixtures; all resolved/confirmed filters survive; missing required filters reject; serialized response schema stable. | Disable route adapter; existing remote B HTTP seam remains. |
| **B2 — shared provider/config adapters** | A settings/LLM factory + embedding provider and Neo4j adapter seams; dependency lock changes. | Provider matrix tests for generator/critic/embedding selection; credentials injected without values in logs; explicit timeout/token/retry; LangSmith callbacks preserved; no external calls in unit tests. | Feature flag off; restore dependency lock/config. |
| **B3 — retrieval-only schema and experience** | Chroma table/column/experience repositories, index manifest, versioned index CLI; no Oracle SQL execution. | Golden retrieval recall/ranking against fixed dataset; feedback writes same collection; missing/stale index is readiness-degraded; atomic old/new index rollback demonstrated on disposable local store. | Point config to prior index version; leave B deployment untouched. |
| **B4 — Oracle read repositories and deterministic tools** | DWH metadata/code/join repositories through A Oracle adapter; typed tool wrappers. | Fake/captured Oracle fixtures; query count/time budget; SQLGlot policy rejects writes/multi-statement/unconfirmed filters; read-only adapter tests; CTE SELECT works. | Disable DWH tool registry/route, retain B service as external fallback. |
| **B5 — bounded graph orchestration + critic** | DWH graph/subgraph, prompt loader, explicit state and bounded retries/iterations. | Offline graph transition fixtures; provider matrix; critique cannot override deterministic SQL policy; event-loop and resource bounds; streaming/error contract stable. | Route flag returns to A’s existing external `PIOTECH_AI_URL` integration. |
| **B6 — route/session/feedback integration** | A-owned route/service, session policy, feedback contract; no B `main.py` import. | API contract tests; route collision resolved; idempotent feedback event; PII retention/redaction; no side effects on import. | Revert route flag to external adapter; preserve data. |
| **B7 — index/graph operations and optional dictionary enrichment** | Owner-run CLIs; versioned Chroma + Neo4j builds; separate reviewed write command. | Rebuild rollback; count/fingerprint checks; dry-run exact diff; scoped update tests; permission gate and auditable batch transaction. | Promote prior index/graph version; DB backup/reverse update plan. |
| **B8 — cutover and retirement** | Config/observability, gradual traffic, remove B remote dependency only after acceptance. | Shadow compare/golden SQL accuracy/safety/latency/cost; alert readiness and rollback verified; no unexplained production DWH writes. | Route traffic back to B endpoint; keep old indexes and adapter available for agreed period. |

**Recommended first package: B0.** Create a sanitized characterization/golden baseline before changing B code or A architecture. It has no production behavior or database side effects and gives objective evidence for every later package. First implementation after that should be B1 (typed intent adapter), but B0 is the safest first package.

### Characterization and golden-set evaluation plan

Before refactoring, preserve behavior at these seams with fixtures (sanitized, deterministic, no production calls):

- **Intent boundary:** accepted Service A intent payloads, malformed JSON, `ready_for_handoff` false/missing, unknown extras, empty/ambiguous filters, date/baseline examples; compare B behavior to A typed contract.
- **Graph routing:** one-turn success, tool loop, critique approval, critique rejection then repair, duplicate rejection stop, max tool limit, provider error, Oracle error, empty retrieval, optional Neo4j failure, missing collection, memory compaction threshold.
- **Retrieval:** table/column/experience query fixtures with expected IDs/ranks and metadata; exact collection names; provider/model/index version; no hidden live embeddings in unit tests.
- **Oracle/tool SQL:** SELECT, WITH, comments, semicolon/multiple statement, nested write, quoted identifiers, aliases, literals and binds; confirmed/unconfirmed EXPL_CODE, CUS_NUM, country/institution scope, rolling/baseline windows; cap and truncation semantics.
- **Provider matrix:** OpenAI, Cohere, LM Studio, SambaNova and OpenRouter generator/critic, plus local/OpenAI/Cohere embedding; replace network clients with fakes and assert model selection, timeout, retry and max token args.
- **Feedback:** append/idempotency, corrected SQL presence, JSONL parse/recovery, collection consistency, partial file/vector failure handling, redaction and data retention.
- **Golden dataset:** owner-approved, deidentified question → expected table/column retrieval, permitted SQL AST, required intent filters, execution outcome on a safe snapshot or deterministic fake, and critic decision. Report safety precision/recall, SQL exact/normalized match, execution pass, required-filter coverage, retrieval recall@k, latency/tool/DB call count, and provider cost. Use a holdout split by intent/template to prevent near-duplicate leakage; include adversarial negative cases (write SQL, missing filters, negated/contradictory filters, ambiguous dates, unsupported tables).

Do not use B’s current reward score alone as a release gate. Review golden examples for licensing, PII, and SQL correctness; never ship raw production row previews as fixtures.

## 8. Unknowns to resolve before implementation

1. **Corpus/index ownership:** Where are deployed Chroma DBs and Neo4j graph, what are their exact embedding/model/index versions, sizes, freshness cadence, and rebuild owner? Are ignored data/artifact directories needed for a reproducible migration?
2. **Data access/privacy:** Which Oracle schemas/grants are used, which returned columns contain PII, may data/schema descriptions be sent to each external provider, and what retention/logging policy applies?
3. **Oracle writes:** Are dictionary description updates approved in production? Who owns the dictionary tables and downstream consumers? Can we identify a stable table/column primary key and scope the glossary update correctly? What backup/reversal process is required?
4. **Supported providers:** Which of the five LLM providers and four embedding modes are actually supported in production? Is LM Studio dev-only? Confirm package extras, credentials, model IDs, regional/data residency requirements, and expected timeouts/cost caps.
5. **A/B operation:** Will B become an internal A subgraph or a separately deployable worker behind A’s adapter during migration? Is a temporary 8006 fallback required? What session/checkpoint history must be retained?
6. **Training workflow:** Does anyone actively run SFT/DPO? Who owns model artifact review/release and training-data governance? Does feedback actually persist all fields consumed by DPO? If no owner, remove scripts from the target scope.
7. **Index promotion and graph schema:** Is Neo4j GDS available in target; what is the intended graph schema contract? Confirm whether old `REFERENCES` helpers are historical or external consumers. What latency/quality threshold justifies reranking?
8. **Behavioral success criteria:** Which SQL safety and accuracy metrics, DWH latency budgets, maximum per-request Oracle calls/rows, and degraded-mode behaviors are acceptable? What is the safe shadow/test database for differential evaluation?
9. **Intent semantics:** Which A `resolved_filters` fields map to B SQL predicates, what constitutes user-confirmed vs inferred, and how should unsupported scenario types be rejected or routed?

For read-only verification in a controlled environment, owners should provide: (a) sanitized config *key names only* and provider matrix, (b) index manifest/collection metadata without row contents or credentials, (c) Oracle grants/schema owner and exact update row counts from reviewed dry-run SQL, (d) a safe golden fixture pack, and (e) a deployment health snapshot showing Oracle/Chroma/Neo4j readiness. Do not run B’s destructive build/enrichment scripts to answer these questions.

## Executive summary (15 lines maximum)

- **VERIFIED scope:** 15 Python runtime files / 7,873 LOC plus 3 prompt/domain files / 175 LOC; 11 owner-run scripts are additional operational surface.
- Preserve B’s schema/experience Chroma indexes, local/remote embedding providers, business-code/join tools, and Neo4j PageRank reranker.
- B is not a copy-ready package: its 2,942-line agent module, duplicate app/settings/Oracle/LLM stack, and startup side effects need decomposition into A boundaries.
- Top risks: CTE SQL always rejected by Oracle helper; feedback collection mismatch; remote embedding keys not injected; memory summarizer tuple bug; non-OpenAI critic mismatch.
- Further high-risk concerns: production dictionary enrichment update scope, direct production query execution, 10k row materialization, missing provider timeouts, unsafe Neo4j default credential.
- **Top 5 decisions:** external-provider data approval; Oracle write ownership/scope; supported LLM/embedding provider matrix; internal subgraph vs temporary external fallback; active ownership of SFT/DPO scripts.
- **Recommended first package: B0**, sanitized characterization and golden baseline, with no production behavior or DB writes.
