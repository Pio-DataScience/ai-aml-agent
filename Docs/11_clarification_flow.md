# Clarification Question Flow

**Date:** 2026-10-18
**Repos:** `ai-aml-agent` (backend) · `AI_AML_INTERFACE` (frontend, branch `feat/clarification-flow`)

---

## 1. What and why

When a request does not state a parameter that decides which alerts fire, the Scenario Builder now **asks instead of
guessing**: it stops before code discovery, the plan and the shadow test, and shows a form in the side panel.

| Parameter | Counts as stated when | Question | Industry default |
|---|---|---|---|
| Target entity | a single-transaction rule ("any single…", "transactions above X"), or the text names customers / accounts | single select: per customer, per account, each transaction | Per customer |
| Threshold amount | a threshold whose number the user wrote (a guessed or invented number is not) | number (JOD) | 10,000 JOD |
| Timeframe | a lookback ("within 7 days"), a period ("in 2019", "between 2016 and 2022"), or a frequency ("daily") | single select 1 / 7 / 30 / 90 / 180 days, 1 year, or a fixed date range (two dates) | 30 days |
| Channels | the user named the channel ("cash deposit", "outward transfer"), said "any / all transactions", or a debit / credit filter covers it. A bare "deposits" / "transfers" is asked | multi select (cash deposit, cash withdrawal, outward / inward transfer, cheque deposit, ATM withdrawal, all) | Cash deposit + cash withdrawal, or the options matching the user's word ("deposits" → cash and cheque deposit) |

Verified live (2026-10-18): the five demo prompts that state everything pass straight through; "Flag suspicious
deposits" and "Detect large transfers" ask all four; "Customers with cash deposits above 20,000 JOD" asks only the
timeframe.

This replaces, for these four parameters only, the 2026-10-17 behaviour of filling unstated values with assumed
defaults. Automatic correction of PioTech refusals (Doc 07) is unchanged.

## 2. How it works

```
user request ─► analyze_intent_and_discover_explanation_codes
                  intent LLM (marks unstated values 'assumed_default')
                  attribute filters, type cleanup
                  clarification.find_gaps  ── gaps? ──► NEEDS_CLARIFICATION ─► SSE clarification_request ─► form (side panel)
                  │ no gaps                                                                                   │ submit
                  ▼                                                                                           ▼
                code search, plan (COMPLETE) ◄── apply_answers ◄── same tool, answers in run config ◄── POST /chat/stream + clarification_answers
```

- **Gap decision in code** (`web/services/clarification.py`), not by the LLM: a value the LLM filled in without the
  user saying it is a gap. The intent prompt (rule 15) asks the LLM to mark such values `assumed_default`.
- **Answers** come with the next request as `clarification_answers` (`{question id: value}`), travel to the tool in the
  run config (`configurable.clarification_answers`), and override the LLM. The tool restores the original request
  and the partial intent from its own earlier result in the conversation state.
- **Chat message**: the user's bubble reads `Clarification answers: Target entity: Per customer; Threshold amount:
  10,000 JOD; Timeframe: last 30 days; Channels: Cash deposit, Cheque deposit`.
- **History**: the form is restored on its message; the UI reopens it only when it is still unanswered.
- **Modification requests** ("change the amount to 5,000") on a confirmed scenario ask nothing.

### Payloads

`clarification_request` SSE event (`data`):

```json
{
  "status": "NEEDS_CLARIFICATION",
  "understood_summary": ["Goal: Flag deposit transactions that are suspicious."],
  "missing_fields": ["target_entity", "threshold_amount", "timeframe", "channels"],
  "questions": [
    {"id": "target_entity", "field_name": "target_entity", "input_type": "single_select",
     "question_text": "What should be monitored?", "options": [{"value": "CUSTOMER", "label": "Per customer (...)"}],
     "default_value": "CUSTOMER", "required": true},
    {"id": "threshold_amount", "field_name": "threshold_amount", "input_type": "number",
     "question_text": "Above which amount should activity be flagged?", "default_value": 10000.0, "required": true, "unit": "JOD"},
    {"id": "timeframe", "field_name": "timeframe", "input_type": "single_select", "default_value": 30, "required": true, "unit": "days",
     "options": [{"value": 7, "label": "7 days"}, {"value": "custom", "label": "A fixed date range"}]},
    {"id": "date_range", "field_name": "timeframe", "input_type": "date_range", "default_value": null, "required": false,
     "show_if": {"question_id": "timeframe", "equals": "custom"}},
    {"id": "channels", "field_name": "channels", "input_type": "multi_select",
     "default_value": ["CASH DEPOSIT", "CHEQUE DEPOSIT"], "required": true}
  ],
  "original_request": "Flag suspicious deposits"
}
```

Request with answers (`POST /chat/stream`):

```json
{"messages": [{"role": "user", "content": "Clarification answers: ..."}],
 "metadata": {"user_id": "...", "project_id": "default", "chat_id": "..."},
 "clarification_answers": {"target_entity": "CUSTOMER", "threshold_amount": 10000, "timeframe": "custom",
                           "date_range": {"start": "2016-01-01", "end": "2019-12-31"},
                           "channels": ["CASH DEPOSIT", "CHEQUE DEPOSIT"]}}
```

A complete intent result now carries `"status": "COMPLETE"` (models: `ClarificationQuestion`,
`ClarificationResponse`, `ScenarioIntentResponse` in `schemas.py`).

### Frontend

| File | Role |
|---|---|
| `src/components/ClarificationForm.tsx` | "What I understood", one input per question, **Apply Industry Defaults**, **Generate Scenario** |
| `src/lib/clarificationForm.ts` | Pure form logic (visibility, missing answers, defaults, submission text) |
| `src/types/clarification.ts`, `agentEvents.ts`, `chat.ts` | Payload and event types |
| `src/hooks/useChatStream.ts` | Handles `clarification_request`; sends `clarification_answers`; synchronous send lock |
| `src/app/api/chat/route.ts` | Forwards `clarification_answers` |
| `src/app/chat/[sessionId]/page.tsx` | Panel "Scenario Details Needed", history restore, Enter auto-repeat guard |

### Duplicate sends (same change)

- Backend: one run per conversation; a second request while one runs gets *"Your previous message in this chat is
  still being processed…"* and never starts a second agent run.
- Frontend: the hook locks sending with a ref (covers the plan's "proceed" button too); Enter auto-repeat and IME
  composition events are ignored. `handleSubmit` already had a lock (`submissionInFlightRef`, voice composer change).

## 3. How to run and verify

```powershell
$env:PYTHONPATH = "services\aml_builder"
pytest services\aml_builder\tests\test_clarification.py -q

cd ..\AI_AML_INTERFACE
npm run test:clarification
```

Live: ask *"Flag suspicious deposits"* → the side panel shows **Scenario Details Needed** → **Apply Industry Defaults** →
**Generate Scenario** → the plan appears with the answered values.

## 4. Configuration

| Variable | Default | Effect |
|---|---|---|
| `AML_CLARIFICATION_FLOW` | `true` | `false` restores the assumed-default behaviour |
| `CLARIFICATION_DEFAULT_AMOUNT` | `10000` | Default threshold (JOD) |
| `CLARIFICATION_DEFAULT_WINDOW_DAYS` | `30` | Default lookback (days) |
| `CLARIFICATION_DEFAULT_CHANNELS` | `CASH DEPOSIT,CASH WITHDRAWAL` | Default channels when the request suggests none |
| `CLARIFICATION_DEFAULT_ENTITY` | `CUSTOMER` | Default target entity |
