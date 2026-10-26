# B4 retrieval and readiness

## What changed and why

Service A now has injectable `Retriever` and `GraphReranker` interfaces, a Chroma collection adapter, an index manifest contract, and an optional lazy Neo4j reranker. Retrieval always requires an institution ID, passes it as a Chroma metadata predicate, and defensively removes returned rows with a different institution. Results sort by descending similarity and then ID. Required schema collections and incompatible/malformed manifests fail closed. A missing or empty experience collection returns no examples and reports `empty`.

Neo4j remains disabled by default. When enabled it requires URI, username, and password settings, lazily creates and reuses a driver, limits result count and query time, and falls back to deterministic Chroma order on errors. `/health` now returns per-dependency readiness through injectable checks; no remote clients or embeddings are constructed by the health handler. The HTTP SQL generator remains the active default.

## Configuration

`DWH_SQL_SCHEMA_TABLES_COLLECTION`, `DWH_SQL_SCHEMA_COLUMNS_COLLECTION`, and `DWH_SQL_EXPERIENCE_COLLECTION` set collection identifiers. `DWH_SQL_CHROMA_PERSIST_DIR` points at the deployment's local Chroma store; unset means schema collections are reported unconfigured and experience is empty. `DWH_SQL_SCHEMA_TABLES_K`, `DWH_SQL_SCHEMA_COLUMNS_K`, `DWH_SQL_EXPERIENCE_K`, `DWH_SQL_RETRIEVAL_THRESHOLD`, `DWH_SQL_SEMANTIC_WEIGHT`, and `DWH_SQL_GRAPH_WEIGHT` set retrieval defaults. `DWH_SQL_NEO4J_ENABLED` defaults to `false`; when enabled, set `DWH_SQL_NEO4J_URI`, `DWH_SQL_NEO4J_USER`, and `DWH_SQL_NEO4J_PASSWORD`. Timeout and result bounds use `DWH_SQL_NEO4J_TIMEOUT_SECONDS` and `DWH_SQL_NEO4J_MAX_RESULTS`.

Chroma collections must store `embedding_provider`, `embedding_model`, `embedding_dimension`, `embedding_fingerprint`, `built_at`, and JSON-encoded `source_row_counts` metadata. These are compared against the configured embedding identity before retrieval.

## Offline verification

From the repository root, run:

```powershell
$env:DEBUG = "false"
.\.venv\Scripts\python.exe -m pytest -q services\aml_builder\tests\test_b4_retrieval.py services\aml_builder\tests\characterization\dwh_scenario\test_bug_ledger.py
```

The tests use in-memory collections and fake dependency probes; they do not access Oracle, model providers, Chroma servers, or Neo4j.

## OWNER items

- **OWNER:** confirm deployed Chroma collection versions and their manifest identities before connecting a deployment.
- **OWNER:** confirm Neo4j availability/GDS requirements and provide deployment-managed credentials if graph reranking is enabled.
