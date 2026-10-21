# Customer, Account and Transaction Attribute Filters

**Date:** 2026-10-18
**Database:** OPENBI2 (`192.168.30.43:1521/OPENBI2`), country 400 / institution 1

---

## 1. What and why

Next to the explanation-code filter (which picks transaction **types**), a scenario can now restrict
**which** customers, accounts or transactions it covers, using the bank's lookup tables:

> *"Cash deposits above 10,000 JOD into **closed accounts** between 2016 and 2019"*
> → `Account status IN (CLOSED, Closed By Bank, Closed by Customer, …)` → 48 transactions across 31 customers.

| Attribute | Applies to | Source column | Lookup |
|---|---|---|---|
| Account status | account | `PIO_ACCOUNTS.STA_CODE` | `PIO_ACCOUNT_STATUS` |
| Account nature | account | `PIO_ACCOUNTS.ACCT_NAT` | `PIO_ACCOUNT_NATURE` |
| Account product | account | `PIO_ACCOUNTS.PRODUCT_CODE` | `PIO_PRODUCTS` |
| Account type | account | `PIO_ACCOUNTS.ACCT_TYPE` | `PIO_ACCOUNT_TYPE` |
| Individual / corporate | customer | `PIO_CUSTOMERS.INDV_CORP_IND` | fixed list in code: 1, I = Individual; 2, C = Corporate |
| Profession | customer | `PIO_CUSTOMERS.PROFESSION` | `PIO_PROFFISSION` |
| Occupation | customer | `PIO_CUSTOMERS.CUST_OCCUPATION` | `PIO_CUSTOMER_OCCUPATION` |
| Marital status | customer | `PIO_CUSTOMERS.MARITAL_ST` | `PIO_MARITAL_STATUS` |
| Customer classification | customer | `PIO_CUSTOMERS.CUS_CLASS` | `PIO_CUSTOMER_CLASSIFICATIONS` |
| City | customer | `PIO_CUSTOMERS.CITY_LOC_CODE` | `PIO_CITIES` |
| Debit / credit | transaction | `PIO_TRANSACTIONS.DEB_CRE_IND` | `PIO_DEP_CRED_LOOKUP` |

## 2. How it works

| Step | Where | What |
|---|---|---|
| Resolve | `analyze_intent_and_discover_explanation_codes` → `attribute_filters.resolve_attribute_filters` | Attributes whose keywords or description words appear in the request are sent, with their live country-400 catalog, to the fast LLM, which picks the codes (all spelling variants, `NOT IN` for exclusions). Every code is checked against the catalog. Stored in the intent as `attribute_filters`; shown in the plan overview. |
| Hide from PioTech | `execute_oracle_dwh_shadow_test` → `strip_attribute_phrases` | The restriction words are removed from the text PioTech reads (name, logic, conditions, defaults); conditions, segments and thresholds that restate the attribute are dropped. Without this PioTech refuses them, or re-applies "dormant" as an inactivity rule. Transaction types, codes, other thresholds and windows are untouched. |
| Apply | `apply_attribute_filters` | Added to PioTech's SQL `WHERE`, before `GROUP BY`/`HAVING`, so aggregates count only matching rows: `t.ACCOUNT_NUMBER IN (…PIO_ACCOUNTS…)`, `t.CUS_NUM IN (…PIO_CUSTOMERS…)`, `t.DEB_CRE_IND IN (…)`. The value is taken from each account's / customer's **latest snapshot** (`DAY_DATE`). A statement that cannot be split (joins, `WITH`, `UNION`) is wrapped and filtered on its `CUS_NUM`; a debit/credit filter is then skipped and reported. |
| Save and run | persistence, alert engine | The saved `RAW_SQL` already contains the filters. The result lists them in `attribute_filters_applied`. |

Dormancy stated over a period ("no activity for 6 months") stays behavioural, handled by PioTech; only a plain
"dormant accounts" becomes the status filter.

**Priority over explanation codes.** Filters are resolved before the explanation-code search. A transaction type
that is only a filter's value (`CREDIT`, `DEBIT`, "credit transactions" under the debit/credit filter) is removed with
its codes, so it is handled by the filter alone; real types that share a word (`CREDIT CARD`, `CREDIT TRANSFER`) keep
their codes. Example: "credit transactions above 500,000 JOD in 2019" → no `EXPL_CODE` filter, `DEB_CRE_IND IN ('2',
'C')` → 543 transactions across 68 customers (before: 8 across 2, limited to 30 loosely matched codes).

