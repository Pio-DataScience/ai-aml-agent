# P7: Filter catalog, resolution, and A-side SQL check

P7 adds catalog-driven business filter dimensions to the existing intent, parameter-card, plan, revision, and shadow-test flow. Explanation codes are the first fully enabled dimension. `account_status` and `customer_profession` load from `filters.yaml` but stay disabled until their source columns and join details are confirmed.

## Why

Filters extracted by the model or dropped by Service B need a deterministic owner-side representation and a check before a shadow result can be approved. The catalog and SQL recheck make those filters reviewable and enforceable.

## How it works

The extraction prompt lists dimension names and support status from the catalog, without exposing physical column names. It emits `filters` mentions with a dimension, original phrase, and polarity. Service A resolves enabled dimensions into confidence-ranked values. Ambiguous and unresolved values, failed lookups, disabled dimensions, and unknown names appear as `kind: filter` rows in the existing P6 card. The officer can choose values, remove a filter, or acknowledge proceeding without an unsupported dimension. Card choices are recorded in the permanent card audit.

After confirmation, the final filters are stored with the revision intent and included in `params_hash`. The plan shows resolved filters and explains customer-level baseline semantics when a customer filter is combined with a transaction-level detail threshold. The Service B request includes only resolved filters and catalog-generated instructions naming the target table, column, and join hint. The Service B repository is unchanged.

After SQL extraction, Service A parses Oracle SQL with SQLGlot and checks predicates against each confirmed filter's target and value set. A missing or mismatched predicate returns `FILTER_NOT_APPLIED`, keeps the generated SQL in `raw_sql`, and leaves the workflow at `PLAN_APPROVED`, so result approval stays unavailable. Unextractable predicates are `unverified`; `policy.yaml:filter_check.unverifiable` controls whether they warn or fail. The default is `warn`. `ANY` and `ALL` transaction scope does not require an explanation-code predicate.

## Configuration

Edit `config/aml_catalogs/filters.yaml` to add dimensions. Every enabled dimension needs a label, level, target table and column, resolver, value source, match mode, polarity setting, required flag, and confirmed status. Lookup and semantic resolvers also need `resolver_config.match_threshold`, `ambiguity_margin`, and `max_options`. Enum resolvers use a static `resolver_config.options` list. Keep dimensions at `status: needs_advisor_input` while their source or physical target is unknown.

Set `config/aml_catalogs/policy.yaml` `filter_check.unverifiable` to `warn` or `fail`. No cache is used for resolver failures.

## Run and verify

Start Service A with:

```powershell
.\run_dev.ps1
```

Run the new offline tests and the P1 intent golden-fixture regression with:

```powershell
.\.venv\Scripts\python.exe -m pytest -q services\aml_builder\tests\test_p7_filter_resolution.py services\aml_builder\tests\test_p7_filter_applied_check.py services\aml_builder\tests\test_p7_filter_cards.py services\aml_builder\tests\test_p7_filter_catalog.py services\aml_builder\tests\test_p7_filter_codec.py services\aml_builder\tests\test_p7_filter_plan.py services\aml_builder\tests\test_p1_intent_normalization_baseline.py
```

Compile the service with:

```powershell
.\.venv\Scripts\python.exe -m compileall -q services app.py run_alert_engine.py
```

These tests use fake resolver candidates and synthetic SQL. They do not call the LLM, Oracle, or Service B.
