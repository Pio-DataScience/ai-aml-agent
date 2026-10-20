# P9 — Scenario versioning flows

P9 adds controlled version changes to the agent-owned Oracle registry. A deployed
scenario can seed a new parameter-confirmation chat, a superseded version can be
reactivated through rollback, and an active version can be retired without deleting
rows. Each Oracle status transition updates its matching `PIO_AML_AGENT_SCENARIOS`
`ACTIVE_FLAG` and appends audit data in the same transaction.

## How to run and verify

From the Service A repository root, with the virtual environment installed:

```powershell
$env:DEBUG = "false"
.\.venv\Scripts\python.exe -m compileall -q services app.py run_alert_engine.py
.\.venv\Scripts\python.exe -m pytest -q --basetemp=.pytest-p9 services/aml_builder/tests/test_p9_versioning.py services/aml_builder/tests/test_p9_registry_queries.py services/aml_builder/tests/test_p4_deploy_service.py services/aml_builder/tests/test_p4_oracle_repo.py services/aml_builder/tests/test_p4_deploy_route.py
```

These tests use fakes and local SQLite. They do not call Oracle, an LLM, or Service B.
The UI checks run from `AI_AML_INTERFACE`:

```powershell
npm run lint
npm run build
```

## Create a new version

`POST /workflow/{project}/{chat}/{user}/actions/create_new_version` accepts
`{"scenario_id": "..."}` and requires `scenario.revise`. The target must be the
latest version for its stable key and have status `ACTIVE` or `RETIRED`. The server
returns `new_chat_id`, `parent_scenario_id`, `parent_version`, `scenario_key`, and
`reserved_version`.

The server seeds the chat from the selected Oracle row's `PARAMS_JSON`,
`FILTERS_JSON`, `METADATA_JSON`, and `SIGNATURE_JSON`. It does not copy the source
chat's local workflow. Missing required intent or metadata fields return
`VERSION_SOURCE_INCOMPLETE` with the missing field names. The chat starts at
`PARAMS_PENDING`; it receives a pending card and uses the normal plan, approval,
shadow-test, and deploy steps. SQL is generated again through the normal shadow flow.
Repeated requests by the same user for the same parent return their existing open
child chat.

The new child has the same `SCENARIO_KEY`, a reserved next version ID, and a parent
link. At deploy, a 5–500 character change reason is required. An active parent is
superseded before the child insert in the same Oracle transaction, freeing the active
signature index. A retired parent is not superseded again. If the parent is no longer
the latest eligible version, deployment returns `REVISE_STALE`. A new version with
the same signature and metadata hashes as its parent returns `IDENTICAL_TO_PARENT`.
The same signature with changed metadata is allowed and its receipt records
`change_scope: metadata`.

## Rollback and retirement

`rollback_version` requires `scenario.rollback`, a 5–500 character reason, the
target `scenario_id`, and `expected_active_version`. The target must be `SUPERSEDED`
under the same stable key. A mismatch returns `VERSION_STALE`. Before changing rows,
the service runs the P8 signature conflict check against other active scenarios.
Blocking matches refuse rollback; warning matches require an explicit acknowledgement
and are recorded with the supplied reason. The target is reactivated and the current
active version is superseded in one transaction, with both production and metadata
status rows and audit entries updated together.

`retire_version` requires `scenario.delete` and a 5–500 character reason. It only
accepts `ACTIVE`, writes `RETIRED`/`IS_ACTIVE=0` and metadata `ACTIVE_FLAG='0'`, and
adds its audit row in that transaction. There is no un-retire action. A later version
can be created from a retired latest version. Superseded and retired versions stop
running in the alert engine; existing alert records remain available.

Every status update includes an exact `SCENARIO_ID` plus expected `STATUS` predicate
and checks for exactly one changed production row and one matching metadata row. A
stale row-count result raises `VERSION_STALE` and rolls back the transaction. The P4
active-signature unique index remains the final duplicate guard.

## Version history and registry queries

`GET /workflow/{project}/{chat}/{user}/versions` requires chat ownership (`chat.read`)
and is available only from the latest version's locked chat. It includes every version
for the key, newest first, with creator/time and last status-change details from the
Oracle audit table. `OVERRIDE_CONFLICT` events are summarized as a count.

The read-only `query_production_scenario_registry` tool also accepts `list_versions`
and `compare_versions`. Comparison is a deterministic path-based diff of stored
signature, parameter, filter, and metadata JSON; it does not call an LLM.

## Configuration

The role/action matrix remains in `config/aml_catalogs/policy.yaml`:

- `viewer`: `chat.read`
- `creator`: `chat.read`, `scenario.create`
- `manager`: read, create, revise, rollback, and delete/retire
- `reason_required_for`: revise, rollback, and retire

The UI collects a version change reason only on version chats, and collects rollback
or retirement reasons when those actions are requested. All operations are authorized
again by Service A; UI permission state is only a presentation aid.
