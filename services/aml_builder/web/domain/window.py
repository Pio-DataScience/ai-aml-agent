"""Pure conversions for AML observation windows."""

from typing import Any, Dict, Optional, Tuple


def time_window_days(time_window: Optional[Dict[str, Any]]) -> Optional[int]:
    """Convert a time window to its established approximate day count.

    Args:
        time_window: Optional AMLIntent window dictionary.

    Returns:
        Window size in days, or None for an absent or zero-valued window.
    """
    window = time_window or {}
    value = int(window.get("value", 0) or 0)
    unit = str(window.get("unit", "DAYS")).upper()
    unit_to_days = {"DAYS": 1, "WEEKS": 7, "MONTHS": 30, "YEARS": 365}
    return value * unit_to_days.get(unit, 1) if value else None


def map_period(
    time_window: Optional[Dict[str, Any]],
) -> Tuple[Optional[str], Optional[int]]:
    """Map an intent window to the PIO_AML_SCENARIO period fields.

    Args:
        time_window: Optional AMLIntent time-window dictionary.

    Returns:
        A period type and number; weeks are converted to days.
    """
    if not time_window:
        return None, None
    unit = str(time_window.get("unit", "")).upper()
    value = time_window.get("value")
    if value is None:
        return None, None
    if unit == "WEEKS":
        return "D", int(value) * 7
    return {"DAYS": "D", "MONTHS": "M", "YEARS": "Y"}.get(unit), int(value)
