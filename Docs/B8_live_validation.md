# B8 controlled live acceptance validation

## Current state

The bounded Oracle result-parity runner is implemented and covered by offline
fakes. The approved Stage 1 metadata discovery did open the existing acceptance
connection and completed its metadata-only SELECT manifest. A separate,
explicitly approved synthetic A/B generation-only run also completed; it did not
execute scenario SQL, retrieve customer rows, or call critics. `DWH_SQL_MODE=http`
remains unchanged. Neither workflow approves, persists, deploys, or activates SQL.

The existing shadow Oracle pool is deliberately not reused: its initializer and
connection getter fall back to the primary pool when shadow settings are absent.
The B8 acceptance path instead requires a separate `DWH_SQL_ACCEPTANCE_*`
credential set and creates a one-connection pool only when explicitly called.
There is no fallback to `ORACLE_*` or `SHADOW_ORACLE_*`.

## Scope and hard limits

`AcceptancePlan` approval covers the scenario request and confirmed filters,
HTTP endpoint, provider route description, approved provider-data reference,
schema/object allowlists, DBA Resource Manager plan, and every limit. Any
change changes the scope SHA-256 and invalidates approval.

Hard ceilings:

| Limit | Default | Absolute ceiling |
| --- | ---: | ---: |
| Scenarios | 2 | 3 |
| Objects | 4 | 8 |
| Distinct customers returned per query | 100 | 250 |
| Oracle statement timeout | 10 seconds | 10 seconds |
| Generator timeout | 45 seconds | 60 seconds |
| Overall run timeout | 120 seconds | 180 seconds |
| Total statements including preflight and both generators | 24 | 32 |

The plan rejects unqualified objects, missing allowlists, scenarios without
confirmed catalog filters, non-rolling windows, and rolling windows longer than
365 days. Each candidate must pass A's Oracle AST policy and AML temporal/code
checks before it can execute. Candidate literals are converted to bind
variables. Execution selects distinct `CUS_NUM` values through a server-side
`ROWNUM` cap and fetches at most one additional row to detect truncation.
Truncated sets block parity.

The preflight performs read-only queries for the actual session user, active
system privileges, enabled roles and their object/column grants, object and
column metadata, object grants, SELECT access, and the active
`V$PARAMETER.resource_manager_plan`. It fails closed unless the user
matches the dedicated account, no write/broad privileges are present, every
allowlisted object exists and is readable, and the active plan matches the
configured DBA-approved plan. Each call has a driver timeout. The SQL allowlist,
bounded temporal predicates, row cap, and required Resource Manager plan work
together to limit query risk; no `EXPLAIN PLAN` path writes to `PLAN_TABLE`.

## Review before authorizing live execution

The proposed initial test scope is one synthetic 30-day cash-deposit case with
confirmed explanation code `4`. `BI_DWH.PIO_TRANSACTIONS` is only a candidate
object name from the offline fixture; the dictionary preflight must verify it.
The proposed initial limits are:

- 1 scenario; explicit `BI_DWH` schema and DBA-confirmed object allowlist.
- 100 distinct customer rows per candidate; a truncated result blocks parity.
- 10 seconds per Oracle statement; 45 seconds per generator; 120 seconds total.
- 16 total Oracle statements, including preflight and local repair attempts.
- The exact HTTP endpoint, local generator/critic/embedding providers, and
  provider-data approval reference are recorded in the plan.
- A DBA-provided Resource Manager plan name is mandatory.

These are a proposal for review, not authorization. Provider-data approval and
DBA account/resource-plan confirmation remain separate owner gates.

## API and activation boundary

The offline suite exercises `bind_sql_literals`, `run_preflight`,
`AcceptanceOracleRepository`, actual dictionary column checks, set comparison,
approval gating and scope hashing.

After the owner approves the exact `AcceptancePlan.scope_summary()` and limits,
an operator may explicitly:

