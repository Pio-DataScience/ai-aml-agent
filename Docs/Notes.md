## Scenario 1

catch any customer whos transactions exceeded 10k in one day.

step one

1) login in into bankbi
2) go to scanrios managemnt.
3) hit the plus tab
4) fill the blanks.
5) approve it.

step 2.

we have big issue: if error is appened to the ""

## CURRENT SYSTEM STATE"" the llm stops me.

===================================================================================
7/16/2026

1) what is the diffrence between the intent_analyst_system_v2 and the prompt hardcoded in the intent_analyst_node function?
2) decomposer_node: this function perioid types isnt fully supported. check the code its if statments to days month year, add a live lookup to the table "PIO_PERIOD_TYPE". do that for the risk level too PIO_AML_DEGREE_RISK, then we might add llm to support it rather than hard coded. if its only a right decision.
3) All lookups tables should be filtered on country and inst code. this will return the items for the specific bank we deploying on rather than the duplicates and noise.
4) The database manager told me that the TRANS_TYPE column is a deprycated legacy column, here in scenarios we only need to fitler on EXPL_CODE. (This needs to be handeled on service b level, just mentioned it here so i rember it, dont do anything here).

===================================================================================
7/22/2026

- The infinte loop issue of intent planner issue is still persistant. only when i ask the agent to add a new logic.

===================================================================================
7/23/2026

- **Decomposer logical operator limitation (OR mapping gap):**
  * **The Problem:** The `QBRuleDetail` mapping engine in `agent.py` currently hardcodes the `combined_rule` parameter to `'AND'` for all rule condition rows (setting only the last sequence row to `'-'`). It does not support mapping `'OR'` connectors.
  * **Why it matters:** When a scenario requires alternative conditions (e.g. `Count >= 5 AND (Volume >= 50k OR Volume > 2x Baseline)`), the engine writes all rule rows with `'AND'` into the database table `PIO_AML_RULES_DETAILS`. This forces the database engine to require BOTH volume conditions, breaking the business logic.
  * **Solution to Implement:** Update the `decomposer` parsing logic in `agent.py` to inspect the generated SQL WHERE/HAVING clauses. If conditions/expressions are separated by an `OR` operator in the query, the decomposer must dynamically pass `combined="OR"` to `_make_detail` for the corresponding rule row.

===================================================================================
8/10/2026

4. Interface — what a production AML console needs beyond the chat

Your chat + side-panel pattern is right for the build experience. Production-ready means adding these surfaces:

Screen	Purpose
Scenario Library / Catalog	Every persisted scenario, searchable, with status (active/paused/deprecated), owner, last-run date, alert volume trend
Version History & Diff Viewer	Side-by-side comparison of scenario versions — what threshold changed, who changed it
Approval Queue	Pending scenarios awaiting checker sign-off, with SLA countdown
Shadow Test Sandbox	Re-run any historical scenario against a chosen date range without touching production
Rollback / Deactivate	One-click disable of a live scenario if it's generating false-positive floods, with reason logging
Explainability Panel	Plain-English "why did this alert" trace-back from a production alert to the originating AMLIntent field
Alert Volume Dashboard	Post-deployment monitoring — is this scenario's daily alert count drifting from shadow test baseline?
Admin / Config Console	Manage Extraction Laws, explanation code catalog, embedding cache refresh — without redeploying code
Escalation Report Center	You already have this artifact — needs a persistent inbox view, not just chat-ephemeral

- huge feature integration completing "`Production Scenario Registry Module`":

what is next?

The tables need to expand PIO_AML_PRODUCTION_SCENARIOS beyound this created one, currently we insert into it the query only.

we need to start persisting scnarios deatlis along with audit deatlis e.g. the exact intent payload that generated this query, along with the implementaion plan, with versioning of the schema pydantc that was in service when it was created etc...
thats one.

 another tables should be created for scenario deatils and that will be used to insert the alerts on the alerts tables "PIO_AML_CUSTOMERS", "PIO_AML_CUSTOMERS_DET"

PIO_AML_SCENARIO IS ALSO NEEDED NOT BEACUSE WE NEED THIS TABE EXACTLY RATHER THE INFORMATION WE WILL fill in it is all scenario descriptions etc. used by other modules in the company to show alerts with their deatils so we need to stick to these 3 tables for now.

