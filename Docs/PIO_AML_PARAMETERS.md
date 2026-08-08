
Regarding your question about `PIO_AML_RULES_DETAILS`: **How does the engine know which physical table and column a row represents if `PIO_AML_RULES_DETAILS` only contains `PARAMETER_CODE`?**

Here is the exact architectural mechanism behind how the Query Builder engine resolves `PARAMETER_CODE` to a physical table and column.

---

### The Metadata Catalog Chain

`PIO_AML_RULES_DETAILS` is intentionally designed as a  **purely abstract condition table** . It does not store physical table or column names. Instead, `PARAMETER_CODE` acts as a foreign key pointer into a 3-tier catalog:

$$
\text{PIO_AML_RULES_DETAILS} \xrightarrow{\text{PARAMETER_CODE}} \text{PIO_AML_PARAMETERS} \xrightarrow{\text{TABLE_CODE, COLUMN_CODE}} \begin{cases} \text{PIO_AML_TABLES (Table Name)} \ \text{PIO_AML_COLUMNS (Column Name)} \end{cases}
$$

---

### The 3 Metadata Tables Behind `PARAMETER_CODE`

1. **`PIO_AML_PARAMETERS` (The Parameter Registry)** This is the central dictionary. Every `PARAMETER_CODE` (e.g. `'1'`, `'2'`, `'5'`, `'6'`, `'104'`) is registered here with its metadata:
   * `PARAMETER_CODE` (Primary Key)
   * `TABLE_CODE` (Pointer to target table)
   * `COLUMN_CODE` (Pointer to target column)
   * `AGGREGATION_CODE` (`'1'` = Raw `WHERE` row filter, `'6'` = `HAVING SUM()`, `'2'` = `HAVING COUNT()`)
   * `PARAMETER_ELEMENT` (Human description, e.g. "Transaction Type", "Transaction Amount")
2. **`PIO_AML_TABLES` (The Table Registry)** Maps `TABLE_CODE` $\rightarrow$ physical database table name (e.g. `'PIO_TRANSACTIONS'`, `'PIO_CUSTOMERS'`, `'PIO_ACCOUNTS'`).
3. **`PIO_AML_COLUMNS` (The Column Registry)** Maps `(TABLE_CODE, COLUMN_CODE)` $\rightarrow$ physical column name (e.g. `'EXPL_CODE'`, `'EQU_TRA_AMT'`, `'CUS_CLASS'`, `'TRA_DATE'`).

---

### How `FILL_PIO_AML_CUSTOMERS` Resolves It at Runtime

When the PL/SQL stored procedure runs for a scenario, it executes an internal dynamic query that joins `PIO_AML_RULES_DETAILS` back to these catalog tables:

