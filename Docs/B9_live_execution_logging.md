# B9 live-run execution journal

The optional B9 live runner now writes a new JSONL execution journal before it makes any request. It uses exclusive file creation, flushes and `fsync`s each record, and refuses to start if the journal path already exists. Set `B9_JOURNAL_PATH` to a fresh, run-specific path alongside the existing approved-scope, provider, environment, and result variables.

Each Service A and Service B dispatch first writes `provider_call_write_ahead` with the case ID, service, payload SHA-256, timestamp, and dispatch number. That record is durable before the awaited call begins. A returned or raised call gets a second durable record containing the delivery assessment, request ID when exposed by the adapter, SQL candidate and digest when available, validation findings, SSE events, error, and timestamp. Each case has start and outcome records, and the run has an explicit completed, failed, or interrupted terminal record. The final JSON result is written to a synced temporary file and atomically replaced only after all cases finish.

The runner enforces a hard maximum of 24 provider dispatch intents for a run. It also refuses to run Service A unless provider retries are disabled (`DWH_SQL_RETRY_BUDGET=0`, `DWH_SQL_MAX_ATTEMPTS=1`); Service B's generation-only path uses zero model retries. A dispatch intent without a conclusive outcome, and a recorded call failure whose delivery cannot be established, are classified as uncertain. Recovery does not retry or edit the journal.

Inspect a journal without starting either service or making network calls:

```powershell
\.venv\Scripts\python.exe -m services.aml_builder.tests.eval.dwh_scenario.b9_execution_journal C:\path\to\b9-run.jsonl
```

The output reports terminal state, dispatch count, uncertain calls, unfinished cases, and any torn final record. A local dispatch count proves only how many requests the runner began to dispatch. If a process ends after its write-ahead record, provider receipt remains unknown unless separate provider-side records establish it. The approved B9 scope remains `6690f75a0acc814a5be7cb3ea2e679818b99aac267d25624d812837edb7e54ce`; its prior one-run authorization is not reusable, so a fresh authorization is required before a new live run.
