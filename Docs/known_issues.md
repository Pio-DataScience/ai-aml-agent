# Known Issues & Environment Constraints — AML Builder Engine

This document tracks known issues and data/environment limitations discovered in the database and engine execution environment.

---

## 1. Hardcoded Scenario Filter in DWH Procedure

### Description
The database stored procedure `FILL_PIO_AML_CUSTOMERS` (which evaluates scenarios and generates alerts) contained a hardcoded filter in its main driving cursor `GET_SCENARIOS`:
```sql
CURSOR GET_SCENARIOS IS
      SELECT *
        FROM PIO_AML_SCENARIO
       WHERE     COUNTRY_CODE = ...
             AND ACTIVE_FLAG = '1' 
              AND SCENARIO_CODE IN ('1782638293246757')  -- <--- HARDCODED FILTER
```

### Impact
The compliance engine only processed rule checks for the single scenario `1782638293246757` (the test scenario from the implementation team) and ignored all newly created scenario codes.

### Status / Workaround
- **Status**: Identified.
- **Workaround**: Commented out the filter `AND SCENARIO_CODE IN ('1782638293246757')` in the stored procedure to allow the engine to evaluate all active scenarios in the table.

---

## 2. Missing Customer Master Profiles in Test Dataset

### Description
There is a referential integrity mismatch in the simulated database test dataset. For rules with high transaction frequencies (e.g., Daily Transactions > 100), the engine correctly flags multiple distinct customers (e.g., 47 customers yielding 30,350 matched transaction detail records in `PIO_AML_CUSTOMERS_DET`). 
However, only **1 customer** (`20210249`) is populated in the header alert table `PIO_AML_CUSTOMERS`.

### Cause
The stored procedure performs an `INNER JOIN` between `PIO_AML_CUSTOMERS_DET` (the flagged transaction details) and `PIO_CUSTOMERS` (the customer master profile table) on the customer number (`CUS_NUM`) to populate name, CIF, and branch code for the final alert:
```sql
FROM PIO_AML_CUSTOMERS_DET DET, PIO_AML_SCENARIO SC, PIO_CUSTOMERS CUS
WHERE CUS.DAY_DATE = DET.DAY_DATE
  AND CUS.COUNTRY_CODE = DET.COUNTRY_CODE
  AND CUS.INST_CODE = DET.INST_CODE
  AND CUS.CUS_NUM = DET.CUS_NUM
```
Out of the 47 flagged customers in `PIO_AML_CUSTOMERS_DET`, only `20210249` actually has a record in the simulated master table `PIO_CUSTOMERS`. The other 46 customers (including `2027`) have transactions in `PIO_TRANSACTIONS` but no master profile in `PIO_CUSTOMERS`.

### Status / Workaround
- **Status**: Identified test dataset constraint.
- **Workaround**: No action required. In production environments, all active transaction customers must have profiles in the customer master table `PIO_CUSTOMERS`. For local testing, use the alerted customer `20210249` to verify end-to-end alert pipelines.
