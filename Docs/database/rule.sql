SET DEFINE OFF;
Insert into BI_DWH.PIO_AML_RULES
   (COUNTRY_CODE, INST_CODE, RULE_CODE, RULE_DESC_ENG, RULE_DESC_ARB, 
    ACTIVE_FLAG, APPROVAL_FLAG, USE_SEQUENTIALLY_FLAG, SEQUENTIALLY_COUNT, DEFAULT_RULE_FLAG)
 Values
   (368, 1, '777777', 'Customer Checker \Outside Black LIST', '????? ????? ???????\???? ??????? ???????', 
    '1', '2', '0', 0, '1');
Insert into BI_DWH.PIO_AML_RULES
   (COUNTRY_CODE, INST_CODE, RULE_CODE, RULE_DESC_ENG, RULE_DESC_ARB, 
    ACTIVE_FLAG, APPROVAL_FLAG, USE_SEQUENTIALLY_FLAG, SEQUENTIALLY_COUNT, DEFAULT_RULE_FLAG)
 Values
   (818, 1, '777777', 'Customer Checker \Outside Black LIST ', '????? ????? ???????\???? ??????? ???????', 
    '1', '2', '0', 0, '1');
Insert into BI_DWH.PIO_AML_RULES
   (COUNTRY_CODE, INST_CODE, RULE_CODE, RULE_DESC_ENG, RULE_DESC_ARB, 
    ACTIVE_FLAG, APPROVAL_FLAG, USE_SEQUENTIALLY_FLAG, SEQUENTIALLY_COUNT, DEFAULT_RULE_FLAG)
 Values
   (729, 1, '777777', 'Customer Checker \Outside Black LIST', '????? ????? ???????\???? ??????? ???????', 
    '1', '2', '0', 0, '1');
Insert into BI_DWH.PIO_AML_RULES
   (COUNTRY_CODE, INST_CODE, RULE_CODE, RULE_DESC_ENG, RULE_DESC_ARB, 
    ACTIVE_FLAG, APPROVAL_FLAG, USE_SEQUENTIALLY_FLAG, SEQUENTIALLY_COUNT, DEFAULT_RULE_FLAG)
 Values
   (736, 1, '777777', 'Customer Checker \Outside Black LIST ', '????? ????? ???????\???? ??????? ???????', 
    '1', '2', '0', 0, '1');
Insert into BI_DWH.PIO_AML_RULES
   (COUNTRY_CODE, INST_CODE, RULE_CODE, RULE_DESC_ENG, RULE_DESC_ARB, 
    ACTIVE_FLAG, PERIOD_TYPE, PERIOD_DAYS, USE_SEQUENTIALLY_FLAG, SEQUENTIALLY_COUNT, 
    DEFAULT_RULE_FLAG, EXCLUDE_DAYS, CREATED_BY, CREATED_DATE, UPDATED_BY, 
    UPDATED_DATE)
 Values
   (729, 1, '888890', 'Non-Profit Organization', 'Non-Profit Organization', 
    '1', '0', 1, '0', 0, 
    '0', 0, 12, TO_DATE('2/19/2026', 'MM/DD/YYYY'), 12, 
    TO_DATE('2/19/2026', 'MM/DD/YYYY'));
Insert into BI_DWH.PIO_AML_RULES
   (COUNTRY_CODE, INST_CODE, RULE_CODE, RULE_DESC_ENG, RULE_DESC_ARB, 
    ACTIVE_FLAG, PERIOD_TYPE, PERIOD_DAYS, USE_SEQUENTIALLY_FLAG, SEQUENTIALLY_COUNT, 
    DEFAULT_RULE_FLAG, EXCLUDE_DAYS, CREATED_BY, UPDATED_BY, UPDATED_DATE)
 Values
   (729, 1, '888891', 'Large sum (credit) transactions (7 days)', 'Large sum (credit) transactions (7 days)', 
    '1', '0', 7, '0', 0, 
    '0', 0, 0, 12, TO_DATE('2/27/2026', 'MM/DD/YYYY'));
