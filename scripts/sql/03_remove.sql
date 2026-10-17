-- P0 removal runbook. Owner-run in TOAD only after checking all backups/counts.
-- Do not add COMMIT. Review affected-row counts after EACH DELETE; commit manually
-- only after they match 01_check.sql. ROLLBACK remains available before commit.
-- All predicates resolve to the exact PRD_ IDs in the existing production table.

-- Child rows first; these are constrained to scenario IDs in PIO_PROD_BAK.
DELETE FROM PIO_AML_CUSTOMERS_DET
WHERE AML_SCENARIO_CODE IN (
  SELECT SCENARIO_ID FROM PIO_AML_PRODUCTION_SCENARIOS
  WHERE SUBSTR(SCENARIO_ID, 1, 4) = 'PRD_'
);

DELETE FROM PIO_AML_CUSTOMERS
WHERE AML_SCENARIO_CODE IN (
  SELECT SCENARIO_ID FROM PIO_AML_PRODUCTION_SCENARIOS
  WHERE SUBSTR(SCENARIO_ID, 1, 4) = 'PRD_'
);

-- The shared table is scoped to exact production scenario IDs only.
DELETE FROM PIO_AML_SCENARIO
WHERE SCENARIO_CODE IN (
  SELECT SCENARIO_ID FROM PIO_AML_PRODUCTION_SCENARIOS
  WHERE SUBSTR(SCENARIO_ID, 1, 4) = 'PRD_'
);

-- Remove only the legacy PRD_ rows from the old production registry.
DELETE FROM PIO_AML_PRODUCTION_SCENARIOS
WHERE SUBSTR(SCENARIO_ID, 1, 4) = 'PRD_';

-- If 01_check.sql found other child tables, add explicit DELETE statements for
-- those tables here, each keyed to the exact PRD_ scenario IDs, before removing
-- parent rows. Do not use an unscoped or dynamically generated DELETE.
-- No COMMIT is included. Verify counts, then choose COMMIT or ROLLBACK in TOAD.
