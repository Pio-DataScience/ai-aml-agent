### Answers regarding your questions:

1) here are the whole tables the system uses:

"""
(these tables have Table / Field Catalog, but we will igonre them why? beacuse we dont need to cap our self with them only the piotech agent can wrtie any query then why use the capped tables/filed that where made for the bank officers).
pio_aml_tables
pio_aml_columns
pio_aml_parameters
"""

"""
(These are the tables we need to use for building scenarios)
PIO_AML_SCENARIO
PIO_AML_SCENARIO_RULES
PIO_AML_RULES
PIO_AML_RULES_DETAILS
"""

(we ignore these ones for now )
PIO_LTG_DEFINITION
PIO_LTG_TRASACTION_TYPE
LTG_ACCOUNT

2) we have this procdure we need to run it then it will populate and run all the dfined scenarios in the above tables. "FILL_PIO_AML_CUSTOMERS"
then the alerts will be populated in PIO_AML_CUSTOMERS table we can filter ours scenario_code for the scenario we created and count its alerts. using a column called AML_SCENARIO_CODE in PIO_AML_CUSTOMERS table.

3) already answeereeed in point 1.

4) 
PIO_AML_CUSTOMERS

5) DWH general 
http://localhost:8001/chat/stream

6) notes:
we are going to have a configred .env file for every thing along side the defulats values e.g. country_code = 400, inst_code = 1 etc..


## Main tables columns needed to be filled

PIO_AML_SCENARIO
(COUNTRY_CODE, INST_CODE, SCENARIO_CODE, SCENARIO_DES_ENG, ACTIVE_FLAG, VIOLATION_LEVEL, DEGREE_RISK_FLAG, CREATED_BY, CREATED_DATE)

PIO_AML_SCENARIO_RULES
(COUNTRY_CODE, INST_CODE, AML_RULE_CODE, AML_SCENARIO, RULE_SEQ, RULE_TYPE, AMT_PERC, MARGIN_PERC, STOP_PERIOD).

PIO_AML_RULES
(COUNTRY_CODE, INST_CODE, RULE_CODE, RULE_DES_ENG, ACTIVE_FLAG, PERIOD_TYPE, PERIOD_DAYS, LTG_CODE, CREATED_BY, CREATED_DATE).

PIO_AML_RULES_DETAILS
(COUNTRY_CODE, INST_CODE, PARAMETER_CODE, RULE_CODE, RULE_SEQ,RULE_OPERATOR, COMPARISON_VALUE_FROM, COMBINED_RULE, COMPARISON_VALUE_TO, COMPARISON_VALUE_FROM_DES, SCENARIO_CODE). This table still have more columns you can query it dirctly from the db to understand it more thats what i know for now.