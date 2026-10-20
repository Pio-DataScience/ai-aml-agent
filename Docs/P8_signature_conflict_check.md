# P8: Signature conflict check

P8 adds deterministic conflict detection across the Service A workflow, registry, and scenario panel. The comparator operates on canonical signatures; it does not ask the LLM to decide whether scenarios conflict or to produce similarity percentages or diff text.

## Workflow

- During intent extraction, a provisional report may be emitted after the comparable signature core is resolved. It is informational, is not saved on the revision, and never gates progress.
- Confirming parameters runs the authoritative registry check. An `IDENTICAL` result returns `CONFLICT_BLOCKED` and leaves the card pending. Warning relations create a `PARAMS_CONFIRMED` revision with `conflict_json.status=needs_ack`; the plan is rendered only after `acknowledge_conflicts`.
- `acknowledge_conflicts` requires `scenario.create` and accepts `{revision_id, conflict_hash, choice}`. Acknowledgement writes a `conflict_acknowledged` SQLite `workflow_events` row. It does not write Oracle.
- Deployment rechecks ACTIVE registry scenarios. A new blocking result returns `CONFLICT_BLOCKED`. New or worsened warnings return `CONFLICT_ACK_REQUIRED` while preserving `METADATA_READY`. Same-or-fewer warning matches remain covered by the previous acknowledgements. A unique-index violation remains the final race-condition backstop.
- An unavailable registry allows confirmation with an unavailable report and warning; deployment refuses with `CONFLICT_CHECK_UNAVAILABLE` when required by policy.
- On successful deployment, the receipt contains acknowledgement details and each acknowledged match adds one `OVERRIDE_CONFLICT` row to `PIO_AML_AGENT_SCENARIO_AUDIT` in the same transaction as deployment. Fields include actor, role, time, revision, other scenario, relation, and optional reason.

## Comparator and reports

Only ACTIVE scenarios are compared. The same workflow thread and same `SCENARIO_KEY` are excluded, except an identical parent copy remains blocked as a no-op. `SAME_CHAT_REDEPLOY` is handled by deployment locking/idempotency and is not a comparator relation.

Comparison normalizes scenario type case, but different types are not compared. Threshold ranges preserve open and closed ends; transaction scope uses set containment (`all` contains any specific set); missing filters mean all values, and negated filters are modeled as excluded sets. Window mismatches use `conflict_check.window_mismatch_relation` (default `RELATED`). `IDENTICAL` blocks, warning relations require acknowledgement, and `RELATED`/`SIMILAR` are informational by default. Semantic similarity is a fallback only when a registry row has no usable signature.

The `conflict_report` SSE event and workflow state carry revision id, provisional flag, status, conflict hash, blocking/ack flags, and matches with scenario id/name/version, relation, similarity percentage, deterministic dimension diff, and action. The registry has no scenario-detail read endpoint for this panel; View scenario therefore displays the ID and diff only.

## Configuration

`config/aml_catalogs/policy.yaml` validates relation actions, `conflict_check.on_unavailable`, `window_mismatch_relation`, and deterministic diff templates. The enforced defaults are:

```yaml
workflow:
  enforcement:
    conflict_check: enforce
  conflict_check_required: true
conflict_check:
  on_unavailable: block
  window_mismatch_relation: RELATED
```

## Run and verify

From the Service A repository root:

```powershell
$env:DEBUG = "false"
.\.venv\Scripts\python.exe -m compileall -q services app.py run_alert_engine.py
.\.venv\Scripts\python.exe -m pytest services/aml_builder/tests/test_p8_signature_compare.py services/aml_builder/tests/test_p8_conflict_workflow.py services/aml_builder/tests/test_p8_deploy_conflicts.py services/aml_builder/tests/test_p8_override_audit.py -q
```

The tests use fake registry and workflow repositories. They do not call Oracle, the LLM, or Service B.
