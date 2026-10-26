# B1: Typed DWH SQL contract adapter

## What and why

B1 establishes `AMLIntent` as the typed boundary for DWH SQL generation. `ScenarioSqlRequest` carries the validated intent, catalog-resolved confirmed filters, and optional version/trace context. `ScenarioSqlResult` carries generated SQL, approval, execution evidence, validation findings, provider/model metadata, and trace identity. `HttpScenarioSqlGenerator` implements the asynchronous generator protocol while preserving the existing Service B SSE request envelope and SQL extraction fallback.

Contract checks for the Service B handoff are pure functions. They cover the required `CUS_NUM` projection, explanation-code requirements, rolling day windows, and non-overlapping day baselines. They are findings only at this stage; the existing shadow-test response and filter deployment gate remain authoritative, so HTTP behavior is unchanged.

Filter semantics follow Service A: only catalog-resolved rows with nonempty values are sent to Service B and verified in generated SQL. Unresolved/ambiguous/lookup-failed filter rows reject the typed request. Disabled dimensions are omitted. The strict filter model includes Service A's card audit fields (`audit_choice` and `acknowledged_without_filter`) so confirmed rows retain their current wire representation. For `ALL`/`ANY` transaction scope, Service A intentionally excludes the explanation-code dimension from its confirmed-filter check; the contract check also does not require an EXPL_CODE predicate when there are no explicit transaction types or codes.

## Service A and Service B contract differences

- Service B's `ready_for_handoff` defaults to `true` and its intent models allow extras. Service A's typed request rejects unknown root/nested intent fields and rejects clarification state that is not ready.
- Service B checks transaction-code requirements with regular expressions. Service A independently verifies confirmed filter predicates with SQLGlot, checks target table/column, value set, and polarity, and skips the explanation-code dimension for `ALL`/`ANY`. These checks have different scopes: Service B's explicit transaction type can require an `EXPL_CODE` predicate even when no explanation codes were supplied, while Service A verifies only catalog-resolved filters.
- Service B rejects an `EXPL_CODE` restriction for an all/no-type intent. Service A's confirmed-filter checker skips the explanation-code filter for `ALL`/`ANY`; it does not itself reject an unrelated generated `EXPL_CODE` predicate. The B check is retained as a finding and does not change the current HTTP response/gating behavior.
- Service B's day-window checks are regex-based and cover rolling day windows and isolated day baselines only. Other units and non-rolling windows are not checked there. The B1 findings preserve this limited scope; Service A's existing workflow and window rendering remain unchanged.
- Service B requires `CUS_NUM` from execution column metadata. B1 checks the final SQL projection statically because this adapter does not execute candidate SQL; the existing Service A shadow count remains the only HTTP-side execution.

## Offline verification

Run from the repository root:

```powershell
$env:DEBUG = "false"
$env:PYTHONPATH = "services\aml_builder"
.\.venv\Scripts\python.exe -m pytest -q --tb=short services\aml_builder\tests\test_b1_dwh_contract.py services\aml_builder\tests\characterization\dwh_scenario services\aml_builder\tests\test_p3_workflow_guards.py services\aml_builder\tests\test_p4_deploy_service.py
```

The command validates AMLIntent variants, malformed and unready requests, confirmed-filter selection, strict extra-field rejection, SQL contract findings, and result evidence. It does not call Oracle, Service B, an LLM, or embedding services.

## Configuration

The adapter uses the existing `PIOTECH_AI_URL`, `PIOTECH_AI_TIMEOUT_SECONDS`, `PIOTECH_AI_USER_ID`, and `PIOTECH_AI_PROJECT_ID` settings. B1 adds no settings or HTTP routes.

## B0 compatibility audit

Compared with B0's `execute_oracle_dwh_shadow_test` implementation:

- The body remains `messages` with one user message containing the same `AML_SCENARIO_GENERATION` JSON, `metadata` with user/project/random chat ID, and `reasoning_mode: instant`. The adapter keeps the configured URL and timeout and the same Accept, Content-Type, request-ID, and correlation-ID headers.
- SSE handling retains `final_answer` precedence, concatenated `content` fallback when no SQL extracts from the final answer, `[DONE]` termination, ignored non-data/empty lines, and ignored malformed JSON events.
- `httpx.HTTPError` retains the existing `PioTech AI HTTP error: ...` result; missing SQL retains the existing error text. Task cancellation is not caught or converted.
- Extracted SQL is returned without a further rewrite. The tool still checks the same confirmed filters, wraps that exact string as `SELECT COUNT(*) FROM (<sql>) shadow_query`, and returns it as `raw_sql` in success and shadow-failure payloads.
- The workflow guard still requires `PLAN_APPROVED` before the shadow tool; result approval and deployment remain separate later workflow stages. The adapter's `approved` metadata records whether the SQL came from `final_answer`; for B0 compatibility the tool still accepts the established content-event SQL fallback even when that metadata is false.

The fake SSE regression tests cover the payload against B0's `build_service_b_intent` transformation, timeout and headers, event precedence/fallback, malformed events, end marker, HTTP and empty-SQL errors, cancellation propagation, and extracted SQL identity. Existing P3 workflow-guard, P4 deployment-guard, and B0 Service B characterization tests are run with the offline verification command below.