1. Set `DWH_SQL_ACCEPTANCE_ENABLED=true`, dedicated acceptance DSN/user/password,
   the DBA-approved Resource Manager plan, and strict DWH allowlists.
2. Create the dedicated pool with `create_acceptance_pool_provider()`. Do not
   use the primary or shadow Oracle provider.
3. Construct `LiveValidationApproval` using the approved scope hash, approver,
   timestamp and provider-data approval reference.
4. Call `run_live_validation()` with the existing HTTP adapter and a local
   factory that injects the supplied `AcceptanceOracleRepository` into
   `build_local_generator(config, repository=repository)`.
5. Save `serialize_acceptance_report(report)` to the approved audit location;
   close the returned pool after the run.

The runner refuses execution without a matching approval digest, enabled
dedicated account, matching endpoint and resource plan, configured allowlists,
and an acceptance-pool provider marker. It also checks that `DWH_SQL_MODE` is
still `http`. Reports contain SQL hashes, safe bind values (sensitive-column
binds are masked), query counts, truncation, timings, customer-set equality and
symmetric-difference counts. Customer identifiers and result rows are never
included in the report or emitted by this module to logs.

## Stage 1 discovery plan

`SchemaDiscoveryPlan` records a BI_DWH-only metadata scope with 11 exact SELECT
statements and a maximum 12-statement budget. The scope hash covers statements,
conditional mapping gates, binds, limits, expected user, and object list; a DBA
Resource Manager plan name is not required. After the live `ORA-00904` finding,
the privilege-view SQL was corrected and the approved review digest is
`3b62f3ecccac746ee3ee4debc213f47c026d5c7a9c469a5c8ae73879cdf20cd4`.
For Stage 1, database system and scoped object write privileges are recorded
as security warnings; the fixed SELECT-only manifest remains enforced. This
policy is included in the approval digest.
`V$PARAMETER` is optional. If inaccessible, the report requests DBA
confirmation. Object and relevant-column inventories run before mappings;
missing objects/columns are reported and dependent mapping queries are skipped.
The dictionary mapping is marked preliminary. No transaction or customer rows
are selected. `WHERE 1=0` is not used as proof of effective read access; grants
are inventoried separately. The Stage 1 runner uses the existing acceptance
pool and requires matching scope approval. The previous approved run completed
identity, system-privilege, and enabled-role SELECTs, then failed on the fourth
SELECT against `ALL_TAB_PRIVS` with `ORA-00904`. No object/column discovery or
mapping query ran. Oracle documents `TABLE_SCHEMA` for both privilege views;
the corrected SQL aliases it to `OWNER` to preserve the report's positional
shape. The revised digest was approved and all 11 statements completed. The
discovery status was PASS. Compatibility was initially reported BLOCK because
the inventory treated alternate names as simultaneous requirements; the
semantic reassessment below now passes against the generator's actual contract.
All seven scoped objects existed. The preliminary dictionary query
returned no rows; the EXPL_CODE=4 query returned one mapping. No synonym rows
were returned. `V$PARAMETER` returned `None`; this does not verify DBA resource
directives or limits, so DBA confirmation remains required. The account's
elevated system privileges were recorded as a warning; no scoped object or
column write grants were observed. The run used 11/12 statements and completed
in 3,888.406 ms. No scenario SQL or customer/transaction rows were selected.

The remaining dictionary projections were checked against Oracle Database
Reference: `ALL_OBJECTS` (`OWNER`, `OBJECT_NAME`, `OBJECT_TYPE`, `STATUS`),
`ALL_SYNONYMS` (`OWNER`, `SYNONYM_NAME`, `TABLE_OWNER`, `TABLE_NAME`,
`DB_LINK`), and `ALL_TAB_COLUMNS` (`OWNER`, `TABLE_NAME`, `COLUMN_NAME`,
`DATA_TYPE`, `DATA_LENGTH`, `NULLABLE`, `COLUMN_ID`). Session views use
`SESSION_PRIVS.PRIVILEGE` and `SESSION_ROLES.ROLE`; optional `V$PARAMETER`
uses `NAME` and `VALUE`. BI_DWH mapping columns remain verified dynamically
before conditional mapping statements are run.

