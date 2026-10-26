# B2 provider configuration

## What changed

Service A now has one role-aware LLM factory for the existing A callers and the DWH SQL roles. The legacy `build_llm()` behavior remains the default; callers opt into `generator`, `critic`, or `summarizer` role settings explicitly. OpenAI-compatible construction supports OpenAI, LM Studio, SambaNova, and OpenRouter. Cohere is loaded only when selected and requires its optional integration package. Tracing callbacks use A's LangSmith settings.

DWH SQL settings separate output tokens from context budget, connect/read timeouts, retry budget, max attempts, candidate row limit, and schema allowlist. Embedding adapters support OpenAI, Cohere, and lazy local SentenceTransformer models (including BGE and finance model identifiers). Embedding identity includes provider, model, dimension, and a stable fingerprint. Local models are cached by provider/model/device and inference is bounded to one concurrent local call.

## Configuration

Set these environment variables in the Service A environment. Credentials are read only from environment/.env and are intentionally not listed with values in `.env.example`.

| Setting | Purpose |
| --- | --- |
| `DWH_SQL_GENERATOR_PROVIDER`, `DWH_SQL_GENERATOR_MODEL` | SQL generator role; defaults to legacy `LLM_PROVIDER` / `LLM_MODEL`. |
| `DWH_SQL_CRITIC_PROVIDER`, `DWH_SQL_CRITIC_MODEL` | Independent critic selection; absent values inherit legacy provider/model. |
| `DWH_SQL_SUMMARIZER_PROVIDER`, `DWH_SQL_SUMMARIZER_MODEL` | Independent summarizer selection. |
| `DWH_SQL_EMBEDDINGS_PROVIDER`, `DWH_SQL_EMBEDDINGS_MODEL`, `DWH_SQL_EMBEDDINGS_DIMENSION`, `DWH_SQL_EMBEDDINGS_FINGERPRINT`, `DWH_SQL_EMBEDDINGS_DEVICE` | Vector index identity and local model device. Provider choices: `openai`, `cohere`, `local_bge`, `local_finance`. |
| `DWH_SQL_CONNECT_TIMEOUT_SECONDS`, `DWH_SQL_READ_TIMEOUT_SECONDS`, `DWH_SQL_DEADLINE_SECONDS` | Network and end-to-end budget values. |
| `DWH_SQL_RETRY_BUDGET`, `DWH_SQL_MAX_ATTEMPTS` | Bounded transient retry configuration. |
| `DWH_SQL_OUTPUT_TOKEN_LIMIT`, `DWH_SQL_CONTEXT_BUDGET` | Independent completion limit and input context policy budget. |
| `DWH_SQL_MAX_CANDIDATE_ROWS`, `DWH_SQL_ALLOWED_SCHEMAS` | Candidate query cap and SQL schema policy inputs for B3. |
| `DWH_SQL_OPENAI_API_KEY`, `DWH_SQL_COHERE_API_KEY`, `DWH_SQL_SAMBANOVA_API_KEY`, `DWH_SQL_OPENROUTER_API_KEY` | Optional provider credentials; OpenAI embedding falls back to `OPENAI_API_KEY`. |
| `DWH_SQL_BASE_URL`, `DWH_SQL_TOOL_CALLING` | Compatible endpoint override and provider tool-call capability switch. |

The context budget is available as a distinct policy setting for prompt assembly; this package does not truncate or rewrite A's existing prompt messages. Configure output tokens independently. Provider retry behavior is delegated to the supported SDK's bounded transient retry mechanism; role construction caps retries by both retry budget and max attempts.

## Optional dependencies

`requirements.txt` remains unchanged. `requirements-optional.txt` lists packages for enabled optional integrations; install only the needed packages. Local model weights are not downloaded or loaded at import time. Creating `EmbeddingProviderManager` is offline; the first `get()` loads a local model or initializes a remote provider client.

## Offline verification

```powershell
$env:DEBUG = "false"
.\.venv\Scripts\python.exe -m pytest -q services\aml_builder\tests\characterization\dwh_scenario\test_bug_ledger.py services\aml_builder\tests\characterization\dwh_scenario\test_b2_provider_factory.py
```

These tests use fake clients and temporary configuration; they do not call provider APIs.
