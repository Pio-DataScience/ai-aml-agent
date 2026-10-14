# PioTech Rejection Passthrough

**Date:** 2026-10-14
**Scope:** `execute_oracle_dwh_shadow_test` and everything downstream of it (agent reply, UI side panel, chat history).

---

## 1. What and why

The AML Builder delegates SQL generation to the PioTech DWH Scenario Agent (`PIOTECH_AI_URL`, port 8006). That
service fails closed: when a scenario contains a condition it cannot verify on the data, or the intent is
ambiguous, it returns **no SQL** and streams two events explaining why:

```json
{"type": "scenario_report",
 "implemented": [],
 "not_implemented": [{"condition": "without maintaining a balance", "reason": "... compare money out with money in instead ..."}],
 "warnings": ["..."],
 "blocked_by": "without maintaining a balance",
 "implementable_without": ["transaction types CASH DEPOSIT, CASH WITHDRAWAL", "per account", "within 30 days"]}
{"type": "error", "message": "NOT_IMPLEMENTABLE: \"without maintaining a balance\" cannot be implemented: ..."}
```

Before this change the shadow-test tool read only `final_answer` and `content` events. A refusal became
*"PioTech AI did not return a valid SQL query. Try refining the scenario intent."* The agent then invented a cause,
retried the same intent several times, and could move on to the governance step with nothing to deploy.

Now the officer sees, in chat and in the side panel:

| Section | Source |
|---|---|
| **Blocked by** | `blocked_by`, or the `not_implemented` conditions, or the open items of an `UNRESOLVED:` message |
| **Reason** | Each `not_implemented` reason, verbatim |
| **Suggestion** | PioTech's own advice sentences (e.g. "compare money out with money in instead"), plus "resend without X" when exactly one condition blocks |
| **What can still be generated without it** | `implementable_without` |
| **Warnings** | `warnings` (coverage, data gaps), minus the one restated as a suggestion |

## 2. How it works

| File | Change |
|---|---|
| `services/aml_builder/web/services/piotech_report.py` | **New.** Pure functions: `normalize_scenario_report`, `build_rejection`, `build_unavailable_rejection`, `render_rejection_markdown`, `summarize_rejection`. No I/O. |
| `services/aml_builder/web/services/tools.py` | `execute_oracle_dwh_shadow_test` captures `scenario_report` and `error`; when no SQL is extracted it returns a rejection instead of the generic error. A transport error (`httpx.HTTPError`) returns a `SERVICE_UNAVAILABLE` rejection. |
| `services/aml_builder/web/api/routes/chat.py` | A tool result with `rejection_report` is emitted as an `escalation_report` SSE event. |
| `services/aml_builder/web/api/routes/sessions.py` | History rebuild attaches that markdown as `escalation_report` on the assistant message that follows. |
| `services/aml_builder/web/services/prompts/tool_driven_system.md` | Phase 2 rules for a rejection: present it as given, do not guess, do not retry the same intent, do not enter Phase 3 without SQL. |

**Rejection kinds**

| `kind` | When |
|---|---|
| `NOT_IMPLEMENTABLE` | The data cannot support a condition (e.g. empty PEP flag, unreliable balance). |
| `UNRESOLVED` | The intent is ambiguous or marked as needing the user. |
| `VALIDATION_FAILED` | SQL was attempted but no candidate passed PioTech's checks. |
| `NO_SQL` | Neither SQL, report nor error came back. |
| `SERVICE_UNAVAILABLE` | PioTech could not be reached (e.g. `WinError 10061`). The scenario was not evaluated. |

**Tool result on rejection**

```json
{
  "error": "PioTech AI did not generate SQL: NOT_IMPLEMENTABLE — blocked by \"without maintaining a balance\"",
  "rejection": {"kind": "...", "title": "...", "blocked_by": [], "reasons": [], "suggestions": [],
                "implementable_without": [], "warnings": [], "message": "..."},
  "rejection_report": "## Scenario not generated ...",
  "scenario_report": { "...": "raw, normalized PioTech report" }
}
```

The UI needs no change: `escalation_report` is already part of its event protocol (`AI_AML_INTERFACE/src/types/agentEvents.ts`)
and renders as markdown in the side panel. The panel title reads "Escalation Report"; renaming it is an optional
frontend change.

## 3. How to run and verify

```powershell
# Unit tests (no network, Oracle or LLM needed)
$env:PYTHONPATH = "services\aml_builder"
pytest services\aml_builder\tests\test_piotech_rejection.py -q
```

Live check (both services up, no persist):

1. Start the PioTech DWH scenario service on port 8006 and the AML Builder with `.\run_dev.ps1`.
2. In the chat, send: *Identify accounts with more than 10 cash deposits AND 10 cash withdrawals within 30 days,
   indicating high-pass-through velocity without maintaining a balance.*
3. Approve the plan. Expected: no metrics; the reply and the side panel show **Blocked by: without maintaining a
   balance**, the balance reason, and the suggestion to compare money out with money in.
4. Ask to proceed to governance. Expected: the agent declines because no SQL was generated.
5. Stop the PioTech service and approve a plan. Expected: `SERVICE_UNAVAILABLE`, saying the scenario was not evaluated.

## 4. Configuration

No new settings. Uses the existing:

| Variable | Default | Role |
|---|---|---|
| `PIOTECH_AI_URL` | `http://127.0.0.1:8006/chat/stream` | Endpoint called; shown in `SERVICE_UNAVAILABLE` reasons |
| `PIOTECH_AI_TIMEOUT_SECONDS` | `120` | HTTP timeout for the PioTech stream |

## 5. Not covered here

- Successful SQL that comes with warnings or partly implemented conditions (e.g. "branches or ATMs" with the ATM
  part not implemented) is still shown without the PioTech report.
- The shadow test still evaluates against `SYSDATE`, so it returns 0 on historical data.
- Fee/commission and non-cash codes still reach the code list from intent discovery.
