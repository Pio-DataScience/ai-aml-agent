# B3: Oracle read repositories and SQL policy

## What changed

Candidate SQL and DWH dictionary reads now use a repository over Service A's initialized Oracle pool. Candidate SQL is parsed with SQLGlot's Oracle dialect before database access. The policy requires one SELECT statement (CTEs are supported), rejects comments, writes, locking reads, missing binds, out-of-allowlist objects, missing `CUS_NUM`, and confirmed filters whose literal or bound values do not match the confirmed request.

Candidate reads fetch at most `DWH_SQL_MAX_CANDIDATE_ROWS + 1` rows to compute an explicit `truncated` flag. `row_count` is the number of returned sample rows and makes no claim about the total result size. Oracle call timeout is bounded by both the configured read timeout and remaining request deadline. A shared `OracleRequestBudget` limits statement count and elapsed wall time. The exact SQL string validated is passed unchanged to `cursor.execute`.

The repository exposes bounded reads for dictionary tables, columns, join metadata, business-code lookup metadata and rows, plus a sampled column-value probe. Dynamic identifiers used by the latter reads are validated as Oracle identifiers and remain subject to the same configured schema/object policy. It has no Oracle write method and does not include explain-plan code.

## Configuration

- `DWH_SQL_ALLOWED_SCHEMAS`: Oracle schemas candidate reads may reference, configured as a JSON array (for example `["BI_DWH"]`). This must be explicitly configured before candidate SQL can run.
- `DWH_SQL_ALLOWED_OBJECTS`: optional tighter object allowlist, using `SCHEMA.OBJECT` or object names.
- `DWH_SQL_MAX_CANDIDATE_ROWS`: maximum returned sample rows (default `100`).
- `DWH_SQL_READ_TIMEOUT_SECONDS`: per-call timeout upper bound (default `120`).
- `DWH_SQL_DEADLINE_SECONDS`: per-request wall-time budget (default `180`).

The OWNER/DBA must confirm schema ownership and read grants before configuring these values. Do not use the application to probe a live environment as part of this package.

## Offline verification

```powershell
.\.venv\Scripts\python.exe -m pytest -q services\aml_builder\tests\test_b3_oracle_sql_policy.py
```

Tests use fake connections only. They cover unsafe statements, multi-statement input, comments, CTEs, quoted identifiers and aliases, literals, binds, confirmed filters, Oracle failures, statement budgets, truncation, and successful empty results.

## B1 characterization limitation on Windows sandbox

**VERIFIED:** The bounded verbose B1 run stalls in `test_http_adapter_preserves_existing_sse_payload_and_extracts_final_sql` at `services/aml_builder/tests/test_b1_dwh_contract.py:337`. With `faulthandler_timeout=5` and `faulthandler_exit_on_timeout=true`, the traceback stops in Python 3.14 Windows Proactor loop initialization: `asyncio.run()` → `new_event_loop()` → `_make_self_pipe()` → `_fallback_socketpair()` → `socket.accept()`. The test body and fake HTTP adapter are not reached. The same test file is unchanged from the B2 base, and B3 does not modify the adapter or this test. This matches the repository's previously documented sandbox restriction on asyncio socketpair creation; it is not a B3 regression.

The bounded reruns passed: B1 synchronous cases `24 passed, 5 deselected`; B0 characterization `28 passed, 6 xfailed`; B0 evaluation `2 passed`; B2 provider factory `6 passed`; B3 tests `24 passed`. The five deselected B1 tests all exercise the HTTP adapter via `asyncio.run()` and share the same loop-creation limitation in this environment. The exact failing test can be diagnosed without an indefinite wait using:

```powershell
.\.venv\Scripts\python.exe -m pytest -vv -s --tb=short -o faulthandler_timeout=5 -o faulthandler_exit_on_timeout=true services\aml_builder\tests\test_b1_dwh_contract.py -k test_http_adapter_preserves_existing_sse_payload_and_extracts_final_sql
```

The expected bounded result is a timeout traceback at `_fallback_socketpair`, not a normal test assertion failure. Do not weaken or delete the characterization tests; run them in an environment where the Windows event-loop socketpair can initialize.

## Rollback

The B3 repository is not registered as the default SQL generator. Keep the HTTP adapter as the active path. To roll back B3 code, revert this package commit; no database state is changed by B3.