we might but still i need to confrim fill the minum filed from these tables SELECT * FROM PIO_AML_RULES
SELECT * FROM PIO_AML_SCENARIO_RULES why? beeacuse they are connected to the other tables as primary keys and existing legacy systems might fail on them if they are empty. (not important now even if we dont know their logic).

## Interactive scenario creation

once we reach the final level of this scenario life cycle "`Registry Module`" a determanistic scenario fileds filling layer should start, whith the help of the llm so it is informed with meta data about every thing the user selects. or keep it fully handeled by the llm, this needs to be disscussed.

to make sure o undertsand this layer correctly ill give you examples:

a scenario needs a defined risk degree flag (values defined in a lookup table called PIO_AML_DEGREE_RISK) an officer picks one of the values in it for the scenario he creetaed. thats one example of the things we need to fill to create the scnario workflow.

===================================================================================
8/11/2026

## 5 Main System Tables Structure & Schema (DDL)

Below is the documented column structure and DDL for the 5 core tables driving the AML scenario creation and execution workflow.

---

### 1. `PIO_AML_SCENARIO`

**Purpose**: Stores high-level scenario metadata, descriptions, category, risk degree, period type, and status flags.

| Column Name         | Data Type         | Nullable | Description / Usage                        |
| ------------------- | ----------------- | -------- | ------------------------------------------ |
| `COUNTRY_CODE`    | `NUMBER`        | NO       | Country identifier code                    |
| `INST_CODE`       | `NUMBER`        | NO       | Institution identifier code                |
| `SCENARIO_CODE`   | `VARCHAR2(40)`  | NO       | Unique scenario ID string                  |
| `SCENARIO_DESC`   | `VARCHAR2(400)` | YES      | Natural language scenario description      |
| `CATEG_CODE`      | `VARCHAR2(40)`  | YES      | Category classification code               |
| `SCENARIO_STATE`  | `VARCHAR2(40)`  | YES      | State flag (e.g. Active/Inactive)          |
| `PERIOD_TYPE`     | `VARCHAR2(40)`  | YES      | Observation window unit (e.g. D, M, Y)     |
| `PERIOD_NUM`      | `NUMBER`        | YES      | Observation window numeric duration        |
| `RISK_DEGREE`     | `VARCHAR2(40)`  | YES      | Risk level classification                  |
| `VIOLATION_LEVEL` | `VARCHAR2(40)`  | YES      | Severity level of scenario breach          |
| `CREATED_BY`      | `NUMBER`        | YES      | User ID of author                          |
| `ACTIVE_FLAG`     | `VARCHAR2(40)`  | YES      | Active flag ('1' = Active, '0' = Inactive) |
| `SYS_DATE`        | `DATE`          | YES      | Creation/modification timestamp            |
| `VERSION_NUM`     | `NUMBER`        | YES      | Schema versioning tracker                  |
| `USER_NAME`       | `VARCHAR2(400)` | YES      | Username of author/modifier                |

```sql
CREATE TABLE PIO_AML_SCENARIO (
    COUNTRY_CODE NUMBER NOT NULL,
    INST_CODE NUMBER NOT NULL,
    SCENARIO_CODE VARCHAR2(40) NOT NULL,
    SCENARIO_DESC VARCHAR2(400),
    CATEG_CODE VARCHAR2(40),
    SCENARIO_STATE VARCHAR2(40),
    PERIOD_TYPE VARCHAR2(40),
    PERIOD_NUM NUMBER,
    RISK_DEGREE VARCHAR2(40),
    VIOLATION_LEVEL VARCHAR2(40),
    CREATED_BY NUMBER,
    ACTIVE_FLAG VARCHAR2(40),
    SYS_DATE DATE,
    VERSION_NUM NUMBER,
    USER_NAME VARCHAR2(400),
    PRIMARY KEY (COUNTRY_CODE, INST_CODE, SCENARIO_CODE)
);
```

---

### 2. `PIO_AML_PRODUCTION_SCENARIOS`

**Purpose**: Production registry table for agent-generated scenarios. Stores the raw Oracle SQL detection queries executed in daily ETL alert runs.

