# Local Inference Model Inventory & Hardware Sizing

**Date:** 2026-10-04 · **Revised:** 2026-10-06
**Purpose:** Enumerate every model the AML scenario-builder system invokes at runtime, classify each as
API-based or locally served, identify what must change to run with **zero API-based inference**, and specify
the hardware required to do so.

> **Revision 2026-10-06:** Service B was originally documented as `services/dwh` on port 8001. The module
> actually deployed is **`services/dwh_scenario_agent` on port 8006**, which is leaner — no supervisor, vision
> or safeguard model. Counts corrected from 10 roles / 6 models to **7 roles / 4 models**, and the vision +
> guardrail VRAM removed from all hardware tables.

---

## 1. Scope

The AML scenario builder is not self-contained. Two services must both run for the scenario-creation workflow
to complete, and both invoke models:

| Service | Repo / path | Port | Role in the AML workflow |
| --- | --- | --- | --- |
| **Service A — AML Builder** | `ai-aml-agent` (`services/aml_builder/`) | 8005 | The ReAct scenario-builder agent. This repo. |
| **Service B — PioTech-AI DWH Scenario Agent** | `PioTech-AI` (`services/dwh_scenario_agent/`) | 8006 | Text-to-SQL agent. Called by `execute_oracle_dwh_shadow_test` via SSE at `PIOTECH_AI_URL`. Without it, no production SQL is generated. Binds `127.0.0.1` only. |

**Only the `services/dwh_scenario_agent` module of PioTech-AI is in scope.** The AML agent calls
`http://127.0.0.1:8006/chat/stream`, which is `services.dwh_scenario_agent.web.api.main:app` (default
`SERVER_PORT=8006` at `services/dwh_scenario_agent/web/services/settings.py:124`; `/chat/stream` at
`services/dwh_scenario_agent/web/api/main.py:653`).

> **This is a purpose-built fork of the generic `services/dwh` module (port 8001), and it is leaner.**
> `dwh_scenario_agent` has **no supervisor node, no vision tool and no safeguard guardrail** — it never calls
> LM Studio at all. Do not substitute `services/dwh` when sizing: that module carries three extra model roles.

The sibling PioTech-AI modules — `services/dwh` (8001), `services/aml` (8002), `services/collection` (8003),
`services/cockpit`, `services/speech` — are **not** on the AML scenario-creation path and are excluded. The
`services/speech` module in particular would add ASR/TTS models; if it is ever brought into scope, this
inventory must be re-run.

Also excluded, because they are offline training scripts and never execute at request time:
`services/dwh_scenario_agent/scripts/train_model.py` (`unsloth/mistral-7b-instruct-v0.3-bnb-4bit`),
`services/dwh_scenario_agent/scripts/train_dpo.py` (`models/fine_tuned_adapter`, fallback
`unsloth/llama-3-8b-Instruct-bnb-4bit`), `services/dwh_scenario_agent/scripts/enrich_glossary_metadata.py`
(`gpt-4o-mini-2024-07-18`) and `scripts/templatize_golden_dataset.py` (`gpt-4o`) — both one-off index/dataset
enrichment.

---

## 2. Model inventory

**7 runtime model roles, 4 distinct models.** Three are API-based (must be replaced); one already runs locally.

### 2.1 Service A — AML Builder (`ai-aml-agent`)

| # | Role | Model (as configured) | Hosting | Call site | Local-ready? |
| --- | --- | --- | --- | --- | --- |
| 1 | **Orchestrator** — the ReAct agent that drives all six tools, decides tool order, and writes user-facing responses | `gpt-4o` (`LLM_MODEL`) | **API** (OpenAI) | `graph.py:54` → `create_react_agent(model=llm)` | Config only — `LLM_PROVIDER=lmstudio` |
| 2 | **Intent extraction** — NL to structured `AMLIntent` JSON | `gpt-4o` (`LLM_MODEL`) | **API** (OpenAI) | `tools.py:94`, `tools.py:106` | Config only |
| 3 | **Explanation-code reranker** — picks/ranks explanation codes from vector candidates, must emit a strict JSON array | `gpt-4o-mini` (`LLM_MODEL_FAST`) | **API** (OpenAI) | `explanation_code_search.py:195` via `build_llm(fast=True)` | Config only |
| 4 | **Explanation-code embeddings** — builds and queries the `PIO_EXPLANATION_CODE` vector index (cached to a pickle) | `text-embedding-ada-002` (1536-dim) | **API** (OpenAI) | `explanation_code_search.py:95` (index build), `explanation_code_search.py:137` (per-query) | **No — code change required** |
| 5 | **Registry semantic-search embeddings** — embeds all persisted scenarios + the query, live on every call, no cache | `text-embedding-ada-002` (1536-dim) | **API** (OpenAI) | `scenario_registry_analytics.py:164-170` | **No — code change required** |

