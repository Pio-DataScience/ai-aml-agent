# P6 — Parameter catalog and confirmation card

## What changed

P6 adds a validated parameter catalog in `config/aml_catalogs/params.yaml`, pure gap detection in `web/domain/param_gaps.py`, and a server-owned pending-card flow. Requiredness, defaults, input types, options, validation, prompts, per-parameter status, and `required_if` rules come from the catalog. `required_if` accepts only the structured `{param, equals}` form and is evaluated by direct comparison; no expression is executed.

Intent analysis can flag ambiguity through the existing `Clarification` contract. The server builds card rows from the normalized intent and independently checks catalog-required values, so an LLM `ready_for_handoff: true` cannot clear a missing or ambiguous required value. Repeated thresholds appear as indexed rows with comparator, value, scope, and transaction type. `baseline_window` and `semantic_conditions` are displayed as read-only rows with a change-via-chat hint.

The UI card separates missing/ambiguous rows, assumed defaults, values extracted from the request, and read-only understood content. It uses typed controls and sends one `confirm_params` action for all selected values. The card remains in workflow state across reloads. Confirm/discard actions require `scenario.create`; the chat action message is sent only after a successful response.

Confirmation applies explicit values to the stored intent, records provenance, clears clarification questions, sets `ready_for_handoff`, and maintains `applied_defaults` as a list of strings. The confirmation action and `generate_scenario_execution_plan` share the deterministic `render_plan` function. Shadow and persistence tools ignore their model-supplied intent argument in favor of the confirmed revision intent and record an `intent_arg_mismatch` workflow event when the values differ.

Incomplete submissions return 422 `PARAMS_INCOMPLETE` with `missing[]` and `unresolved[]`. Out-of-range and all-time values return 422 `PARAMS_INVALID` with reasons. Refusals append `card_refused` workflow events; they do not write an audit row. `param_card_audit` is inserted once when a card resolves as `confirmed`, `auto_confirmed`, `discarded`, or `superseded`. It snapshots the shown rows, choices, user/role/source, shown and resolved timestamps, revision ID, and refusal-attempt count. Audit rows are not updated or evicted.

P6 creates the first revision. P6b now creates a new revision when a confirmed card changes parameters after a plan exists. It copies the confirmed card, revalidated edited governance metadata, and newly rendered plan; the previous trio remains in capped history. A planless current revision is still updated in place. See [P6b revision flow](P6b_revision_flow.md) for the atomic write, eviction, retry, and response behavior.

## API and configuration

- `POST /workflow/{project_id}/{chat_id}/{user_id}/actions/confirm_params`
  - Body: `request_id`, explicit `values` (each may include `value`, `choice`, and `provenance`), optional `accept_defaults`, and `source: ui | api`.
- `POST /workflow/{project_id}/{chat_id}/{user_id}/actions/discard_card`
  - Body: `request_id` and `source: ui | api`.
- `GET /workflow/{project_id}/{chat_id}/{user_id}/state`
  - Returns the durable `pending_card`, including its rows, request ID, and base revision.
- `/chat/stream` emits `parameter_card` after intent analysis; the regular `stage_changed` event refreshes workflow state.

`config/aml_catalogs/params.yaml` controls the card and ambiguity behavior, required values, editable controls, defaults, and validation. `config/aml_catalogs/policy.yaml` enables `workflow.enforcement.card_confirmation: enforce`; approvals remain enforced and conflict checks remain observed for P8.

## How to run and verify

From the Service A repository root in PowerShell:

```powershell
$env:DEBUG = "false"
.\.venv\Scripts\python.exe -m compileall -q services app.py run_alert_engine.py
.\.venv\Scripts\python.exe -m pytest --basetemp=.pytest-tmp services/aml_builder/tests/test_p6_param_gaps.py services/aml_builder/tests/test_p6_param_cards.py services/aml_builder/tests/test_config_loader.py services/aml_builder/tests/test_p5_workflow_actions.py -q
```

The tests use in-memory/fake LLM data and temporary SQLite repositories; they do not call Oracle, the LLM provider, or Service B. UI checks:

```powershell
npm run lint
npm run build
```

Manually verify that an incomplete analysis opens the card, required values gate confirmation, defaults remain editable, reload restores the card, discard and confirm clear it, and a successful confirmation shows the generated plan before the user presses **Proceed**.
