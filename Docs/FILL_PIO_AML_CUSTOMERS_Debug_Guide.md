# Debugging Guide: FILL_PIO_AML_CUSTOMERS Stored Procedure

This document provides a technical guide for future AI agents and engineers on how to inspect, execute, and debug the database-side dynamic scenario validation procedure `FILL_PIO_AML_CUSTOMERS` in `BI_DWH`.

---

## 1. Stored Procedure Overview

The `FILL_PIO_AML_CUSTOMERS` stored procedure is the main execution engine that generates AML alerts for custom and pre-defined scenarios. 

* **Hardcoded Evaluation Date:** The procedure hardcodes the transaction evaluation date to June 6, 2010:
  ```sql
  PDATE DATE := '6-JUN-2010';
  ```
  All transactional dates, periods, and balances are calculated relative to this static anchor.

* **Target Isolation:** The procedure evaluates all scenarios in the catalog where `ACTIVE_FLAG = '1'` (completely ignoring `RUN_FLAG`). If a single active scenario has a SQL compilation failure or parameter inconsistency, the entire procedure crashes with `P_STATUS = -1` and rolls back all alerts.
  
  **How to Isolate a Scenario for Testing:**
  Before executing the procedure, ensure only your target scenario is active:
  ```sql
  UPDATE PIO_AML_SCENARIO SET ACTIVE_FLAG = '0' WHERE SCENARIO_CODE <> :target_code;
  UPDATE PIO_AML_SCENARIO SET ACTIVE_FLAG = '1', RUN_FLAG = '1' WHERE SCENARIO_CODE = :target_code;
  COMMIT;
  ```

---

## 2. Dynamic SQL Rules & Details Compilation

For dynamic scenarios (those not hardcoded statically in the beginning of the body), `FILL_PIO_AML_CUSTOMERS` iterates through Rules and Rule Details.

### Rule Execution Cursor (`GET_RULES`)
The procedure queries rules for the active scenario:
```sql
FOR J IN GET_RULES(S.SCENARIO_CODE) LOOP
```
It extracts the `PERIOD_TYPE`, `PERIOD_DAYS`, and `FREQUENCY_DAYS` to dynamically establish the query's date boundary window.

### Rule Details Compilation (`GET_RULE_DETAILS`)
The procedure loops over parameter details for each rule:
```sql
FOR L IN GET_RULE_DETAILS (J.AML_RULE_CODE) LOOP
```
For each detail `L`, it retrieves:
* The corresponding database column name and table code from `PIO_AML_PARAMETERS` and `PIO_AML_COLUMNS`.
* The comparison values and operator.

---

## 3. How `AND` / `OR` Rule Aggregation Works

For transactional-level checks (`AGGREGATION_CODE = '1'`), the stored procedure uses a multi-step insertion logic to combine filters:

1. **First Rule Detail (Initial Filter):**
   * If `PREV_COMBINED_RULE` is `NULL`, it takes the initial branch and performs a direct insert of matching transaction records into `PIO_AML_CUSTOMERS_DET`:
     ```sql
     INSERT INTO PIO_AML_CUSTOMERS_DET (...) SELECT DISTINCT ... WHERE ... <condition 1>;
     ```

2. **Subsequent Rule Details Joined with `AND`:**
   * If the previous detail's `COMBINED_RULE` was `'AND'`, the procedure:
     * Truncates the temporary table: `TRUNCATE TABLE PIO_AML_CUSTOMERS_DET_TEMP;`
     * Inserts the matching transactions for the **current** rule detail into the temp table:
       ```sql
       INSERT INTO PIO_AML_CUSTOMERS_DET_TEMP (...) SELECT DISTINCT ... WHERE ... <condition 2>;
       ```
     * Performs a clean-up `DELETE` on the main alert detail table, removing all rows that do **not** have matching transaction identifiers in the temp table:
       ```sql
       DELETE PIO_AML_CUSTOMERS_DET DD WHERE NOT EXISTS (
           SELECT 1 FROM PIO_AML_CUSTOMERS_DET_TEMP AA WHERE DD.TRA_SEQ1 = AA.TRA_SEQ1 AND ...
       );
       ```
     This filters the results down to records that satisfy both (and all subsequent) `AND` conditions.

---

## 4. How to Debug Failures (Step-by-Step)

If `FILL_PIO_AML_CUSTOMERS` returns `P_STATUS = -1`, follow this checklist:

### Step 1: Capture `DBMS_OUTPUT` SQL Statements
Enable DBMS output and print the dynamically compiled statements (`V_IMM`, `V_IMM1`, `V_IMM3`):
```python
import oracledb

# Connect and enable output
cursor.execute("BEGIN DBMS_OUTPUT.ENABLE(NULL); END;")

# Call procedure
p_status = cursor.var(oracledb.NUMBER)
cursor.execute("BEGIN FILL_PIO_AML_CUSTOMERS(:country, :inst, :status); END;", ...)

# Retrieve lines
line = cursor.var(oracledb.STRING)
status = cursor.var(oracledb.NUMBER)
while True:
    cursor.execute("BEGIN DBMS_OUTPUT.GET_LINE(:line, :status); END;", {"line": line, "status": status})
    if status.getvalue() != 0: break
    print(line.getvalue())
```

### Step 2: Validate String Quoting inside `COMPARISON_VALUE_FROM`
* For `IN` conditions, verify that the string in `PIO_AML_RULES_DETAILS.COMPARISON_VALUE_FROM` has the format:
  `''1'',''660''` (each code wrapped in double-single-quotes).
  * **Warning:** If it is wrapped in an additional layer of single quotes (e.g. `'''1'',''660'''`), the database will attempt to match literal quote characters in standard transaction code lookups and return `0` alerts.

### Step 3: Run the Extracted SQL Queries Manually
Take the output printed in `DBMS_OUTPUT` and run it directly in a SQL client (or python script). Oracle will return the exact line number and cause of any syntax or permission failures.
