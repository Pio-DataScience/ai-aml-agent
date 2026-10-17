# P1 — Typed envelope and domain extraction

## What and why

P1 gives every agent tool result one versioned `ToolResult` envelope with a
status, optional error code, and a tool-specific payload. History reconstruction,
SSE mapping, and deployment recovery decode through the shared codec. Messages
without `schema_version` remain readable as legacy payloads.

Pure intent normalization, explanation-code binding, SQL extraction, metadata
rules, observation-window conversion, and hashing now live under
`services/aml_builder/web/domain/`. Oracle lookup and persistence operations
remain in the service and adapter code. The plan renderer remains in
`web/services/plan_renderer.py`; the plan tool is its only caller and the intent
tool no longer renders a plan.

Injected intent defaults record `provenance: "default"`. Scalar field provenance
is recorded in `field_provenance`, and `applied_defaults` remains a `list[str]`
with entries such as `time_window.unit: DAYS`. `BaselineWindow` now supports a
provenance value.

SQL hashes ignore comments, whitespace, keyword case outside single-quoted
literals, and a trailing semicolon. `stable_hash` hashes sorted-key compact JSON
encoded as UTF-8 with SHA-256.

## Run and verify

From the repository root in PowerShell:

```powershell
$env:DEBUG = "false"
$env:PYTHONPATH = "services/aml_builder"
.\.venv\Scripts\python.exe -m compileall -q services app.py run_alert_engine.py
.\.venv\Scripts\python.exe -m pytest -q
```

The commands compile the service and run the offline suite. No Oracle, LLM, or
Service B access is needed by the P1 tests. The tests exercise tool result JSON,
synthetic checkpoint messages, synthetic SQL, metadata, and time-window inputs.

## Inputs and outputs

- Tool wrappers still receive the same arguments and keep the same six tool names.
- Tool output is JSON with `schema_version`, `status`, `error_code`, and `payload`.
- Existing output keys remain in `payload`, including `plan_artifact`, `fields`,
  `scenario_metadata`, `write_success`, `header_alert_count`, `raw_sql`, and
  `validation_success` where relevant.
- A shadow execution failure uses `status: "error"` and
  `error_code: "SHADOW_FAILED"`; its payload retains SQL, the false validation
  flag, and the failure message so SSE still emits `scenario_result`.
- Legacy checkpoint results without a version field are wrapped during decode.
- SSE event names and their UI payloads are unchanged.

## Configuration

P1 adds no runtime configuration and no dependency. Existing `.env` settings,
catalog files, Oracle configuration, and Service B settings continue to be read
by the same service adapters.

