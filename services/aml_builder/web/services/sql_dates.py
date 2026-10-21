"""
Anchor scenario SQL to an explicit business date instead of Oracle's clock.

The PioTech service writes observation windows against ``SYSDATE`` (``TRA_DATE >= SYSDATE - 30``,
``ADD_MONTHS(SYSDATE, -1)``, ``TRUNC(SYSDATE, 'MM')`` ...). A rule written that way silently
depends on the moment it runs, and a historical test has to patch the text. This module rewrites
those forms so the SQL never reads the live clock:

=============================================  =========================================================
Generated form                                  Rewritten form
=============================================  =========================================================
``SYSDATE - N`` / ``TRUNC(SYSDATE) - N``        ``:bd_minus_N`` - first day of a window of N business days
``SYSDATE - INTERVAL 'N' DAY``                  ``:bd_minus_N``
``ADD_MONTHS(SYSDATE, -N)``                     ``ADD_MONTHS(:as_of_date, -N)`` (calendar months)
``TRUNC(SYSDATE, 'MM')``                        ``TRUNC(:as_of_date, 'MM')`` (calendar period)
``SYSDATE`` / ``TRUNC(SYSDATE)``                ``:as_of_date``
=============================================  =========================================================

``CURRENT_DATE``, ``SYSTIMESTAMP`` and ``LOCALTIMESTAMP`` are treated like ``SYSDATE``.

Every lower bound of a window (``col >= <start>``) also gets an upper bound
``col < :as_of_date + 1``, so a rule evaluated for a past business date never counts later data.

A window given in weeks counts 5 business days per week. The bind values are computed by
:func:`bind_values` from the business date (see ``business_dates``). String literals are never
edited, and the rewrite is idempotent: SQL that already uses the binds passes through unchanged.
"""

import re
from datetime import date, timedelta
from typing import Any, Dict, Final, List, Optional, Tuple

from services.aml_builder.web.services.business_dates import window_start

AS_OF: Final = ":as_of_date"
_BD_PREFIX: Final = ":bd_minus_"
_MARK: Final = "\u0001"  # wraps the index of a protected string literal: <MARK>3<MARK>

_CLOCK: Final = r"(?:SYSDATE|CURRENT_DATE|SYSTIMESTAMP|LOCALTIMESTAMP)"
_CLOCK_OR_TRUNC: Final = rf"(?:TRUNC\(\s*{_CLOCK}\s*\)|{_CLOCK})"
_INT_EXPR: Final = r"\d+(?:\s*\*\s*\d+)*"
_PLACEHOLDER: Final = rf"{_MARK}\d+{_MARK}"

_LITERAL_RE: Final = re.compile(r"'(?:[^']|'')*'")
_TRUNC_FMT_RE: Final = re.compile(rf"TRUNC\(\s*{_CLOCK}\s*,\s*({_PLACEHOLDER})\s*\)", re.IGNORECASE)
_ADD_MONTHS_RE: Final = re.compile(rf"ADD_MONTHS\(\s*{_CLOCK_OR_TRUNC}\s*,\s*(-?\s*{_INT_EXPR})\s*\)", re.IGNORECASE)
_MINUS_INTERVAL_RE: Final = re.compile(rf"{_CLOCK_OR_TRUNC}\s*-\s*INTERVAL\s*{_PLACEHOLDER}\s*DAY", re.IGNORECASE)
_MINUS_DAYS_RE: Final = re.compile(
    rf"{_CLOCK_OR_TRUNC}\s*-\s*(?:\(\s*({_INT_EXPR})\s*\)|({_INT_EXPR}))", re.IGNORECASE
)
_BARE_CLOCK_RE: Final = re.compile(_CLOCK_OR_TRUNC, re.IGNORECASE)
_ANY_CLOCK_RE: Final = re.compile(rf"\b{_CLOCK}\b", re.IGNORECASE)
_BIND_RE: Final = re.compile(r":(as_of_date|bd_minus_(\d+))\b", re.IGNORECASE)

