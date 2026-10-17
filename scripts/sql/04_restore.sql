-- P0 restore runbook for the owner to run in TOAD.
-- These are the backup table names recorded in the 2026-10-17 owner run log.
-- The backups contain the 17 approved original PRD_ rows in each registry.
-- Restore does not touch any other PIO_AML_SCENARIO rows.
-- No COMMIT is included. Review counts, then COMMIT or ROLLBACK manually.

-- 1. Confirm both backup tables still contain the expected 17 rows.
SELECT 'PRODUCTION_BACKUP' AS SOURCE, COUNT(*) AS ROW_COUNT
FROM PIO_AML_PROD_NIDAL_20261017_BKP
UNION ALL
SELECT 'GOVERNANCE_BACKUP', COUNT(*)
FROM PIO_AML_SCEN_NIDAL_20261017_BKP;

-- 2. Check how many backed-up keys are already present in the target tables.
-- If rows already exist, review them before continuing. The INSERTs below skip
-- keys already present; they do not overwrite or update existing rows.
SELECT COUNT(*) AS PRODUCTION_KEYS_ALREADY_PRESENT
FROM PIO_AML_PROD_NIDAL_20261017_BKP b
WHERE EXISTS (
  SELECT 1 FROM PIO_AML_PRODUCTION_SCENARIOS p
  WHERE p.SCENARIO_ID = b.SCENARIO_ID
);

SELECT COUNT(*) AS GOVERNANCE_KEYS_ALREADY_PRESENT
FROM PIO_AML_SCEN_NIDAL_20261017_BKP b
WHERE EXISTS (
  SELECT 1 FROM PIO_AML_SCENARIO s
  WHERE s.COUNTRY_CODE = b.COUNTRY_CODE
    AND s.INST_CODE = b.INST_CODE
    AND s.SCENARIO_CODE = b.SCENARIO_CODE
);

-- 3. Restore missing production registry rows.
INSERT INTO PIO_AML_PRODUCTION_SCENARIOS
SELECT b.*
FROM PIO_AML_PROD_NIDAL_20261017_BKP b
WHERE NOT EXISTS (
  SELECT 1 FROM PIO_AML_PRODUCTION_SCENARIOS p
  WHERE p.SCENARIO_ID = b.SCENARIO_ID
);

-- 4. Restore missing matching governance rows.
INSERT INTO PIO_AML_SCENARIO
SELECT b.*
FROM PIO_AML_SCEN_NIDAL_20261017_BKP b
WHERE NOT EXISTS (
  SELECT 1 FROM PIO_AML_SCENARIO s
  WHERE s.COUNTRY_CODE = b.COUNTRY_CODE
    AND s.INST_CODE = b.INST_CODE
    AND s.SCENARIO_CODE = b.SCENARIO_CODE
);

-- 5. Verify all 17 backed-up keys are present in each target.
SELECT 'PRODUCTION_TARGET' AS SOURCE, COUNT(*) AS ROW_COUNT
FROM PIO_AML_PRODUCTION_SCENARIOS p
WHERE EXISTS (
  SELECT 1 FROM PIO_AML_PROD_NIDAL_20261017_BKP b
  WHERE b.SCENARIO_ID = p.SCENARIO_ID
)
UNION ALL
SELECT 'GOVERNANCE_TARGET', COUNT(*)
FROM PIO_AML_SCENARIO s
WHERE EXISTS (
  SELECT 1 FROM PIO_AML_SCEN_NIDAL_20261017_BKP b
  WHERE b.COUNTRY_CODE = s.COUNTRY_CODE
    AND b.INST_CODE = s.INST_CODE
    AND b.SCENARIO_CODE = s.SCENARIO_CODE
);

-- Expect 17 rows from each backup and 17 matching rows in each target.
-- If counts are unexpected, ROLLBACK in this session. Otherwise COMMIT manually.
