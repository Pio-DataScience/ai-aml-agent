# B8 — Final integration and cutover readiness

## Result

**B8 implementation: PASS for the completed scoped checks.**
**Production cutover: BLOCKED.** `DWH_SQL_MODE=http` remains the default.
The approved one-fixture A/B run established generation-only semantic parity;
it did not execute scenario SQL or prove result-set parity. Production approval
and production readiness are not claimed.

Stage 1 completed the approved metadata-only Oracle discovery: seven configured
objects and 180 column rows were verified from `BI_DWH`. The account's elevated
privileges were recorded as an operations warning; the DBA resource plan was
not observable and remains unverified. The local BGE Chroma index was rebuilt
from the verified OPENBI2 metadata, staged, validated, promoted, and smoke-tested
for all seven objects. The synthetic “Cash deposits over 14 days” run used only
the approved intent and schema context, OpenAI gpt-5-nano, and fixed-date
`cs_b1`/`cs_b2`/`cs_b3` binds. No customer rows, critic call, or Oracle scenario
execution was part of that run.

The controlled Oracle result-parity workflow in
`web/services/dwh_scenario/live_validation.py` remains unexecuted. Strict
execution preflight still requires least-privilege credentials and DBA-confirmed
resource controls. Details and the remaining limits are in
`Docs/B8_live_validation.md`.

B8 tightens B6's contract and B7's activation behavior. Shadow mode returns the
HTTP ScenarioSqlResult unchanged, including SQL, approval flag, execution
evidence, findings and trace metadata. Local generation failures remain isolated
in shadow mode. In-process mode fails closed if a required dependency is absent,
the schema collections do not share a version, or Neo4j is required but is not
ready. Neo4j remains optional when DWH_SQL_NEO4J_REQUIRED=false.
When required, the selected graph must publish an owner-managed
DwhScenarioGraphManifest node named active, and its version must equal
DWH_SQL_NEO4J_GRAPH_VERSION; connectivity alone is not readiness.

The B0 evaluation now validates SELECT safety and confirmed filters with the
same AST policy used by the local generator. Its semantic signature ignores
formatting and reordered conjuncts while retaining tables, projections,
predicates, joins and grouping. A changed filter value is a semantic mismatch.
The comparison report uses distinct HTTP/local fake outputs and reports
semantic parity separately from structural constraints. There is no officer
gold set, so this remains synthetic structural evaluation rather than measured
production parity.

B7 now stamps schema table and column collections with one schema_index_version;
both must be staged and count-validated before promotion. If the second promotion
fails, the first is restored to its previous collection (or removed if no
previous collection existed). Readiness reports schema_set=down and local
generator initialization fails closed for mixed or unversioned sets. Chroma
does not offer a cross-collection transaction, so a process crash between
renames can temporarily leave a mixed set; readiness blocks its activation
until the owner restores or rebuilds a consistent pair.

## Verification commands

Run offline from the repository root. Set DEBUG=false and
PYTHONPATH=services\aml_builder, then run:

    .\.venv\Scripts\python.exe -m pytest -q --basetemp=.pytest-temp\b8 services\aml_builder\tests\test_b8_live_validation.py services\aml_builder\tests\test_b8_readiness.py services\aml_builder\tests\eval\dwh_scenario\test_runner.py
    .\.venv\Scripts\python.exe -m pytest -q --basetemp=.pytest-temp\b4b7 services\aml_builder\tests\test_b4_retrieval.py services\aml_builder\tests\test_b7_toolchain.py
    .\.venv\Scripts\python.exe -m pytest -q --basetemp=.pytest-temp\b3 services\aml_builder\tests\test_b3_oracle_sql_policy.py
    .\.venv\Scripts\python.exe -m pytest -q --basetemp=.pytest-temp\bind services\aml_builder\tests\test_b1_dwh_contract.py -k "approved_b8_bind or bind_sql"

Results: B8/evaluation 26 passed; B4/B7 22 passed; B3 SQL policy 24 passed;
B1 bind-contract regressions 4 passed. `compileall` and `git diff --check` also
passed. Unit/evaluation suites use offline fakes. The approved Stage 1 metadata
discovery and one synthetic generation-only A/B run were separate scoped live
actions; no scenario SQL or customer rows were retrieved. These checks do not
prove Oracle result parity or production readiness. B5/B6 async suite attempts
stalled during Windows Proactor socketpair initialization; no pass claim is made.
The OPENBI2 index build and seven-object local retrieval smoke test completed.

## Rollback

