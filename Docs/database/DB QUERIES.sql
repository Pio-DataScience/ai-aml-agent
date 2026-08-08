--senario code =  1000000         
--rule code =888908   
 
 SELECT PIO_TRANSACTIONS.CUS_NUM
  FROM PIO_TRANSACTIONS, PIO_CUSTOMERS
 WHERE     PIO_CUSTOMERS.COUNTRY_CODE =
           PIO_TRANSACTIONS.COUNTRY_CODE
       AND PIO_CUSTOMERS.INST_CODE = PIO_TRANSACTIONS.INST_CODE
       AND PIO_CUSTOMERS.CUS_NUM = PIO_TRANSACTIONS.CUS_NUM
       AND PIO_CUSTOMERS.DAY_DATE = '01-JUN-26'
       AND PIO_TRANSACTIONS.COUNTRY_CODE = 729
       AND PIO_TRANSACTIONS.INST_CODE = 1
       AND PIO_TRANSACTIONS.TRA_DATE BETWEEN   TO_DATE ('01062026',
                                                               'DDMMYYYY')
                                                    - 0
                                                AND TO_DATE ('01062026',
                                                             'DDMMYYYY')
       AND 1 * PIO_TRANSACTIONS.equ_tra_amt >= (45000000);
	   
	   
----------------------------------------------------------------------------------------
	    
--senario code =  1000002
--rule code =888891

  SELECT PIO_TRANSACTIONS.CUS_NUM
    FROM PIO_TRANSACTIONS, PIO_CUSTOMERS, LTG_ACCOUNT
   WHERE     PIO_CUSTOMERS.COUNTRY_CODE = PIO_TRANSACTIONS.COUNTRY_CODE
         AND PIO_CUSTOMERS.INST_CODE = PIO_TRANSACTIONS.INST_CODE
         AND PIO_CUSTOMERS.CUS_NUM = PIO_TRANSACTIONS.CUS_NUM
         AND PIO_CUSTOMERS.DAY_DATE = TO_DATE ('01-JUN-2026', 'DD-MM-YYYY')
         AND LTG_ACCOUNT.COUNTRY_CODE = PIO_TRANSACTIONS.COUNTRY_CODE
         AND LTG_ACCOUNT.INST_CODE = PIO_TRANSACTIONS.INST_CODE
         AND LTG_ACCOUNT.BRA_CODE = PIO_TRANSACTIONS.BRA_CODE
         AND LTG_ACCOUNT.CUS_NUM = PIO_TRANSACTIONS.CUS_NUM
         AND LTG_ACCOUNT.CUR_CODE = PIO_TRANSACTIONS.CUR_CODE
         AND LTG_ACCOUNT.LED_CODE = PIO_TRANSACTIONS.LED_CODE
         AND LTG_ACCOUNT.SUB_ACCT_CODE = PIO_TRANSACTIONS.SUB_ACCT_CODE
         AND LTG_ACCOUNT.ACCOUNT_NUMBER = PIO_TRANSACTIONS.ACCOUNT_NUMBER
         AND LTG_ACCOUNT.DAY_DATE = PIO_TRANSACTIONS.TRA_DATE
         AND (   LTG_ACCOUNT.LTG_DEBIT_CREDIT_CODE =
                 PIO_TRANSACTIONS.DEB_CRE_IND
              OR LTG_ACCOUNT.LTG_DEBIT_CREDIT_CODE = 0)
         AND EXPL_CODE IN
                 (SELECT TRANSACTION_TYPE
                    FROM BI_DWH.PIO_LTG_TRASACTION_TYPE
                   WHERE     COUNTRY_CODE = 729
                         AND INST_CODE = 1
                         AND LTG_CODE = LTG_ACCOUNT.LTG_CODE
                         AND LTG_TRANSACTION_CODE = 1)
         AND PIO_TRANSACTIONS.COUNTRY_CODE = 729
         AND PIO_TRANSACTIONS.INST_CODE = 1
         AND PIO_TRANSACTIONS.TRA_DATE BETWEEN   TO_DATE ('01062026',
                                                          'DDMMYYYY')
                                               - 6
                                           AND TO_DATE ('01062026', 'DDMMYYYY')
         AND LTG_ACCOUNT.ltg_code = ('730')
  HAVING     1 * COUNT (PIO_TRANSACTIONS.equ_tra_amt) > (2)
         AND 1 * SUM (PIO_TRANSACTIONS.equ_tra_amt) >= (45000000)
