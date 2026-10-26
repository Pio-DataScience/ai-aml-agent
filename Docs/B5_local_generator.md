# B5 — Local DWH SQL generator orchestration

## What and why

B5 adds an injected local SQL-generation pipeline behind Service A's existing
`ScenarioSqlGenerator` contract. It is intentionally not registered in the active
shadow-test tool: the HTTP SSE adapter remains the production default until B6.
The local graph compiles only when `build_local_graph` is explicitly called.

Each request performs institution-scoped experience/table/column retrieval,
assembles a bounded context, asks the configured generator role for SQL, applies
the Oracle AST policy and Service A intent checks, executes only valid SQL through
the read-only DWH repository, and asks the configured critic role for an advisory
verdict. The critic receives candidate SQL and columns/count/error metadata, never
raw result rows. A zero-row query is a successful execution. Candidate SQL is
returned only when validation passes, execution succeeds, and critique approves.

The former memory compaction behavior is deliberately absent: a request is based on
one typed AML intent and has a bounded context, so prior-chat summarization is not
needed. There is no fallback canned summary.

## Run offline checks

From the repository root, run the focused B5, B4 and bug-ledger checks:

```powershell
$env:DEBUG = "false"
$env:PYTHONPATH = "services/aml_builder"
.\.venv\Scripts\python.exe -m pytest -q services\aml_builder\tests\test_b5_local_generator.py services\aml_builder\tests\test_b4_retrieval.py services\aml_builder\tests\characterization\dwh_scenario\test_bug_ledger.py
```

The tests use scripted model, retrieval and Oracle fakes. No provider or database
is contacted. Compile checks can be run with:

```powershell
$env:PYTHONPATH = "services/aml_builder"
.\.venv\Scripts\python.exe -m compileall -q services/aml_builder/web/services/dwh_scenario services/aml_builder/tests/test_b5_local_generator.py
```

## Configuration

The local generator consumes the existing B2/B3/B4 settings: role-specific
`DWH_SQL_GENERATOR_*` and `DWH_SQL_CRITIC_*` providers/models and keys;
`DWH_SQL_CONTEXT_BUDGET`, output token limit and request deadline; allowed Oracle
schemas/objects; candidate row limit; collection names and retrieval `K` settings;
and optional Neo4j reranking settings. B5 adds these hard caps:

| Setting | Default | Purpose |
|---|---:|---|
| `DWH_SQL_MAX_TOOL_CALLS` | 25 | Retrieval, generation, execution and critique actions per request |
| `DWH_SQL_MAX_CRITIQUE_ATTEMPTS` | 4 | Candidate review/repair rounds |
| `DWH_SQL_MAX_ORACLE_STATEMENTS` | 20 | Shared Oracle read budget per request |

`AML_INST_CODE` scopes retrieval. B5 does not select a generator mode or change
startup behavior. Do not configure or activate the local generator in production
as part of this package; mode selection belongs to B6. Critique row previews are
not implemented, so no result rows are sent under any configuration.

## Limitations

Provider and Oracle behavior is verified only through fakes. Real schema/index
compatibility, provider data approval and candidate-query grants remain owner-run
checks from B2–B4.