Insert into BI_DWH.PIO_AML_RULES
   (COUNTRY_CODE, INST_CODE, RULE_CODE, RULE_DESC_ENG, RULE_DESC_ARB, 
    ACTIVE_FLAG, PERIOD_TYPE, PERIOD_DAYS, USE_SEQUENTIALLY_FLAG, SEQUENTIALLY_COUNT, 
    DEFAULT_RULE_FLAG, EXCLUDE_DAYS, CREATED_BY, UPDATED_BY, UPDATED_DATE)
 Values
   (729, 1, '888892', 'Large sum (debit)transactions (7 days)', 'Large sum (debit)transactions (7 days)', 
    '1', '0', 7, '0', 0, 
    '0', 0, 0, 12, TO_DATE('2/27/2026', 'MM/DD/YYYY'));
Insert into BI_DWH.PIO_AML_RULES
   (COUNTRY_CODE, INST_CODE, RULE_CODE, RULE_DESC_ENG, RULE_DESC_ARB, 
    ACTIVE_FLAG, PERIOD_TYPE, PERIOD_DAYS, USE_SEQUENTIALLY_FLAG, SEQUENTIALLY_COUNT, 
    DEFAULT_RULE_FLAG, EXCLUDE_DAYS, CREATED_BY, UPDATED_BY, UPDATED_DATE)
 Values
   (729, 1, '888893', 'Large sum (debit)transactions (3 months)', 'Large sum (debit)transactions (3 months)', 
    '1', '4', 90, '0', 0, 
    '0', 0, 0, 12, TO_DATE('2/27/2026', 'MM/DD/YYYY'));
Insert into BI_DWH.PIO_AML_RULES
   (COUNTRY_CODE, INST_CODE, RULE_CODE, RULE_DESC_ENG, RULE_DESC_ARB, 
    ACTIVE_FLAG, PERIOD_TYPE, PERIOD_DAYS, USE_SEQUENTIALLY_FLAG, SEQUENTIALLY_COUNT, 
    DEFAULT_RULE_FLAG, EXCLUDE_DAYS, CREATED_BY, UPDATED_BY, UPDATED_DATE)
 Values
   (729, 1, '888894', 'Large sum (credit)transactions (3 months)', 'Large sum (credit)transactions (3 months)', 
    '1', '4', 90, '0', 0, 
    '0', 0, 0, 12, TO_DATE('2/27/2026', 'MM/DD/YYYY'));
Insert into BI_DWH.PIO_AML_RULES
   (COUNTRY_CODE, INST_CODE, RULE_CODE, RULE_DESC_ENG, RULE_DESC_ARB, 
    ACTIVE_FLAG, PERIOD_TYPE, PERIOD_DAYS, USE_SEQUENTIALLY_FLAG, SEQUENTIALLY_COUNT, 
    DEFAULT_RULE_FLAG, EXCLUDE_DAYS, CREATED_BY, UPDATED_BY, UPDATED_DATE)
 Values
   (729, 1, '888895', 'Collective accounts (3 months)', 'Collective accounts (3 months)', 
    '1', '4', 90, '0', 0, 
    '0', 0, 0, 12, TO_DATE('2/27/2026', 'MM/DD/YYYY'));
Insert into BI_DWH.PIO_AML_RULES
   (COUNTRY_CODE, INST_CODE, RULE_CODE, RULE_DESC_ENG, RULE_DESC_ARB, 
    ACTIVE_FLAG, PERIOD_TYPE, PERIOD_DAYS, USE_SEQUENTIALLY_FLAG, SEQUENTIALLY_COUNT, 
    DEFAULT_RULE_FLAG, EXCLUDE_DAYS, CREATED_BY, UPDATED_BY, UPDATED_DATE)
 Values
   (729, 1, '888896', 'Internet Banking (30 days)', 'Internet Banking (30 days)', 
    '1', '3', 30, '0', 0, 
    '0', 0, 0, 12, TO_DATE('2/27/2026', 'MM/DD/YYYY'));
