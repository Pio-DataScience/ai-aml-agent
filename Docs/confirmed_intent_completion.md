# Confirmed intent descriptive completion

## What and why

When a parameter card is confirmed, the backend applies the confirmed values and then runs a constrained pass through the configured fast LLM. It may make minimal spelling or grammar corrections to user-authored scenario text, and generate a missing, inferred/default, or generic scenario name, detection description, or baseline-window description. Generic placeholders such as “New AML Scenario” and “no additional business constraints provided” are eligible for full regeneration rather than typo-only corrections. The prompt supplies parameter-grounded examples and directs the model to use the confirmed metric, aggregation, transaction scope, grain, time window, threshold, filters, and exclusions in plain business language. If the model returns empty or generic scenario text, a deterministic description built from the confirmed intent fills the eligible scenario name and detection logic fields.

The LLM receives the complete confirmed intent, a compact snapshot of card choices, JSON-safe eligible field paths, and fallback examples. It cannot change structured business parameters such as thresholds, operators, windows, transaction scope, filters, explanation codes, exclusions, or aggregation settings. Corrections are accepted only when they remain highly similar to the original text. If model generation is empty or generic, deterministic fallback text is built from the confirmed values; missing thresholds are called out rather than invented. This keeps the plan readable if the model or parsing fails.

Parameter-card application also maps the selected metric into both the aggregation profile (used by the plan) and the first threshold (used by threshold evaluation). When the intent has no threshold object yet, metric and comparator selections create the standard threshold structure before applying the confirmed value.

Extracted `field_provenance` is preserved by intent normalization. Explicitly stated names and descriptions are retained as user-authored; inferred/default fields are eligible for completion. The feature uses the existing `LLM_PROVIDER`, `LLM_MODEL_FAST`, and provider credentials/settings and adds no new configuration.

## Run

From the backend repository root in PowerShell:

```powershell
cd C:\Users\nidal.shahin\Code\ai-aml-agent
.\run_dev.ps1
```

The UI can be run separately from its repository root:

```powershell
cd C:\Users\nidal.shahin\Code\AI_AML_INTERFACE
npm run dev
```

Confirm a parameter card and inspect the stored revision intent and generated implementation plan. No separate feature flag is required.