# A whole column reference, never the tail of one ("t.TRA_DATE", not "TRA_DATE" inside it).
_IDENT: Final = r"(?:[A-Za-z_][\w$#]*\.)?[A-Za-z_][\w$#]*"
_COLUMN: Final = rf"(?<![\w$#.])(TRUNC\(\s*{_IDENT}\s*\)|{_IDENT})"
# A whole window start, never a prefix of one (":bd_minus_210", not ":bd_minus_21").
_WINDOW_START: Final = (
    rf"(:bd_minus_\d+(?!\d)|ADD_MONTHS\(:as_of_date,\s*-?\d+\)"
    rf"|ADD_MONTHS\(TRUNC\(:as_of_date,\s*{_PLACEHOLDER}\),\s*-?\d+\)|TRUNC\(:as_of_date,\s*{_PLACEHOLDER}\))"
)
_LOWER_BOUND_RE: Final = re.compile(rf"{_COLUMN}\s*(>=|>)\s*{_WINDOW_START}(?!\s+AND\s+\1\s*<)", re.IGNORECASE)
_ANY_LOWER_BOUND_RE: Final = re.compile(rf"{_COLUMN}\s*(>=|>)\s*{_WINDOW_START}", re.IGNORECASE)
_AS_OF_PLUS_ONE_RE: Final = re.compile(r":as_of_date\s*\+\s*1(?![\d.])", re.IGNORECASE)
_AS_OF_RE: Final = re.compile(r":as_of_date\b", re.IGNORECASE)


def anchor_sql(sql: str, intent: Optional[Dict[str, Any]] = None) -> str:
    """Rewrite every clock reference in scenario SQL to business-date binds.

    Args:
        sql (str): The generated scenario SQL.
        intent (Optional[Dict[str, Any]]): The scenario intent, used to read windows given in weeks.

    Returns:
        str: SQL that uses ``:as_of_date`` and ``:bd_minus_N`` and no clock function.

    Raises:
        ValueError: If a clock function remains after the rewrite (an unrecognized form).
    """
    weeks = _week_conversions(intent)

    def days_to_bind(days: int) -> str:
        business = weeks.get(days, days)
        return f"{_BD_PREFIX}{business}" if business > 0 else AS_OF

    out, literals = _protect(sql)

    def literal_days(placeholder: str) -> int:
        text = literals[int(placeholder.strip(_MARK))].strip("'").strip()
        return int(text) if text.isdigit() else 0

    out = _TRUNC_FMT_RE.sub(lambda m: f"TRUNC({AS_OF}, {m.group(1)})", out)
    out = _ADD_MONTHS_RE.sub(lambda m: f"ADD_MONTHS({AS_OF}, {_evaluate(m.group(1))})", out)
    out = _MINUS_INTERVAL_RE.sub(lambda m: days_to_bind(literal_days(re.search(_PLACEHOLDER, m.group(0)).group(0))), out)
    out = _MINUS_DAYS_RE.sub(lambda m: days_to_bind(_evaluate(m.group(1) or m.group(2))), out)
    out = _BARE_CLOCK_RE.sub(AS_OF, out)
    out = _LOWER_BOUND_RE.sub(lambda m: f"{m.group(0)} AND {m.group(1)} < {AS_OF} + 1", out)

    if _ANY_CLOCK_RE.search(out):
        raise ValueError("Scenario SQL still references the system clock after anchoring")
    return _restore(out, literals)


def binds_used(sql: str) -> List[str]:
    """List the business-date bind names a SQL statement uses.

    Args:
        sql (str): Anchored SQL.

    Returns:
        List[str]: Bind names without the colon, e.g. ['as_of_date', 'bd_minus_30'].
    """
    return sorted({m.group(1).lower() for m in _BIND_RE.finditer(_LITERAL_RE.sub("''", sql))})


def bind_values(sql: str, anchor: date) -> Dict[str, date]:
    """Compute the value of every business-date bind in a SQL statement.

    Args:
        sql (str): Anchored SQL.
        anchor (date): The business date the rule is evaluated for.

    Returns:
        Dict[str, date]: {'as_of_date': anchor, 'bd_minus_N': first day of the N-business-day window}
            for exactly the binds the SQL uses (Oracle rejects binds a statement does not use).
    """
    values: Dict[str, date] = {}
    for name in binds_used(sql):
        values[name] = anchor if name == "as_of_date" else window_start(anchor, int(name.rsplit("_", 1)[1]))
    return values


