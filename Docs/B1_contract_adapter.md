# B1: Typed DWH SQL contract adapter

## What and why

B1 establishes `AMLIntent` as the typed boundary for DWH SQL generation. `ScenarioSqlRequest` carries the validated intent, catalog-resolved confirmed filters, and optional version/trace context. `ScenarioSqlResult` carries generated SQL, approval, execution evidence, validation findings, provider/model metadata, and trace identity. `HttpScenarioSqlGenerator` implements the asynchronous generator protocol while preserving the existing Service B SSE request envelope and SQL extraction fallback.

Contract checks for the Service B handoff are pure functions. They cover the required `CUS_NUM` projection, explanation-code requirements, rolling day windows, and non-overlapping day baselines. They are findings only at this stage; the existing shadow-test response and filter deployment gate remain authoritative, so HTTP behavior is unchanged.

Filter semantics follow Service A: only catalog-resolved rows with nonempty values are sent to Service B and verified in generated SQL. Unresolved/ambiguous/lookup-failed filter rows reject the typed request. Disabled dimensions are omitted. For `ALL`/`ANY` transaction scope, Service A intentionally excludes the explanation-code dimension from its confirmed-filter check; the contract check also does not require an EXPL_CODE predicate when there are no explicit transaction types or codes.

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
.\.venv\Scripts\python.exe -m pytest -q services\aml_builder\tests\test_b1_dwh_contract.py
```

The command validates AMLIntent variants, malformed and unready requests, confirmed-filter selection, strict extra-field rejection, SQL contract findings, and result evidence. It does not call Oracle, Service B, an LLM, or embedding services.

## Configuration

The adapter uses the existing `PIOTECH_AI_URL`, `PIOTECH_AI_TIMEOUT_SECONDS`, `PIOTECH_AI_USER_ID`, and `PIOTECH_AI_PROJECT_ID` settings. B1 adds no settings or HTTP routes.
