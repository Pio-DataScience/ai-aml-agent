# Scope Guard

**Date:** 2026-10-15

## 1. What and why

The AML Scenario Builder answered any question, including "who is Real Madrid" and "who won the 2014 World Cup".
In front of a bank that reads as an assistant with no boundaries. The scope guard declines messages that are clearly
unrelated to AML scenarios, with a fixed refusal, before the agent runs. It is adapted from PioTech-AI's AML-module
guard (`services/aml/web/services/scope_guard.py`).

## 2. How it decides

The fast model (`LLM_MODEL_FAST`) receives the scope description, the last 4 turns of the conversation and the new
message, and puts the message in **one** category:

| Category | Example | Outcome |
|---|---|---|
| `scenario_building` | "Detect cash deposits over 10,000 JOD in 7 days", "approve the plan" | continues |
| `scenario_registry` | "How many scenarios do we have?" | continues |
| `aml_knowledge` | "What is structuring?", "What is a PEP?" | continues |
| `assistant` | "Hello", "What can you do?", "thanks" | continues |
| `unsure` | anything the model cannot place | continues |
| `unrelated` | "Who is Real Madrid?", "Write me a poem" | **declined** |

It errs towards answering — a legitimate request refused is worse than an unrelated one answered:

- only `unrelated` is declined;
- a reply of 4 words or fewer inside a conversation ("proceed", "yes", "30 days") is not judged;
- a failed, slow (8 s) or senseless judge reply lets the turn continue, and is not cached;
- sidebar governance submissions are never judged.

Refusal (Arabic when the message is in Arabic):

> I can only help with anti-money-laundering (AML) detection scenarios: designing, testing and deploying scenarios,
> reviewing existing scenarios and their alerts, and AML concepts. Please ask me something in that area.

The declined exchange is saved in the conversation, so it shows in chat history and gives context to the next message.

A second layer is the SCOPE section of the agent's system prompt, for anything the guard lets through as `unsure`.

## 3. Files

| File | Role |
|---|---|
| `web/services/scope_guard.py` | Categories, refusal texts, `check()`, cache, `LLMScopeJudge` |
| `web/api/routes/chat.py` | `_scope_refusal()` runs before the agent; streams the refusal and records it |
| `web/services/prompts/tool_driven_system.md` | SCOPE section (answer AML concepts and greetings; refuse only clearly unrelated requests) |
| `tests/test_scope_guard.py` | 15 tests, no LLM needed |

## 4. How to run and verify

```powershell
$env:PYTHONPATH = "services\aml_builder"
pytest services\aml_builder\tests\test_scope_guard.py -q
```

Live: in a new chat, ask "who is real madrid" (declined in about 2 s, no tools called) and "What is structuring in
AML?" (answered).

## 5. Configuration

| Variable | Default | Effect |
|---|---|---|
| `AML_SCOPE_GUARD` | `true` | `false` switches the guard off (the prompt's SCOPE section still applies). |
| `LLM_MODEL_FAST` | `gpt-5.4-mini` (current `.env`) | Model used as the judge. |

Each judged message costs one short fast-model call (about 1–3 s).
