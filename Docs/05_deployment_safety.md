# Deployment Safety and Approval Integrity

## What changed

Both deployment routes now pass through one server-side guard before Oracle
persistence. The guard reconstructs the current artifact from LangGraph tool
messages and requires:

- the latest shadow test to have `validation_success: true`;
- the intent and SQL to match that exact shadow-test invocation and result;
- no later intent revision;
- definitive governance metadata validation;
- an explicit later deployment instruction for the chat tool, or the direct
  `POST /scenario/deploy` action;
- a SHA-256 artifact identity covering intent, SQL, and metadata.

`save_production_scenario` requires this evidence as a keyword-only argument,
so neither public route nor an accidental internal caller can bypass the guard.
The direct route now reads only the exact composite checkpoint thread and no
longer searches fallback thread identifiers.

## Concurrency and retry behavior

The SQLite checkpoint database now contains `deployment_artifacts`. An atomic
`IN_PROGRESS` claim prevents two requests from deploying the same artifact.
Successful Oracle writes mark the claim `DEPLOYED`. A failure before Oracle
commit releases the claim, allowing an intentional retry. If Oracle commits but
ledger finalization fails, the claim remains in progress so the system fails
closed rather than creating a duplicate scenario.

## Verification

Run the offline suite from the repository root:

```powershell
$env:DEBUG = "false"
.\.venv\Scripts\python.exe -m pytest -q services\aml_builder\tests\test_deployment_guard.py
```

Run structural verification:

```powershell
$env:DEBUG = "false"
.\.venv\Scripts\python.exe -m compileall -q services app.py run_alert_engine.py
.\.venv\Scripts\python.exe -m pytest --collect-only -q
```

No new environment variables are required. The ledger uses
`CHECKPOINT_DB_PATH` and is initialized at startup and lazily before claims.

## Compatibility

The chat and SSE response shapes are unchanged. The direct deployment request
shape is unchanged. Existing valid sessions continue to deploy when their exact
composite thread contains current successful shadow evidence. Legacy sessions
that only resolve through fallback thread IDs must be shadow-tested again; this
is intentional to avoid cross-user or cross-project artifact recovery.

## Rollback

Revert the deployment guard integration in `tools.py`, `deploy.py`, and
`production_registry.py`, then remove `deployment_guard.py`. The additive
`deployment_artifacts` SQLite table can remain unused safely or be removed in a
controlled maintenance window after confirming no deployment is in progress.
No Oracle schema rollback is required.
