# Intent Extractor Compatibility Normalization

## What and why

The semantic-contract schema introduced in Package 03 uses typed grains and
strict enum values. Live extraction showed that an otherwise complete scenario
could fail before SQL handoff when the LLM used business-equivalent friendly
labels such as `user_input`, `PER CUSTOMER PER WINDOW`, `CURRENT`, `WEEK`, or
`CURRENT_DATE`.

`schemas.py` now performs a narrow, fail-closed compatibility normalization
before `AMLIntent` validation. It converts only known equivalent values:

- provenance aliases such as `user_input` to `stated`;
- unambiguous customer/account/transaction grain strings to `GrainDefinition`;
- an accidentally typed legacy `aggregation.grain` back to its required
  plain-language string;
- singular time units and common time-purpose, type, anchor, and precision
  aliases to canonical v1 enums, including the observed `BASE` and
  `EVENT_RELATIVE` labels and their unambiguous swapped-purpose/type shape.

Unknown values are retained and rejected by Pydantic. The normalizer therefore
does not invent business semantics, weaken ambiguity handling, add an LLM call,
or bypass Service B validation.

The extraction prompt now instructs the model to emit canonical values and
uses the receipt-then-third-party-transfer request as a sequence example. It
requires distinct receipt and outward-transfer populations plus both the weekly
observation and six-hour event-relative windows.

## Representative request

`Customers who receive at least 10k, then transfer at least 80% of those funds
to a third-party account within 6 hours, evaluated over the last week.`

The regression test confirms that legacy/friendly extractor values for this
request normalize into a valid v1 contract and preserve the receipt-to-transfer
sequence, customer rolling-window grain, weekly observation period, and
six-hour event-relative period.

## Verification

```powershell
cd C:\Users\nidal.shahin\Code\ai-aml-agent
$env:DEBUG = "false"
.\.venv\Scripts\python.exe -m pytest -q services\aml_builder\tests\test_semantic_intent_contract.py services\aml_builder\tests\test_deployment_guard.py
.\.venv\Scripts\python.exe -m compileall -q services app.py run_alert_engine.py
```

## Rollback

Revert the normalizer, prompt guidance, and its tests together. No data,
checkpoint, transport, or Service B migration is involved.
