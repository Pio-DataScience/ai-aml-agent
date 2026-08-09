-- =============================================================================
-- DDL Script: PIO_AML_PRODUCTION_SCENARIOS
-- Description: Registry table for active, confirmed production-grade AML scenarios
-- Target Engine: Oracle DWH (BI_DWH / PIO_AML)
-- =============================================================================

CREATE TABLE PIO_AML_PRODUCTION_SCENARIOS (
    SCENARIO_ID          VARCHAR2(50) PRIMARY KEY,
    SCENARIO_NAME        VARCHAR2(250) NOT NULL,
    SCENARIO_TYPE        VARCHAR2(50) NOT NULL,
    DETECTION_LOGIC      CLOB,
    RAW_SQL              CLOB NOT NULL,
    TIME_WINDOW_DAYS     NUMBER,
    CREATED_BY           VARCHAR2(100) DEFAULT 'COMPLIANCE_OFFICER',
    CREATED_AT           TIMESTAMP DEFAULT SYSTIMESTAMP,
    IS_ACTIVE            NUMBER(1) DEFAULT 1
);

-- Table & Column Comments
COMMENT ON TABLE PIO_AML_PRODUCTION_SCENARIOS IS 'Registry table for confirmed production AML scenarios executed by daily ETL runner';
COMMENT ON COLUMN PIO_AML_PRODUCTION_SCENARIOS.SCENARIO_ID IS 'Unique scenario identifier (e.g. PRD_A1B2C3D4)';
COMMENT ON COLUMN PIO_AML_PRODUCTION_SCENARIOS.SCENARIO_NAME IS 'Human-readable name of the AML scenario';
COMMENT ON COLUMN PIO_AML_PRODUCTION_SCENARIOS.SCENARIO_TYPE IS 'Scenario detection category (CUSTOMER, ACCOUNT, TRANSACTION)';
COMMENT ON COLUMN PIO_AML_PRODUCTION_SCENARIOS.DETECTION_LOGIC IS 'Plain English explanation of detection ideology';
COMMENT ON COLUMN PIO_AML_PRODUCTION_SCENARIOS.RAW_SQL IS 'Production-grade ANSI Oracle SQL query from Service B';
COMMENT ON COLUMN PIO_AML_PRODUCTION_SCENARIOS.TIME_WINDOW_DAYS IS 'Observation lookback window in days';
COMMENT ON COLUMN PIO_AML_PRODUCTION_SCENARIOS.CREATED_BY IS 'User or agent that activated the scenario';
COMMENT ON COLUMN PIO_AML_PRODUCTION_SCENARIOS.CREATED_AT IS 'Timestamp when scenario was committed to production';
COMMENT ON COLUMN PIO_AML_PRODUCTION_SCENARIOS.IS_ACTIVE IS '1 = Active for daily ETL execution, 0 = Disabled';

-- Performance Index for ETL query lookup
CREATE INDEX IDX_PIO_AML_PRD_ACTIVE ON PIO_AML_PRODUCTION_SCENARIOS (IS_ACTIVE, CREATED_AT);