GROUP BY PIO_TRANSACTIONS.CUS_NUM;

--------------------------------------------------------------------------------------------

--senario code =  1000001
--rule code =888890


   SELECT  
           PIO_TRANSACTIONS.CUS_NUM
      FROM PIO_TRANSACTIONS, PIO_CUSTOMERS
     WHERE     PIO_CUSTOMERS.COUNTRY_CODE =
               PIO_TRANSACTIONS.COUNTRY_CODE
           AND PIO_CUSTOMERS.INST_CODE =
               PIO_TRANSACTIONS.INST_CODE
           AND PIO_CUSTOMERS.CUS_NUM = PIO_TRANSACTIONS.CUS_NUM
           AND PIO_CUSTOMERS.DAY_DATE = '01-JUN-26' 
           AND PIO_TRANSACTIONS.COUNTRY_CODE = 729
           AND PIO_TRANSACTIONS.INST_CODE = 1
           AND PIO_TRANSACTIONS.TRA_DATE BETWEEN   TO_DATE (
                                                              '01062026',
                                                              'DDMMYYYY')
                                                        - 0
                                                    AND TO_DATE ('01062026',
                                                                 'DDMMYYYY')
           AND PIO_TRANSACTIONS.expl_code = ('19')
           AND PIO_CUSTOMERS.indv_corp_ind = (2);
 
 
 --------------------------------------------------------------------------------------------
 
--senario code =  1000006
--rule code =888896

  SELECT PIO_TRANSACTIONS.CUS_NUM
    FROM PIO_TRANSACTIONS, PIO_CUSTOMERS, LTG_ACCOUNT
   WHERE     PIO_CUSTOMERS.COUNTRY_CODE = PIO_TRANSACTIONS.COUNTRY_CODE
         AND PIO_CUSTOMERS.INST_CODE = PIO_TRANSACTIONS.INST_CODE
         AND PIO_CUSTOMERS.CUS_NUM = PIO_TRANSACTIONS.CUS_NUM
         AND PIO_CUSTOMERS.DAY_DATE = TO_DATE ('01-JUN-2026', 'DD-MM-YYYY')
         AND LTG_ACCOUNT.COUNTRY_CODE = PIO_TRANSACTIONS.COUNTRY_CODE
         AND LTG_ACCOUNT.INST_CODE = PIO_TRANSACTIONS.INST_CODE
         AND LTG_ACCOUNT.BRA_CODE = PIO_TRANSACTIONS.BRA_CODE
         AND LTG_ACCOUNT.CUS_NUM = PIO_TRANSACTIONS.CUS_NUM
         AND LTG_ACCOUNT.CUR_CODE = PIO_TRANSACTIONS.CUR_CODE
         AND LTG_ACCOUNT.LED_CODE = PIO_TRANSACTIONS.LED_CODE
         AND LTG_ACCOUNT.SUB_ACCT_CODE = PIO_TRANSACTIONS.SUB_ACCT_CODE
         AND LTG_ACCOUNT.ACCOUNT_NUMBER = PIO_TRANSACTIONS.ACCOUNT_NUMBER
         AND LTG_ACCOUNT.DAY_DATE = PIO_TRANSACTIONS.TRA_DATE
         AND (   LTG_ACCOUNT.LTG_DEBIT_CREDIT_CODE =
                 PIO_TRANSACTIONS.DEB_CRE_IND
              OR LTG_ACCOUNT.LTG_DEBIT_CREDIT_CODE = 0)
         AND EXPL_CODE IN
                 (SELECT TRANSACTION_TYPE
                    FROM BI_DWH.PIO_LTG_TRASACTION_TYPE
                   WHERE     COUNTRY_CODE = 729
                         AND INST_CODE = 1
                         AND LTG_CODE = LTG_ACCOUNT.LTG_CODE
                         AND LTG_TRANSACTION_CODE = 1)
         AND PIO_TRANSACTIONS.COUNTRY_CODE = 729
         AND PIO_TRANSACTIONS.INST_CODE = 1
         AND PIO_TRANSACTIONS.TRA_DATE BETWEEN   TO_DATE ('01062026',
                                                          'DDMMYYYY')
                                               - 29
                                           AND TO_DATE ('01062026', 'DDMMYYYY')
         AND LTG_ACCOUNT.ltg_code IN ('213', '214', '215')
         AND PIO_CUSTOMERS.indv_corp_ind = '1'
  HAVING 1 * SUM (PIO_TRANSACTIONS.equ_tra_amt) >= (45000000)
