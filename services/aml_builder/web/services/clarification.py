"""
Clarification Question Flow: ask for the mandatory scenario parameters instead of guessing them.

Four parameters decide which alerts a scenario raises. When the request does not state one of them,
the intent tool stops before code discovery, the plan and the shadow test, and returns a
``NEEDS_CLARIFICATION`` payload; the UI shows it as a form, and the answers come back with the next
chat request (``ChatRequest.clarification_answers``) and are merged into the intent here.

==================  ===================================================================  ============
Parameter           Stated when                                                          Question
==================  ===================================================================  ============
target_entity       a single-transaction rule, or the text names customers / accounts    single_select
threshold_amount    a threshold whose number appears in the text                         number
timeframe           a lookback ("within 7 days", "last month"), a period ("in 2019"),    single_select
                    or a frequency ("daily")                                             (+ date_range)
channels            the text names the intent's transaction types, says "any / all",    multi_select
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
    {"value": 90, "label": "90 days"},
    {"value": 180, "label": "180 days"},
    {"value": 365, "label": "1 year"},
    {"value": "custom", "label": "A fixed date range"},
]
CHANNEL_OPTIONS: Final[List[Dict[str, str]]] = [
    {"value": "ALL", "label": "All transaction types"},
    {"value": "CASH DEPOSIT", "label": "Cash deposit"},
    {"value": "CASH WITHDRAWAL", "label": "Cash withdrawal"},
    {"value": "OUTWARD TRANSFER", "label": "Outward transfer"},
    {"value": "INWARD TRANSFER", "label": "Inward transfer"},
    {"value": "CHEQUE DEPOSIT", "label": "Cheque deposit"},
    {"value": "ATM WITHDRAWAL", "label": "ATM withdrawal"},
]

_UNSTATED: Final = {"assumed_default", "needs_user"}
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
_NUMBER: Final = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(k|thousand|m|mn|million|b|bn|billion|%)?\b", re.IGNORECASE)
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


def numbers_in(text: str) -> Set[float]:
    """Every number written in a text, with k / million / % expanded.

    Args:
        text (str): Free text.

    Returns:
        Set[float]: The values; a percentage is also returned as its ratio (90% -> 90.0 and 0.9).
    """
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
    """Whether a threshold's number was written by the user.

    Args:
        intent (Dict[str, Any]): The intent.
        text (str): The user's request.

    Returns:
        bool: True when a threshold is not marked as assumed and its value appears in the text.
    """
    written = numbers_in(text)
    for threshold in intent.get("thresholds") or []:
        if not isinstance(threshold, dict) or str(threshold.get("provenance") or "stated") in _UNSTATED:
            continue
        values = [threshold.get("value_from"), threshold.get("value_to")]
        numeric = []
        for value in values:
            try:
                numeric.append(float(value))
            except (TypeError, ValueError):
                continue
        if any(any(abs(v - w) < 1e-6 for w in written) for v in numeric):
            return True
    return False


def _timeframe_stated(intent: Dict[str, Any], text: str) -> bool:
    """Whether the user gave a lookback, a fixed period or a frequency.

    Args:
        intent (Dict[str, Any]): The intent.
        text (str): The user's request.

    Returns:
        bool: True when the text states a timeframe (and the intent does not mark its window as assumed).
    """
    window = intent.get("time_window") or {}
    if isinstance(window, dict) and str(window.get("provenance") or "stated") in _UNSTATED and not find_fixed_period(text):
        return False
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
        return str(intent.get("transaction_type") or "").strip().upper() in ("ALL", "ANY", "ALL TRANSACTIONS") and bool(
            re.search(r"\b(?:any|all|every)\b", text or "", re.IGNORECASE))
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
            "id": "threshold_amount", "field_name": "threshold_amount", "input_type": "number",
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
    period = _date_range(answers.get("date_range")) if str(answers.get("timeframe")) == "custom" else None
    if period:
        parts.append(f"Timeframe: {period[0].isoformat()} to {period[1].isoformat()}")
    elif answers.get("timeframe") not in (None, "", "custom"):
        parts.append(f"Timeframe: last {answers['timeframe']} days")
    channels = answers.get("channels")
    if channels:
        values = channels if isinstance(channels, list) else [channels]
        labels = {o["value"]: o["label"] for o in CHANNEL_OPTIONS}
        parts.append("Channels: " + ", ".join(labels.get(str(v).upper(), str(v)) for v in values))
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
    filled = {k for k in ("target_entity", "threshold_amount", "channels") if answers.get(k) not in (None, "", [])}
    timeframe = answers.get("timeframe")
    if timeframe not in (None, "") and (str(timeframe) != "custom" or _date_range(answers.get("date_range"))):
        filled.add("timeframe")
    return filled


def apply_answers(intent: Dict[str, Any], answers: Dict[str, Any]) -> Dict[str, Any]:
    """Merge the form answers into an intent; they override whatever the LLM guessed.

    Args:
        intent (Dict[str, Any]): The intent (not modified).
        answers (Dict[str, Any]): {question id: value}.

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
        thresholds = [t for t in updated.get("thresholds") or [] if isinstance(t, dict)]
        money = [t for t in thresholds if re.search(r"amount|volume|value|sum|total", str(t.get("field") or ""), re.I)]
        if money:
            for threshold in money:
                threshold["value_from"] = amount
                threshold["provenance"] = "stated"
        else:
            thresholds.append({
                "field": "cumulative_transaction_volume" if aggregated else "transaction_amount",
                "operator": ">=", "value_from": amount,
                "target_scope": "AGGREGATE" if aggregated else "DETAIL", "provenance": "stated",
            })
        updated["thresholds"] = thresholds

    if "timeframe" in filled:
        if str(answers["timeframe"]) == "custom":
            first, last = _date_range(answers.get("date_range"))  # type: ignore[misc]
            updated["anchor_date"] = last.isoformat()
            updated["time_window"] = window_for_period(first, last)
        else:
            updated["time_window"] = {"unit": "DAYS", "value": int(answers["timeframe"]), "is_rolling": True,
                                      "provenance": "stated"}

    if "channels" in filled:
        values = answers["channels"] if isinstance(answers["channels"], list) else [answers["channels"]]
        values = [str(v).strip().upper() for v in values if str(v).strip()]
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
