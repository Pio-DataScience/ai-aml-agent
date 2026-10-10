# Semantic Intent Contract v1

## Purpose

Package 03 adds an optional `semantic_contract` to `AMLIntent`. It makes
business semantics explicit before SQL generation while retaining every legacy
intent field and the existing Service A → Service B endpoint.

The contract separates Service A business authority from Service B SQL design:

- Service A owns evaluation/output grain, populations, metrics, filter scope,
  time meaning, exclusions, relationships, evidence, and ambiguity.
- Service B owns tables, columns, joins, CTEs, predicates, aggregation strategy,
  and other Oracle implementation choices.
- `DETAIL` and `AGGREGATE` remain compatible legacy business scopes; they no
  longer prescribe `WHERE` and `HAVING`.

## Contract structure

`semantic_contract.contract_version` is `1.0`. Its main objects are:

| Object | Meaning |
|---|---|
| `evaluation_grain` / `output_grain` | Business entity, keys, and optional period evaluated and returned. |
| `populations` | Separate base, numerator, denominator, and comparison populations with their own filters and windows. |
| `metrics` | Metric kind, measure, population references, grain, comparison, and NULL/zero-denominator policy. |
| `global_filters` / population or metric `filters` | Explicit applicability plus RECORD/GROUP/METRIC/OUTPUT business evaluation phase. |
| `time_windows` | Purpose, rolling/calendar/fixed meaning, anchor, offset, inclusivity, precision, and timezone. |
| `relationships` | Required business entity relationships and cardinality without join-column guesses. |
| `evidence` | Required output grain, identifiers, and matching transaction evidence. |
| `ambiguities` / `unsupported_requirements` | Visible blocking gaps that prevent handoff instead of being guessed. |

Ratio and percentage metrics must name numerator and denominator populations
and a zero-denominator policy. Model validation rejects dangling references.
Blocking ambiguities force `ready_for_handoff=false`.

## Extraction and enrichment

The existing intent-extraction LLM call produces the extended contract; no
additional agent or model call was added. The prompt permits defaults only when
business-equivalent and requires blocking markers for material uncertainty.
Explanation-code discovery binds codes into a population or predicate only when
that object names the corresponding transaction type.

The deterministic implementation plan displays the contract for officer review,
including population separation, metric policies, time boundaries, evidence,
and semantic blocks.

## Compatibility

- `semantic_contract` is optional, so existing valid intents still parse.
- Existing top-level thresholds, windows, transaction bindings, and
  clarifications remain unchanged.
- Service B validates v1 when present. For legacy payloads it creates a
  loss-aware `semantic_contract_effective` adapter view and lists unknown
  semantics rather than manufacturing values.
- Package 09 should version the transport/envelope independently and can later
  make semantic contract versions negotiable. It should not rename v1 fields.

## Verification

Service A:

```powershell
$env:DEBUG = "false"
.\.venv\Scripts\python.exe -m pytest -q services\aml_builder\tests\test_semantic_intent_contract.py
```

Service B:

```powershell
.\.venv\Scripts\python.exe -m pytest -q services\dwh_scenario_agent\tests\test_semantic_contract_extension.py
```

The tests cover numerator-only filtering, denominator preservation,
zero-denominator policy, exact time boundaries, required evidence, blocking
ambiguity, dangling references, and legacy compatibility.

## Rollback

Remove the optional model field and its nested models, restore the legacy prompt
wording and plan headings, and remove the Service B v1 models/adapter. Because
the extension is additive and does not alter transport or persistence schemas,
no data migration or endpoint rollback is required.