| Column Name          | Data Type         | Nullable | Description / Usage                                       |
| -------------------- | ----------------- | -------- | --------------------------------------------------------- |
| `SCENARIO_ID`      | `VARCHAR2(50)`  | NO       | Primary key identifier (e.g.,`PRD_178263`)              |
| `SCENARIO_NAME`    | `VARCHAR2(250)` | NO       | Descriptive title of detection scenario                   |
| `SCENARIO_TYPE`    | `VARCHAR2(50)`  | NO       | Target grain (`CUSTOMER`, `TRANSACTION`, `ACCOUNT`) |
| `DETECTION_LOGIC`  | `CLOB`          | YES      | Executive plain-English summary                           |
| `RAW_SQL`          | `CLOB`          | NO       | Executable Oracle SQL statement                           |
| `TIME_WINDOW_DAYS` | `NUMBER`        | YES      | Extracted observation window duration in days             |
| `CREATED_BY`       | `VARCHAR2(100)` | YES      | Author ID / service tag (`aml_builder_agent`)           |
| `CREATED_AT`       | `TIMESTAMP(6)`  | YES      | UTC creation timestamp                                    |
| `IS_ACTIVE`        | `NUMBER(1,0)`   | YES      | Active status flag (1 = Active, 0 = Inactive)             |

```sql
CREATE TABLE PIO_AML_PRODUCTION_SCENARIOS (
    SCENARIO_ID VARCHAR2(50) NOT NULL PRIMARY KEY,
    SCENARIO_NAME VARCHAR2(250) NOT NULL,
    SCENARIO_TYPE VARCHAR2(50) NOT NULL,
    DETECTION_LOGIC CLOB,
    RAW_SQL CLOB NOT NULL,
    TIME_WINDOW_DAYS NUMBER,
    CREATED_BY VARCHAR2(100),
    CREATED_AT TIMESTAMP(6),
    IS_ACTIVE NUMBER(1,0) DEFAULT 1
);
```

---

### 3. `PIO_AML_CUSTOMERS`

**Purpose**: Alert Header table storing high-level customer alert records generated by daily detection queries.

| Column Name                | Data Type          | Nullable | Description / Usage                  |
| -------------------------- | ------------------ | -------- | ------------------------------------ |
| `DAY_DATE`               | `DATE`           | NO       | Daily batch run snapshot date        |
| `COUNTRY_CODE`           | `NUMBER`         | NO       | Country identifier code              |
| `INST_CODE`              | `NUMBER`         | NO       | Institution identifier code          |
| `CUS_NUM`                | `VARCHAR2(400)`  | NO       | Flagged customer number              |
| `AML_SCENARIO_CODE`      | `VARCHAR2(40)`   | NO       | Associated scenario code             |
| `CUS_NAME`               | `VARCHAR2(200)`  | YES      | Flagged customer full name           |
| `INITIAL_STATUS`         | `VARCHAR2(40)`   | YES      | Alert triage initial status          |
| `FINAL_STATUS`           | `VARCHAR2(40)`   | YES      | Final resolution status              |
| `SEQ`                    | `NUMBER`         | NO       | Alert sequence counter               |
| `WORKCASE`               | `VARCHAR2(40)`   | YES      | Compliance case tracker reference    |
| `CURRENT_STEP`           | `VARCHAR2(40)`   | YES      | Workflow step ID                     |
| `CURRENT_STATUS`         | `VARCHAR2(40)`   | YES      | Workflow current status              |
| `MANUAL_ALERT_DESC`      | `VARCHAR2(4000)` | YES      | Officer narrative / notes            |
| `RISK_DEGREE`            | `VARCHAR2(40)`   | YES      | Evaluated risk score flag            |
| `CIF`                    | `VARCHAR2(40)`   | YES      | Customer Information File identifier |
| `BRA_CODE`               | `VARCHAR2(40)`   | YES      | Primary branch code                  |
| `MOBILE_NO`              | `VARCHAR2(40)`   | YES      | Contact mobile number                |
| `ID_NUMBER`              | `VARCHAR2(40)`   | YES      | National / Civil ID number           |
| `CREATION_DATE`          | `DATE`           | YES      | Alert creation date                  |
| `AI_GENERATED_NARRATIVE` | `CLOB`           | YES      | Agent-generated alert explanation    |
| `AI_MACRO_PAYLOAD`       | `CLOB`           | YES      | Macro feature context payload        |

