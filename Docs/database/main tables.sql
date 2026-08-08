/* Output tables */
SELECT * FROM PIO_AML_CUSTOMERS where aml_scenario_code ='1782750712001428'
SELECT * FROM PIO_AML_CUSTOMERS_DET where aml_scenario_code ='1782750712001428'

DELETE FROM PIO_AML_CUSTOMERS
DELETE FROM PIO_AML_CUSTOMERS_DET


/* Engine tables */
SELECT * FROM PIO_AML_SCENARIO 
SELECT * FROM PIO_AML_RULES
SELECT * FROM PIO_AML_SCENARIO_RULES
SELECT * FROM PIO_AML_RULES_DETAILS 

SELECT * FROM PIO_BRANCHES

DELETE FROM PIO_AML_SCENARIO
DELETE FROM PIO_AML_RULES
DELETE FROM PIO_AML_SCENARIO_RULES
DELETE FROM PIO_AML_RULES_DETAILS

/* ========================================================= */
SELECT * FROM PIO_AML_PARAMETERS
SELECT * FROM PIO_AML_TABLES
SELECT * FROM PIO_AML_COLUMNS
SELECT * FROM PIO_LTG_DEFINITION
SELECT * FROM PIO_LTG_TRASACTION_TYPE
SELECT * FROM PIO_PERIOD_TYPE
SELECT * FROM PIO_AML_DEGREE_RISK
SELECT * FROM PIO_AML_AGGREGATIONS
SELECT * FROM PIO_LTG_DEFINITION
SELECT * FROM PIO_LTG_TRASACTION_TYPE

SELECT country_code, inst_code, count(country_code) FROM PIO_EXPLANATION_CODE
where
group by country_code, inst_code

SELECT EXPL_CODE FROM PIO_TRANSACTIONS WHERE EXPL_CODE IS NOT NULL

PIO_BANKBI_DICTIONARY_TABLES
PIO_BANKBI_DICTIONARY_COL
PIO_BANKBI_DICT_JOIN_TABLES
PIO_EXPLANATION_CODE
PIO_EXPLAINATIONS
pio_loans
PIO_OUTWARD_CENTRAL_TRANSFERS
PIO_INWARD_CENTRAL_TRANSFERS

SELECT COLUMN_CODE FROM PIO_AML_COLUMNS
order by COLUMN_CODE desc;

INSERT INTO BI_DWH.PIO_AML_COLUMNS (
    COLUMN_CODE, 
    COLUMN_TYPE,            -- Catalog type code
    TABLE_CODE, 
    COLUMN_NAME,
    COLUMN_BUSINESS_NAME, 
    COLUMN_BUSINESS_NAME_NAT,
    LOOKUP_FLAG, 
    SD_USED_FLAG, 
    BALANCE_FLAG, 
    HIS_FLAG
)
VALUES (
    '320',                -- Next available COLUMN_CODE
    '1',                  -- COLUMN_TYPE code (1 = Number)
    '6',                  -- TABLE_CODE for PIO_LOANS
    'LOAN_AMT',           -- Column Name
    'Loan Amount',        -- English business label
    '???? ?????',         -- Arabic business label
    '0',                  -- No lookup flag
    '0',                  -- SD used flag
    '0',                  -- Balance flag
    '0'                   -- History flag
);


-- 1. Ensure BRA_CODE is registered in PIO_AML_COLUMNS under PIO_TRANSACTIONS (Table Code '3')
MERGE INTO BI_DWH.PIO_AML_COLUMNS C
USING (SELECT '3' AS TABLE_CODE, '140' AS COLUMN_CODE FROM DUAL) D
ON (C.TABLE_CODE = D.TABLE_CODE AND C.COLUMN_CODE = D.COLUMN_CODE)
WHEN NOT MATCHED THEN
  INSERT (TABLE_CODE, COLUMN_CODE, COLUMN_NAME, COLUMN_TYPE, COLUMN_BUSINESS_NAME)
  VALUES ('3', '140', 'BRA_CODE', '2', 'Branch Code');

-- 2. Register Parameter 117 in PIO_AML_PARAMETERS
-- Linking to PIO_TRANSACTIONS (Table '3'), BRA_CODE (Column '140'), with AGGREGATION_CODE = '3' (COUNT DISTINCT)
MERGE INTO BI_DWH.PIO_AML_PARAMETERS P
USING (SELECT '117' AS PARAMETER_CODE FROM DUAL) D
ON (P.PARAMETER_CODE = D.PARAMETER_CODE)
WHEN NOT MATCHED THEN
  INSERT (PARAMETER_CODE, TABLE_CODE, COLUMN_CODE, AGGREGATION_CODE, PARAMETER_ELEMENT)
  VALUES ('117', '3', '140', '3', 'Distinct Branch Count');

COMMIT;


LTG_ACCOUNT.ltg_code

select * from pio_accounts a
WHERE  a.day_date = '6-jun-2010' and EXISTS (
    SELECT 1
    FROM pio_loans l
    JOIN pio_transactions t
        ON t.account_number = a.account_number and t.country_code = a.COUNTRY_CODE and t.inst_code = a.inst_code and t.day_date = a.day_date
    WHERE l.account_number = a.account_number
      AND t.tra_date <= ADD_MONTHS(l.INT_DATE, 3) and l.PRINCIPL_LOAN_AMT <= l.PRINCIPL_INST_AMT and t.expl_code is null)






select ltg_code from PIO_LTG_DEFINITION where LTG_ENGLISH_NAME = 'INTEREST EXPENSE'


select * from pio_transactions
where expl_code in (select transaction_type from pio_ltg_trasaction_type where ltg_code  = (select ltg_code from PIO_LTG_DEFINITION where LTG_ENGLISH_NAME = 'INTEREST EXPENSE' and country_cod  = 400 and inst_code 1 ))



SELECT TRANS_AMT from PIO_TRANSACTIONS