GROUP BY PIO_TRANSACTIONS.CUS_NUM;

-------------------------------------------------------------------------------------------------

--senario code =  1000007
--rule code =888895

 
  SELECT PIO_TRANSACTIONS.CUS_NUM
    FROM PIO_TRANSACTIONS, PIO_CUSTOMERS, LTG_ACCOUNT
   WHERE     PIO_CUSTOMERS.COUNTRY_CODE = PIO_TRANSACTIONS.COUNTRY_CODE
         AND PIO_CUSTOMERS.INST_CODE = PIO_TRANSACTIONS.INST_CODE
         AND PIO_CUSTOMERS.CUS_NUM = PIO_TRANSACTIONS.CUS_NUM
         AND PIO_CUSTOMERS.DAY_DATE = TO_DATE ('01-JUN-2026', 'DD-MM-YYYY')
         AND LTG_ACCOUNT.COUNTRY_CODE = PIO_TRANSACTIONS.COUNTRY_CODE
         AND LTG_ACCOUNT.INST_CODE = PIO_TRANSACTIONS.INST_CODE
         AND LTG_ACCOUNT.BRA_CODE = PIO_TRANSACTIONS.BRA_CODE
         AND LTG_ACCOUNT.CUS_NUM = PIO_TRANSACTIONS.CUS_NUM
         AND LTG_ACCOUNT.CUR_CODE = PIO_TRANSACTIONS.CUR_CODE
         AND LTG_ACCOUNT.LED_CODE = PIO_TRANSACTIONS.LED_CODE
         AND LTG_ACCOUNT.SUB_ACCT_CODE = PIO_TRANSACTIONS.SUB_ACCT_CODE
         AND LTG_ACCOUNT.ACCOUNT_NUMBER = PIO_TRANSACTIONS.ACCOUNT_NUMBER
         AND LTG_ACCOUNT.DAY_DATE = PIO_TRANSACTIONS.TRA_DATE
         AND (   LTG_ACCOUNT.LTG_DEBIT_CREDIT_CODE =
                 PIO_TRANSACTIONS.DEB_CRE_IND
              OR LTG_ACCOUNT.LTG_DEBIT_CREDIT_CODE = 0)
         AND EXPL_CODE IN
                 (SELECT TRANSACTION_TYPE
                    FROM BI_DWH.PIO_LTG_TRASACTION_TYPE
                   WHERE     COUNTRY_CODE = 729
                         AND INST_CODE = 1
                         AND LTG_CODE = LTG_ACCOUNT.LTG_CODE
                         AND LTG_TRANSACTION_CODE = 1)
         AND PIO_TRANSACTIONS.COUNTRY_CODE = 729
         AND PIO_TRANSACTIONS.INST_CODE = 1
         AND PIO_TRANSACTIONS.TRA_DATE BETWEEN   TO_DATE ('01062026',
                                                          'DDMMYYYY')
                                               - 90
                                           AND TO_DATE ('01062026', 'DDMMYYYY')
         AND LTG_ACCOUNT.ltg_code IN ('101',
                                      '102',
                                      '103',
                                      '107',
                                      '108',
                                      '109',
                                      '124',
                                      '142',
                                      '143',
                                      '144',
                                      '275',
                                      '997',
                                      '998')
  HAVING     1 * COUNT (PIO_TRANSACTIONS.equ_tra_amt) > (5)
         AND 1 * SUM (PIO_TRANSACTIONS.equ_tra_amt) >= (45000000)
GROUP BY PIO_TRANSACTIONS.CUS_NUM;