```sql
CREATE TABLE PIO_AML_CUSTOMERS (
    DAY_DATE DATE NOT NULL,
    COUNTRY_CODE NUMBER NOT NULL,
    INST_CODE NUMBER NOT NULL,
    CUS_NUM VARCHAR2(400) NOT NULL,
    AML_SCENARIO_CODE VARCHAR2(40) NOT NULL,
    CUS_NAME VARCHAR2(200),
    INITIAL_STATUS VARCHAR2(40),
    FINAL_STATUS VARCHAR2(40),
    SEQ NUMBER NOT NULL,
    WORKCASE VARCHAR2(40),
    CURRENT_STEP VARCHAR2(40),
    CURRENT_STATUS VARCHAR2(40),
    MANUAL_ALERT_DESC VARCHAR2(4000),
    SEND_BY NUMBER(7,0),
    SWIFT_ACH_PK NUMBER,
    RISK_DEGREE VARCHAR2(40),
    CIF VARCHAR2(40),
    BRA_CODE VARCHAR2(40),
    HR_FLAG VARCHAR2(40),
    EVIDENCE_FLAG VARCHAR2(40),
    MODULE_TYPE VARCHAR2(40),
    API_CASE_ID VARCHAR2(400),
    CHECK_BY NUMBER(7,0),
    MOBILE_NO VARCHAR2(40),
    ID_NUMBER VARCHAR2(40),
    CUS_NAME_NAT VARCHAR2(400),
    ID VARCHAR2(200),
    FOLLOW_UP_FLAG VARCHAR2(40),
    FOLLOW_UP_DATE DATE,
    LOCKED_ALERT NUMBER,
    LOCKED_BY NUMBER,
    CREATION_DATE DATE,
    MT_TYPE VARCHAR2(40),
    MT_REF_NUM VARCHAR2(4000),
    IAML_NEXT_DECISION VARCHAR2(400),
    IAML_NEXT_ACCTION VARCHAR2(400),
    AML_NEXT_DECISION VARCHAR2(400),
    AML_NEXT_ACTION VARCHAR2(400),
    AI_GENERATED_NARRATIVE CLOB,
    AI_MACRO_PAYLOAD CLOB,
    AML_NEXT_DECISION_AR VARCHAR2(4000),
    AML_REASON_DESC_AR VARCHAR2(4000),
    PRIMARY KEY (DAY_DATE, COUNTRY_CODE, INST_CODE, CUS_NUM, AML_SCENARIO_CODE, SEQ)
);
```

---

### 4. `PIO_AML_CUSTOMERS_DET`

**Purpose**: Alert Detail table mapping specific underlying transactions from `PIO_TRANSACTIONS` to the parent alert header in `PIO_AML_CUSTOMERS`.

| Column Name            | Data Type        | Nullable | Description / Usage                           |
| ---------------------- | ---------------- | -------- | --------------------------------------------- |
| `DAY_DATE`           | `DATE`         | NO       | Alert evaluation snapshot date                |
| `TRA_DAY_DATE`       | `DATE`         | NO       | Transaction execution day date                |
| `COUNTRY_CODE`       | `NUMBER`       | NO       | Country identifier code                       |
| `INST_CODE`          | `NUMBER`       | NO       | Institution identifier code                   |
| `TRA_DATE`           | `DATE`         | NO       | Transaction timestamp                         |
| `TRA_SEQ1`           | `VARCHAR2(40)` | NO       | Primary transaction sequence component        |
| `TRA_SEQ2`           | `VARCHAR2(40)` | NO       | Secondary transaction sequence component      |
| `BRA_CODE`           | `VARCHAR2(40)` | NO       | Originating branch code                       |
| `CUS_NUM`            | `VARCHAR2(40)` | NO       | Flagged customer number                       |
| `CUR_CODE`           | `VARCHAR2(40)` | NO       | Transaction currency code                     |
| `LED_CODE`           | `VARCHAR2(40)` | NO       | Ledger code                                   |
| `SUB_ACCT_CODE`      | `VARCHAR2(40)` | NO       | Sub-account code                              |
| `ACCOUNT_NUMBER`     | `VARCHAR2(40)` | YES      | Full account number                           |
| `AML_RULE_CODE`      | `VARCHAR2(40)` | NO       | Specific rule breach identifier               |
| `AML_SCENARIO_CODE`  | `VARCHAR2(40)` | NO       | Parent scenario code                          |
| `TRA_AMT`            | `NUMBER`       | YES      | Raw transaction amount                        |
| `EQU_TRA_AMT`        | `NUMBER`       | YES      | Equivalent transaction amount (base currency) |
| `EXPL_CODE`          | `VARCHAR2(40)` | YES      | Transaction explanation code                  |
| `SEQ`                | `NUMBER`       | NO       | Detail sequence counter                       |
| `LINK_CUS_NUM`       | `VARCHAR2(40)` | NO       | Counterparty / linked customer number         |
| `REL_TYPE`           | `VARCHAR2(40)` | NO       | Counterparty relationship type                |
| `ENTITIY_SIGNATORY`  | `VARCHAR2(40)` | NO       | Entity signatory flag                         |
| `AI_MICRO_PAYLOAD`   | `CLOB`         | YES      | Micro-feature payload per transaction         |
| `AI_MICRO_NARRATIVE` | `CLOB`         | YES      | Transaction-level explanation narrative       |