Insert into BI_DWH.PIO_AML_RULES
   (COUNTRY_CODE, INST_CODE, RULE_CODE, RULE_DESC_ENG, RULE_DESC_ARB, 
    ACTIVE_FLAG, PERIOD_TYPE, PERIOD_DAYS, USE_SEQUENTIALLY_FLAG, SEQUENTIALLY_COUNT, 
    DEFAULT_RULE_FLAG, EXCLUDE_DAYS, CREATED_BY, UPDATED_BY, UPDATED_DATE)
 Values
   (729, 1, '888897', 'Credit Cards', 'Credit Cards', 
    '1', '0', 1, '0', 0, 
    '0', 0, 0, 12, TO_DATE('2/27/2026', 'MM/DD/YYYY'));
Insert into BI_DWH.PIO_AML_RULES
   (COUNTRY_CODE, INST_CODE, RULE_CODE, RULE_DESC_ENG, RULE_DESC_ARB, 
    ACTIVE_FLAG, PERIOD_TYPE, PERIOD_DAYS, USE_SEQUENTIALLY_FLAG, SEQUENTIALLY_COUNT, 
    DEFAULT_RULE_FLAG, EXCLUDE_DAYS, CREATED_BY, UPDATED_BY, UPDATED_DATE)
 Values
   (729, 1, '888898', 'offshore', 'offshore', 
    '1', '0', 1, '0', 0, 
    '0', 0, 0, 12, TO_DATE('2/27/2026', 'MM/DD/YYYY'));
Insert into BI_DWH.PIO_AML_RULES
   (COUNTRY_CODE, INST_CODE, RULE_CODE, RULE_DESC_ENG, RULE_DESC_ARB, 
    ACTIVE_FLAG, PERIOD_TYPE, PERIOD_DAYS, USE_SEQUENTIALLY_FLAG, SEQUENTIALLY_COUNT, 
    DEFAULT_RULE_FLAG, EXCLUDE_DAYS, CREATED_BY, CREATED_DATE, UPDATED_BY, 
    UPDATED_DATE)
 Values
   (729, 1, '888902', 'Sudden transaction increase', 'Sudden transaction increase', 
    '1', '0', 1, '0', 0, 
    '0', 0, 12, TO_DATE('3/7/2026', 'MM/DD/YYYY'), 12, 
    TO_DATE('3/7/2026', 'MM/DD/YYYY'));
Insert into BI_DWH.PIO_AML_RULES
   (COUNTRY_CODE, INST_CODE, RULE_CODE, RULE_DESC_ENG, RULE_DESC_ARB, 
    ACTIVE_FLAG, PERIOD_TYPE, PERIOD_DAYS, USE_SEQUENTIALLY_FLAG, SEQUENTIALLY_COUNT, 
    DEFAULT_RULE_FLAG, EXCLUDE_DAYS, CREATED_BY, CREATED_DATE, UPDATED_BY, 
    UPDATED_DATE)
 Values
   (729, 1, '888903', 'Rapid (IN/Out) movements of funds (weekly)(CR)', 'Rapid (IN/Out) movements of funds (weekly)(CR)', 
    '1', '0', 7, '0', 0, 
    '0', 0, 12, TO_DATE('3/7/2026', 'MM/DD/YYYY'), 12, 
    TO_DATE('3/7/2026', 'MM/DD/YYYY'));
Insert into BI_DWH.PIO_AML_RULES
   (COUNTRY_CODE, INST_CODE, RULE_CODE, RULE_DESC_ENG, RULE_DESC_ARB, 
    ACTIVE_FLAG, PERIOD_TYPE, PERIOD_DAYS, USE_SEQUENTIALLY_FLAG, SEQUENTIALLY_COUNT, 
    DEFAULT_RULE_FLAG, EXCLUDE_DAYS, CREATED_BY, CREATED_DATE, UPDATED_BY, 
    UPDATED_DATE)
 Values
   (729, 1, '888904', 'Rapid (IN/Out) movements of funds (weekly)(DR)', 'Rapid (IN/Out) movements of funds (weekly)(DR)', 
    '1', '0', 7, '0', 0, 
    '0', 0, 12, TO_DATE('3/7/2026', 'MM/DD/YYYY'), 12, 
    TO_DATE('3/7/2026', 'MM/DD/YYYY'));
