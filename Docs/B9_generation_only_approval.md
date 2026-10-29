# B9 development-only generation approval package

**Milestone status: development-complete, production-unverified.** The exact updated scope is preserved for provenance, but it was not run live. No further live evaluation is part of this milestone.

**Current status: no live run authorized for the updated scope.** Offline correction of Service A COUNT evidence guidance changed its prompt and payload hashes. The regenerated 12-case scope is `d24ceb8538482a8e8b551a22890f704c78f07c4a135f3878be599e56569766a9`; it requires separate authorization. No model calls have been made for this scope.

The prior authorized run under `bd9bf5e7a65667f6029017e93925ae689dbb9313cf86baeaebcf5699c2dbf2ad` is complete and consumed. Its 24 SQL candidates and journal are preserved in [snapshot](B9_live_snapshot_bd9bf5e7_20261029.json).

**Status: one authorized run completed; authorization consumed.**
The earlier authorized run and its outcomes remain historical in
[`B9_live_results.json`](B9_live_results.json). Targeted temporal contract,
prompt, and aggregate-matcher corrections changed the model-visible content.
The current parity summary is in
[`B9_synthetic_sql_parity.md`](B9_synthetic_sql_parity.md).

The machine-readable scope is
[`B9_generation_only_scope.json`](B9_generation_only_scope.json). It covers
exactly 12 synthetic cases and binds the fixture file, schema records, both
generator prompts, provider identities, and the complete model-bound messages
for each service. The complete payload disclosure is
[`B9_model_payloads.json`](B9_model_payloads.json); its digest and the canonical
scope digest are in the scope file.

The completed one-run scope SHA-256 is
`bd9bf5e7a65667f6029017e93925ae689dbb9313cf86baeaebcf5699c2dbf2ad`.
It supersedes prior approval hashes and is recorded in
[B9_live_results_bd9bf5e7_20261029.json](B9_live_results_bd9bf5e7_20261029.json)
and its durable journal. This authorization cannot be reused.

## Corrected scope

- Case 2 now requests only a transaction amount threshold. The unsupported
  cash-deposit classification was removed; no explanation code is inferred.
- Case 12 is a per-customer average amount calculation using verified
  `AVG(TRA_AMT)`, with no unsupported explanation-code filter or bind.
- The other ten synthetic cases retain their confirmed filters and typed
  semantics. Evaluator SQL is disclosed in the offline fixture but is not
  included in either model's message list.

## Exact content and identities

Both services use `BI_DWH.PIO_TRANSACTIONS` with only `CUS_NUM VARCHAR2`,
`TRA_DATE DATE`, `EXPL_CODE VARCHAR2`, and `TRA_AMT NUMBER`. No experience
examples or customer data are included. The payload file records both roles'
complete system and user messages, provider identity, and per-case payload
hash. Service A's assembled user context and Service B's exact
`AML_SCENARIO_GENERATION` JSON are serialized separately for all cases.

- Service A: OpenAI `gpt-5.4-mini`; existing local generation-only method;
  prompt `generator_v1.md`.
- Service B: OpenAI `gpt-5-nano`, instant mode; existing guarded HTTP SSE
  generation path; prompt `generation_only_system.md`.
- Development-only Service B process settings are prepared in
  [`PioTech-AI/Docs/B9_service_b_development.env.example`](../../PioTech-AI/Docs/B9_service_b_development.env.example).
  Tracing is disabled, the generation-only guard is enabled, and scope/schema,
  prompt, provider, and per-case payload hashes are pinned. This file is an
  example process configuration; no production configuration was changed.

## Guarded run boundary

The approved operation would make one generation call per case to each
provider, then stop after deterministic validation. It excludes Oracle
execution, customer-row retrieval, critic calls, database value probes, CSV
exports, deployment, persistence, prompt changes, retries, and legacy fallback.
Service A remains `DWH_SQL_MODE=http`. Service B's generation-only guard is
default-disabled and development-only.

## Authorization record

After the correction pass, the user authorized one run for the current scope
bd9bf5e7a65667f6029017e93925ae689dbb9313cf86baeaebcf5699c2dbf2ad. It
completed with 24 observed outcomes; the authorization is consumed. Per-case
SQL, SSE, and validation records are in
[B9_live_results_bd9bf5e7_20261029.json](B9_live_results_bd9bf5e7_20261029.json);
the durable journal is
[B9_execution_journal_bd9bf5e7_20261029.jsonl](B9_execution_journal_bd9bf5e7_20261029.jsonl).
No additional generation calls have been made.

The user authorized one development-only live generation-only A/B run for
exactly the 12 cases and content bound by scope SHA-256
`92ac99431f62033c5c5e27d2ddd0444fa51bc03f8ece4ee7bf1a93eb6246df50`. That
run is complete and its outputs are recorded in `B9_live_results.json`.
Case 9 fixture SQL continues to include the required aggregate evidence
projection. This correction pass changed both generator prompts to specify
comparison bind roles and exact requested output measures; all 24 model-bound
payload disclosures were regenerated and hashed into the current scope above.
The prior authorization does not cover this updated scope, and no calls were
made under it. No Oracle execution, customer-row retrieval, critics, probes,
exports, deployment, persistence, legacy fallback, or production configuration
changes occurred.

## Latest offline scope update

The Service A prompt now requires aliased COUNT evidence for b9-04 and b9-05. This is the only prompt change in this correction pass; Service B's prompt and validators were not changed. The exact typed payload disclosure and pinned metadata have been regenerated in `B9_model_payloads.json` and `B9_generation_only_scope.json`. The current scope hash is `d24ceb8538482a8e8b551a22890f704c78f07c4a135f3878be599e56569766a9`. This is an approval package only; a fresh explicit authorization is required before any live generation call.