```sql
CREATE TABLE PIO_AML_CUSTOMERS_DET (
    DAY_DATE DATE NOT NULL,
    TRA_DAY_DATE DATE NOT NULL,
    COUNTRY_CODE NUMBER NOT NULL,
    INST_CODE NUMBER NOT NULL,
    TRA_DATE DATE NOT NULL,
    TRA_SEQ1 VARCHAR2(40) NOT NULL,
    TRA_SEQ2 VARCHAR2(40) NOT NULL,
    BRA_CODE VARCHAR2(40) NOT NULL,
    CUS_NUM VARCHAR2(40) NOT NULL,
    CUR_CODE VARCHAR2(40) NOT NULL,
    LED_CODE VARCHAR2(40) NOT NULL,
    SUB_ACCT_CODE VARCHAR2(40) NOT NULL,
    ACCOUNT_NUMBER VARCHAR2(40),
    AML_RULE_CODE VARCHAR2(40) NOT NULL,
    AML_SCENARIO_CODE VARCHAR2(40) NOT NULL,
    TRA_AMT NUMBER,
    EXPL_CODE VARCHAR2(40),
    TIME_STAMP VARCHAR2(50),
    EQU_TRA_AMT NUMBER,
    SEQ NUMBER NOT NULL,
    BENF_ID VARCHAR2(40),
    CIF VARCHAR2(40),
    LAST_EQU_TRA_AMT NUMBER,
    LAST_TRA_AMT NUMBER,
    COMPANY_CODE VARCHAR2(40),
    COMPANY_NAME VARCHAR2(200),
    STOCK_TRADE_PRICE NUMBER,
    DEPTOR_CUS_NUMBER VARCHAR2(40),
    ORIGT_BRA_CODE VARCHAR2(40),
    LINK_CUS_NUM VARCHAR2(40) NOT NULL,
    REL_TYPE VARCHAR2(40) NOT NULL,
    ORIGT_COUNTRY_CODE VARCHAR2(40),
    MERCHANT_NAME VARCHAR2(200),
    ID_NUMBER VARCHAR2(40),
    ID_TYPE VARCHAR2(40),
    MOBILE_NO VARCHAR2(40),
    ID VARCHAR2(200),
    ENTITIY_SIGNATORY VARCHAR2(40) NOT NULL,
    CREDIT_CARD_NUMBER VARCHAR2(40),
    BANK_NAME VARCHAR2(100),
    PAYMENT_METHOD VARCHAR2(40),
    ADDITIONAL_INFORMATION VARCHAR2(200),
    AML_COL1 VARCHAR2(100),
    AML_COL2 VARCHAR2(100),
    AML_COL3 VARCHAR2(100),
    AML_COL4 VARCHAR2(100),
    AML_COL5 VARCHAR2(100),
    AML_COL6 NUMBER,
    AML_COL7 NUMBER,
    AML_COL8 NUMBER,
    AML_COL9 NUMBER,
    AML_COL10 NUMBER,
    CONFIDENCE_SCORE VARCHAR2(10),
    AML_REASON_COLS VARCHAR2(1000),
    AML_REASON_DESC CLOB,
    AI_MICRO_PAYLOAD CLOB,
    AI_MICRO_NARRATIVE CLOB,
    AML_NEXT_DECISION_AR VARCHAR2(4000),
    AML_REASON_DESC_AR VARCHAR2(4000),
    AI_SUPERVISED_PAYLOAD CLOB,
    AI_SUPERVISED_NARRATIVE CLOB,
    AI_SUPERVISED_SCORE NUMBER(10,4),
    AI_SUPERVISED_DECISION VARCHAR2(500),
    PRIMARY KEY (DAY_DATE, COUNTRY_CODE, INST_CODE, CUS_NUM, AML_SCENARIO_CODE, TRA_DATE, TRA_SEQ1, TRA_SEQ2, SEQ)
);
```