def pin_period(sql: str, start: date, end: date) -> Tuple[str, bool]:
    """Fix an anchored scenario's window to an explicit calendar period.

    For a request such as "between 2016 and 2022" the window must cover exactly 1 Jan 2016 -
    31 Dec 2022, not a count of days back from the evaluation date. When the anchored SQL has
    exactly one window start, it becomes ``col >= DATE 'start'`` and the evaluation date becomes
    ``DATE 'end'`` (``:as_of_date + 1`` becomes the day after the period).

    Args:
        sql (str): SQL already rewritten by :func:`anchor_sql`.
        start (date): First day of the period.
        end (date): Last day of the period.

    Returns:
        Tuple[str, bool]: The SQL and True when it was pinned; the SQL unchanged and False when it
            has no window start, or several different ones (e.g. a baseline window) that cannot all
            be mapped to the one period.
    """
    out, literals = _protect(sql)
    starts = {re.sub(r"\s+", "", m.group(3)).lower() for m in _ANY_LOWER_BOUND_RE.finditer(out)}
    if len(starts) != 1:
        return sql, False
    first = f"DATE '{start.isoformat()}'"
    out = _ANY_LOWER_BOUND_RE.sub(lambda m: f"{m.group(1)} >= {first}", out)
    out = _AS_OF_PLUS_ONE_RE.sub(f"DATE '{(end + timedelta(days=1)).isoformat()}'", out)
    out = _AS_OF_RE.sub(f"DATE '{end.isoformat()}'", out)
    return _restore(out, literals), True


_AGGREGATE_START: Final = re.compile(r"\b(COUNT|SUM|AVG|MIN|MAX)\s*\(", re.IGNORECASE)
_TX_FROM: Final = re.compile(r"^FROM\s+(?:BI_DWH\.)?PIO_TRANSACTIONS\s+(?:AS\s+)?([A-Za-z_]\w*)\s*$", re.IGNORECASE)
_COLUMN_REF: Final = re.compile(r"^(?:([A-Za-z_]\w*)\.)?([A-Za-z_][\w$#]*)$")


def _aggregates(text: str) -> List[str]:
    """Every aggregate call in a SQL fragment, as written (balanced parentheses).

    Args:
        text (str): A SELECT list or HAVING clause.

    Returns:
        List[str]: e.g. ['COUNT(*)', 'SUM(t.EQU_TRA_AMT)'].
    """
    found: List[str] = []
    for match in _AGGREGATE_START.finditer(text):
        depth, i = 0, match.end() - 1
        while i < len(text):
            depth += {"(": 1, ")": -1}.get(text[i], 0)
            if depth == 0:
                found.append(text[match.start():i + 1])
                break
            i += 1
    return found


def _window_range(window: Dict[str, Any]) -> Optional[str]:
    """The RANGE frame of a time window, ending on each transaction's date.

    Args:
        window (Dict[str, Any]): The intent's time_window (unit, value).

    Returns:
        Optional[str]: e.g. '6 PRECEDING' for 1 week, "INTERVAL '1' MONTH PRECEDING", or None.
    """
    try:
        size = int(window.get("value") or 0)
    except (TypeError, ValueError):
        return None
    unit = str(window.get("unit") or "DAYS").strip().upper().rstrip("S") + "S"
    if size < 1:
        return None
    if unit == "WEEKS":
        return f"{7 * size - 1} PRECEDING"
    if unit == "DAYS":
        # N working days (Sunday-Thursday) span about N * 7 / 5 calendar days; 1 working day is that day.
        return "0 PRECEDING" if size == 1 else f"{-(-size * 7 // 5) - 1} PRECEDING"
    if unit in ("MONTHS", "YEARS"):
        return f"INTERVAL '{size}' {unit[:-1]} PRECEDING"
    return None