Production SQL generation remains on HTTP. If a deployment explicitly selects
the local mode during an approved non-production evaluation, restore behavior
with DWH_SQL_MODE=http and restart Service A. Keep PIOTECH_AI_URL configured.
Shadow mode also uses the HTTP result as authority. For Chroma, retain the
_previous_<timestamp> collection names returned by B7; before enabling local
mode, restore a matched table/column version pair and confirm /health reports
schema_set as ok. Do not delete retained versions until owner sign-off.

## Cutover gates still requiring owner evidence

- **Owner/compliance:** approve provider data classes and any row-preview policy.
- **Owner:** review the scoped generation-only comparison and authorize any separate execution evaluation; explicit production approval is required before changing the default.
- **Owner:** verify the production deployment uses the validated Chroma collection pair, embedding manifest, and approved experience data.
- **DBA:** provide a least-privilege reader and confirm resource controls and query/object allowlists.
- **Owner/DBA (if Neo4j is required):** confirm endpoint, graph schema, and GDS support; graph loading/promotion remains owner-gated.
- **Compliance/officer:** approve any broader provider-data use and provide reviewed scenario/SQL evaluation pairs; one synthetic fixture approval does not extend to production data.

## Bug ledger reconciliation

| ID | B8 status |
| --- | --- |
| BL1 | PASS — AST policy and Oracle adapter contract tested in B3 regressions. |
| BL2 | PASS — one experience collection constant and empty/missing behavior. |
| BL3 | PASS — provider credentials injected; provider-matrix test evidence is B2. |
| BL4 | PASS — local one-request context has no lossy compaction fallback. |
| BL5 | PASS — critic role provider is isolated; B2 provider matrix evidence. |
| BL6 | PASS — embedding cache key includes device. |
| BL7 | PASS — dependency readiness is explicit; B8 adds schema-set consistency. |
| BL8 | PASS with limits — staged promotion and rollback; Chroma lacks a two-collection transaction. |
| BL9 | PASS — bounded fetch and truncation evidence. |
| BL10 | PASS — output-token and context budgets are separate. |
| BL11 | N/A — feedback endpoint was intentionally not ported. |

Status reconciles package reports and targeted offline evidence; it does not
substitute for rerunning every prior package on this branch.
# B8 local Chroma compatibility

Service A now embeds retrieval queries with the same configured provider, model,
dimension, and fingerprint used for index builds. Query-vector and stored-manifest
dimensions are checked before retrieval. Schema and experience build commands
require an explicit `DWH_SQL_INDEX_INSTITUTION_ID`; every indexed document is
stamped with that tenant scope, and conflicting source metadata fails before any
staging collection is created. Schema table/column collection names and the
persistence path remain settings-driven. Schema collections must carry matching
embedding manifests and one shared `schema_index_version`; missing, empty,
unversioned, mixed-version, and embedding-incompatible required indexes fail
closed.

Service B's existing DWH scenario collections were created with local
`BAAI/bge-large-en-v1.5` vectors (1024 dimensions), default collection names,
unversioned collection metadata, and row metadata without B8's
`institution_id`. Service A previously defaulted to OpenAI
`text-embedding-3-small` (1536 dimensions) and issued `query_texts`, delegating
query embedding to Chroma's default function. The old Service B indexes are not
compatible with B8 even if their persistence directory is configured. They are
left untouched and must be rebuilt into a separate Service A persistence path
through the versioned B8 index builder.

For a new client, configure `DWH_SQL_CHROMA_PERSIST_DIR`, schema/experience
collection names, `DWH_SQL_EMBEDDINGS_PROVIDER`, `DWH_SQL_EMBEDDINGS_MODEL`,
`DWH_SQL_EMBEDDINGS_DIMENSION`, optional fingerprint/device, and the explicit
owner-run `DWH_SQL_INDEX_INSTITUTION_ID`. Use OpenAI or Cohere with their
configured credentials, or install `requirements-local-embeddings.txt` after
the main requirements for `local_bge`/`local_finance`. Prepare canonical JSONL
with one schema-table or schema-column record per line, then run:

```powershell
$env:PYTHONPATH = "."
\.venv\Scripts\python.exe scripts\dwh_scenario\cli.py build-schema-index --input <schema.jsonl>
```

The builder stages both collections, validates counts and manifests, promotes
them as one schema version, and retains prior collections for rollback. Build
experience vectors with `build-experience-index` using the same configured
embedding identity and institution scope. Chroma is installed by
`requirements.txt`.

The caller still supplies its institution scope from application configuration;
retrieval filters and defensively checks every returned document against it.
Changing embedding model/provider/dimension requires a fresh staged index build;
do not relabel existing vectors or point at an unversioned legacy store.
