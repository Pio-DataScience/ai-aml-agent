# B7 owner-run DWH toolchain

B7 adds isolated maintenance commands for bounded dictionary export, versioned Chroma builds, experience-pair ingestion, join projection, and Oracle enrichment SQL emission. The app does not import or run this package. HTTP remains the default SQL generator; B8 cutover is not part of B7.

## Commands

Run from the repository root with `PYTHONPATH=services/aml_builder` (PowerShell: `$env:PYTHONPATH = "services\aml_builder"`). All commands accept `--dry-run`; dry runs validate local inputs or show planned limits without connecting or writing.

```powershell
python -m scripts.dwh_scenario.cli harvest-dictionary --output artifacts/dwh/dictionary.jsonl --max-rows 5000
python -m scripts.dwh_scenario.cli build-schema-index --input artifacts/dwh/dictionary.jsonl
python -m scripts.dwh_scenario.cli ingest-pairs --input officer-pairs.jsonl --output artifacts/dwh/experience.jsonl
python -m scripts.dwh_scenario.cli build-experience-index --input artifacts/dwh/experience.jsonl
python -m scripts.dwh_scenario.cli build-join-graph --module DWH
python -m scripts.dwh_scenario.cli enrich-dictionary --input enrichment.jsonl --output-dir artifacts/dwh/enrichment
```

`harvest-dictionary` uses Service A's dedicated DWH-reader pool and deterministic SQL policy to query bounded Oracle dictionary metadata. It requires explicit `DWH_SQL_ALLOWED_SCHEMAS` and `DWH_SQL_ALLOWED_OBJECTS`; it never expands to a whole schema. The optional `DWH_SQL_INDEX_INTERNAL_OBJECTS` setting classifies selected allowlisted objects as internal metadata without widening scope. Records contain actual Oracle names, types, nullability, and available comments; descriptions and joins are not inferred. Missing configured objects are recorded in the harvest manifest, and the schema builder rejects incomplete allowlist coverage before staging. Schema records use one JSON object per line with `kind` (`table` or `column`), `id`, `text`, and optional scalar `metadata`. Before promotion, the schema builder validates exact staged IDs, documents and metadata against its input, institution scope, embedding identity, and one version across both collections. It retains the previous collections under timestamped rollback names. Existing enrichment is retained only when its tenant and referenced object/column still match current metadata; authoritative Oracle dictionary records always come from the current harvest. Empty experience JSONL is valid. Pair input accepts `{ "question": "...", "sql": "..." }` or `{ "scenario_description": "...", "golden_sql_template": "..." }`; duplicate pairs are removed.

Enrichment input is canonical JSONL with table/column/project identity and old/new values. The command emits only `backup.sql`, `updates.sql`, and `reverse.sql`; no Oracle write executor exists in the toolchain. Review and run the backup SELECT first in Toad, then the scoped updates in an owner-approved transaction, then retain the reverse script with the change record.

## Configuration and safeguards

The index builders use `DWH_SQL_CHROMA_PERSIST_DIR`, the B4 collection names, and B2 embedding provider/model/dimension/fingerprint settings. Optional `chromadb`, provider, and Neo4j packages are imported only by their selected commands. Harvest requires A's Oracle settings and only runs when explicitly invoked. No credentials are printed. B's old enrichment scripts updated `PIO_BANKBI_DICTIONARY_COL` using only `WHERE COLUMN_NAME = :c_name`; repeated column names could update unrelated tables. B's table-description script updated `PIO_BANKBI_DICTIONARY_TABLES` by table name. The new emitted updates include table and column plus project where supplied.

The current graph command is intentionally fail-closed until an owner-configured GDS loader is supplied; the bounded versioned join projection is available through `build_join_projection`. It does not delete or replace a live graph. Confirm GDS before enabling/promoting graph data.

For a deployment that sets `DWH_SQL_NEO4J_REQUIRED=true`, the owner-managed
loader must publish a `DwhScenarioGraphManifest` node with `name='active'` and
the promoted `version`. Configure that same identifier as
`DWH_SQL_NEO4J_GRAPH_VERSION`; Service A reports the required graph as degraded
and refuses local generation until the manifest matches.

## Owner runbook

1. Confirm Oracle dictionary ownership/grants and the approved read-only account.
2. Review the harvest manifest and data classification before indexing.
3. Confirm the deployed embedding fingerprint and Chroma persistence path; retain the previous collection until retrieval checks pass.
4. Confirm Neo4j endpoint/schema and GDS PageRank availability. Load a versioned graph projection only through the owner-approved promotion procedure.
5. For enrichment, inspect the backup SELECT output in Toad, review scoped UPDATE predicates and reverse SQL, obtain DBA approval, then execute manually. The application and scripts never execute these writes.
