#!/usr/bin/env python
"""
Fill the codes missing from the attribute lookup tables, for a demo (insert-only).

For every attribute in ``attribute_filters.ATTRIBUTES`` that has a lookup table, the codes stored in
the source column (country / institution from settings) that have no lookup row are added with a
description. Nothing is updated or deleted, and a row is inserted only if its key does not exist.

Every inserted row is written to ``demo_data/lookup_fills_<timestamp>.json``, with a matching
``..._rollback.sql`` (DELETE statements for exactly those rows; NOT executed by this script).

Usage:
  # Show what would be inserted and write demo_data/lookup_fills_plan.json (no database change)
  python fill_demo_lookups.py

  # Insert, in one transaction, and write the record + rollback script
  python fill_demo_lookups.py --apply
"""

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Final, List, Optional, Tuple

from services.aml_builder.web.services.attribute_filters import ATTRIBUTES, ENTITY_TABLES, Attribute
from services.aml_builder.web.services.logging_config import setup_logging
from services.aml_builder.web.services.oracle import atomic_connection, init_pool, run_readonly
from services.aml_builder.web.services.settings import settings

OUT_DIR: Final = Path("demo_data")
_MEANING_UNKNOWN: Final = "(MEANING UNKNOWN - DEMO FILL)"

# Codes whose meaning is known from elsewhere in the database.
KNOWN: Final[Dict[str, Dict[str, Tuple[str, Optional[str], str]]]] = {
    "account_status": {
        "0": ("OPEN", "مفتوح", "same code in PIO_ACCOUNT_STATUS for country 414 / inst 1"),
        "8": ("BLACK LISTED", "مدرج بالقائمة السوداء", "same code in PIO_ACCOUNT_STATUS for countries 414 and 508"),
    },
    "account_product": {
        "2": ("TIME DEPOSITS", "ودائع لأجل", "same code in PIO_PRODUCTS for country 414 / inst 1"),
        "3": ("SAVING", "التوفير", "same code in PIO_PRODUCTS for country 414 / inst 1"),
    },
    "debit_credit": {
        "1": ("Debit", "مدين", "cash deposits post the cash ledger 1000 as '1', the customer as '2' (OPENBI2)"),
        "2": ("Credit", "دائن", "cash deposits post the cash ledger 1000 as '1', the customer as '2' (OPENBI2)"),
        "D": ("Debit", "مدين", "letter code for debit"),
        "C": ("Credit", "دائن", "letter code for credit"),
    },
}
_MARITAL: Final[Dict[str, str]] = {
    "married": "Married", "m": "Married", "mered": "Married", "marriged": "Married",
    "unmarried": "Single", "single": "Single", "s": "Single",
    "divorced": "Divorced", "widowed": "Widowed", "other": "Other",
}


def describe(attribute: Attribute, code: str) -> Tuple[str, Optional[str], str]:
    """Description for a stored code that has no lookup row.

    Args:
        attribute (Attribute): The attribute.
        code (str): The stored value.

    Returns:
        Tuple[str, Optional[str], str]: (English description, native description, basis).
    """
    known = KNOWN.get(attribute.key, {}).get(code)
    if known:
        return known
    has_letters = bool(re.search(r"[A-Za-z]", code))
    if attribute.key == "marital_status":
        mapped = _MARITAL.get(code.strip().lower())
        if mapped:
            return mapped, None, "the stored text, normalized"
    if attribute.key in ("profession", "occupation") and has_letters:
        return " ".join(code.upper().split()), None, "the stored text itself"
    if attribute.key == "customer_class" and re.fullmatch(r"[A-Za-z_]{4,}", code):
        return code.replace("_", " ").upper(), None, "the stored code text"
    if attribute.key == "city":
        return f"CITY CODE {code} (NAME UNKNOWN - DEMO FILL)", None, "placeholder: no name in the database"
    return f"{attribute.label.upper()} {code} {_MEANING_UNKNOWN}", None, "placeholder: meaning not in the database"


def source_table(attribute: Attribute) -> str:
    """The table holding the attribute's source column.

    Args:
        attribute (Attribute): The attribute.

    Returns:
        str: Qualified table name.
    """
    return ENTITY_TABLES[attribute.entity][0] if attribute.entity in ENTITY_TABLES else "BI_DWH.PIO_TRANSACTIONS"