-------------------------------------------------------------------------------------------------

--senario code =  1000008
--rule code =888897


 
  SELECT PIO_TRANSACTIONS.CUS_NUM
    FROM PIO_TRANSACTIONS, PIO_CUSTOMERS
   WHERE     PIO_CUSTOMERS.COUNTRY_CODE = PIO_TRANSACTIONS.COUNTRY_CODE
         AND PIO_CUSTOMERS.INST_CODE = PIO_TRANSACTIONS.INST_CODE
         AND PIO_CUSTOMERS.CUS_NUM = PIO_TRANSACTIONS.CUS_NUM
         AND PIO_CUSTOMERS.DAY_DATE = '01-JUN-26'
         AND PIO_TRANSACTIONS.COUNTRY_CODE = 729
         AND PIO_TRANSACTIONS.INST_CODE = 1
         AND PIO_TRANSACTIONS.TRA_DATE BETWEEN   TO_DATE ('01062026',
                                                          'DDMMYYYY')
                                               - 0
                                           AND TO_DATE ('01062026', 'DDMMYYYY')
         AND PIO_TRANSACTIONS.expl_code IN ('261', '262', '263')
  HAVING 1 * COUNT (PIO_TRANSACTIONS.equ_tra_amt) >= (2)
GROUP BY PIO_TRANSACTIONS.CUS_NUM;

------------------------------------------------------------------------------------------------

--senario code =  1000009
--rule code =888898

   SELECT PIO_TRANSACTIONS.CUS_NUM
    FROM PIO_TRANSACTIONS, PIO_CUSTOMERS
   WHERE     PIO_CUSTOMERS.COUNTRY_CODE = PIO_TRANSACTIONS.COUNTRY_CODE
         AND PIO_CUSTOMERS.INST_CODE = PIO_TRANSACTIONS.INST_CODE
         AND PIO_CUSTOMERS.CUS_NUM = PIO_TRANSACTIONS.CUS_NUM
         AND PIO_CUSTOMERS.DAY_DATE = '01-JUN-26'
         AND PIO_TRANSACTIONS.COUNTRY_CODE = 729
         AND PIO_TRANSACTIONS.INST_CODE = 1
         AND PIO_TRANSACTIONS.TRA_DATE BETWEEN   TO_DATE ('01062026',
                                                          'DDMMYYYY')
                                               - 0
                                           AND TO_DATE ('01062026', 'DDMMYYYY')
         AND PIO_TRANSACTIONS.expl_code IN ('886',
                                            '889',
                                            '997',
                                            '998',
                                            '999')
        AND PIO_CUSTOMERS.indv_corp_ind = (2)                                    
  HAVING     1 * COUNT (PIO_TRANSACTIONS.equ_tra_amt) > (1)
         AND 1 * SUM (PIO_TRANSACTIONS.equ_tra_amt) >= (2000000)
GROUP BY PIO_TRANSACTIONS.CUS_NUM;

--------------------------------------------------------------------------------

