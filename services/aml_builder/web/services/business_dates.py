"""
Business-date arithmetic for scenario observation windows.

The bank works Sunday through Thursday; Friday and Saturday are the weekend
(configurable through ``settings.BUSINESS_WEEKEND_DAYS``). A scenario window of
N days is counted backward across business days only, from the business date the
rule is evaluated for.

Definitions used everywhere in the AML Builder:

* **business date** (``anchor``): the date a rule is evaluated for — the shadow-test
  anchor, or the alert engine's evaluation date. It is never taken from Oracle's clock.
* **window of N business days ending at the anchor**: the anchor itself (when it is a
  business day) plus the N-1 business days before it. If the anchor falls on a weekend,
  counting starts from the previous business day.
* transactions dated on a weekend day *inside* that span still fall in the window —
  only the counting skips weekends.
"""

import copy
import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, Final, FrozenSet, Optional, Tuple, Union

from services.aml_builder.web.services.settings import settings

_DAY_NAMES: Final[Dict[str, int]] = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6}


def weekend_days() -> FrozenSet[int]:
    """Return the configured weekend as Python weekday numbers (Monday=0).

    Returns:
        FrozenSet[int]: Weekday numbers that are not business days (default Friday, Saturday).

    Raises:
        ValueError: If BUSINESS_WEEKEND_DAYS names an unknown day.
    """
    names = [n.strip().upper()[:3] for n in settings.BUSINESS_WEEKEND_DAYS.split(",") if n.strip()]
    unknown = [n for n in names if n not in _DAY_NAMES]
    if unknown:
        raise ValueError(f"BUSINESS_WEEKEND_DAYS has unknown day(s): {unknown}")
    return frozenset(_DAY_NAMES[n] for n in names)


def is_business_day(day: date) -> bool:
    """Whether a date is a business day (not a weekend day).

    Args:
        day (date): The date.

    Returns:
        bool: True for Sunday-Thursday with the default weekend.
    """
    return day.weekday() not in weekend_days()


def latest_business_day(day: date) -> date:
    """Return the date itself if it is a business day, else the business day before it.

    Args:
        day (date): The date.

    Returns:
        date: The last business day on or before ``day``.
    """
    while not is_business_day(day):
        day -= timedelta(days=1)
    return day


def window_start(anchor: date, business_days: int) -> date:
    """First date of a window of N business days ending at the anchor.

    Args:
        anchor (date): The business date the rule is evaluated for.
        business_days (int): Window length in business days (N >= 1).

    Returns:
        date: The N-th business day counting back from the anchor, the anchor's own
            business day counted as the first. N=1 returns that business day itself.

    Raises:
        ValueError: If business_days is less than 1.
    """
    if business_days < 1:
        raise ValueError("A window must cover at least one business day")
    day = latest_business_day(anchor)
    counted = 1
    while counted < business_days:
        day -= timedelta(days=1)
        if is_business_day(day):
            counted += 1
    return day