def slide_window_over_period(sql: str, start: date, end: date, window: Dict[str, Any]) -> Tuple[str, bool]:
    """Check a short window ANYWHERE inside a fixed period ("3 deposits in one week, in 2019").

    The anchored SQL evaluates its window once, ending on the period's last day. This rewrites a
    single-table rule grouped per customer / account with a HAVING condition so that the window
    slides over every transaction date of the period: each aggregate becomes a moving aggregate over
    the window (COUNT(*) OVER (PARTITION BY ... ORDER BY TRA_DATE RANGE ...)), the rows are limited to
    the period, and a group is flagged when its best window meets the condition (MAX of the moving value).

    Args:
        sql (str): SQL anchored by :func:`anchor_sql` (one window start).
        start (date): First day of the period.
        end (date): Last day of the period.
        window (Dict[str, Any]): The intent's time_window (the short window).

    Returns:
        Tuple[str, bool]: The rewritten SQL and True; or the SQL unchanged and False when the rule is not of
            that form (no GROUP BY / HAVING, joins, DISTINCT aggregates, ORDER BY, or a "below" condition).
    """
    from services.aml_builder.web.services.attribute_filters import top_level_clauses

    frame = _window_range(window or {})
    pinned, ok = pin_period(sql, start, end)
    clauses = top_level_clauses(pinned) if ok and frame else None
    if not clauses or not {"GROUP BY", "HAVING"} <= set(clauses) or "ORDER BY" in clauses:
        return sql, False
    from_text = pinned[clauses["FROM"][0]:clauses["FROM"][1]].strip()
    alias_match = _TX_FROM.match(from_text)
    if not alias_match:
        return sql, False
    alias = alias_match.group(1)
    select_list = pinned[clauses["SELECT"][0] + len("SELECT"):clauses["FROM"][0]].strip()
    group_list = pinned[clauses["GROUP BY"][0] + len("GROUP BY"):clauses["GROUP BY"][1]].strip()
    having = pinned[clauses["HAVING"][0] + len("HAVING"):clauses["HAVING"][1]].strip()
    body = pinned[clauses["FROM"][0]:clauses["GROUP BY"][0]].rstrip()

    # Only "at least / more than" conditions: the best window decides (MAX). "Fewer than" cannot slide this way.
    if re.search(r"<|(?<![<>!])=(?!=)", re.sub(r">=", ">", having)):
        return sql, False
    aggregates = list(dict.fromkeys(_aggregates(select_list) + _aggregates(having)))
    if not aggregates or any(re.search(r"\bDISTINCT\b", a, re.IGNORECASE) for a in aggregates):
        return sql, False
    groups = [g.strip() for g in group_list.split(",") if g.strip()]
    names = []
    for group in groups:
        column = _COLUMN_REF.match(group)
        if not column:
            return sql, False
        names.append(column.group(2))

    partition = ", ".join(groups)
    moving = [f"{agg} OVER (PARTITION BY {partition} ORDER BY {alias}.TRA_DATE RANGE BETWEEN {frame} AND CURRENT ROW) AS W{i}"
              for i, agg in enumerate(aggregates, 1)]
    inner_select = ", ".join([f"{g} AS {n}" for g, n in zip(groups, names)] + moving)

    def outer(text: str) -> str:
        for i, agg in enumerate(aggregates, 1):
            text = text.replace(agg, f"MAX(W{i})")
        return re.sub(rf"\b{re.escape(alias)}\.", "", text)

    rewritten = (
        f"SELECT {outer(select_list)}\nFROM (\nSELECT {inner_select}\n{body}\n) w\n"
        f"GROUP BY {', '.join(names)}\nHAVING {outer(having)}"
    )
    return rewritten, True


# ─────────────────────────────── helpers ────────────────────────────────


def _evaluate(expr: str) -> int:
    """Evaluate a product of integers such as ``7 * 4`` (optionally negative).

    Args:
        expr (str): Digits joined by ``*``, optionally with a leading minus.

    Returns:
        int: The value.
    """
    sign = -1 if expr.strip().startswith("-") else 1
    value = 1
    for part in expr.replace("-", "").split("*"):
        value *= int(part.strip())
    return sign * value


def _week_conversions(intent: Optional[Dict[str, Any]]) -> Dict[int, int]:
    """Map calendar-day counts that encode weeks to business days (5 per week).

    Args:
        intent (Optional[Dict[str, Any]]): The scenario intent, if known.

    Returns:
        Dict[int, int]: {calendar days: business days} for the intent's week-based windows.
    """
    out: Dict[int, int] = {}
    for key, size_key in (("time_window", "value"), ("baseline_window", "duration")):
        window = (intent or {}).get(key) or {}
        size = str(window.get(size_key, ""))
        if str(window.get("unit", "")).upper() == "WEEKS" and size.isdigit():
            out[7 * int(size)] = 5 * int(size)
    return out


def _protect(sql: str) -> Tuple[str, List[str]]:
    """Replace every quoted string literal with a placeholder so the rewrite never edits inside it.

    Args:
        sql (str): SQL text.

    Returns:
        Tuple[str, List[str]]: The SQL with placeholders, and the literals in order.
    """
    literals: List[str] = []

    def keep(match: "re.Match[str]") -> str:
        literals.append(match.group(0))
        return f"{_MARK}{len(literals) - 1}{_MARK}"

    return _LITERAL_RE.sub(keep, sql), literals


def _restore(sql: str, literals: List[str]) -> str:
    """Put protected string literals back.

    Args:
        sql (str): SQL with placeholders.
        literals (List[str]): The literals returned by _protect.

    Returns:
        str: The SQL with its original literals.
    """
    return re.sub(rf"{_MARK}(\d+){_MARK}", lambda m: literals[int(m.group(1))], sql)