Insert into BI_DWH.PIO_AML_RULES
   (COUNTRY_CODE, INST_CODE, RULE_CODE, RULE_DESC_ENG, RULE_DESC_ARB, 
    ACTIVE_FLAG, PERIOD_TYPE, PERIOD_DAYS, USE_SEQUENTIALLY_FLAG, SEQUENTIALLY_COUNT, 
    DEFAULT_RULE_FLAG, EXCLUDE_DAYS, CREATED_BY, CREATED_DATE, UPDATED_BY, 
    UPDATED_DATE)
 Values
   (729, 1, '888905', 'Large number of visits to different branch offices in the last 30 days', 'Large number of visits to different branch offices in the last 30 days', 
    '1', '3', 30, '0', 0, 
    '0', 0, 12, TO_DATE('3/7/2026', 'MM/DD/YYYY'), 12, 
    TO_DATE('3/7/2026', 'MM/DD/YYYY'));
Insert into BI_DWH.PIO_AML_RULES
   (COUNTRY_CODE, INST_CODE, RULE_CODE, RULE_DESC_ENG, RULE_DESC_ARB, 
    ACTIVE_FLAG, PERIOD_TYPE, PERIOD_DAYS, USE_SEQUENTIALLY_FLAG, SEQUENTIALLY_COUNT, 
    DEFAULT_RULE_FLAG, EXCLUDE_DAYS, CREATED_BY, CREATED_DATE, UPDATED_BY, 
    UPDATED_DATE)
 Values
   (729, 1, '888908', '?? ????? ????? ?????? 15,000 ????? ?? ?? ???????', '?? ????? ????? ?????? 15,000 ????? ?? ?? ???????', 
    '1', '0', 1, '0', 0, 
    '0', 0, 12, TO_DATE('3/16/2026', 'MM/DD/YYYY'), 12, 
    TO_DATE('3/16/2026', 'MM/DD/YYYY'));
Insert into BI_DWH.PIO_AML_RULES
   (COUNTRY_CODE, INST_CODE, RULE_CODE, RULE_DESC_ENG, RULE_DESC_ARB, 
    ACTIVE_FLAG, PERIOD_TYPE, PERIOD_DAYS, USE_SEQUENTIALLY_FLAG, SEQUENTIALLY_COUNT, 
    DEFAULT_RULE_FLAG, EXCLUDE_DAYS, CREATED_BY, CREATED_DATE, UPDATED_BY, 
    UPDATED_DATE)
 Values
   (729, 1, '888910', 'Scenario detects customers (staff) who receive internal transfers, with total amount of 2,000 USD or more (30 days) (1 transaction or more)', 'Scenario detects customers (staff) who receive internal transfers, with total amount of 2,000 USD or more (30 days) (1 transaction or more)', 
    '1', '3', 30, '0', 0, 
    '0', 0, 12, TO_DATE('4/9/2026', 'MM/DD/YYYY'), 12, 
    TO_DATE('4/9/2026', 'MM/DD/YYYY'));
Insert into BI_DWH.PIO_AML_RULES
   (COUNTRY_CODE, INST_CODE, RULE_CODE, RULE_DESC_ENG, RULE_DESC_ARB, 
    ACTIVE_FLAG, PERIOD_TYPE, PERIOD_DAYS, USE_SEQUENTIALLY_FLAG, SEQUENTIALLY_COUNT, 
    DEFAULT_RULE_FLAG, EXCLUDE_DAYS, CREATED_BY, CREATED_DATE, UPDATED_BY, 
    UPDATED_DATE)
 Values
   (729, 1, '888911', 'New customer (customer opening date) with large cash deposits', 'New customer (customer opening date) with large cash deposits', 
    '1', '0', 1, '0', 0, 
    '0', 0, 12, TO_DATE('4/9/2026', 'MM/DD/YYYY'), 12, 
    TO_DATE('4/9/2026', 'MM/DD/YYYY'));
