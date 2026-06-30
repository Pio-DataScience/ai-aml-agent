# compliance catalog Table Schemas

This document contains the physical Oracle database schemas for the metadata catalog tables:
- `PIO_AML_TABLES`
- `PIO_AML_COLUMNS`
- `PIO_AML_PARAMETERS`

These schemas serve as a developer reference for writing metadata configuration records dynamically.

---

## Table: `PIO_AML_TABLES`

| Column Name | Data Type | Data Length | Nullable |
|-------------|-----------|-------------|----------|
| `TABLE_CODE` | VARCHAR2 | 40 | N |
| `TABLE_NAME` | VARCHAR2 | 40 | Y |
| `BUSINESS_NAME` | VARCHAR2 | 40 | Y |
| `BUSINESS_NAME_NAT` | VARCHAR2 | 200 | Y |

---

## Table: `PIO_AML_COLUMNS`

| Column Name | Data Type | Data Length | Nullable |
|-------------|-----------|-------------|----------|
| `COLUMN_CODE` | VARCHAR2 | 40 | N |
| `COLUMN_TYPE` | VARCHAR2 | 40 | Y |
| `TABLE_CODE` | VARCHAR2 | 40 | Y |
| `COLUMN_NAME` | VARCHAR2 | 40 | Y |
| `COLUMN_BUSINESS_NAME` | VARCHAR2 | 200 | Y |
| `LOOKUP_FLAG` | VARCHAR2 | 40 | Y |
| `LOOKUP_NAME` | VARCHAR2 | 40 | Y |
| `LOOKUP_CODE_COLUMN` | VARCHAR2 | 40 | Y |
| `LOOKUP_DESC_COLUMN` | VARCHAR2 | 40 | Y |
| `SD_USED_FLAG` | VARCHAR2 | 40 | Y |
| `COLUMN_BUSINESS_NAME_NAT` | VARCHAR2 | 200 | Y |
| `LOOKUP_DESC_COLUMN_NAT` | VARCHAR2 | 40 | Y |
| `HIS_FLAG` | VARCHAR2 | 40 | Y |
| `LOOKUP_COUNTRY_COLUMN` | VARCHAR2 | 40 | Y |
| `LOOKUP_INST_COLUMN` | VARCHAR2 | 40 | Y |
| `BALANCE_FLAG` | VARCHAR2 | 40 | Y |

---

## Table: `PIO_AML_PARAMETERS`

| Column Name | Data Type | Data Length | Nullable |
|-------------|-----------|-------------|----------|
| `PARAMETER_CODE` | VARCHAR2 | 40 | N |
| `PARAMETER_ELEMENT` | VARCHAR2 | 400 | Y |
| `TABLE_CODE` | VARCHAR2 | 400 | Y |
| `COLUMN_CODE` | VARCHAR2 | 400 | Y |
| `LGM_SCENARIO_BASED_FLAG` | VARCHAR2 | 400 | Y |
| `LGM_GROUP_BASED_FLAG` | VARCHAR2 | 400 | Y |
| `AGGREGATION_CODE` | VARCHAR2 | 40 | Y |
| `PARAMETER_PERC_FLAG` | VARCHAR2 | 40 | Y |
| `PARAMETER_ELEMENT_NAT` | VARCHAR2 | 400 | Y |
| `CREATED_BY` | NUMBER | 22 | Y |
| `CREATED_DATE` | DATE | 7 | Y |
| `UPDATED_BY` | NUMBER | 22 | Y |
| `UPDATED_DATE` | DATE | 7 | Y |

---
