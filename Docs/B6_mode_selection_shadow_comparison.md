# B6 — Generator modes and shadow comparison

## What and why

B6 registers the B5 in-process SQL generator behind the existing typed boundary. `DWH_SQL_MODE` is `http` by default, preserving the HTTP adapter's payload and behavior. `inprocess` selects the local generator. `shadow` runs HTTP and local generation for comparison, but the HTTP result remains authoritative. Shadow mode is rejected when `DWH_SQL_ENVIRONMENT=production`.

Shadow comparison records parse and SELECT safety, required `CUS_NUM` projection, confirmed-filter coverage, execution evidence, latency, LLM/tool/token counters, errors, and measurable normalized-AST differences. It does not log SQL, prompts, result rows, or customer values. SQL text differences alone are not discrepancies. Local generation errors and timeouts do not affect the HTTP result. The local result is never returned to A's approval, scenario persistence, or deployment gates.

## Configuration

| Setting | Default | Meaning |
| --- | --- | --- |
| `DWH_SQL_MODE` | `http` | `http`, `inprocess`, or `shadow`; invalid values fail settings validation. |
| `DWH_SQL_ENVIRONMENT` | `development` | Set to `production` to prohibit shadow mode. |
| `DWH_SQL_SHADOW_TIMEOUT_SECONDS` | `30` | Maximum local shadow-generation duration. |
| `DWH_SQL_CHROMA_PERSIST_DIR` | unset | Required to initialize the local generator. |

In-process mode also requires the B2 provider configuration, B4 Chroma collections and manifests, and the B3 read-only Oracle pool. Missing optional packages or invalid index/provider configuration fail closed.

## Run and verify

Run A normally with `.\run_dev.ps1`. To select a mode, set the environment variable before process startup, for example:

```powershell
$env:DWH_SQL_MODE = "shadow"
$env:DWH_SQL_ENVIRONMENT = "development"
.\run_dev.ps1
```

Run the offline B6 tests and synthetic comparison report from the repository root:

```powershell
$env:DEBUG = "false"
$env:PYTHONPATH = "services\aml_builder"
.\.venv\Scripts\python.exe -m pytest -q services\aml_builder\tests\test_b6_generator_modes.py services\aml_builder\tests\eval\dwh_scenario\test_runner.py
.\.venv\Scripts\python.exe -m services.aml_builder.tests.eval.dwh_scenario.comparison_report
```

The comparison report uses the B0 deterministic stub against the synthetic case set. It checks evaluation plumbing and structural scoring offline; it does not claim measured parity for real providers or live data.

## Safety boundaries

The local generator can perform policy-guarded read-only candidate queries through A's Oracle repository. Shadow mode grants no approval or deployment authority and the local SQL is discarded after comparison. Only the HTTP result continues through A's existing filter check, read-only shadow test, and approval workflow. Never run shadow mode in production.
