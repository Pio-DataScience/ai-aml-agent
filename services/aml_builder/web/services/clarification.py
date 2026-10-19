"""
Clarification Question Flow: ask for the mandatory scenario parameters instead of guessing them.

Four parameters decide which alerts a scenario raises. When the request does not state one of them,
the intent tool stops before code discovery, the plan and the shadow test, and returns a
``NEEDS_CLARIFICATION`` payload; the UI shows it as a form, and the answers come back with the next
chat request (``ChatRequest.clarification_answers``) and are merged into the intent here.

==================  ===================================================================  ============
Parameter           Stated when                                                          Question
==================  ===================================================================  ============
target_entity       not asked by default (business decision: per customer, see          single_select
                    default_entity); asked only with CLARIFICATION_ASK_ENTITY=True
threshold_amount    a threshold whose number appears in the text                         number
timeframe           a lookback ("within 7 days", "last month"), a period ("in 2019"),    single_select
                    or a frequency ("daily")                                             (+ quarter year,
                                                                                         date_range)
channels            the text names the intent's transaction types, says "any / all",    multi_select
                    (or a type typed under "Other")                                      (+ text)
                    or a debit / credit filter covers them
==================  ===================================================================  ============

The decision is made here, in code, not by the LLM: a value the LLM filled in without the user
saying it is a gap. ``settings.AML_CLARIFICATION_FLOW = False`` switches the flow off.
"""

import copy
import json
import re
from datetime import date
from typing import Any, Dict, Final, List, Optional, Set, Tuple

from services.aml_builder.web.services.business_dates import find_fixed_period, to_date, window_for_period
from services.aml_builder.web.services.settings import settings

FIELD_LABELS: Final[Dict[str, str]] = {
    "target_entity": "Target entity",
    "threshold_amount": "Threshold amount",
    "timeframe": "Timeframe",
    "channels": "Channels",
}
ENTITY_OPTIONS: Final[List[Dict[str, str]]] = [
    {"value": "CUSTOMER", "label": "Per customer (all of a customer's accounts together)"},
    {"value": "ACCOUNT", "label": "Per account"},
    {"value": "TRANSACTION", "label": "Each transaction on its own"},
]
LOOKBACK_OPTIONS: Final[List[Dict[str, Any]]] = [
    {"value": 1, "label": "1 day"},
    {"value": 7, "label": "7 days"},
    {"value": 30, "label": "30 days"},
    {"value": 90, "label": "90 days (one quarter)"},
    {"value": 180, "label": "180 days"},
    {"value": 365, "label": "1 year"},
    {"value": "Q1", "label": "First quarter (Jan-Mar)"},
    {"value": "Q2", "label": "Second quarter (Apr-Jun)"},
    {"value": "Q3", "label": "Third quarter (Jul-Sep)"},
    {"value": "Q4", "label": "Last quarter (Oct-Dec)"},
    {"value": "custom", "label": "A fixed date range"},
]
QUARTERS: Final[Dict[str, Tuple[int, int]]] = {"Q1": (1, 3), "Q2": (4, 6), "Q3": (7, 9), "Q4": (10, 12)}
QUARTER_NAMES: Final[Dict[str, str]] = {"Q1": "First quarter", "Q2": "Second quarter", "Q3": "Third quarter",
                                        "Q4": "Last quarter"}
CHANNEL_OPTIONS: Final[List[Dict[str, str]]] = [
    {"value": "ALL", "label": "All transaction types"},
    {"value": "CASH DEPOSIT", "label": "Cash deposit"},
    {"value": "CASH WITHDRAWAL", "label": "Cash withdrawal"},
    {"value": "OUTWARD TRANSFER", "label": "Outward transfer"},
    {"value": "INWARD TRANSFER", "label": "Inward transfer"},
    {"value": "CHEQUE DEPOSIT", "label": "Cheque deposit"},
    {"value": "ATM WITHDRAWAL", "label": "ATM withdrawal"},
    {"value": "OTHER", "label": "Other (type it)"},
]

_ENTITY_WORDS: Final = re.compile(
    r"\b(customers?|clients?|persons?|people|individuals?|corporates?|compan(?:y|ies)|entit(?:y|ies)|"
    r"accounts?|account holders?|per (?:customer|account|client))\b",
    re.IGNORECASE,
)
_SINGLE_TRANSACTION: Final = re.compile(r"\b(any|a|each|every|one)\s+single\b|\bsingle\s+(?:cash\s+)?\w+\b|\bper transaction\b",
                                        re.IGNORECASE)
_TIME_PHRASE: Final = re.compile(
    r"\b(?:within|in|over|during|for|last|past|previous|next|of)\s+(?:the\s+)?(?:last\s+|past\s+)?"
    r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|twelve|a|an)?\s*"
    r"(?:business\s+|working\s+|calendar\s+)?(?:days?|weeks?|months?|years?|quarters?)\b"
    r"|\b(?:daily|weekly|monthly|quarterly|yearly|annually|same day|same-day|per day|per week|per month)\b"
    r"|\b\d+\s*-?\s*(?:days?|weeks?|months?|years?)\b",
    re.IGNORECASE,
)
_ALL_TYPES: Final = re.compile(r"\b(?:any|all)\s+(?:kinds?\s+of\s+|types?\s+of\s+)?transactions?\b|\ball transaction types\b",
                               re.IGNORECASE)