def to_date(value: Union[date, datetime, str, None]) -> Optional[date]:
    """Parse a date given as a date, datetime or 'YYYY-MM-DD' string.

    Args:
        value (Union[date, datetime, str, None]): The value.

    Returns:
        Optional[date]: The date, or None for an empty value.

    Raises:
        ValueError: If a string is not in YYYY-MM-DD format.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value).strip()[:10], "%Y-%m-%d").date()


# ─────────────────────────── fixed date periods ───────────────────────────
#
# "between 2016 and 2022", "in 2019", "from March 2020 to June 2021", "2020-01-01 to 2020-03-31":
# a request for an explicit calendar period rather than a window counted back from a business date.
# The period is carried in the intent by two existing fields - ``anchor_date`` (the last day of the
# period) and a non-rolling ``time_window`` (its exact length) - so it survives model validation and
# can always be recovered with :func:`period_of`.

_YEAR: Final = r"(?:19[5-9]\d|20\d\d)"
_MONTH_NAME: Final = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_MONTHS: Final[Dict[str, int]] = {
    m: i + 1 for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"))
}
_NOT_AMOUNT: Final = r"(?![\d,.]\d)(?!\s*(?:jod|usd|eur|gbp|\$|dinars?|dollars?|%))"
_TOKEN: Final = (
    r"(?:\d{4}-\d{1,2}-\d{1,2}"
    rf"|\d{{1,2}}(?:st|nd|rd|th)?\s+{_MONTH_NAME}\s+{_YEAR}"
    rf"|{_MONTH_NAME}\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+{_YEAR}"
    rf"|{_MONTH_NAME}\s+{_YEAR}"
    rf"|(?<![\d,.$]){_YEAR}){_NOT_AMOUNT}"
)
_DASHES: Final = "-–—"  # hyphen, en dash, em dash
_RANGE_RE: Final = re.compile(
    rf"\bbetween\s+(?P<a1>{_TOKEN})\s+and\s+(?P<b1>{_TOKEN})"
    rf"|(?:\b(?:from|since)\s+)?(?P<a2>{_TOKEN})\s*(?:\bto\b|\buntil\b|\btill\b|\bthrough\b|\bthru\b|\binto\b|[{_DASHES}])"
    rf"\s*(?P<b2>{_TOKEN})",
    re.IGNORECASE,
)
_SINGLE_RE: Final = re.compile(
    rf"\b(?:in|during|for|throughout|over)\s+(?:the\s+)?(?:year\s+|month\s+of\s+)?(?P<s>{_TOKEN})", re.IGNORECASE
)


def _month_end(year: int, month: int) -> date:
    """Last day of a month.

    Args:
        year (int): Year.
        month (int): Month (1-12).

    Returns:
        date: The month's last day.
    """
    first_next = date(year + (month == 12), month % 12 + 1, 1)
    return first_next - timedelta(days=1)


def _token_span(token: str) -> Optional[Tuple[date, date]]:
    """First and last day covered by one date token (a year, a month, or a day).

    Args:
        token (str): E.g. '2016', 'March 2020', '1 Jan 2020', '2020-01-31'.

    Returns:
        Optional[Tuple[date, date]]: The span, or None if the token is not a valid date.
    """
    text = token.strip().lower().rstrip(",.")
    try:
        if re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", text):
            day = datetime.strptime(text, "%Y-%m-%d").date()
            return day, day
        if re.fullmatch(_YEAR, text):
            return date(int(text), 1, 1), date(int(text), 12, 31)
        year_match = re.search(_YEAR, text)
        month = next((n for k, n in _MONTHS.items() if re.search(rf"\b{k}", text)), None)
        if month is None or year_match is None:
            return None
        year = int(year_match.group(0))
        day_match = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\b", text.replace(year_match.group(0), ""))
        if day_match:
            day = date(year, month, int(day_match.group(1)))
            return day, day
        return date(year, month, 1), _month_end(year, month)
    except ValueError:
        return None


def find_fixed_period(text: str) -> Optional[Tuple[date, date, str]]:
    """Find an explicit calendar period in free text.

    Amounts are never read as years: only 1950-2099, never preceded by a digit, comma, point or '$',
    and never followed by more digits, a currency or a percent sign.

    Args:
        text (str): Free text, e.g. the user's request.

    Returns:
        Optional[Tuple[date, date, str]]: (first day, last day, the matched phrase), or None.
    """
    for match in _RANGE_RE.finditer(text or ""):
        first = _token_span(match.group("a1") or match.group("a2"))
        last = _token_span(match.group("b1") or match.group("b2"))
        if first and last and first[0] <= last[1]:
            return first[0], last[1], match.group(0).strip()
    match = _SINGLE_RE.search(text or "")
    if match:
        span = _token_span(match.group("s"))
        if span:
            return span[0], span[1], match.group(0).strip()
    return None


def window_for_period(first: date, last: date) -> Dict[str, Any]:
    """The exact, non-rolling time window that covers a calendar period.

    Args:
        first (date): First day of the period.
        last (date): Last day of the period.

    Returns:
        Dict[str, Any]: A time_window in whole YEARS or MONTHS when the period is made of them,
            otherwise in calendar DAYS; ``is_rolling`` is False.
    """
    if (first.month, first.day) == (1, 1) and (last.month, last.day) == (12, 31):
        unit, value = "YEARS", last.year - first.year + 1
    elif first.day == 1 and last == _month_end(last.year, last.month):
        unit, value = "MONTHS", (last.year - first.year) * 12 + last.month - first.month + 1
    else:
        unit, value = "DAYS", (last - first).days + 1
    return {"unit": unit, "value": value, "is_rolling": False, "provenance": "stated"}


def _unit(window: Dict[str, Any]) -> str:
    """A window's unit in the plural form the schema uses ('YEAR' and 'years' become 'YEARS').

    Args:
        window (Dict[str, Any]): A time window.

    Returns:
        str: DAYS, WEEKS, MONTHS or YEARS (DAYS when missing).
    """
    unit = str(window.get("unit") or "DAYS").strip().upper()
    return unit if unit.endswith("S") else unit + "S"


def period_of(intent: Dict[str, Any]) -> Optional[Tuple[date, date]]:
    """Recover the fixed calendar period an intent describes, if it describes one.

    Args:
        intent (Dict[str, Any]): The scenario intent.

    Returns:
        Optional[Tuple[date, date]]: (first day, last day) when the intent has an ``anchor_date`` and
            a non-rolling ``time_window``; None for a window counted back from a business date.
    """
    window = intent.get("time_window") or {}
    try:
        last = to_date(intent.get("anchor_date"))
        size = int(window.get("value") or 0)
    except (TypeError, ValueError):
        return None
    if not last or window.get("is_rolling", True) or size < 1:
        return None
    unit = _unit(window)
    if unit == "YEARS":
        return date(last.year - size + 1, 1, 1), last
    if unit == "MONTHS":
        months = last.year * 12 + last.month - 1 - (size - 1)
        return date(months // 12, months % 12 + 1, 1), last
    if unit == "WEEKS":
        return last - timedelta(days=7 * size - 1), last
    return last - timedelta(days=size - 1), last


def apply_fixed_period(intent: Dict[str, Any], *texts: str) -> Tuple[Dict[str, Any], Optional[Tuple[date, date]]]:
    """Set the evaluation date and exact window of an intent from an explicit period in the request.

    Args:
        intent (Dict[str, Any]): The scenario intent (not modified).
        *texts (str): Where to look for the period, most reliable first (the user's request, then
            the detection logic and condition phrases).

    Returns:
        Tuple[Dict[str, Any], Optional[Tuple[date, date]]]: The intent with ``anchor_date`` set to the
            last day of the period and ``time_window`` set to its exact length, and the period; or the
            unchanged intent and the period it already describes (None if it describes none).
            A stated window much shorter than the period ("within 7 days, from 2020 into 2022") is
            the rule's own aggregation window and is kept; only ``anchor_date`` is set then, and
            :func:`period_of` of the result is None.
    """
    for text in texts:
        found = find_fixed_period(text or "")
        if found:
            first, last, _ = found
            updated = copy.deepcopy(intent)
            updated["anchor_date"] = last.isoformat()
            if not _is_inner_window(intent.get("time_window"), (last - first).days + 1):
                updated["time_window"] = window_for_period(first, last)
            return updated, (first, last)
    return intent, period_of(intent)


_APPROX_DAYS: Final[Dict[str, int]] = {"DAYS": 1, "WEEKS": 7, "MONTHS": 30, "YEARS": 365}


def _is_inner_window(window: Any, period_days: int) -> bool:
    """Whether a time window is a stated aggregation window inside a period, not the period itself.

    Args:
        window (Any): The intent's ``time_window``.
        period_days (int): Length of the stated period in days.

    Returns:
        bool: True for a stated window shorter than half the period (e.g. 7 days in 2020-2022);
            False for a missing or assumed window, or one the size of the period (e.g. 7 years).
    """
    if not isinstance(window, dict) or window.get("provenance") == "assumed_default":
        return False
    try:
        days = int(window.get("value") or 0) * _APPROX_DAYS.get(_unit(window), 1)
    except (TypeError, ValueError):
        return False
    return 0 < days < period_days / 2


def clean_for_sql_service(intent: Dict[str, Any]) -> Dict[str, Any]:
    """Copy of an intent with every explicit period phrase removed from its free text.

    The SQL service reads numbers in condition text; "between 2016 and 2022" was read as two amounts
    contradicting a threshold. The period is already carried by ``anchor_date`` and ``time_window``.

    Args:
        intent (Dict[str, Any]): The scenario intent (not modified).

    Returns:
        Dict[str, Any]: The cleaned copy: condition phrases that state a period are dropped, and the
            period wording is removed from ``detection_logic``.
    """
    cleaned = copy.deepcopy(intent)
    conditions = cleaned.get("semantic_conditions")
    if isinstance(conditions, list):
        cleaned["semantic_conditions"] = [
            c for c in conditions
            if not (isinstance(c, dict)
                    and (find_fixed_period(str(c.get("raw_phrase", ""))) or find_fixed_period(str(c.get("predicate", "")))))
        ]
    logic = cleaned.get("detection_logic")
    for _ in range(5):  # a description rarely states more than one period
        found = find_fixed_period(logic) if isinstance(logic, str) else None
        if not found:
            break
        logic = re.sub(r"\s{2,}", " ", logic.replace(found[2], " ")).replace(" ,", ",").replace(" .", ".").strip()
    if isinstance(logic, str):
        cleaned["detection_logic"] = logic
    return cleaned