<pre><div node="[object Object]" class="relative whitespace-pre-wrap word-break-all my-2 rounded-xl bg-muted border"><div class="min-h-7 relative box-border flex flex-row items-center justify-between rounded-t border-b border-border px-2 py-0.5"><div class="font-sans text-sm text-muted-foreground">sql</div><div class="flex flex-row gap-2 justify-end"></div></div><div class="p-3"><div class="w-full h-full text-xs cursor-text"><div class="code-block"><div class="code-line" data-line-number="1" data-line-start="1" data-line-end="1"><div class="line-content"><span class="mtk6">SELECT</span><span class="mtk1"></span></div></div><div class="code-line" data-line-number="2" data-line-start="2" data-line-end="2"><div class="line-content"><span class="mtk1">    D.RULE_SEQ,</span></div></div><div class="code-line" data-line-number="3" data-line-start="3" data-line-end="3"><div class="line-content"><span class="mtk1">    D.PARAMETER_CODE,</span></div></div><div class="code-line" data-line-number="4" data-line-start="4" data-line-end="4"><div class="line-content"><span class="mtk1">    D.RULE_OPERATOR,</span></div></div><div class="code-line" data-line-number="5" data-line-start="5" data-line-end="5"><div class="line-content"><span class="mtk1">    D.COMPARISON_VALUE_FROM,</span></div></div><div class="code-line" data-line-number="6" data-line-start="6" data-line-end="6"><div class="line-content"><span class="mtk1">    D.COMPARISON_VALUE_TO,</span></div></div><div class="code-line" data-line-number="7" data-line-start="7" data-line-end="7"><div class="line-content"><span class="mtk1">    D.COMBINED_RULE,</span></div></div><div class="code-line" data-line-number="8" data-line-start="8" data-line-end="8"><div class="line-content"><span class="mtk1">    P.AGGREGATION_CODE,</span></div></div><div class="code-line" data-line-number="9" data-line-start="9" data-line-end="9"><div class="line-content"><span class="mtk1">    T.TABLE_NAME,    </span><span class="mtk5">-- Physical table name (e.g. PIO_TRANSACTIONS)</span></div></div><div class="code-line" data-line-number="10" data-line-start="10" data-line-end="10"><div class="line-content"><span class="mtk1">    C.COLUMN_NAME    </span><span class="mtk5">-- Physical column name (e.g. EXPL_CODE, EQU_TRA_AMT)</span></div></div><div class="code-line" data-line-number="11" data-line-start="11" data-line-end="11"><div class="line-content"><span class="mtk6">FROM</span><span class="mtk1"> PIO_AML_RULES_DETAILS D</span></div></div><div class="code-line" data-line-number="12" data-line-start="12" data-line-end="12"><div class="line-content"><span class="mtk6">JOIN</span><span class="mtk1"> PIO_AML_PARAMETERS P </span><span class="mtk6">ON</span><span class="mtk1"> D.PARAMETER_CODE </span><span class="mtk3">=</span><span class="mtk1"> P.PARAMETER_CODE</span></div></div><div class="code-line" data-line-number="13" data-line-start="13" data-line-end="13"><div class="line-content"><span class="mtk6">JOIN</span><span class="mtk1"> PIO_AML_TABLES T </span><span class="mtk6">ON</span><span class="mtk1"> P.TABLE_CODE </span><span class="mtk3">=</span><span class="mtk1"> T.TABLE_CODE</span></div></div><div class="code-line" data-line-number="14" data-line-start="14" data-line-end="14"><div class="line-content"><span class="mtk6">JOIN</span><span class="mtk1"> PIO_AML_COLUMNS C </span><span class="mtk6">ON</span><span class="mtk1"> P.TABLE_CODE </span><span class="mtk3">=</span><span class="mtk1"> C.TABLE_CODE </span><span class="mtk6">AND</span><span class="mtk1"> P.COLUMN_CODE </span><span class="mtk3">=</span><span class="mtk1"> C.COLUMN_CODE</span></div></div><div class="code-line" data-line-number="15" data-line-start="15" data-line-end="15"><div class="line-content"><span class="mtk6">WHERE</span><span class="mtk1"> D.SCENARIO_CODE </span><span class="mtk3">=</span><span class="mtk1"> :P_SCENARIO_CODE</span></div></div><div class="code-line" data-line-number="16" data-line-start="16" data-line-end="16"><div class="line-content"><span class="mtk6">ORDER BY</span><span class="mtk1"> TO_NUMBER(D.RULE_SEQ);</span></div></div></div></div></div></div></pre>

As the stored procedure loops through these rows, it constructs dynamic SQL snippets based on `P.AGGREGATION_CODE`:

* **If `AGGREGATION_CODE = '1'` (Detail WHERE filter):** It builds: `AND PIO_TRANSACTIONS.EXPL_CODE IN ('1316')`
* **If `AGGREGATION_CODE = '6'` (Summation HAVING filter):** It builds: `HAVING SUM(PIO_TRANSACTIONS.EQU_TRA_AMT) >= 50000`
* **If `AGGREGATION_CODE = '2'` (Count HAVING filter):** It builds: `HAVING COUNT(*) >= 3`

---

### Key Takeaways for Our Hybrid Decomposer

1. **`PIO_AML_RULES_DETAILS` requires NO column/table names:** You only need to supply the valid `PARAMETER_CODE`, `RULE_OPERATOR`, `COMPARISON_VALUE_FROM`, and `COMBINED_RULE`.
2. **The Decomposer's Job:** The Decomposer must identify which physical column/expression in Service B's generated SQL corresponds to which `PARAMETER_CODE` in `PIO_AML_PARAMETERS`.
3. **Auto-Provisioning:** If Service B relies on a column that has no `PARAMETER_CODE` in `PIO_AML_PARAMETERS`, our system auto-provisions a new `PARAMETER_CODE` by inserting into `PIO_AML_COLUMNS` and `PIO_AML_PARAMETERS` first, so `FILL_PIO_AML_CUSTOMERS` can resolve it cleanly.
