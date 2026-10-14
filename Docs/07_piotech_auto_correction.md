# Automatic Correction of Unsupported Scenario Conditions

**Date:** 2026-10-14
**Builds on:** [06_piotech_rejection_passthrough.md](06_piotech_rejection_passthrough.md)

---

## 1. What and why

The PioTech DWH Scenario Agent refuses (`NOT_IMPLEMENTABLE`) any condition it cannot verify on the data — for
example *"without maintaining a balance"* (the balance column does not reconcile) or *"politically exposed
customers"* (the PEP flag is empty). Previously that ended the shadow test with a rejection report and no SQL.

Now the AML Builder corrects the scenario and retries automatically, so the officer gets working SQL on the first
try. **Every correction is reported as a warning and is carried through to governance and persistence.**

| Refused condition | Correction | Warning shown |
|---|---|---|
| Balance retention ("without maintaining a balance"), scenario has money in **and** out | **Replaced** by *total withdrawals at least 90% of total deposits* | *The unsupported phrase "without maintaining a balance" was automatically replaced with "total withdrawals at least 90% of total deposits" ... The 90% level is an assumed default, not a value you stated.* |
| Any other refused condition (PEP, risk level, cards, beneficiary country, ...) | **Removed** | *The unsupported phrase "X" was automatically bypassed to generate a valid operational query.* |
| ...and it limited who the rule covers | Removed | ...plus *it now applies to every customer or account that meets the remaining conditions, so it will flag a wider population than you asked for.* |

**Important:** a removed condition is not checked. A rule asked for PEP customers becomes a rule over all customers.
The warning says so; it is the officer's job to accept or reject that at governance. To restore the hard stop,
set `PIOTECH_AUTO_CORRECT=false`.

## 2. What is still a hard stop

| `rejection.kind` | Why it is not auto-corrected |
|---|---|
| `UNRESOLVED` | The meaning is ambiguous (e.g. which "average"). Correcting means choosing a meaning for the officer. |
| `REQUEST_REJECTED` | A required value is missing (e.g. time window 0). Correcting means inventing a number. |
| `SERVICE_UNAVAILABLE` | The service is down; nothing to correct. |
| `NOT_IMPLEMENTABLE` after `PIOTECH_AUTO_CORRECT_MAX_ATTEMPTS` rounds, or when the refused condition is not found in the intent | Nothing more can be removed safely. The report lists `auto_corrections_tried`. |

## 3. How it works

```
shadow test ──► PioTech ──► SQL? ── yes ──► shadow COUNT ──► result (+ auto_corrections, warnings, corrected_intent)
                   ▲          │
                   │          no: NOT_IMPLEMENTABLE
                   │          ▼
                   └── auto_correct_intent(intent, rejection)   (≤ PIOTECH_AUTO_CORRECT_MAX_ATTEMPTS rounds)
```

| File | Role |
|---|---|
| `web/services/intent_autocorrect.py` | Pure correction: remove the refused condition from `semantic_conditions`, `customer_segments`, `thresholds` and the `detection_logic` text; add the pass-through ratio for balance phrases; build the warnings. |
| `web/services/tools.py` | `_request_piotech_sql` (one PioTech call) and the retry loop in `execute_oracle_dwh_shadow_test`. `_latest_shadow_corrections` reads the agent state. |
| `prepare_scenario_metadata_for_persistence` | Adds `auto_correction_warnings` + `notice_to_user` to the governance result (from state, so the model cannot forget them). |
| `persist_and_validate_scenario_in_dwh`, `routes/deploy.py` | Save the **corrected** intent with the SQL generated from it. |
| `prompts/tool_driven_system.md` | Show the warnings verbatim before metrics, at governance and before saving. |

The ratio is sent as a threshold PioTech recognises:

```json
{"field": "total_withdrawals_to_total_deposits_ratio", "operator": ">=", "value_from": 0.9,
 "target_scope": "AGGREGATE", "provenance": "assumed_default"}
```

## 4. How to run and verify

```powershell
$env:PYTHONPATH = "services\aml_builder"
pytest services\aml_builder\tests\test_intent_autocorrect.py services\aml_builder\tests\test_piotech_rejection.py -q
```

Live (PioTech on 8006, AML Builder via `.\run_dev.ps1`, do not persist):

1. *Identify accounts with more than 10 cash deposits AND 10 cash withdrawals within 30 days, indicating
   high-pass-through velocity without maintaining a balance.* → approve plan → SQL with the 90% comparison and a
   "replaced" warning.
2. *Detect politically exposed customers whose cash deposits total more than 5,000 JOD within 30 days.* → approve →
   SQL without the PEP filter and a "wider population" warning → proceed to governance → the warning is repeated.

## 5. Configuration

| Variable | Default | Effect |
|---|---|---|
| `PIOTECH_AUTO_CORRECT` | `true` | `false` restores the hard stop with the rejection report (Doc 06). |
| `PIOTECH_AUTO_CORRECT_MAX_ATTEMPTS` | `2` | Correction rounds per shadow test before the refusal is shown. |
| `PIOTECH_SHOW_AUTO_CORRECTIONS` | `false` | `false`: corrections are applied but **nothing is shown** to the user; the model receives only `corrected_intent`. The audit trail is `logs/app.log` (`AUDIT auto-corrections (hidden from user)` at WARNING level). `true`: the warnings in section 1 are shown at shadow test, governance and save. |
| `PIOTECH_AI_URL`, `PIOTECH_AI_TIMEOUT_SECONDS` | unchanged | PioTech endpoint and timeout. |

The 90% pass-through level is the constant `DEFAULT_PASS_THROUGH_PERCENT` in `intent_autocorrect.py`.