# Threshold fields that hold money (the others are counts, ratios, ages ...).
_AMOUNT_FIELD: Final = re.compile(r"amount|amt|volume|value|sum|total|balance", re.IGNORECASE)
# Amount words that need a number ("3 high cash deposits").
_VAGUE_AMOUNT: Final = re.compile(
    r"\b(?:high|higher|large|larger|big|huge|significant|substantial|heavy|sizeable|sizable|major)\b(?![- ]?risk)"
    r"|\b(?:high|large)[- ]value\b|مرتفع|كبير|كبيرة|ضخم",
    re.IGNORECASE,
)
_ANY_WORD: Final = re.compile(r"\b(?:any|all|every)\b", re.IGNORECASE)
_NUMBER: Final = re.compile(
    r"(\d[\d,]*(?:\.\d+)?)\s*(%|(?:k|thousand|m|mn|million|b|bn|billion)\b)?", re.IGNORECASE
)
# Arabic-Indic and Persian digits -> Western digits.
_DIGITS: Final = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
# Every way of writing a percent after a number: "50 %", "50 percent", "50٪", "50 بالمئة" -> "50%".
_PERCENT_WORDS: Final = re.compile(
    r"(\d)\s*(?:%|per\s?cent\b|pct\b|بالمئة|بالمائة|في\s+المئة|في\s+المائة)", re.IGNORECASE
)
_MULTIPLIERS: Final[Dict[str, float]] = {
    "k": 1e3, "thousand": 1e3, "m": 1e6, "mn": 1e6, "million": 1e6, "b": 1e9, "bn": 1e9, "billion": 1e9,
}
# A channel named only by one of these words ("deposits", "transfers") does not say which channel.
_BARE_CHANNEL_WORDS: Final = frozenset(
    {"transaction", "transactions", "transfer", "transfers", "payment", "payments", "deposit", "deposits",
     "withdrawal", "withdrawals", "activity", "activities", "movement", "movements"}
)
# "transactions above 500,000": an amount on each transaction, so each transaction is monitored.
_PER_TRANSACTION_AMOUNT: Final = re.compile(
    r"\b(?:transactions?|deposits?|withdrawals?|transfers?|payments?)\s+(?:of\s+)?"
    r"(?:above|over|exceeding|greater than|more than|larger than|higher than|>=?)\s*(?:jod\s*|\$\s*)?\d",
    re.IGNORECASE,
)


# ─────────────────────────────── gap analysis ────────────────────────────────


def normalize_text(text: str) -> str:
    """Normalize numbers before parsing: Western digits, and every percent form written as 'N%'.

    "50 %", "50 percent", "٥٠٪", "50٪" and "50 بالمئة" all become "50%". Arabic decimal and thousands
    separators become '.' and ','.

    Args:
        text (str): Free text (English or Arabic).

    Returns:
        str: The normalized text.
    """
    normalized = (text or "").translate(_DIGITS).replace("٫", ".").replace("٬", ",").replace("٪", "%")
    return _PERCENT_WORDS.sub(r"\1%", normalized)


def percentages_in(text: str) -> List[float]:
    """The percentages written in a text, as ratios (50% -> 0.5).

    Args:
        text (str): Free text.

    Returns:
        List[float]: One ratio per percentage, in order.
    """
    return [float(v) / 100 for v in re.findall(r"(\d+(?:\.\d+)?)%", normalize_text(text))]


def numbers_in(text: str) -> Set[float]:
    """Every number written in a text, with k / million / % expanded.

    Args:
        text (str): Free text.

    Returns:
        Set[float]: The values; a percentage is also returned as its ratio (90% -> 90.0 and 0.9).
    """
    text = normalize_text(text)
    found: Set[float] = set()
    for digits, suffix in _NUMBER.findall(text or ""):
        try:
            value = float(digits.replace(",", ""))
        except ValueError:
            continue
        suffix = (suffix or "").lower()
        if suffix == "%":
            found.update({value, value / 100})
        else:
            found.add(value * _MULTIPLIERS.get(suffix, 1))
    for words, multiplier in (("thousand", 1e3), ("million", 1e6)):
        if re.search(rf"\ba\s+{words}\b", text or "", re.IGNORECASE):
            found.add(multiplier)
    return found


def _threshold_stated(intent: Dict[str, Any], text: str) -> bool:
    """Whether the user gave the amount (or a count rule that needs no amount).

    The user's words decide, not the LLM's provenance label (it is not consistent: a written 50,000 can
    come back as 'assumed_default'). A count ("3 deposits") is not an amount: it states the threshold
    only when the request does not also ask for "high" / "large" amounts.

    Args:
        intent (Dict[str, Any]): The intent.
        text (str): The user's request.

    Returns:
        bool: True when an amount threshold with a value above 0 appears in the text, or when a count
            threshold appears in the text and no amount word ("high", "large", ...) is left without a number.
    """
    written = numbers_in(text)
    count_written = False
    for threshold in intent.get("thresholds") or []:
        if not isinstance(threshold, dict):
            continue
        if threshold.get("kind") == "ratio" and threshold.get("value_from") not in (None, ""):
            return True  # "50% of the balance": the percentage is the threshold
        numeric = []
        for value in (threshold.get("value_from"), threshold.get("value_to")):
            try:
                numeric.append(float(value))
            except (TypeError, ValueError):
                continue
        if not any(any(abs(v - w) < 1e-6 for w in written) for v in numeric):
            continue
        if _AMOUNT_FIELD.search(str(threshold.get("field") or "")):
            if any(v > 0 for v in numeric):
                return True
        else:
            count_written = True
    return count_written and not _VAGUE_AMOUNT.search(text or "")


def _timeframe_stated(intent: Dict[str, Any], text: str) -> bool:
    """Whether the user gave a lookback, a fixed period or a frequency.

    Args:
        intent (Dict[str, Any]): The intent.
        text (str): The user's request.

    Returns:
        bool: True when the text states a timeframe; the LLM's provenance label is not used.
    """
    return bool(find_fixed_period(text) or _TIME_PHRASE.search(text or ""))


def _channels_stated(intent: Dict[str, Any], text: str) -> bool:
    """Whether the user named the channels (or said all of them).

    Args:
        intent (Dict[str, Any]): The intent.
        text (str): The user's request.

    Returns:
        bool: True for "any / all transactions", a debit / credit filter, or types whose words appear in the text.
    """
    if _ALL_TYPES.search(text or ""):
        return True
    if any(f.get("attribute") == "debit_credit" for f in intent.get("attribute_filters") or [] if isinstance(f, dict)):
        return True
    types = [t for t in intent.get("transaction_types") or [] if t]
    if not types and intent.get("transaction_type"):
        types = [p.strip() for p in str(intent["transaction_type"]).split(",") if p.strip()]
    types = [t for t in types if str(t).strip().upper() not in ("ALL", "ANY", "ALL TRANSACTIONS")]
    if not types:
        # "Flag any above 50,000 JOD": "any" / "all" with no type named means every transaction type.
        return bool(_ANY_WORD.search(text or ""))
    lowered = (text or "").lower()
    for transaction_type in types:
        # The qualifier ("cash", "outward", "cheque") is what names the channel; it must be the user's word.
        qualifiers = [w for w in re.findall(r"[a-z]+", str(transaction_type).lower())
                      if w not in _BARE_CHANNEL_WORDS and len(w) > 2]
        if not qualifiers or not all(w[:5] in lowered for w in qualifiers):
            return False
    return True


