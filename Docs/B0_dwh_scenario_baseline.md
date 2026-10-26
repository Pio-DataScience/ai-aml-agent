# B0 DWH Scenario Baseline

B0 adds an offline safety net before moving Service B into Service A. It does not change application behavior. Tests use deterministic fake adapters; Service B's dependency-light handoff contract is loaded directly from its source file, and SQL execution/SSE approval boundaries are inspected without importing its heavy `torch` API module. BL1–BL10 strict xfails are executable source assertions for the verified legacy defects, not unconditional failures. When each assigned B# implementation lands in A, switch that row from the legacy source probe to a behavioral test of the new adapter; keep Service B read-only.

The prior catalog failures were stale expectations, not a catalog defect: `config/aml_catalogs/policy.yaml` sets `workflow.enforcement.conflict_check: enforce` and `conflict_check_required: true`; `WorkflowEnforcement.conflict_check` accepts both `observe` and `enforce`; and deploy consumes the catalog's required flag. Both the catalog-loader and P5 workflow test now expect the configured `enforce` value. These changes align assertions with current policy without changing runtime behavior.

## Run

From the repository root in PowerShell:

```powershell
$env:DEBUG = "false"
$env:PYTHONPATH = "services\aml_builder"
.\.venv\Scripts\python.exe -m pytest -q services\aml_builder\tests\characterization\dwh_scenario
```

This runs fake-backed routing and handoff characterization checks. It needs no Oracle, LLM, Chroma, Neo4j, or `torch` installation. If the sibling `PioTech-AI` checkout is absent, source-contract characterization tests skip; the remaining cases still run.

Run the structural evaluation harness:

```powershell
$env:PYTHONPATH = "services\aml_builder"
.\.venv\Scripts\python.exe -m tests.eval.dwh_scenario.runner
```

It reads `cases.jsonl`, calls the deterministic stub generator, and prints per-case parse, SELECT-only, projection, filter, safety, fake execution, latency, LLM/tool-call, and token metrics. Exact SQL matching is included only for a case with `expected_sql`. The runner's `evaluate(cases, generator)` function accepts any callable with the same simple interface, so future generators can be evaluated without changing the case format.

Run the existing offline A suite:

```powershell
$env:DEBUG = "false"
.\.venv\Scripts\python.exe -m pytest -q --basetemp artifacts\b0-pytest
```

The case file includes 22 synthetic scenarios using A's intent and filter-catalog dimensions, including rolling and baseline windows, `ANY`, multiple explanation codes, positive/negative filter mentions, contradictory filters, ambiguous dates, unsupported tables, missing filters, and write SQL. When the AML officer supplies examples, add JSONL rows with `source: "officer"`; do not change the runner or schema. Set `expected_sql` only when exact-match reporting is desired. The B0 routing fixtures assert the required route sequences; an actual migrated graph is not present until B5, so those fixtures become end-to-end graph tests there.

## Configuration

No new runtime settings or dependencies are introduced. The harness uses the repository's existing `sqlglot` dependency and standard-library JSON. `PYTHONPATH` selects A's package; `DEBUG=false` avoids collisions with unrelated ambient variables. The source-contract tests read the sibling `PioTech-AI` checkout but never modify it or import it into A's normal module namespace.

## Lifecycle stall diagnosis and full-suite result

The lifespan test was not reaching application startup. Python 3.14.7 on Windows selected `ProactorEventLoop`; `asyncio.run()` blocked while `_make_self_pipe()` called `socket.socketpair()`, whose `_fallback_socketpair()` was stuck in local TCP `accept()`. A direct `asyncio.new_event_loop()` probe and pytest-asyncio's event-loop fixture in `test_blocking_work_runs_on_a_worker_thread` showed the same stack. This occurs inside the restricted sandbox, before the lifespan or its mocks execute. Running the lifespan test outside the sandbox passed (`1 passed in 2.32s`), and the full suite's async tests then ran. The test and lifecycle implementation are present in the parent commit, so the stall predates B0. No application or test bypass was needed.

The bounded full run outside the sandbox completed in 411.19 seconds: **285 passed, 10 xfailed, 5 failed**. The remaining failures were `test_history_endpoint_decodes_synthetic_tool_envelope`, `test_legacy_normalization_matches_frozen_golden_fixtures`, `test_pre_model_hook_keeps_existing_tool_flow_available_in_observe_mode`, `test_confirmation_snapshots_audit_once_and_applies_confirmed_intent`, and `test_two_tabs_confirming_different_values_have_one_typed_conflict`. Their test files and related implementation were unchanged from the parent commit; these are pre-existing suite failures, not B0 regressions. The config-loader and P5 `conflict_check` expectations were corrected to match the verified `enforce` catalog value.