## 3. Data coverage (measured 2026-10-18)

| | Transacting | Present in master table |
|---|---|---|
| Accounts (`PIO_ACCOUNTS`) | 141,094 | 111,061 (79%); 2016–2019 transactions almost all tie |
| Customers (`PIO_CUSTOMERS`) | 154,254 | **1,067 (<1%)**; the table holds 9,365 customers |

**Account and debit/credit filters work on the data. Customer filters are correct but currently match very few
customers**, because most transacting customers are not in `PIO_CUSTOMERS`. Not a key-format issue (a transaction's
`CUS_NUM` always equals its account's holder). The lookup fill below cannot fix this; it would need customer records.
Product is filled on few accounts (30 of 87,604 cash legs tied to an account).

## 4. Demo lookup fill (insert-only)

`fill_demo_lookups.py` adds a lookup row for every stored code that had none (country 400 / inst 1). Nothing is
updated or deleted; a row is inserted only if its key is absent.

```powershell
$env:PYTHONPATH = "services\aml_builder"
python fill_demo_lookups.py            # dry run: writes demo_data\lookup_fills_plan.json, no change
python fill_demo_lookups.py --apply    # inserts in one transaction, writes the record + rollback script
```

Applied 2026-10-18 09:50: **513 rows**. Record: `demo_data/lookup_fills_20261018_095008.json` (every row, its source
row count and the basis of its description). Rollback: `demo_data/lookup_fills_20261018_095008_rollback.sql`
(DELETE by exact key; **not executed**; review before running).

| Table | Rows | Descriptions |
|---|---|---|
| `PIO_ACCOUNT_STATUS` | 4 | 0 = OPEN, 8 = BLACK LISTED (same codes in institutions 414 / 508); 6, 7 = marked unknown |
| `PIO_PRODUCTS` | 3 | 2 = TIME DEPOSITS, 3 = SAVING (as institution 414); 0 = marked unknown |
| `PIO_ACCOUNT_TYPE` | 7 | marked unknown (8 accounts) |
| `PIO_PROFFISSION` | 180 | the stored text itself (the column holds text, e.g. EMPLOYEE, TEACHER) |
| `PIO_CUSTOMER_OCCUPATION` | 164 | the stored text itself |
| `PIO_MARITAL_STATUS` | 12 | normalized: Married / M / MERED → Married, Unmarried / S → Single, … |
| `PIO_CUSTOMER_CLASSIFICATIONS` | 56 | readable codes as text (EMPL_GOVT → EMPL GOVT); others marked unknown |
| `PIO_CITIES` | 83 | `CITY CODE R101 (NAME UNKNOWN - DEMO FILL)`: no names exist in the database |
| `PIO_DEP_CRED_LOOKUP` | 4 | 1 / D = Debit, 2 / C = Credit (cash deposits post the cash ledger as 1 and the customer as 2) |

Note: the other countries' rows in `PIO_DEP_CRED_LOOKUP` say 1 = Credit; on country 400 the data shows 1 = Debit.
The existing country-400 row `PIO_ACCOUNT_TYPE` 1 = "Current Account" (native text "resident") was left unchanged.

## 5. How to run and verify

```powershell
$env:PYTHONPATH = "services\aml_builder"
pytest services\aml_builder\tests\test_attribute_filters.py -q
```

Live (PioTech on 8006, `.\run_dev.ps1`, do not persist): ask *"Flag any single cash deposit above 10,000 JOD into
closed accounts between 2016 and 2019"*. The plan shows **Account status**; the shadow test returns 48 transactions
across 31 distinct customers (equal to a hand query). Other prompts that return data: deposits into *business*,
*savings* or *trust* accounts; *credit transactions above 500,000 JOD in 2019* (8 transactions, 2 customers).

## 6. Configuration

| Setting | Effect |
|---|---|
| `AML_COUNTRY_CODE`, `AML_INST_CODE` | Scope of the catalogs, the filters and the fill |
| `LLM_MODEL_FAST` | Model of the resolver |

Catalogs are cached for 10 minutes per process (`attribute_filters.clear_catalog_cache()` resets).