Insert into BI_DWH.PIO_AML_RULES
   (COUNTRY_CODE, INST_CODE, RULE_CODE, RULE_DESC_ENG, RULE_DESC_ARB, 
    ACTIVE_FLAG, PERIOD_TYPE, PERIOD_DAYS, USE_SEQUENTIALLY_FLAG, SEQUENTIALLY_COUNT, 
    DEFAULT_RULE_FLAG, EXCLUDE_DAYS, CREATED_BY, CREATED_DATE, UPDATED_BY, 
    UPDATED_DATE)
 Values
   (729, 1, '888912', 'Countries to be observed (inward)', 'Countries to be observed (inward)', 
    '1', '0', 1, '0', 0, 
    '0', 0, 12, TO_DATE('4/11/2026', 'MM/DD/YYYY'), 12, 
    TO_DATE('4/11/2026', 'MM/DD/YYYY'));
Insert into BI_DWH.PIO_AML_RULES
   (COUNTRY_CODE, INST_CODE, RULE_CODE, RULE_DESC_ENG, RULE_DESC_ARB, 
    ACTIVE_FLAG, PERIOD_TYPE, PERIOD_DAYS, USE_SEQUENTIALLY_FLAG, SEQUENTIALLY_COUNT, 
    DEFAULT_RULE_FLAG, EXCLUDE_DAYS, CREATED_BY, CREATED_DATE, UPDATED_BY, 
    UPDATED_DATE)
 Values
   (729, 1, '888913', 'Countries to be observed (Outward)', 'Countries to be observed (Outward)', 
    '1', '0', 1, '0', 0, 
    '0', 0, 12, TO_DATE('4/11/2026', 'MM/DD/YYYY'), 12, 
    TO_DATE('4/11/2026', 'MM/DD/YYYY'));
Insert into BI_DWH.PIO_AML_RULES
   (COUNTRY_CODE, INST_CODE, RULE_CODE, RULE_DESC_ENG, RULE_DESC_ARB, 
    ACTIVE_FLAG, PERIOD_TYPE, PERIOD_DAYS, USE_SEQUENTIALLY_FLAG, SEQUENTIALLY_COUNT, 
    DEFAULT_RULE_FLAG, EXCLUDE_DAYS, CREATED_BY, CREATED_DATE, UPDATED_BY, 
    UPDATED_DATE)
 Values
   (729, 1, '888914', 'Short-term account', 'Short-term account', 
    '1', '0', 1, '0', 0, 
    '0', 0, 12, TO_DATE('4/11/2026', 'MM/DD/YYYY'), 12, 
    TO_DATE('4/11/2026', 'MM/DD/YYYY'));
Insert into BI_DWH.PIO_AML_RULES
   (COUNTRY_CODE, INST_CODE, RULE_CODE, RULE_DESC_ENG, RULE_DESC_ARB, 
    ACTIVE_FLAG, PERIOD_TYPE, PERIOD_DAYS, USE_SEQUENTIALLY_FLAG, SEQUENTIALLY_COUNT, 
    DEFAULT_RULE_FLAG, EXCLUDE_DAYS, CREATED_BY, CREATED_DATE, UPDATED_BY, 
    UPDATED_DATE)
 Values
   (729, 1, '888915', 'Early loan repayment, large amount', 'Early loan repayment, large amount', 
    '1', '0', 1, '0', 0, 
    '0', 0, 12, TO_DATE('4/12/2026', 'MM/DD/YYYY'), 12, 
    TO_DATE('4/12/2026', 'MM/DD/YYYY'));
