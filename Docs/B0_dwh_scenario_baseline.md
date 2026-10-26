# B0 DWH Scenario Baseline

B0 adds an offline safety net before moving Service B into Service A. It does not change application behavior. Tests use deterministic fake adapters; the Service B handoff contract is loaded directly from its source file so its bare `web.services.*` imports cannot pollute A's test process. Bug-ledger cases are strict expected failures until their assigned migration packages implement them.

## Run

From the repository root in PowerShell:

```powershell
$env:DEBUG = "false"
$env:PYTHONPATH = "services\aml_builder"
.\.venv\Scripts\python.exe -m pytest -q services\aml_builder\tests\characterization\dwh_scenario
```

This runs fake-backed routing and handoff characterization checks. It needs no Oracle, LLM, Chroma, or Neo4j service. If the sibling `PioTech-AI` checkout is absent, source-contract characterization tests skip; the remaining cases still run.

Run the structural evaluation harness:

```powershell
$env:PYTHONPATH = "services\aml_builder"
.\.venv\Scripts\python.exe -m tests.eval.dwh_scenario.runner
```

It reads `cases.jsonl`, calls the deterministic stub generator, and prints per-case parse, SELECT-only, projection, filter, safety, fake execution, latency, LLM/tool-call, and token metrics. Exact SQL matching is included only for a case with `expected_sql`. The runner's `evaluate(cases, generator)` function accepts any callable with the same simple interface, so future generators can be evaluated without changing the case format.

Run the existing offline A suite:

```powershell
$env:DEBUG = "false"
..\.venv\Scripts\python.exe -m pytest -q --basetemp artifacts\b0-pytest
```

The case file includes 22 synthetic scenarios using A's intent and filter-catalog dimensions, including rolling and baseline windows, `ANY`, multiple explanation codes, positive/negative filter mentions, contradictory filters, ambiguous dates, unsupported tables, missing filters, and write SQL. When the AML officer supplies examples, add JSONL rows with `source: "officer"`; do not change the runner or schema. Set `expected_sql` only when exact-match reporting is desired.

## Configuration

No new runtime settings or dependencies are introduced. The harness uses the repository's existing `sqlglot` dependency and standard-library JSON. `PYTHONPATH` selects A's package; `DEBUG=false` avoids collisions with unrelated ambient variables. The source-contract tests read the sibling `PioTech-AI` checkout but never modify it or import it into A's normal module namespace.