def plan_rows() -> List[Dict[str, Any]]:
    """Every lookup row to add: stored codes without a lookup entry (read-only).

    Returns:
        List[Dict[str, Any]]: One dict per row: table, key, values, source_rows, basis.
    """
    country, inst = int(settings.AML_COUNTRY_CODE), int(settings.AML_INST_CODE)
    rows: List[Dict[str, Any]] = []
    for attribute in ATTRIBUTES:
        if not attribute.lookup_table:
            continue
        sql = (
            f"SELECT TO_CHAR(s.{attribute.column}) v, COUNT(*) n FROM {source_table(attribute)} s "
            f"WHERE s.COUNTRY_CODE = :country AND s.INST_CODE = :inst AND s.{attribute.column} IS NOT NULL "
            f"AND NOT EXISTS (SELECT 1 FROM BI_DWH.{attribute.lookup_table} l WHERE l.COUNTRY_CODE = :country "
            f"AND l.INST_CODE = :inst AND TO_CHAR(l.{attribute.code_column}) = TO_CHAR(s.{attribute.column})) "
            f"GROUP BY TO_CHAR(s.{attribute.column}) ORDER BY n DESC"
        )
        _, found = run_readonly(sql, {"country": country, "inst": inst})
        for code, count in found:
            english, native, basis = describe(attribute, str(code))
            values = {attribute.desc_column: english}
            if attribute.desc_nat_column and native:
                values[attribute.desc_nat_column] = native
            rows.append({
                "attribute": attribute.key,
                "table": f"BI_DWH.{attribute.lookup_table}",
                "key": {"COUNTRY_CODE": country, "INST_CODE": inst, attribute.code_column: str(code)},
                "values": values,
                "source_rows": int(count),
                "basis": basis,
            })
    return rows


def insert_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Insert the planned rows in one transaction; existing keys are skipped.

    Args:
        rows (List[Dict[str, Any]]): Rows from plan_rows.

    Returns:
        List[Dict[str, Any]]: The rows actually inserted.
    """
    inserted: List[Dict[str, Any]] = []
    with atomic_connection() as conn:
        cursor = conn.cursor()
        for row in rows:
            columns = {**row["key"], **row["values"]}
            names = list(columns)
            code_column = names[2]
            sql = (
                f"INSERT INTO {row['table']} ({', '.join(names)}) "
                f"SELECT {', '.join(':' + n.lower() for n in names)} FROM dual WHERE NOT EXISTS "
                f"(SELECT 1 FROM {row['table']} WHERE COUNTRY_CODE = :country_code AND INST_CODE = :inst_code "
                f"AND {code_column} = :{code_column.lower()})"
            )
            cursor.execute(sql, {n.lower(): v for n, v in columns.items()})
            if cursor.rowcount == 1:
                inserted.append(row)
    return inserted


def rollback_sql(rows: List[Dict[str, Any]], record_name: str) -> str:
    """DELETE statements for exactly the inserted rows (for later review; not executed here).

    Args:
        rows (List[Dict[str, Any]]): Inserted rows.
        record_name (str): Name of the JSON record they belong to.

    Returns:
        str: The SQL script.
    """
    lines = [
        f"-- Rollback for {record_name}: removes ONLY the demo lookup rows inserted by fill_demo_lookups.py.",
        "-- NOT executed automatically. Review, then run it yourself if the demo rows must be removed.",
        "",
    ]
    for row in rows:
        conditions = " AND ".join(
            f"{k} = {v}" if isinstance(v, int) else f"{k} = '{str(v).replace(chr(39), chr(39) * 2)}'"
            for k, v in row["key"].items()
        )
        lines.append(f"DELETE FROM {row['table']} WHERE {conditions};")
    lines += ["", "COMMIT;", ""]
    return "\n".join(lines)


def main() -> None:
    """Plan (default) or apply the demo lookup fills."""
    parser = argparse.ArgumentParser(description="Fill missing attribute lookup codes for a demo (insert-only).")
    parser.add_argument("--apply", action="store_true", help="Insert the rows (default: dry run).")
    args = parser.parse_args()

    setup_logging()
    init_pool()
    rows = plan_rows()
    OUT_DIR.mkdir(exist_ok=True)
    by_table: Dict[str, int] = {}
    for row in rows:
        by_table[row["table"]] = by_table.get(row["table"], 0) + 1
    print("=" * 70)
    print(f"[DEMO LOOKUP FILL] {'APPLY' if args.apply else 'DRY RUN'}  "
          f"country {settings.AML_COUNTRY_CODE} / inst {settings.AML_INST_CODE}  ({settings.ORACLE_DSN})")
    for table, count in by_table.items():
        print(f"  {table:<40} {count:>4} rows")
    print("=" * 70)

    header = {"created_at": datetime.now().isoformat(timespec="seconds"), "database": settings.ORACLE_DSN,
              "user": settings.ORACLE_USER, "country_code": settings.AML_COUNTRY_CODE,
              "inst_code": settings.AML_INST_CODE}
    if not args.apply:
        path = OUT_DIR / "lookup_fills_plan.json"
        path.write_text(json.dumps({**header, "applied": False, "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Plan written to {path} (nothing inserted). Run with --apply to insert.")
        return

    inserted = insert_rows(rows)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    record = OUT_DIR / f"lookup_fills_{stamp}.json"
    record.write_text(json.dumps({**header, "applied": True, "rows": inserted}, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    rollback = OUT_DIR / f"lookup_fills_{stamp}_rollback.sql"
    rollback.write_text(rollback_sql(inserted, record.name), encoding="utf-8")
    print(f"Inserted {len(inserted)} of {len(rows)} planned rows (the rest already existed).")
    print(f"Record:   {record}")
    print(f"Rollback: {rollback} (not executed)")


if __name__ == "__main__":
    sys.exit(main())
