# Confirmed intent descriptive completion

## What and why

When a parameter card is confirmed, the backend applies the confirmed values and then runs a constrained pass through the configured fast LLM. It may make minimal spelling or grammar corrections to the scenario name and detection description, and fill a missing or inferred/default scenario name, detection description, or baseline-window description. The plan is rendered from the resulting intent.

The LLM receives the complete confirmed intent and a compact snapshot of card choices. It cannot change structured business parameters such as thresholds, operators, windows, transaction scope, filters, explanation codes, exclusions, or aggregation settings. Corrections are accepted only when they remain highly similar to the original text. If the model or parsing fails, the already-confirmed intent is used unchanged so the confirmation workflow can still complete.

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
