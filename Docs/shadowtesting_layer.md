
You have pinpointed the exact dilemma of shadow testing against production Data Warehouses (DWH):  **Latency vs. Statistical Accuracy** .

Using a hard row cap like `FETCH FIRST 100 ROWS ONLY` at the end of a query  **completely destroys statistical validity** , but running full scans across millions of transaction records during interactive agent testing will kill your database performance and bring development to a crawl.

Here is why hard row capping fails, followed by the **three industry-standard strategies** to get real scenario statistics instantly without running full scans.

### Why `FETCH FIRST 100 ROWS ONLY` Breaks AML Statistics

When you apply a row cap to a windowed aggregate or multi-stage CTE query in Oracle:

1. **Destroys Aggregate Rules:** If a scenario checks for `COUNT(*) >= 5` or `SUM(AMOUNT) >= 50,000` over a 30-day window, truncating transactions *before* or *after* aggregation guarantees false negatives and completely skewed alert density calculations.
2. **Arbitrary Truncation:** `FETCH FIRST 100 ROWS` simply truncates whatever block of records the optimizer touches first. It does not give you a representative sample of customers or transactions.
3. **Misleading Alert Density:** Your calculation of **$\text{Alert Density} = \frac{\text{Total Flagged Customers}}{\text{Total Evaluated Customers}}$** becomes pure noise.

### 3 Production-Grade Alternatives for Shadow Testing

Instead of arbitrarily capping output rows at the end of the query, use **Customer-Level Sampling** or  **Time-Windowed Partitioning** .

#### 1. Hash-Based Customer Sampling (Best for True Statistics)

Instead of capping transactions, cap the **number of evaluated customers** using deterministic hashing. By selecting 1% or 5% of your customer base and scanning  **100% of their transaction history** , all mathematical scenario rules (`SUM`, `COUNT`, `AVG`, `HAVING`) remain 100% accurate.

**SQL**

```
-- Evaluates 100% of transaction history for a deterministic 5% sample of customers
WITH Sampled_Customers AS (
    SELECT CUS_NUM, COUNTRY_CODE, INST_CODE
    FROM BI_DWH.PIO_CUSTOMERS
    WHERE ORA_HASH(CUS_NUM, 99) < 5 -- Exact 5% customer sample
),
-- Run scenario logic strictly against Sampled_Customers
...
```

* **Why it works:** Execution time drops by ~95%, but your **Alert Density ratio** remains statistically identical to running against the full production dataset.
* **Extrapolation:** **$\text{Estimated Total Production Alerts} = \text{Sample Alerts} \times \left(\frac{100}{5}\right)$**.

#### 2. Temporal Partition Sampling (Time Window Reduction)

For initial interactive testing, constrain the observation window to a single week or single month rather than a full 6-month historical lookback.

**SQL**

```
-- Interactive Mode: Lookback 7 Days
WHERE T.TRA_DATE >= TRUNC(SYSDATE) - 7

-- Full Shadow Batch Mode: Lookback 180 Days
WHERE T.TRA_DATE >= ADD_MONTHS(TRUNC(SYSDATE), -6)
```

* **Why it works:** Scans a fraction of the transaction partition while keeping 100% of the customer base intact.

#### 3. Dual-Execution Pipeline (Interactive vs. Nightly Shadow Batch)

Decouple your interactive agent testing from full statistical auditing:

| **Phase**                   | **Sampling Strategy**                         | **Goal**                                                                       | **Target Latency** |
| --------------------------------- | --------------------------------------------------- | ------------------------------------------------------------------------------------ | ------------------------ |
| **Interactive Development** | **1% Customer Hash Sample**(`ORA_HASH < 1`) | Validate SQL syntax, CTE logic, join safety, and non-zero result generation.         | **< 3 seconds**    |
| **Shadow Testing Batch**    | **100% Full DWH Scan**(Run off-peak/nightly)  | Calculate true customer alert counts, transaction density, and false-positive rates. | **2 - 10 minutes** |

### Summary Recommendation

* **NEVER** use `FETCH FIRST N ROWS ONLY` on AML/Transaction Monitoring queries—it invalidates the underlying aggregation mathematics.
* **DO** use `ORA_HASH(CUS_NUM, 99) < N` in your initial CTEs during interactive shadow testing. It preserves exact per-customer mathematical accuracy while reducing query execution time linearly with sample size.