Roles 1–3 all funnel through one factory, `build_llm()` in [llm_client.py:21](services/aml_builder/web/services/llm_client.py#L21), which already
has a provider switch: setting `LLM_PROVIDER=lmstudio` and `LLM_BASE_URL=http://127.0.0.1:1234/v1` redirects
every chat call to a local OpenAI-compatible server with no code change.

Roles 4–5 have **no such switch**. Both instantiate `OpenAIEmbeddings(api_key=settings.OPENAI_API_KEY)` directly,
and neither passes `model=`, so both silently use the langchain-openai default **`text-embedding-ada-002`**
(verified at `langchain_openai/embeddings/base.py:187`). This is the only hard code blocker to pure local inference
in Service A.

> **Cache warning:** the `PIO_EXPLANATION_CODE` pickle cache in `explanation_code_search.py` is dimension-bound.
> Switching the embedding model **invalidates that cache and it must be rebuilt**. The registry search embeds
> live on every call, so it needs no rebuild.

### 2.2 Service B — PioTech-AI DWH Scenario Agent (`services/dwh_scenario_agent`)

| # | Role | Model (as configured) | Hosting | Call site | Local-ready? |
| --- | --- | --- | --- | --- | --- |
| 6 | **Reasoner** — the main text-to-SQL loop; emits the Oracle SQL the shadow test runs | `gpt-4o-mini-2024-07-18` (`LLM_MODEL`) | **API** (OpenAI) | `agent.py:2309` (`call_llm_reasoner`), instantiated at `agent.py:2391/2404/2430/2441` | Config only — `LLM_PROVIDER=lmstudio` |
| 7 | **Critique** — reviews and corrects the generated SQL | `gpt-4o-mini-2024-07-18` (`LLM_MODEL` / `LLM_MODEL_FAST`) | **API** (OpenAI) | `agent.py:2647` (`call_critique_llm_node`), instantiated at `agent.py:2692/2694/2702` | Config only |
| 8 | **Schema/glossary RAG embeddings** — Chroma vector store over the DWH schema catalog | `BAAI/bge-large-en-v1.5` (1024-dim) | **LOCAL** — sentence-transformers | `embeddings_provider.py:74` (`BGELocalProvider`), `main.py:118`, `main.py:1427` | **Already local** |

**Roles this module does *not* have** (they exist only in the generic `services/dwh` module and must not be
counted here): the **supervisor** node (`call_supervisor_llm`), the **vision** tool (`qwen/qwen3-vl-8b`) and the
**safeguard** guardrail (`meta-llama/Llama-Guard-3-8B`). `dwh_scenario_agent` declares `VISION_MODEL_*` and
`SAFEGUARD_MODEL_*` in `settings.py:172-188` but no code reads them, so **it never contacts LM Studio.**

Also note `_get_sql_error_explanation` (`agent.py:421`) imports `ChatOpenAI` but is **pure rule-based pattern
matching on ORA- codes** — a dead import, not an LLM call. The equivalent in `services/dwh` *does* call an LLM.

> **Observed runtime caveat:** the live service loaded BGE on **CPU** (`Use pytorch device_name: cpu`, ~2.5 s for
> a single warmup embedding) despite `EMBED_DEVICE=cuda` in `.env`, because the module resolves the device as
> `auto`. On CPU this embedder is a real latency contributor; confirm it lands on GPU before benchmarking.

An alternative local embedding model is already implemented but not active:
`philschmid/bge-base-financial-matryoshka` (`embeddings_provider.py:453`, `BGEFinanceProvider`, selected via
`EMBED_PROVIDER=local-finance`).

### 2.3 Non-model external dependencies

These are not inference, but they are cloud calls and must be disabled for a genuinely air-gapped deployment:

| Dependency | Where | Action for local-only |
| --- | --- | --- |
| **LangSmith tracing** | `LANGSMITH_TRACING=true`, `LANGSMITH_ENDPOINT=https://api.smith.langchain.com` — set in **both** `.env` files | Set `LANGSMITH_TRACING=false` / `LANGCHAIN_TRACING_V2=false`, or self-host |
| Oracle (`BI_DWH`, shadow DWH) | `192.168.30.3:1521/AIDB`, `192.168.30.43:1521/OPENBI2` | Already on-prem |
| Chroma, Neo4j | Scenario-agent module, local paths / `bolt://localhost:7687` | Already local |
| LM Studio (`127.0.0.1:1234`) | **not used by this module** | n/a — only `services/dwh` calls it |

### 2.4 Summary — what blocks pure local inference

| Model | Roles | Blocker class |
| --- | --- | --- |
| `gpt-4o` | A: orchestrator, intent extraction | **Config** (`LLM_PROVIDER=lmstudio`) |
| `gpt-4o-mini` | A: explanation-code reranker | **Config** |
| `gpt-4o-mini-2024-07-18` | B: reasoner, critique | **Config** |
| `text-embedding-ada-002` | A: explanation-code index + registry semantic search | **Code change** — 3 call sites, plus pickle-cache rebuild |
| `BAAI/bge-large-en-v1.5` | B: schema RAG | none — already local |

**Required code work:** route `OpenAIEmbeddings` in `explanation_code_search.py` and
`scenario_registry_analytics.py` through a provider-switching factory (mirroring `build_llm()`), pointing at a
local embedding server or sentence-transformers. The cleanest option is to reuse Service B's already-proven
`BGELocalProvider` pattern. Everything else is `.env`.

---

## 3. Local replacement models (three quality tiers)

The workload is **tool-calling and strict-JSON-emitting**, not chat: the ReAct agent must choose correctly among
six tools, and three call sites parse the output as JSON (`safe_parse_json`, and a bare `json.loads` at
`explanation_code_search.py:208`). Service B additionally does **text-to-SQL against a real Oracle schema**.
Instruction-following and structured-output fidelity matter far more here than prose quality, which is why the
Minimum tier carries real accuracy risk.

| Role | **Tier 1 — Minimum** (viable, accuracy risk) | **Tier 2 — Recommended** (production) | **Tier 3 — Ideal** (gpt-4o-class parity) |
| --- | --- | --- | --- |
| A: Orchestrator + intent (replaces `gpt-4o`) | Qwen3-14B, Q4_K_M (~9 GB) | Qwen3-32B, AWQ/Q4_K_M (~19–20 GB) | Llama-3.3-70B-Instruct FP8 (~70 GB) or Qwen3-235B-A22B FP8 (~235 GB) |
| A: Fast reranker (replaces `gpt-4o-mini`) | Qwen3-4B Q4 (~2.5 GB) | Qwen3-8B Q4/Q5 (~5–6 GB) | Qwen3-14B Q5 (~10 GB) |
| A: Embeddings (replaces `ada-002`) | `BAAI/bge-small-en-v1.5` (~0.13 GB) | **`BAAI/bge-large-en-v1.5`** FP16 (~0.7 GB) — reuse Service B's loader | `BAAI/bge-m3` or `Qwen3-Embedding-4B` (~1–8 GB) |
| B: DWH reasoner + critique (replaces `gpt-4o-mini`) | share Tier-1 Qwen3-14B | Qwen3-32B or a SQL-tuned 32B | share Tier-3 70B+ |
| B: Schema RAG embeddings | `bge-large-en-v1.5` (unchanged) | `bge-large-en-v1.5` (unchanged) | `bge-large-en-v1.5` or `bge-m3` |

**No vision or safeguard model is required.** `dwh_scenario_agent` does not use them. If the system is ever
repointed at the generic `services/dwh` module (port 8001), add ~6–9 GB for `qwen3-vl-8b` and ~5 GB for
`Llama-Guard-3-8B` to every VRAM budget below, plus a supervisor role on the main model.

**Tier-1 caveat, stated plainly:** a 14B model at Q4 driving both a six-tool ReAct loop and Oracle text-to-SQL
will produce materially more malformed JSON, wrong tool choices, and invalid SQL than `gpt-4o`. Tier 1 is for a
dev/UAT box, not for the production scenario-persistence path. Tier 2 is the lowest tier we would deploy.

### 3.1 The dominant VRAM cost is context, not weights

Service B is configured with `LLM_MAX_TOKENS=100000`. The DWH agent stuffs large schema/glossary context into
its prompts, so KV cache — not model weights — sets the VRAM floor.

Per-token FP16 KV cache (GQA, 8 KV heads, head_dim 128):

| Model | KV per token | @ 32k ctx | @ 100k ctx |
| --- | --- | --- | --- |
| Qwen3-14B (40 layers) | 160 KB | ~5 GB | ~16 GB |
| Qwen3-32B (64 layers) | 256 KB | ~8 GB | ~25 GB |
| Llama-3.3-70B (80 layers) | 320 KB | ~10 GB | ~32 GB |

**Two levers, both strongly recommended:** enable **FP8 KV cache** (halves every number above) and **cap the
served context to 32k** unless a specific DWH query provably needs more. Together they cut the KV budget ~4x.

---

## 4. Hardware specifications

### Shared assumptions

- One Linux host (Ubuntu 22.04/24.04) serving **both** Service A and Service B.
- Resident model set is **three**: one main chat model, one fast chat model, one embedder. No vision or guardrail model.
- vLLM or SGLang for the chat models (continuous batching, FP8 KV cache, tensor parallelism); LM Studio /
  llama.cpp is acceptable at Tier 1.
- Oracle stays remote (it already is). Chroma, Neo4j, and the SQLite checkpointer are local and need fast disk.
- **"Concurrent user" is not "one request."** One AML scenario-creation turn is a multi-step ReAct loop: intent
  extraction, embeddings, reranker, a full nested Service-B text-to-SQL agent run (supervisor + reasoner +
  critique), metadata prep, persistence. Expect **8–20+ sequential LLM calls per user turn**, several of them
  long-context. Size for that multiplier, not for the raw user count.

### How the three ranges differ

Model weights are a **fixed, one-time** VRAM cost — loading them once serves all users. What scales with
concurrency is:

1. **KV cache** — roughly linear in concurrent sequences. This is the main driver.
2. **Compute/throughput** — more concurrent decode streams need more memory bandwidth and SM throughput, or
   latency collapses even when VRAM fits.
3. **Co-residency pressure** — at low concurrency the main model, the fast model and the embedder can
   time-share one GPU. At higher concurrency they must be pinned to separate GPUs so the main model is never
   evicted or starved.
4. **Redundancy** — above ~20 users, a single host is a single point of failure for the entire AML workflow.

---

### 4.1 Range A — 1–3 concurrent users (internal / pilot / UAT)

One or two concurrent sequences in flight. All three models — main, fast and embedder — co-reside on a single
large GPU with room to spare.

| Spec | **Minimum** | **Recommended** | **Ideal** |
| --- | --- | --- | --- |
| **Models** | Qwen3-14B Q4 + Qwen3-4B Q4 + bge-small | Qwen3-32B Q4 + Qwen3-8B Q4 + bge-large | Llama-3.3-70B FP8 + Qwen3-14B Q5 + bge-m3 |
| **GPU** | 1x RTX 4090 / 5090 (24–32 GB) | 1x RTX 6000 Ada or A6000 (48 GB) | 1x H100 80 GB, or 2x RTX 6000 Ada 48 GB |
| **VRAM budget** | 24 GB: 9 (main) + 2.5 (fast) + 0.2 (embed) + ~8 KV @32k FP8 | 48 GB: 20 + 5 + 0.7 + ~8 KV @32k FP8, ~14 GB spare | 80 GB: 70 + ~10 KV @32k FP8 (fast model + embedder on CPU or a 2nd card) |
| **Served context** | 16–32k | 32k | 32–64k |
| **CPU** | 8-core (Ryzen 7 / Xeon Silver) | 16-core (Ryzen 9 / EPYC / Xeon Gold) | 24-core+ |
| **System RAM** | 32 GB | 64 GB | 128 GB |
| **Storage** | 1 TB NVMe | 2 TB NVMe Gen4 | 2 TB NVMe Gen4/5 |
| **Model weights on disk** | ~25 GB | ~45 GB | ~120 GB |
| **Expected quality** | Dev/UAT only — expect JSON and SQL failures | Production-acceptable | gpt-4o-class |

### 4.2 Range B — 5–10 concurrent users (small team / department)

KV cache is now the binding constraint: 10 concurrent sequences at 32k means roughly 10x the per-sequence cache.
The fast model and embedder should be **permanently resident** rather than swapped, so the main model's batch is
never interrupted — which, combined with the KV growth, is what forces a second GPU.

| Spec | **Minimum** | **Recommended** | **Ideal** |
| --- | --- | --- | --- |
| **Models** | Qwen3-14B Q4 + Qwen3-8B Q4 + bge-large | Qwen3-32B AWQ + Qwen3-8B + bge-large, all resident | Llama-3.3-70B FP8 (TP2) + Qwen3-14B + bge-m3 |
| **GPU** | 2x RTX 4090/5090 (48–64 GB total) | 2x RTX 6000 Ada 48 GB (96 GB), or 1x H100 80 GB + 1x L40S 48 GB | 2x H100 80 GB (160 GB, NVLink) |
| **VRAM budget** | 48 GB: 9 main + ~20 KV (10 seq @32k FP8) + 5 fast + 0.7 embed + headroom | 96 GB: 20 main + ~40 KV (10 seq @32k FP8) + 6 fast + 0.7 embed, ~29 GB spare for burst KV | 160 GB: 70 main (TP2) + ~50 KV (10 seq @32k FP8) + fast model and embedder on card 2 |
| **Served context** | 16–32k | 32k (hard cap; FP8 KV mandatory) | 32–100k |
| **CPU** | 16-core | 32-core (EPYC 9004 / Xeon Scalable 4th gen) | 48-core+ |
| **System RAM** | 64 GB | 128 GB | 256 GB |
| **Storage** | 2 TB NVMe | 4 TB NVMe Gen4 (RAID 1) | 4 TB NVMe Gen5 (RAID 1) |
| **Network** | 1 GbE to Oracle | 10 GbE to Oracle | 10–25 GbE |
| **Power / cooling** | ~1.2 kW, tower | ~1.6 kW, 4U rack | ~2.5 kW, 4U rack, redundant PSU |
| **Why more than Range A** | 2nd GPU exists purely to hold the KV cache for 10 streams | Spare VRAM absorbs KV bursts; fast model and embedder never evict the main model | TP2 over NVLink for both capacity and decode latency |

### 4.3 Range C — 20+ concurrent users (bank-wide)

At this point a single host is both a capacity and an availability problem. Plan for **2+ nodes behind a load
balancer**, with the shared services (embedder, fast model) factored out into their own small pool so every
node is not duplicating them.

| Spec | **Minimum** | **Recommended** | **Ideal** |
| --- | --- | --- | --- |
| **Models** | Qwen3-32B AWQ (TP2) + Qwen3-8B + bge-large | Llama-3.3-70B FP8 (TP2 or TP4) + Qwen3-14B + bge-large/m3 | Qwen3-235B-A22B FP8 (TP8) + Qwen3-32B + bge-m3 |
| **GPU** | 4x L40S 48 GB (192 GB), or 2x H100 80 GB | 4x H100 80 GB (320 GB, NVLink/NVSwitch) | 8x H100/H200 80–141 GB (640 GB–1.1 TB), NVSwitch |
| **VRAM budget** | 192 GB: 20 main x2 replicas + ~80 KV (25 seq @32k FP8) + fast/embed pool | 320 GB: 70 main (TP2) x2 replicas + ~120 KV (30 seq @32k FP8) + fast/embed pool | 640 GB+: 235 main (TP8) + ~200 KV (40+ seq @64k FP8) + fast/embed pool |
| **Served context** | 16–32k | 32k, FP8 KV, prefix caching on | 64–100k |
| **Nodes** | 1 (no redundancy — accepted risk) | 2 (N+1) | 2–3 (N+1, rolling upgrades) |
| **CPU per node** | 32-core | 64-core (dual EPYC 9004) | 96-core+ (dual EPYC 9004/9005) |
| **System RAM per node** | 128 GB | 512 GB | 1 TB |
| **Storage per node** | 4 TB NVMe | 8 TB NVMe Gen4, RAID 10 | 8–16 TB NVMe Gen5, RAID 10 |
| **Network** | 10 GbE | 25 GbE + dedicated path to Oracle | 100 GbE / InfiniBand between nodes |
| **Power / cooling** | ~3 kW, 4U | ~6–8 kW per node, rack with 10+ kW/rack cooling | ~10 kW per node, liquid or rear-door heat exchanger |
| **Why more than Range B** | Multi-GPU tensor parallelism is now required for throughput, not just capacity | Separate replicas plus a shared fast-model/embedder pool; N+1 so the AML workflow survives a node loss | MoE frontier model for true gpt-4o parity; NVSwitch for TP8 collective bandwidth |

---

## 5. Migration checklist

1. **Stand up a local OpenAI-compatible server** (vLLM/SGLang, or LM Studio on `127.0.0.1:1234`). Nothing
   currently occupies that port on this path — `dwh_scenario_agent` does not call LM Studio.
2. **Service A — config:** `LLM_PROVIDER=lmstudio`, `LLM_BASE_URL=<local>/v1`, `LLM_MODEL=<local main>`,
   `LLM_MODEL_FAST=<local fast>`. Note `build_llm()` sends a dummy `api_key="lm-studio"` automatically
   ([llm_client.py:43](services/aml_builder/web/services/llm_client.py#L43)).
3. **Service A — code:** add an embedding-provider factory and replace the three `OpenAIEmbeddings(...)`
   instantiations in [explanation_code_search.py:95](services/aml_builder/web/services/explanation_code_search.py#L95),
   [explanation_code_search.py:137](services/aml_builder/web/services/explanation_code_search.py#L137), and
   [scenario_registry_analytics.py:164](services/aml_builder/web/services/scenario_registry_analytics.py#L164).
   Add `EMBED_PROVIDER` / `EMBED_MODEL` / `EMBED_BASE_URL` to `settings.py`.
4. **Delete the explanation-code vector pickle cache** and let it rebuild — the existing one is 1536-dim
   ada-002 and is incompatible with any replacement.
5. **Service B — config:** `LLM_PROVIDER=lmstudio`, `LLM_PROVIDER_FAST=lmstudio`, `LLM_BASE_URL`, `LLM_MODEL`,
   `LLM_MODEL_FAST`. Embeddings already local, but **verify BGE lands on GPU** — the running service resolved
   the device as `auto` and loaded on CPU despite `EMBED_DEVICE=cuda`.
   Confirm `PIOTECH_AI_URL=http://127.0.0.1:8006/chat/stream` on the Service A side points at this module and
   not the generic `services/dwh` on 8001.
6. **Cap context:** reduce `LLM_MAX_TOKENS` from 100000 to 32768 and enable FP8 KV cache on the serving engine.
7. **Disable cloud telemetry:** `LANGSMITH_TRACING=false` in both `.env` files.
8. **Re-validate the strict-JSON paths** — `safe_parse_json` in `llm_client.py`, the bare `json.loads` at
   `explanation_code_search.py:208`, and `AMLIntent` Pydantic validation — against the chosen local model before
   trusting the persistence path. This is the single highest-risk step in the migration.

---

## 6. Configuration parameter reference

### Service A (`ai-aml-agent/.env`)

| Variable | Current | Local-inference value |
| --- | --- | --- |
| `LLM_PROVIDER` | `openai` | `lmstudio` |
| `LLM_BASE_URL` | unset | `http://127.0.0.1:1234/v1` |
| `LLM_MODEL` | `gpt-4o` | local main model id |
| `LLM_MODEL_FAST` | `gpt-4o-mini` | local fast model id |
| `LLM_MAX_TOKENS` | `16384` | keep or reduce (this is max **output** tokens) |
| `OPENAI_API_KEY` | set | unused once local (ignored by the `lmstudio` branch) |
| `EMBED_PROVIDER` / `EMBED_MODEL` | **do not exist** | **must be added** (see step 3) |
| `PIOTECH_AI_URL` | `http://127.0.0.1:8006/chat/stream` | unchanged — `127.0.0.1`, not `localhost` (8006 is IPv4-loopback-bound) |
| `LANGSMITH_TRACING` | `true` | `false` |

### Service B (`PioTech-AI/.env`, `services/dwh_scenario_agent`)

| Variable | Current | Local-inference value |
| --- | --- | --- |
| `LLM_PROVIDER` / `LLM_PROVIDER_FAST` | `openai` | `lmstudio` |
| `LLM_MODEL` / `LLM_MODEL_FAST` | `gpt-4o-mini-2024-07-18` | local model ids |
| `LLM_MAX_TOKENS` | `100000` | `32768` recommended |
| `EMBED_MODEL` | `BAAI/bge-large-en-v1.5` | unchanged — already local |
| `EMBED_PROVIDER` | `local` | unchanged |
| `EMBED_DEVICE` | `cuda` | unchanged — but **not honoured**; module resolves `auto` and loaded on CPU |
| `VISION_MODEL_NAME` | `qwen/qwen3-vl-8b` | **unused** — declared in settings, no code reads it |
| `SAFEGUARD_MODEL_NAME` | `meta-llama/Llama-Guard-3-8B` | **unused** — declared in settings, no code reads it |
| `SERVER_PORT` | `8006` (default) | unchanged — must match Service A's `PIOTECH_AI_URL` |
| `LANGSMITH_TRACING` | `true` | `false` |
