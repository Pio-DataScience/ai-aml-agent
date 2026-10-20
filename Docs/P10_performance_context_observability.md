# P10 Performance, Context Control, and Observability

## Baseline measurement

The fake-adapter baseline was measured on 2026-10-20 with the repository virtual
environment (Python 3.14.7), using seven scripted runs. The run used no live LLM,
Oracle, or Service B connections.

Command:

```powershell
.\.venv\Scripts\python.exe -m scripts.benchmark_p10_fake_scenario --mode baseline --runs 7
```

| Measurement | Median |
|---|---:|
| Fake scenario total | 149.472 ms |
| Mean of five fake tool stages | 13.188 ms |
| Three sequential fake type searches | 61.617 ms |
| Event-loop delay during a 20 ms blocking fake | 20.488 ms |
| Model-context input | 5,767 characters |

Each of the five fake stage adapters yields for 2 ms. Each of three retrieval
fakes blocks for 20 ms. A separate 20 ms blocking fake measures event-loop
responsiveness. The context fixture contains three large historical tool results.
These values describe this local synthetic workload, not production Oracle or model
latency.

## P10 measurement procedure

`scripts/benchmark_p10_fake_scenario.py` runs the same fake scenario in `baseline`
and `final` modes. The final report will record the measured result after each
optimization commit. An optimization that does not improve the relevant measured
dimension, or adds unacceptable risk, will be reverted and recorded here.

## Runtime metrics

Service A emits one JSON-lines record for each completed tool call through the
`aml.metrics` logger. Records contain the request/thread correlation ID, thread and
revision IDs, tool name, duration, status, stable error code, and available token
counts. Metric records do not contain prompts, chat messages, SQL, or business
values. LangSmith trace metadata includes the thread ID and correlation ID when
tracing is enabled; existing tracing enablement and environment behavior remain
unchanged.