---

### 5. `PIO_TRANSACTIONS`

**Purpose**: Core DWH transaction ledger table holding all financial transaction records processed across bank channels.

| Column Name        | Data Type          | Nullable | Description / Usage                                    |
| ------------------ | ------------------ | -------- | ------------------------------------------------------ |
| `DAY_DATE`       | `DATE`           | NO       | Ledger posting day date                                |
| `COUNTRY_CODE`   | `NUMBER`         | NO       | Country identifier code                                |
| `INST_CODE`      | `NUMBER`         | NO       | Institution identifier code                            |
| `TRA_DATE`       | `DATE`           | NO       | Transaction timestamp                                  |
| `TRA_SEQ1`       | `VARCHAR2(40)`   | NO       | Primary transaction sequence number                    |
| `TRA_SEQ2`       | `VARCHAR2(40)`   | NO       | Secondary transaction sequence number                  |
| `BRA_CODE`       | `VARCHAR2(40)`   | NO       | Branch code                                            |
| `CUS_NUM`        | `VARCHAR2(40)`   | NO       | Customer account owner number                          |
| `CUR_CODE`       | `VARCHAR2(40)`   | NO       | ISO currency code                                      |
| `LED_CODE`       | `VARCHAR2(40)`   | NO       | Ledger code                                            |
| `SUB_ACCT_CODE`  | `VARCHAR2(40)`   | NO       | Sub-account code                                       |
| `TRA_AMT`        | `NUMBER`         | YES      | Original transaction amount                            |
| `DEB_CRE_IND`    | `VARCHAR2(40)`   | YES      | Debit/Credit indicator ('D'/'C')                       |
| `CRNT_BAL`       | `NUMBER`         | YES      | Post-transaction account balance                       |
| `EXPL_CODE`      | `VARCHAR2(40)`   | YES      | Explanation code (transaction classification)          |
| `EQU_TRA_AMT`    | `NUMBER`         | YES      | Base currency equivalent amount                        |
| `TRANS_TYPE`     | `VARCHAR2(40)`   | YES      | Legacy transaction type (deprecated; use`EXPL_CODE`) |
| `ACCOUNT_NUMBER` | `VARCHAR2(100)`  | YES      | Full account number string                             |
| `TRANS_DESC`     | `VARCHAR2(4000)` | YES      | Detailed transaction narrative / remark                |