--senario code =  1000003
--rule code =888892

   SELECT PIO_TRANSACTIONS.CUS_NUM
    FROM PIO_TRANSACTIONS, PIO_CUSTOMERS, LTG_ACCOUNT
   WHERE     PIO_CUSTOMERS.COUNTRY_CODE = PIO_TRANSACTIONS.COUNTRY_CODE
         AND PIO_CUSTOMERS.INST_CODE = PIO_TRANSACTIONS.INST_CODE
         AND PIO_CUSTOMERS.CUS_NUM = PIO_TRANSACTIONS.CUS_NUM
         AND PIO_CUSTOMERS.DAY_DATE = '01-JUN-26'
         AND LTG_ACCOUNT.COUNTRY_CODE = PIO_TRANSACTIONS.COUNTRY_CODE
         AND LTG_ACCOUNT.INST_CODE = PIO_TRANSACTIONS.INST_CODE
         AND LTG_ACCOUNT.BRA_CODE = PIO_TRANSACTIONS.BRA_CODE
         AND LTG_ACCOUNT.CUS_NUM = PIO_TRANSACTIONS.CUS_NUM
         AND LTG_ACCOUNT.CUR_CODE = PIO_TRANSACTIONS.CUR_CODE
         AND LTG_ACCOUNT.LED_CODE = PIO_TRANSACTIONS.LED_CODE
         AND LTG_ACCOUNT.SUB_ACCT_CODE = PIO_TRANSACTIONS.SUB_ACCT_CODE
         AND LTG_ACCOUNT.ACCOUNT_NUMBER = PIO_TRANSACTIONS.ACCOUNT_NUMBER
         AND LTG_ACCOUNT.DAY_DATE = PIO_TRANSACTIONS.TRA_DATE
         AND (   LTG_ACCOUNT.LTG_DEBIT_CREDIT_CODE =
                 PIO_TRANSACTIONS.DEB_CRE_IND
              OR LTG_ACCOUNT.LTG_DEBIT_CREDIT_CODE = 0)
         AND EXPL_CODE IN
                 (SELECT TRANSACTION_TYPE
                    FROM BI_DWH.PIO_LTG_TRASACTION_TYPE
                   WHERE     COUNTRY_CODE = 729
                         AND INST_CODE = 1
                         AND LTG_CODE = LTG_ACCOUNT.LTG_CODE
                         AND LTG_TRANSACTION_CODE = 1)
         AND PIO_TRANSACTIONS.COUNTRY_CODE = 729
         AND PIO_TRANSACTIONS.INST_CODE = 1
         AND PIO_TRANSACTIONS.TRA_DATE BETWEEN   TO_DATE ('01062026',
                                                          'DDMMYYYY')
                                               - 6
                                           AND TO_DATE ('01062026', 'DDMMYYYY')
         AND LTG_ACCOUNT.ltg_code = ('730')
  HAVING     1 * COUNT (PIO_TRANSACTIONS.equ_tra_amt) > (2)
         AND 1 * SUM (PIO_TRANSACTIONS.equ_tra_amt) >= (45000000)
GROUP BY PIO_TRANSACTIONS.CUS_NUM;


-----------------------------------------------------------------------------------------


--senario code =  1000004
--rule code =888893

   SELECT PIO_TRANSACTIONS.CUS_NUM
    FROM PIO_TRANSACTIONS, PIO_CUSTOMERS, LTG_ACCOUNT
   WHERE     PIO_CUSTOMERS.COUNTRY_CODE = PIO_TRANSACTIONS.COUNTRY_CODE
         AND PIO_CUSTOMERS.INST_CODE = PIO_TRANSACTIONS.INST_CODE
         AND PIO_CUSTOMERS.CUS_NUM = PIO_TRANSACTIONS.CUS_NUM
         AND PIO_CUSTOMERS.DAY_DATE = '01-JUN-26'
         AND LTG_ACCOUNT.COUNTRY_CODE = PIO_TRANSACTIONS.COUNTRY_CODE
         AND LTG_ACCOUNT.INST_CODE = PIO_TRANSACTIONS.INST_CODE
         AND LTG_ACCOUNT.BRA_CODE = PIO_TRANSACTIONS.BRA_CODE
         AND LTG_ACCOUNT.CUS_NUM = PIO_TRANSACTIONS.CUS_NUM
         AND LTG_ACCOUNT.CUR_CODE = PIO_TRANSACTIONS.CUR_CODE
         AND LTG_ACCOUNT.LED_CODE = PIO_TRANSACTIONS.LED_CODE
         AND LTG_ACCOUNT.SUB_ACCT_CODE = PIO_TRANSACTIONS.SUB_ACCT_CODE
         AND LTG_ACCOUNT.ACCOUNT_NUMBER = PIO_TRANSACTIONS.ACCOUNT_NUMBER
         AND LTG_ACCOUNT.DAY_DATE = PIO_TRANSACTIONS.TRA_DATE
         AND (   LTG_ACCOUNT.LTG_DEBIT_CREDIT_CODE =
                 PIO_TRANSACTIONS.DEB_CRE_IND
              OR LTG_ACCOUNT.LTG_DEBIT_CREDIT_CODE = 0)
         AND EXPL_CODE IN
                 (SELECT TRANSACTION_TYPE
                    FROM BI_DWH.PIO_LTG_TRASACTION_TYPE
                   WHERE     COUNTRY_CODE = 729
                         AND INST_CODE = 1
                         AND LTG_CODE = LTG_ACCOUNT.LTG_CODE
                         AND LTG_TRANSACTION_CODE = 1)
         AND PIO_TRANSACTIONS.COUNTRY_CODE = 729
         AND PIO_TRANSACTIONS.INST_CODE = 1
         AND PIO_TRANSACTIONS.TRA_DATE BETWEEN   TO_DATE ('01062026',
                                                          'DDMMYYYY')
                                               - 90
                                           AND TO_DATE ('01062026', 'DDMMYYYY')
         AND LTG_ACCOUNT.ltg_code = ('731')
  HAVING     1 * COUNT (PIO_TRANSACTIONS.equ_tra_amt) > (3)
         AND 1 * SUM (PIO_TRANSACTIONS.equ_tra_amt) >= (45000000)