def _entity_stated(intent: Dict[str, Any], text: str) -> bool:
    """Whether the user said what is monitored: each transaction, or per customer / account.

    Args:
        intent (Dict[str, Any]): The intent.
        text (str): The user's request.

    Returns:
        bool: True for a single-transaction rule, or when the text names customers or accounts.
    """
    if _SINGLE_TRANSACTION.search(text or "") or _ENTITY_WORDS.search(text or ""):
        return True
    if _PER_TRANSACTION_AMOUNT.search(text or ""):
        return True
    aggregation = intent.get("aggregation") or {}
    aggregated = (isinstance(aggregation, dict) and str(aggregation.get("function") or "").upper() in ("SUM", "COUNT", "AVG")) or any(
        isinstance(t, dict) and t.get("target_scope") == "AGGREGATE" for t in intent.get("thresholds") or [])
    return not aggregated and str(intent.get("scenario_type") or "").upper() == "TRANSACTION"


def find_gaps(intent: Dict[str, Any], text: str, known: Optional[Dict[str, Any]] = None,
              answered: Optional[Set[str]] = None) -> List[str]:
    """The mandatory parameters the user has not stated.

    Args:
        intent (Dict[str, Any]): The intent produced from the request.
        text (str): The user's request (with the original request, on an answer turn).
        known (Optional[Dict[str, Any]]): An intent the user already confirmed (a modification
            request): its parameters count as stated.
        answered (Optional[Set[str]]): Parameters answered in the clarification form.

    Returns:
        List[str]: Field names in form order: target_entity, threshold_amount, timeframe, channels.
    """
    answered = answered or set()
    if not settings.CLARIFICATION_ASK_ENTITY:
        answered = answered | {"target_entity"}  # business decision: per customer (default_entity)
    checks = (
        ("target_entity", _entity_stated, lambda k: bool(k.get("scenario_type"))),
        ("threshold_amount", _threshold_stated, lambda k: bool(k.get("thresholds"))),
        ("timeframe", _timeframe_stated, lambda k: bool(k.get("time_window") or k.get("anchor_date"))),
        ("channels", _channels_stated, lambda k: bool(k.get("transaction_types") or k.get("transaction_type")
                                                      or k.get("attribute_filters"))),
    )
    gaps = []
    for name, stated, known_has in checks:
        if name in answered or (known and known_has(known)):
            continue
        if not stated(intent, text):
            gaps.append(name)
    return gaps


def all_types_if_any(intent: Dict[str, Any], text: str) -> Dict[str, Any]:
    """Set the scope to all transaction types when the user said "any" / "all" and named no type.

    Args:
        intent (Dict[str, Any]): The intent (not modified).
        text (str): The user's request.

    Returns:
        Dict[str, Any]: The intent, with transaction_type 'ALL' in that case.
    """
    types = [t for t in intent.get("transaction_types") or [] if t]
    if types or intent.get("transaction_type") or intent.get("attribute_filters") or not _ANY_WORD.search(text or ""):
        return intent
    return {**intent, "transaction_type": "ALL", "transaction_types": None}


def default_entity(intent: Dict[str, Any], text: str) -> Dict[str, Any]:
    """Apply the business decision "monitor per customer" when the request does not say what is monitored.

    A request that names accounts, customers or single transactions keeps what the user wrote.

    Args:
        intent (Dict[str, Any]): The intent (not modified).
        text (str): The user's request.

    Returns:
        Dict[str, Any]: The intent, with scenario_type CLARIFICATION_DEFAULT_ENTITY (and its aggregation grain)
            when the entity was not stated.
    """
    if settings.CLARIFICATION_ASK_ENTITY or _entity_stated(intent, text):
        return intent
    return apply_answers(intent, {"target_entity": settings.CLARIFICATION_DEFAULT_ENTITY.upper()}, record=False)


# ─────────────────────────────── the form ────────────────────────────────


def default_channels() -> List[str]:
    """Channels used by "Apply Industry Defaults".

    Returns:
        List[str]: Transaction types from settings.CLARIFICATION_DEFAULT_CHANNELS.
    """
    return [c.strip().upper() for c in settings.CLARIFICATION_DEFAULT_CHANNELS.split(",") if c.strip()]


def _current_channels(intent: Dict[str, Any]) -> List[str]:
    """Channels already in the intent that are offered as form options.

    Args:
        intent (Dict[str, Any]): The intent.

    Returns:
        List[str]: Option values found in the intent's transaction types.
    """
    offered = [o["value"] for o in CHANNEL_OPTIONS if o["value"] != "ALL"]
    found: List[str] = []
    for transaction_type in intent.get("transaction_types") or []:
        name = str(transaction_type).upper().strip()
        word = re.sub(r"S$", "", name)
        # An exact option, or every option of a bare word ("DEPOSIT" -> cash and cheque deposit).
        matches = [name] if name in offered else [o for o in offered if word and word in o]
        found += [m for m in matches if m not in found]
    return found