Insert into BI_DWH.PIO_AML_RULES
   (COUNTRY_CODE, INST_CODE, RULE_CODE, RULE_DESC_ENG, RULE_DESC_ARB, 
    ACTIVE_FLAG, PERIOD_TYPE, PERIOD_DAYS, USE_SEQUENTIALLY_FLAG, SEQUENTIALLY_COUNT, 
    DEFAULT_RULE_FLAG, EXCLUDE_DAYS, CREATED_BY, CREATED_DATE, UPDATED_BY, 
    UPDATED_DATE)
 Values
   (729, 1, '888916', '??? ???? ???? ???? ?????? ??? ??????? ??????? ?????? ????? ??? ?????? ?????? ?? ???? ???? ??? BBAN ???? ??? ?? ?????? ???? ???? ??????? ?????? ?????? ?? ????? ?????? ??????? ?? ?????? ??????? ???????? ???????.', '??? ???? ???? ???? ?????? ??? ??????? ??????? ?????? ????? ??? ?????? ?????? ?? ???? ???? ??? BBAN ???? ??? ?? ?????? ???? ???? ??????? ?????? ?????? ?? ????? ?????? ??????? ?? ?????? ??????? ???????? ???????.', 
    '1', '0', 7, '0', 0, 
    '0', 0, 12, TO_DATE('7/17/2026', 'MM/DD/YYYY'), 12, 
    TO_DATE('7/17/2026', 'MM/DD/YYYY'));
Insert into BI_DWH.PIO_AML_RULES
   (COUNTRY_CODE, INST_CODE, RULE_CODE, RULE_DESC_ENG, RULE_DESC_ARB, 
    ACTIVE_FLAG, PERIOD_TYPE, PERIOD_DAYS, USE_SEQUENTIALLY_FLAG, SEQUENTIALLY_COUNT, 
    DEFAULT_RULE_FLAG, EXCLUDE_DAYS, CREATED_BY, CREATED_DATE, UPDATED_BY, 
    UPDATED_DATE)
 Values
   (729, 1, '888917', '??? ???? ????? ??????? ?????? ?? ?????? ?????? ?? ???? ???? ??? ????? ???????? ???? ???? ????? (????? 48 ????)? ?? ??? ??? ??????? ?? ??????? ?????.', '??? ???? ????? ??????? ?????? ?? ?????? ?????? ?? ???? ???? ??? ????? ???????? ???? ???? ????? (????? 48 ????)? ?? ??? ??? ??????? ?? ??????? ?????.', 
    '1', '0', 2, '0', 0, 
    '0', 0, 12, TO_DATE('7/17/2026', 'MM/DD/YYYY'), 12, 
    TO_DATE('7/17/2026', 'MM/DD/YYYY'));
Insert into BI_DWH.PIO_AML_RULES
   (COUNTRY_CODE, INST_CODE, RULE_CODE, RULE_DESC_ENG, RULE_DESC_ARB, 
    ACTIVE_FLAG, PERIOD_TYPE, PERIOD_DAYS, USE_SEQUENTIALLY_FLAG, SEQUENTIALLY_COUNT, 
    DEFAULT_RULE_FLAG, EXCLUDE_DAYS, CREATED_BY, CREATED_DATE, UPDATED_BY, 
    UPDATED_DATE)
 Values
   (729, 1, '888918', '??? ???? ????? ??????? ?????? ?? ?????? ?????? ?? ???? ???? ??? ????? ???????? ???? ???? ????? (????? 48 ????)? ?? ??? ??? ??????? ?? ??????? ?????.(out)', '??? ???? ????? ??????? ?????? ?? ?????? ?????? ?? ???? ???? ??? ????? ???????? ???? ???? ????? (????? 48 ????)? ?? ??? ??? ??????? ?? ??????? ?????.(out)', 
    '1', '0', 2, '0', 0, 
    '0', 0, 12, TO_DATE('7/17/2026', 'MM/DD/YYYY'), 12, 
    TO_DATE('7/17/2026', 'MM/DD/YYYY'));
COMMIT;