GROUP BY PIO_TRANSACTIONS.CUS_NUM;

-----------------------------------------------------------------------------------------


--senario code =  1000005
--rule code =888894

   SELECT PIO_TRANSACTIONS.CUS_NUM
    FROM PIO_TRANSACTIONS, PIO_CUSTOMERS, LTG_ACCOUNT
   WHERE     PIO_CUSTOMERS.COUNTRY_CODE = PIO_TRANSACTIONS.COUNTRY_CODE
         AND PIO_CUSTOMERS.INST_CODE = PIO_TRANSACTIONS.INST_CODE
         AND PIO_CUSTOMERS.CUS_NUM = PIO_TRANSACTIONS.CUS_NUM
         AND PIO_CUSTOMERS.DAY_DATE = '01-JUN-26'
         AND LTG_ACCOUNT.COUNTRY_CODE = PIO_TRANSACTIONS.COUNTRY_CODE
         AND LTG_ACCOUNT.INST_CODE = PIO_TRANSACTIONS.INST_CODE
         AND LTG_ACCOUNT.BRA_CODE = PIO_TRANSACTIONS.BRA_CODE
         AND LTG_ACCOUNT.CUS_NUM = PIO_TRANSACTIONS.CUS_NUM
         AND LTG_ACCOUNT.CUR_CODE = PIO_TRANSACTIONS.CUR_CODE
         AND LTG_ACCOUNT.LED_CODE = PIO_TRANSACTIONS.LED_CODE
         AND LTG_ACCOUNT.SUB_ACCT_CODE = PIO_TRANSACTIONS.SUB_ACCT_CODE
         AND LTG_ACCOUNT.ACCOUNT_NUMBER = PIO_TRANSACTIONS.ACCOUNT_NUMBER
         AND LTG_ACCOUNT.DAY_DATE = PIO_TRANSACTIONS.TRA_DATE
         AND (   LTG_ACCOUNT.LTG_DEBIT_CREDIT_CODE =
                 PIO_TRANSACTIONS.DEB_CRE_IND
              OR LTG_ACCOUNT.LTG_DEBIT_CREDIT_CODE = 0)
         AND EXPL_CODE IN
                 (SELECT TRANSACTION_TYPE
                    FROM BI_DWH.PIO_LTG_TRASACTION_TYPE
                   WHERE     COUNTRY_CODE = 729
                         AND INST_CODE = 1
                         AND LTG_CODE = LTG_ACCOUNT.LTG_CODE
                         AND LTG_TRANSACTION_CODE = 1)
         AND PIO_TRANSACTIONS.COUNTRY_CODE = 729
         AND PIO_TRANSACTIONS.INST_CODE = 1
         AND PIO_TRANSACTIONS.TRA_DATE BETWEEN   TO_DATE ('01062026',
                                                          'DDMMYYYY')
                                               - 90
                                           AND TO_DATE ('01062026', 'DDMMYYYY')
         AND LTG_ACCOUNT.ltg_code = ('730')
  HAVING     1 * COUNT (PIO_TRANSACTIONS.equ_tra_amt) > (3)
         AND 1 * SUM (PIO_TRANSACTIONS.equ_tra_amt) >= (45000000)
GROUP BY PIO_TRANSACTIONS.CUS_NUM;