def build_questions(gaps: List[str], intent: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The form for the missing parameters, with the compliance baseline as each default.

    Args:
        gaps (List[str]): Missing parameters (find_gaps).
        intent (Dict[str, Any]): The intent (to pre-select what it already suggests).

    Returns:
        List[Dict[str, Any]]: ClarificationQuestion dicts, in form order.
    """
    questions: List[Dict[str, Any]] = []
    if "target_entity" in gaps:
        questions.append({
            "id": "target_entity", "field_name": "target_entity", "input_type": "single_select",
            "question_text": "What should be monitored?", "options": ENTITY_OPTIONS,
            "default_value": settings.CLARIFICATION_DEFAULT_ENTITY.upper(), "required": True,
            "help_text": "Totals are added up per customer or per account; otherwise each transaction is checked alone.",
        })
    if "threshold_amount" in gaps:
        questions.append({
            "id": "threshold_amount", "field_name": "threshold_amount", "slot_id": amount_slot(intent),
            "input_type": "number",
            "question_text": "Above which amount should activity be flagged?", "options": None,
            "default_value": settings.CLARIFICATION_DEFAULT_AMOUNT, "required": True, "unit": "JOD",
            "help_text": "Applies to each transaction, or to the total when activity is added up.",
        })
    if "timeframe" in gaps:
        questions.append({
            "id": "timeframe", "field_name": "timeframe", "input_type": "single_select",
            "question_text": "Over which period should activity be checked?", "options": LOOKBACK_OPTIONS,
            "default_value": settings.CLARIFICATION_DEFAULT_WINDOW_DAYS, "required": True, "unit": "days",
            "help_text": "Days count working days (Sunday to Thursday).",
        })
        questions.append({
            "id": "quarter_year", "field_name": "timeframe", "input_type": "number",
            "question_text": "Which year?", "options": None, "default_value": None, "required": False,
            "help_text": "The quarter of this year, e.g. 2019.",
            "show_if": {"question_id": "timeframe", "in": list(QUARTERS)},
        })
        questions.append({
            "id": "date_range", "field_name": "timeframe", "input_type": "date_range",
            "question_text": "Which dates?", "options": None, "default_value": None, "required": False,
            "help_text": "Only for a fixed date range.", "show_if": {"question_id": "timeframe", "equals": "custom"},
        })
    if "channels" in gaps:
        questions.append({
            "id": "channels", "field_name": "channels", "input_type": "multi_select",
            "question_text": "Which transaction types should be included?", "options": CHANNEL_OPTIONS,
            "default_value": _current_channels(intent) or default_channels(), "required": True,
        })
        questions.append({
            "id": "other_channels", "field_name": "channels", "input_type": "text",
            "question_text": "Which other transaction type?", "options": None, "default_value": None, "required": False,
            "help_text": "Type its name as the bank calls it; several separated by commas (e.g. LOAN REPAYMENT, SWIFT TRANSFER).",
            "show_if": {"question_id": "channels", "includes": "OTHER"},
        })
    return questions


def _amount(value: Any) -> str:
    """Format an amount for display.

    Args:
        value (Any): A number.

    Returns:
        str: e.g. '10,000'.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{number:,.0f}" if number == int(number) else f"{number:,.2f}"


def understood_summary(intent: Dict[str, Any], gaps: List[str]) -> List[str]:
    """What was understood, in plain lines (never the guessed values of the missing parameters).

    Args:
        intent (Dict[str, Any]): The intent.
        gaps (List[str]): Missing parameters.

    Returns:
        List[str]: One line per understood element.
    """
    lines: List[str] = []
    if intent.get("detection_logic"):
        lines.append(f"Goal: {intent['detection_logic']}")
    if "target_entity" not in gaps and intent.get("scenario_type"):
        lines.append(f"Monitoring: {next((o['label'] for o in ENTITY_OPTIONS if o['value'] == str(intent['scenario_type']).upper()), intent['scenario_type'])}")
    if "channels" not in gaps:
        types = intent.get("transaction_types") or ([intent["transaction_type"]] if intent.get("transaction_type") else [])
        if types:
            lines.append("Transaction types: " + ", ".join(str(t).title() for t in types))
    if "threshold_amount" not in gaps:
        for threshold in intent.get("thresholds") or []:
            if isinstance(threshold, dict) and threshold.get("value_from") is not None:
                lines.append(f"Condition: {str(threshold.get('field', 'amount')).replace('_', ' ')} "
                             f"{threshold.get('operator', '>')} {_amount(threshold['value_from'])}")
    if "timeframe" not in gaps:
        window = intent.get("time_window") or {}
        if intent.get("anchor_date") and isinstance(window, dict) and window.get("is_rolling") is False:
            lines.append(f"Period: {window.get('value')} {str(window.get('unit', '')).lower()} ending {intent['anchor_date']}")
        elif isinstance(window, dict) and window.get("value"):
            lines.append(f"Timeframe: {window.get('value')} {str(window.get('unit', '')).lower()}")
    for f in intent.get("attribute_filters") or []:
        if isinstance(f, dict):
            values = ", ".join(dict.fromkeys(f.get("descriptions") or f.get("codes") or []))
            lines.append(f"{f.get('label')}: {'not ' if f.get('operator') == 'NOT IN' else ''}{values}")
    return lines


def clarification_payload(intent: Dict[str, Any], gaps: List[str], original_request: str) -> Dict[str, Any]:
    """The NEEDS_CLARIFICATION result of the intent tool.

    Args:
        intent (Dict[str, Any]): The partial intent.
        gaps (List[str]): Missing parameters.
        original_request (str): The user's request.

    Returns:
        Dict[str, Any]: ClarificationResponse fields, plus 'partial_intent' (kept for the answer
            turn, never shown) and 'agent_instruction'.
    """
    intent = assign_slots(intent)
    return {
        "status": "NEEDS_CLARIFICATION",
        "understood_summary": understood_summary(intent, gaps),
        "missing_fields": gaps,
        "questions": build_questions(gaps, intent),
        "original_request": original_request,
        "partial_intent": intent,
        "agent_instruction": (
            "STOP. Do not call any other tool. Reply in ONE short sentence asking the user to complete "
            "the details in the side panel (Clarification form). Do not list the questions."
        ),
    }


# ─────────────────────────────── answers ────────────────────────────────


def _date_range(value: Any) -> Optional[Tuple[date, date]]:
    """Read a date-range answer.

    Args:
        value (Any): {'start': 'YYYY-MM-DD', 'end': 'YYYY-MM-DD'} or [start, end].

    Returns:
        Optional[Tuple[date, date]]: (start, end) in order, or None.
    """
    if isinstance(value, dict):
        start, end = value.get("start") or value.get("from"), value.get("end") or value.get("to")
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        start, end = value
    else:
        return None
    try:
        first, last = to_date(start), to_date(end)
    except (TypeError, ValueError):
        return None
    if not first or not last:
        return None
    return (first, last) if first <= last else (last, first)


def _quarter(answers: Dict[str, Any]) -> Optional[Tuple[date, date]]:
    """The period of a quarter answer (timeframe Q1-Q4 + quarter_year).

    Args:
        answers (Dict[str, Any]): {question id: value}.

    Returns:
        Optional[Tuple[date, date]]: First and last day of that quarter, or None.
    """
    months = QUARTERS.get(str(answers.get("timeframe") or "").upper())
    try:
        year = int(float(answers.get("quarter_year")))
    except (TypeError, ValueError):
        return None
    if not months or not 1950 <= year <= 2099:
        return None
    first = date(year, months[0], 1)
    last = date(year + (months[1] == 12), months[1] % 12 + 1, 1)
    return first, date.fromordinal(last.toordinal() - 1)


def _channel_values(answers: Dict[str, Any]) -> List[str]:
    """The channels answered: the chosen options plus any typed under "Other".

    Args:
        answers (Dict[str, Any]): {question id: value}.

    Returns:
        List[str]: Upper-case transaction types, without the 'OTHER' marker.
    """
    chosen = answers.get("channels")
    values = chosen if isinstance(chosen, list) else ([chosen] if chosen else [])
    found = [str(v).strip().upper() for v in values if str(v).strip() and str(v).strip().upper() != "OTHER"]
    typed = answers.get("other_channels")
    if typed and any(str(v).strip().upper() == "OTHER" for v in values):
        found += [t.strip().upper() for t in re.split(r"[,;\n]", str(typed)) if t.strip()]
    return list(dict.fromkeys(found))


def describe_answers(answers: Dict[str, Any]) -> str:
    """The answers as the user message shown in the chat.

    Args:
        answers (Dict[str, Any]): {question id: value}.

    Returns:
        str: e.g. "Clarification answers: Target entity: Per customer; Threshold amount: 10,000 JOD; ...".
    """
    parts: List[str] = []
    entity = answers.get("target_entity")
    if entity:
        parts.append("Target entity: " + next((o["label"].split(" (")[0] for o in ENTITY_OPTIONS if o["value"] == str(entity).upper()), str(entity)))
    if answers.get("threshold_amount") not in (None, ""):
        parts.append(f"Threshold amount: {_amount(answers['threshold_amount'])} JOD")
    timeframe = str(answers.get("timeframe") or "").upper()
    period = _date_range(answers.get("date_range")) if timeframe == "CUSTOM" else None
    quarter = _quarter(answers) if timeframe in QUARTERS else None
    if period:
        parts.append(f"Timeframe: {period[0].isoformat()} to {period[1].isoformat()}")
    elif quarter:
        parts.append(f"Timeframe: {QUARTER_NAMES[timeframe]} {quarter[0].year} ({quarter[0].isoformat()} to {quarter[1].isoformat()})")
    elif answers.get("timeframe") not in (None, "") and timeframe != "CUSTOM" and timeframe not in QUARTERS:
        parts.append(f"Timeframe: last {answers['timeframe']} days")
    channels = _channel_values(answers)
    if channels:
        labels = {o["value"]: o["label"] for o in CHANNEL_OPTIONS}
        parts.append("Channels: " + ", ".join(labels.get(v, v.title()) for v in channels))
    return "Clarification answers: " + "; ".join(parts) if parts else "Clarification answers submitted."


def answered_fields(answers: Optional[Dict[str, Any]]) -> Set[str]:
    """The mandatory parameters a set of answers fills.

    Args:
        answers (Optional[Dict[str, Any]]): {question id: value}.

    Returns:
        Set[str]: Field names with a usable answer.
    """
    if not answers:
        return set()
    filled = {k for k in ("target_entity", "threshold_amount") if answers.get(k) not in (None, "", [])}
    if _channel_values(answers):
        filled.add("channels")
    timeframe = str(answers.get("timeframe") or "").upper()
    if timeframe == "CUSTOM":
        if _date_range(answers.get("date_range")):
            filled.add("timeframe")
    elif timeframe in QUARTERS:
        if _quarter(answers):
            filled.add("timeframe")
    elif timeframe:
        filled.add("timeframe")
    return filled


def apply_answers(intent: Dict[str, Any], answers: Dict[str, Any], record: bool = True,
                  questions: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Merge the form answers into an intent; they override whatever the LLM guessed.

    Each answer updates only the condition its question is bound to: the amount answer fills the slot
    named by the question's ``slot_id`` (or adds one amount threshold), never a balance or ratio condition.

    Args:
        intent (Dict[str, Any]): The intent (not modified).
        answers (Dict[str, Any]): {question id: value}.
        record (bool): Whether to mark the fields as clarified (False for a business default).
        questions (Optional[List[Dict[str, Any]]]): The questions that were asked (for their slot ids).

    Returns:
        Dict[str, Any]: The updated copy, with 'clarified_fields' listing what the answers set.
    """
    updated = copy.deepcopy(intent)
    filled = answered_fields(answers)

    if "target_entity" in filled:
        entity = str(answers["target_entity"]).upper()
        updated["scenario_type"] = entity
        aggregation = updated.get("aggregation")
        if entity in ("CUSTOMER", "ACCOUNT") and isinstance(aggregation, dict):
            aggregation["grain"] = re.sub(r"PER\s+\w+", f"PER {entity}", str(aggregation.get("grain") or f"PER {entity}"), count=1)

    if "threshold_amount" in filled:
        amount = float(answers["threshold_amount"])
        aggregated = isinstance(updated.get("aggregation"), dict) and str(
            (updated.get("aggregation") or {}).get("function") or "").upper() == "SUM"
        updated = assign_slots(updated)
        thresholds = [t for t in updated.get("thresholds") or [] if isinstance(t, dict)]
        asked = next((q for q in questions or [] if q.get("id") == "threshold_amount"), None)
        slot = (asked or {}).get("slot_id") or amount_slot(updated)
        target = next((t for t in thresholds if t.get("slot_id") == slot and _is_amount_slot(t)), None)
        if target is not None:
            target["value_from"] = amount
            target["provenance"] = "stated"
        else:
            thresholds.append({
                "field": "cumulative_transaction_volume" if aggregated else "transaction_amount",
                "operator": ">=", "value_from": amount, "slot_id": f"t{len(thresholds)}",
                "target_scope": "AGGREGATE" if aggregated else "DETAIL", "provenance": "stated",
            })
        updated["thresholds"] = thresholds

    if "timeframe" in filled:
        timeframe = str(answers["timeframe"]).upper()
        if timeframe in ("CUSTOM",) or timeframe in QUARTERS:
            first, last = _date_range(answers.get("date_range")) if timeframe == "CUSTOM" else _quarter(answers)  # type: ignore[misc]
            updated["anchor_date"] = last.isoformat()
            updated["time_window"] = window_for_period(first, last)
        else:
            updated["time_window"] = {"unit": "DAYS", "value": int(answers["timeframe"]), "is_rolling": True,
                                      "provenance": "stated"}

    if "channels" in filled:
        values = _channel_values(answers)
        if "ALL" in values:
            updated["transaction_type"] = "ALL"
            updated["transaction_types"] = None
        else:
            updated["transaction_types"] = values
            updated["transaction_type"] = ", ".join(values)
        updated["explanation_codes"] = None
        updated["explanation_codes_by_type"] = None
        for list_field in ("thresholds", "semantic_conditions"):
            for condition in updated.get(list_field) or []:
                if isinstance(condition, dict):
                    condition["explanation_codes"] = None
                    if condition.get("transaction_type") and str(condition["transaction_type"]).upper() not in values:
                        condition["transaction_type"] = values[0] if len(values) == 1 and "ALL" not in values else None

    if record:
        updated["clarifications"] = []
        updated["clarification_needed"] = False
        updated["clarified_fields"] = sorted(filled | set(updated.get("clarified_fields") or []))
    return updated


def clarification_event_data(payload: Dict[str, Any]) -> Dict[str, Any]:
    """The part of a NEEDS_CLARIFICATION tool result the UI shows.

    Args:
        payload (Dict[str, Any]): The intent tool's result.

    Returns:
        Dict[str, Any]: status, understood_summary, missing_fields, questions, original_request.
    """
    keys = ("status", "understood_summary", "missing_fields", "questions", "original_request")
    return {k: payload.get(k) for k in keys}


def pending_clarification(messages: List[Any]) -> Optional[Dict[str, Any]]:
    """The latest NEEDS_CLARIFICATION result of the intent tool in the conversation, if still open.

    Args:
        messages (List[Any]): The agent state's messages.

    Returns:
        Optional[Dict[str, Any]]: The payload (with 'original_request' and 'partial_intent'), or None when the
            last intent result was a complete scenario.
    """
    for message in reversed(messages or []):
        if getattr(message, "type", None) != "tool":
            continue
        try:
            payload = json.loads(getattr(message, "content", "") or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("status") == "NEEDS_CLARIFICATION":
            return payload
        if "enriched_intent" in payload:
            return None
    return None


# ─────────────────────────────── condition slots ────────────────────────────────

_NOT_AN_AMOUNT: Final = re.compile(r"balance|ratio|percent|pct|count|number|age", re.IGNORECASE)
_MONEY_FIELD: Final = re.compile(r"amount|amt|volume|value|sum|total", re.IGNORECASE)


def assign_slots(intent: Dict[str, Any]) -> Dict[str, Any]:
    """Give every threshold a stable slot id ('t0', 't1', ...) that answers are bound to.

    Args:
        intent (Dict[str, Any]): The intent (not modified).

    Returns:
        Dict[str, Any]: A copy whose thresholds all have a slot_id (existing ids are kept).
    """
    updated = copy.deepcopy(intent)
    thresholds = updated.get("thresholds") or []
    used = {t.get("slot_id") for t in thresholds if isinstance(t, dict)}
    for i, threshold in enumerate(thresholds):
        if isinstance(threshold, dict) and not threshold.get("slot_id"):
            slot, n = f"t{i}", i
            while slot in used:
                n += 1
                slot = f"t{n}"
            threshold["slot_id"] = slot
            used.add(slot)
    return updated


def _is_amount_slot(threshold: Dict[str, Any]) -> bool:
    """Whether a threshold is a plain money amount an amount answer may fill.

    Args:
        threshold (Dict[str, Any]): One threshold.

    Returns:
        bool: False for ratios, balances, counts, ages and percentages.
    """
    field_name = str(threshold.get("field") or "")
    return (threshold.get("kind") or "value") == "value" and bool(_MONEY_FIELD.search(field_name)) \
        and not _NOT_AN_AMOUNT.search(field_name)


def amount_slot(intent: Dict[str, Any]) -> str:
    """The one condition the amount question fills.

    Args:
        intent (Dict[str, Any]): The intent.

    Returns:
        str: The slot id of the plain amount threshold (one without a value first), or 'new'.
    """
    candidates = [t for t in assign_slots(intent).get("thresholds") or [] if isinstance(t, dict) and _is_amount_slot(t)]
    empty = [t for t in candidates if t.get("value_from") in (None, "", 0, 0.0)]
    chosen = (empty or candidates or [None])[0]
    return chosen["slot_id"] if chosen else "new"


# ─────────────────────────────── ratio conditions ("X% of Y") ────────────────────────────────

_RATIO_OPERATORS: Final[Dict[str, str]] = {
    "greater than": ">", "more than": ">", "higher than": ">", "larger than": ">", "above": ">", "over": ">",
    "exceeding": ">", "exceeds": ">", "exceed": ">", "at least": ">=", "less than": "<", "lower than": "<",
    "below": "<", "under": "<", "at most": "<=",
    "أكبر من": ">", "أكثر من": ">", "أعلى من": ">", "يتجاوز": ">", "تتجاوز": ">", "يزيد عن": ">", "تزيد عن": ">",
    "يزيد على": ">", "لا يقل عن": ">=", "أقل من": "<", "لا يزيد عن": "<=",
}
_RATIO_RE: Final = re.compile(
    r"(?P<op>" + "|".join(sorted((re.escape(k) for k in _RATIO_OPERATORS), key=len, reverse=True)) + r")\s*"
    r"(?P<pct>\d+(?:\.\d+)?)%\s*(?P<of>of\b|من|عن|على)?\s*(?P<right>[^.,;\n]{1,80})",
    re.IGNORECASE,
)
# (pattern, field): first match wins, so the more specific phrases come first.
_LEFT_FIELDS: Final = (
    (r"cash deposits?|deposits?|إيداعات?(?:\s+نقدية)?|الإيداعات?|إيداع", "deposit_amount"),
    (r"cash withdrawals?|withdrawals?|سحوبات|السحب|سحب", "withdrawal_amount"),
    (r"transfers?|تحويلات|التحويل|تحويل", "transfer_amount"),
    (r"transactions?|payments?|الحركات?|حركة|معاملات?", "transaction_amount"),
)
_RIGHT_FIELDS: Final = (
    (r"average\s+(?:daily\s+|monthly\s+)?deposits?|avg\.?\s+deposits?|متوسط\s+(?:ال)?إيداعات", "average_deposit_amount"),
    (r"average\s+(?:daily\s+|monthly\s+)?withdrawals?|متوسط\s+(?:ال)?سحوبات", "average_withdrawal_amount"),
    (r"(?:account\s+)?balance|الرصيد|رصيد(?:\s+الحساب)?", "account_balance"),
    (r"(?:total\s+)?deposits|الإيداعات", "total_deposits"),
    (r"(?:total\s+)?withdrawals|السحوبات", "total_withdrawals"),
    (r"(?:declared\s+|monthly\s+)?income|salary|الدخل|الراتب", "declared_income"),
)


def _field_in(text: str, table: Tuple[Tuple[str, str], ...], last: bool = False) -> Optional[str]:
    """The field named in a phrase.

    Args:
        text (str): The phrase.
        table (Tuple[Tuple[str, str], ...]): (pattern, field) pairs.
        last (bool): Take the mention closest to the end of the text (the subject right before an operator).

    Returns:
        Optional[str]: The field, or None.
    """
    hits = []
    for pattern, field_name in table:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            hits.append((match.start(), field_name))
    if not hits:
        return None
    return (max(hits) if last else min(hits))[1]


def parse_ratio_conditions(text: str) -> List[Dict[str, Any]]:
    """Find "<field> greater / less than X% of <other field>" conditions (English and Arabic).

    Args:
        text (str): The user's request.

    Returns:
        List[Dict[str, Any]]: One {type: 'ratio', left, right, operator, factor, phrase} per condition,
            e.g. {left: 'deposit_amount', right: 'account_balance', operator: '>', factor: 0.5}.
    """
    normalized = normalize_text(text)
    found: List[Dict[str, Any]] = []
    for match in _RATIO_RE.finditer(normalized):
        right_text = match.group("right")
        right = _field_in(right_text[:40], _RIGHT_FIELDS)
        if not right and not match.group("of"):
            continue  # "greater than 50% within 7 days": no "of" and no known field -> not a ratio
        if not right:
            words = re.findall(r"[A-Za-z]+", right_text)
            words = [w.lower() for w in words if w.lower() not in ("the", "his", "her", "their", "its", "a", "an", "customer", "customers")]
            right = "_".join(words[:3]) or None
        if not right:
            continue
        left = _field_in(normalized[max(0, match.start() - 80):match.start()], _LEFT_FIELDS, last=True) or "transaction_amount"
        found.append({
            "type": "ratio", "left": left, "right": right,
            "operator": _RATIO_OPERATORS[match.group("op").lower()] if match.group("op").lower() in _RATIO_OPERATORS
            else _RATIO_OPERATORS[match.group("op")],
            "factor": round(float(match.group("pct")) / 100, 6), "phrase": match.group(0).strip(),
        })
    return found


def apply_ratio_conditions(intent: Dict[str, Any], ratios: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Replace the LLM's split of "X% of Y" with one ratio threshold per condition.

    The LLM turned "deposit greater than 50% of the account balance" into ``amount > 0.5`` and
    ``account_balance < None``. Both pieces are removed and one ratio condition is added:
    ``deposit_amount > 0.5 x account_balance``.

    Args:
        intent (Dict[str, Any]): The intent (not modified).
        ratios (List[Dict[str, Any]]): parse_ratio_conditions output.

    Returns:
        Dict[str, Any]: The updated copy (unchanged without ratios).
    """
    if not ratios:
        return intent
    updated = copy.deepcopy(intent)
    thresholds = [t for t in updated.get("thresholds") or [] if isinstance(t, dict)]
    default_type = (updated.get("transaction_types") or [None])[0] if len(updated.get("transaction_types") or []) == 1 else None
    for ratio in ratios:
        already = any(
            t.get("kind") == "ratio" and t.get("field") == ratio["left"] and t.get("relative_to") == ratio["right"]
            and t.get("operator") == ratio["operator"] and abs(float(t.get("value_from") or 0) - ratio["factor"]) < 1e-9
            for t in thresholds
        )
        if already:  # idempotent: the answer turn re-applies the ratios of the stored intent
            continue
        right_words = set(ratio["right"].split("_")) - {"amount", "total", "account"}

        def is_split_piece(threshold: Dict[str, Any]) -> bool:
            names = f"{threshold.get('field') or ''} {threshold.get('value_from') or ''}".lower()
            value = threshold.get("value_from")
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                numeric = None
            return (bool(right_words) and any(w in names for w in right_words)) or value in (None, "") or (
                numeric is not None and (abs(numeric - ratio["factor"]) < 1e-9 or abs(numeric - ratio["factor"] * 100) < 1e-9))

        kept = [t for t in thresholds if t.get("kind") == "ratio" or not is_split_piece(t)]
        transaction_type = next((t.get("transaction_type") for t in thresholds if t not in kept and t.get("transaction_type")),
                                default_type)
        kept.append({
            "field": ratio["left"], "kind": "ratio", "operator": ratio["operator"], "value_from": ratio["factor"],
            "relative_to": ratio["right"], "target_scope": "DETAIL", "provenance": "stated",
            "transaction_type": transaction_type,
        })
        thresholds = kept
    updated["thresholds"] = thresholds
    return assign_slots(updated)


def condition_sql(threshold: Dict[str, Any]) -> str:
    """One condition as a SQL predicate over business field names.

    Args:
        threshold (Dict[str, Any]): A threshold.

    Returns:
        str: e.g. 'deposit_amount > 0.5 * account_balance' or 'transaction_amount >= 10000'.
    """
    field_name, operator, value = threshold.get("field"), threshold.get("operator"), threshold.get("value_from")
    if threshold.get("kind") == "ratio":
        return f"{field_name} {operator} {value:g} * {threshold.get('relative_to')}"
    if operator == "BETWEEN":
        return f"{field_name} BETWEEN {value} AND {threshold.get('value_to')}"
    return f"{field_name} {operator} {value}"


def for_sql_service(intent: Dict[str, Any]) -> Dict[str, Any]:
    """The intent as the SQL service reads it: a ratio becomes its '<left>_to_<right>_ratio' threshold.

    Args:
        intent (Dict[str, Any]): The intent (not modified).

    Returns:
        Dict[str, Any]: A copy; ratio thresholds keep relative_to and gain the service's field name.
    """
    updated = copy.deepcopy(intent)
    for threshold in updated.get("thresholds") or []:
        if isinstance(threshold, dict) and threshold.get("kind") == "ratio":
            threshold["field"] = f"{threshold['field']}_to_{threshold.get('relative_to')}_ratio"
            threshold.pop("kind", None)
    return updated


# ─────────────────────────────── validation before SQL generation ────────────────────────────────

_OPERATORS: Final = frozenset({">", "<", ">=", "<=", "=", "BETWEEN", "IN"})


def _label(field_name: Any) -> str:
    """A field name in plain words.

    Args:
        field_name (Any): e.g. 'account_balance'.

    Returns:
        str: e.g. 'account balance'.
    """
    return re.sub(r"\s+", " ", str(field_name or "").replace("_", " ")).strip() or "a condition"


def validate_conditions(intent: Dict[str, Any]) -> List[str]:
    """Plain-language problems that stop SQL generation (missing value, missing field, unknown operator).

    Args:
        intent (Dict[str, Any]): The intent about to be sent for SQL generation.

    Returns:
        List[str]: One sentence per problem; empty when every condition is complete.
    """
    problems: List[str] = []
    for threshold in intent.get("thresholds") or []:
        if not isinstance(threshold, dict):
            continue
        label = _label(threshold.get("field"))
        value = threshold.get("value_from")
        if not str(threshold.get("field") or "").strip():
            problems.append("one condition does not say what to compare")
            continue
        if str(threshold.get("operator") or "").upper() not in _OPERATORS:
            problems.append(f"the comparison for the {label} is not clear (greater than, less than, between ...)")
        if threshold.get("kind") == "ratio":
            if not threshold.get("relative_to"):
                problems.append(f"what the {label} should be compared with is missing")
            if value in (None, "") or str(value).strip().lower() in ("none", "null"):
                problems.append(f"the percentage for the {label} is missing")
            continue
        if value in (None, "") or str(value).strip().lower() in ("none", "null"):
            problems.append(f"the value for the {label} is missing")
        if str(threshold.get("operator") or "").upper() == "BETWEEN" and threshold.get("value_to") in (None, ""):
            problems.append(f"the upper limit for the {label} is missing")
    return list(dict.fromkeys(problems))


def invalid_rule_message(problems: List[str], text: str = "") -> str:
    """The message shown when a rule is incomplete (never a raw expression).

    Args:
        problems (List[str]): validate_conditions output.
        text (str): The user's request (for the language).

    Returns:
        str: e.g. "I can't build this rule yet: the value for the transaction amount is missing. Please add it and try again."
    """
    if re.search(r"[؀-ۿ]", text or ""):
        return "لا أستطيع بناء هذه القاعدة بعد: بعض الشروط غير مكتملة (" + "؛ ".join(problems) + "). يرجى إكمالها والمحاولة مرة أخرى."
    return "I can't build this rule yet: " + "; ".join(problems) + ". Please add it and try again."


_RAW_EXPRESSION: Final = re.compile(r"\b[\w.]+\s*(?:<=|>=|<|>|=)\s*(?:None|null|nan)\b", re.IGNORECASE)


def plain_text(message: str) -> str:
    """Remove raw expressions such as 'account_balance < None' from a message shown to the user.

    Args:
        message (str): Text from the SQL service.

    Returns:
        str: The text with such expressions replaced by "an incomplete condition".
    """
    return _RAW_EXPRESSION.sub("an incomplete condition", message or "")


# ─────────────────────────────── data capabilities (balance history) ────────────────────────────────

BALANCE_REFUSAL_EN: Final = ("Balances on this database aren't kept day by day, so I can't compare deposits with the balance. "
                             "I can compare them with the customer's average deposits instead.")
BALANCE_REFUSAL_AR: Final = ("الأرصدة في هذه القاعدة غير محفوظة يومياً، لذلك لا أستطيع مقارنة الإيداع بالرصيد. "
                             "يمكنني مقارنته بمتوسط إيداعات العميل بدلاً من ذلك.")
ALTERNATIVE_LOOKBACK_DAYS: Final = 90


def needs_balance(intent: Dict[str, Any]) -> bool:
    """Whether a rule compares against the account balance.

    Args:
        intent (Dict[str, Any]): The intent.

    Returns:
        bool: True when a threshold's field or ratio denominator is a balance.
    """
    return any(
        isinstance(t, dict) and ("balance" in str(t.get("field") or "").lower() or "balance" in str(t.get("relative_to") or "").lower())
        for t in intent.get("thresholds") or []
    )


def balance_capability_refusal(intent: Dict[str, Any], text: str) -> Optional[Dict[str, Any]]:
    """The refusal of a balance rule on a database without balance history, with the alternative rule.

    Extension point: when a bank's warehouse provides a daily account snapshot (previous-day balance in
    JOD), set DATA_CAPABILITIES[<database>].balance_history = true and balance rules pass this check.

    Args:
        intent (Dict[str, Any]): The parsed intent.
        text (str): The user's request (for the language and the alternative).

    Returns:
        Optional[Dict[str, Any]]: The NOT_SUPPORTED tool result, or None when the rule may be built.
    """
    if not needs_balance(intent) or settings.has_capability("balance_history"):
        return None
    ratio = next((t for t in intent.get("thresholds") or [] if isinstance(t, dict) and t.get("kind") == "ratio"), None)
    percent = f"{float(ratio['value_from']) * 100:g}" if ratio and ratio.get("value_from") is not None else "50"
    word = "greater than" if not ratio or str(ratio.get("operator")).startswith(">") else "less than"
    arabic = bool(re.search(r"[؀-ۿ]", text or ""))
    if arabic:
        request = (f"إيداعات نقدية {'أكبر' if word == 'greater than' else 'أقل'} من {percent}% من متوسط إيداعات العميل "
                   f"خلال آخر {ALTERNATIVE_LOOKBACK_DAYS} يوماً")
    else:
        request = (f"Flag cash deposits {word} {percent}% of the customer's average deposits over the previous "
                   f"{ALTERNATIVE_LOOKBACK_DAYS} days")
    return {
        "status": "NOT_SUPPORTED",
        "capability": "balance_history",
        "database": settings.database_name(),
        "user_message": BALANCE_REFUSAL_AR if arabic else BALANCE_REFUSAL_EN,
        "alternative": {"request": request, "condition": {
            "type": "ratio", "left": (ratio or {}).get("field") or "deposit_amount", "right": "average_deposit_amount",
            "operator": (ratio or {}).get("operator") or ">", "factor": float(percent) / 100,
            "lookback_days": ALTERNATIVE_LOOKBACK_DAYS}},
        "agent_instruction": (
            "Reply with user_message verbatim, then in one sentence offer the alternative rule (alternative.request). "
            "Build it ONLY if the user accepts: then call analyze_intent_and_discover_explanation_codes with "
            "alternative.request as user_prompt. Do not call any other tool now."
        ),
    }