### Semantic compatibility reassessment

The local generator's actual contract requires the final `CUS_NUM` projection;
for rolling day windows, `validate_generated_sql` accepts a date-named field
(`*_DATE` or `DATE_*`), while live dictionary validation ensures referenced
columns exist. Explicit transaction codes require `EXPL_CODE`. Reassessment
requires actual `CUS_NUM` with a scalar identifier type, an accepted date-named
column with Oracle `DATE`/`TIMESTAMP` type, and `EXPL_CODE` with a scalar code
type. Amount columns are scenario-dependent; numeric candidates are recorded
but not required unless the confirmed AML request uses an amount threshold.
Missing alternative aliases remain inventory diagnostics and do not by
themselves block compatibility. The Stage 1 evidence resolves `CUS_NUM`
(`VARCHAR2`), `TRA_DATE` (`DATE`), and `EXPL_CODE` (`VARCHAR2`); amount
candidates `TRA_AMT` and `EQU_TRA_AMT` are both `NUMBER`. Compatibility is
therefore PASS without rerunning discovery.

The preliminary query filters on `d.TABLE_NAME='PIO_TRANSACTIONS'` and
`d.COLUMN_NAME='EXPL_CODE'`; its join to `PIO_AML_COLUMNS` is a LEFT JOIN.
Therefore zero returned rows means no exact matching row was found in
`PIO_BANKBI_DICTIONARY_COL`; absence of an AML-column join match alone cannot
produce zero rows. This mapping is not on Service A local generation's schema
retrieval path: table/column candidates come from the paired Chroma collections,
and SQL policy validates referenced actual columns. The empty mapping may
remove optional lookup/join enrichment. A direct `EXPL_CODE='4'` filter needs
no such join, and the separate explanation-code lookup did return one mapping.
Oracle-result behavior remains to be evaluated in a separately authorized execution stage.

### Oracle result-parity stage (not executed)

The next execution comparison should reuse the already-reviewed synthetic “Cash deposits over 14 days” scenario and confirmed `PIO_TRANSACTIONS.EXPL_CODE = '4'`,
one allowed object (`BI_DWH.PIO_TRANSACTIONS`), the required `CUS_NUM` projection,
100-row result cap, 10-second Oracle statement timeout, 45-second per-generator
timeout, 120-second overall timeout, and 12-statement shared budget. Run HTTP
and local generators only after the strict preflight, provider-data approval,
and all configuration gates pass; validate both SQL candidates independently
and compare actual customer sets and execution evidence. The generation-only
approval does not authorize Oracle execution; separate scope authorization
and verified DBA controls are required.

Current Stage 2 blockers: the configured acceptance identity is `BI_DWH`; Stage 1
observed broad privileges and the `DBA` role, so strict execution preflight
blocks. Oracle reported `resource_manager_plan=None`; database-side resource
controls need DBA confirmation. The prior provider approval covered only the
approved synthetic generation payload and does not authorize scenario execution
or production use. Local indexing/retrieval is now available; Neo4j remains
disabled and is not required by the current configuration.
The Stage 1 credential warning policy does not change `run_preflight` for
scenario acceptance: that later workflow continues to block on elevated system
privileges and scoped write grants. Stage 1 evidence distinguishes the account's
capabilities from the SELECT-only operations it actually performs.

## Offline verification

Run with no Oracle, HTTP, LLM, Chroma or Neo4j connection:

    $env:DEBUG = "false"
    $env:PYTHONPATH = "services\aml_builder"
    .\.venv\Scripts\python.exe -m pytest -q services\aml_builder\tests\test_b8_live_validation.py

This command uses fake cursors/providers only. Do not create the dedicated pool
or call the live runner until the exact scope and limits above are approved.