```sql
CREATE TABLE PIO_TRANSACTIONS (
    DAY_DATE DATE NOT NULL,
    COUNTRY_CODE NUMBER NOT NULL,
    INST_CODE NUMBER NOT NULL,
    TRA_DATE DATE NOT NULL,
    TRA_SEQ1 VARCHAR2(40) NOT NULL,
    TRA_SEQ2 VARCHAR2(40) NOT NULL,
    BRA_CODE VARCHAR2(40) NOT NULL,
    CUS_NUM VARCHAR2(40) NOT NULL,
    CUR_CODE VARCHAR2(40) NOT NULL,
    LED_CODE VARCHAR2(40) NOT NULL,
    SUB_ACCT_CODE VARCHAR2(40) NOT NULL,
    TELL_ID VARCHAR2(40),
    EXT_INT_FLAG VARCHAR2(40),
    DEP_CODE VARCHAR2(40),
    DIS_CODE VARCHAR2(40),
    TRA_AMT NUMBER,
    DEB_CRE_IND VARCHAR2(40),
    CRNT_BAL NUMBER,
    MAN_APP VARCHAR2(40),
    MAN_REP VARCHAR2(40),
    EXPL_CODE VARCHAR2(40),
    VAL_DATE DATE,
    INT_DATE DATE,
    CAN_REA_CODE VARCHAR2(40),
    DOC_ALP VARCHAR2(40),
    DOC_NUM VARCHAR2(40),
    CUR_PRI NUMBER,
    EQU_TRA_AMT NUMBER,
    ORIGT_BRA_CODE VARCHAR2(40),
    ORIGT_TRA_DATE DATE,
    ORIGT_TRA_SEQ1 VARCHAR2(40),
    ORIGT_TRA_SEQ2 VARCHAR2(40),
    REMARKS VARCHAR2(200),
    BANK_CODE VARCHAR2(40),
    CITY_LOC_CODE VARCHAR2(40),
    DRA_ON_BRA_CODE VARCHAR2(40),
    HO_TELL_ID VARCHAR2(40),
    UPD_TIME VARCHAR2(40),
    TRANS_TYPE VARCHAR2(40),
    TRANS_CAT VARCHAR2(40),
    ACCOUNT_NUMBER VARCHAR2(100),
    OFFICER_CODE VARCHAR2(40),
    NARR_LINE1 VARCHAR2(250),
    NARR_LINE2_3 VARCHAR2(60),
    REV_FLAG VARCHAR2(40),
    TRANSACTION_KEY VARCHAR2(200),
    PRODUCT_CODE VARCHAR2(40),
    SUND_REF_CODE VARCHAR2(40),
    USR_CODE1 VARCHAR2(40),
    USR_CODE2 VARCHAR2(40),
    PRIN_PASSB_FLAG VARCHAR2(40),
    LOAN_STMT_NUM NUMBER,
    TIME_STAMP VARCHAR2(40),
    WORKSTA_ID VARCHAR2(40),
    AUTH_ID VARCHAR2(40),
    INPUT_BRA VARCHAR2(40),
    TRANS_REF VARCHAR2(40),
    CHANNEL_TRANS_ID VARCHAR2(40),
    REFERENCE_CURRENCY_CODE VARCHAR2(40),
    CUST_MGR VARCHAR2(40),
    MONTH NUMBER,
    YEAR NUMBER,
    DEPTOR_CUS_NUMBER VARCHAR2(40),
    CONDUCTOR_FLAG VARCHAR2(400),
    CONDUCTOR_ID VARCHAR2(400),
    NOT_MY_CLIENT_PASSPORT VARCHAR2(400),
    MERCHANT_NAME VARCHAR2(400),
    EXPL_DET_CODE VARCHAR2(40),
    TELLER_ID VARCHAR2(40),
    CHEUQ_DEPTOR_NUMBER VARCHAR2(300),
    TELLER_NAME VARCHAR2(200),
    TRANS_DESC VARCHAR2(4000),
    STOCK_NORMAL_PRICE NUMBER,
    STOCK_TRADE_PRICE NUMBER,
    BATCH_NO VARCHAR2(40),
    ORIGT_COUNTRY_CODE VARCHAR2(40),
    ORIGT_TELL_ID VARCHAR2(40),
    ID_NUMBER VARCHAR2(40),
    ID_TYPE VARCHAR2(40),
    MOBILE_NO VARCHAR2(40),
    SOURCE_ID VARCHAR2(40),
    FIRST_TRANS_FLAG VARCHAR2(40),
    CREDIT_CARD_NUMBER VARCHAR2(40),
    BANK_NAME VARCHAR2(100),
    PAYMENT_METHOD VARCHAR2(40),
    AML_COL1 VARCHAR2(100),
    AML_COL2 VARCHAR2(100),
    AML_COL3 VARCHAR2(100),
    AML_COL4 VARCHAR2(100),
    AML_COL5 VARCHAR2(100),
    AML_COL6 NUMBER,
    AML_COL7 NUMBER,
    AML_COL8 NUMBER,
    AML_COL9 NUMBER,
    AML_COL10 NUMBER,
    CIF VARCHAR2(40),
    CUR_RATE NUMBER,
    TRANS_ADDRESS_ID VARCHAR2(40),
    TRANSACTION_IS_SUSPICIOUS VARCHAR2(40),
    CONDUCTOR_IS_SUSPECTED VARCHAR2(40),
    REASON VARCHAR2(250),
    TRANS_ADD_INFO_ID VARCHAR2(40),
    PRIMARY KEY (DAY_DATE, COUNTRY_CODE, INST_CODE, TRA_DATE, TRA_SEQ1, TRA_SEQ2, BRA_CODE, CUS_NUM, CUR_CODE, LED_CODE, SUB_ACCT_CODE)
);
```
