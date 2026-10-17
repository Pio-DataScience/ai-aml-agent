"""Canonical SQL and JSON hashing helpers."""

import hashlib
import json
import re


def normalize_sql(sql: str) -> str:
    """Normalize SQL comments, whitespace and keyword case outside literals.

    Args:
        sql: SQL statement to canonicalize.

    Returns:
        Canonical SQL with comments removed and a final semicolon dropped.
    """
    pieces = []
    index = 0
    in_literal = False
    while index < len(sql):
        char = sql[index]
        if char == "'":
            pieces.append(char)
            if in_literal and index + 1 < len(sql) and sql[index + 1] == "'":
                pieces.append("'")
                index += 2
                continue
            in_literal = not in_literal
            index += 1
            continue
        if not in_literal and sql.startswith("--", index):
            newline = sql.find("\n", index)
            index = len(sql) if newline == -1 else newline + 1
            pieces.append(" ")
            continue
        if not in_literal and sql.startswith("/*", index):
            end = sql.find("*/", index + 2)
            index = len(sql) if end == -1 else end + 2
            pieces.append(" ")
            continue
        pieces.append(char if in_literal else char.casefold())
        index += 1
    normalized = re.sub(r"\s+", " ", "".join(pieces)).strip()
    normalized = re.sub(r"\s*([=<>!,()])\s*", r"\1", normalized)
    return normalized[:-1].rstrip() if normalized.endswith(";") else normalized


def stable_hash(value: object) -> str:
    """Return SHA-256 hex digest of compact canonical JSON.

    Args:
        value: JSON-serializable value to hash.

    Returns:
        Lowercase SHA-256 digest of sorted-key UTF-8 JSON.
    """
    canonical_json = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
