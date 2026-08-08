# Analysis: Onsite Scenario 1000002

This document provides a detailed breakdown of the onsite scenario `1000002` ("Large sum (credit) transactions (7 days)"), how it is built, our engine's support capabilities, and an important discrepancy found in the onsite catalog metadata.

---

## 1. Scenario Breakdown & Configuration

The onsite database configures scenario `1000002` across four main tables:

### A. Scenario Header (`PIO_AML_SCENARIO`)
* **Scenario Code:** `1000002`
* **Name (English/Arabic):** `Large sum (credit) transactions (7 days)`
* **Active Flag:** `1` (Active)
* **Group By Flag:** `1` (Aggregates alerts at the customer level)

### B. Scenario Rules Mapping (`PIO_AML_SCENARIO_RULES`)
* **Rule Code:** `888891`
* **Sequence:** `1`

### C. Rule Settings (`PIO_AML_RULES`)
* **Rule Code:** `888891`
* **Description:** `Large sum (credit) transactions (7 days)`
* **Period Days:** `7` (Evaluation window of 7 days)
* **Period Type:** `0` (Daily rolling)

### D. Rule Details & Filters (`PIO_AML_RULES_DETAILS`)
The scenario filters are constructed sequentially as follows:

| Rule Seq | Parameter Code | Concept / Field | Operator | Comparison Value | Combined Rule |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **1** | `0` | Credit/Debit Indicator | `=` | `'730'` (describes `'CR'` for Credit) | `AND` |
| **2** | `2` | Transaction Count | `>` | `2` | `AND` |
| **3** | `6` | Total Sum of Amounts | `>=` | `45,000,000` | `-` (End of chain) |

---

## 2. Our Engine's Support and Creation Capabilities

### Yes, Our Engine Fully Supports This Scenario
Our agentic SQL generation pipeline can easily construct and execute this scenario. 

* **How We Translate It to SQL:**
  The scenario translates to a classic `GROUP BY` and `HAVING` aggregation pattern on the `BI_DWH.PIO_TRANSACTIONS` table:
  ```sql
  SELECT DISTINCT T1.CUS_NUM, T1.COUNTRY_CODE, T1.INST_CODE
  FROM BI_DWH.PIO_TRANSACTIONS T1
  WHERE T1.TRA_DATE BETWEEN TRUNC(SYSDATE) - 7 AND TRUNC(SYSDATE)
    AND T1.DEB_CRE_IND = 'CR'
  GROUP BY T1.CUS_NUM, T1.COUNTRY_CODE, T1.INST_CODE
  HAVING COUNT(T1.TRA_SEQ1) > 2
     AND SUM(T1.EQU_TRA_AMT) >= 45000000
  ```

* **Ability to Create It:**
  If a compliance officer prompts the Scenario Builder (Service A):
  > *"Flag customers who have conducted more than 2 credit transactions over a 7-day rolling period, where the total credit volume exceeds 45 million."*
  
  Our system will:
  1. Parse the intent into structured JSON (`SCENARIO_TYPE: "TRANSACTION"`, `thresholds` for count > 2 and sum >= 45M, and `semantic_conditions` for credit transactions).
  2. The SQL Bridge (Service B) will map "credit transactions" to `DEB_CRE_IND = 'CR'` using column schemas.
  3. Aggregate the metrics using `COUNT` and `SUM` under a 7-day partition-pruning `BETWEEN` window.
  4. Successfully execute and test the query.

---

## 3. Critical Discovery: Onsite Catalog Integrity Issue

During database inspection, we discovered a **critical discrepancy** in the onsite metadata configuration:

* **The Problem:** The table `PIO_AML_RULES_DETAILS` references parameter codes `0`, `2`, and `6` for rule `888891`. However, the lookup table `PIO_AML_PARAMETERS` on the database contains **only 1 row** (parameter `1000` for `Explanation Code`).
* **Impact on Stored Procedure:** When the legacy PL/SQL procedure `FILL_PIO_AML_CUSTOMERS` executes, its internal cursor `RUL_PARAM_DTLS` joins on `PIO_AML_PARAMETERS.PARAMETER_CODE = L.PARAMETER_CODE`. Because codes `0`, `2`, and `6` do not exist in that table, the cursor returns **no rows**. Consequently, the procedure skips generating these filters entirely.
* **Our Engine is Immune:** Because our agent does not rely on the legacy PL/SQL join cursors to write SQL—it queries column metadata directly—our engine is fully capable of generating the correct queries regardless of this database-side parameters catalog gap.
