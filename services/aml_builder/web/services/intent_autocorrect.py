"""
Automatic correction of an AMLIntent that the PioTech service refused as NOT_IMPLEMENTABLE.

When PioTech refuses a scenario because one of its conditions cannot be expressed on the data
(for example "without maintaining a balance", or an empty PEP flag), the shadow-test tool asks
this module for a corrected intent and tries again. The correction is mechanical and fully
reported — it never happens silently:

* the blocked condition is removed from ``semantic_conditions``, ``customer_segments`` and
  ``thresholds``, and its wording is removed from ``detection_logic`` so a persisted rule
  describes what its SQL actually checks;
* a balance-retention condition on a scenario that has both money in and money out is replaced
  by a money-out vs money-in comparison at an assumed percentage
  (``settings``-independent default :data:`DEFAULT_PASS_THROUGH_PERCENT`), the operational
  substitute PioTech itself recommends.

Every change is returned as a correction record with a plain-language warning for the officer.
The module is pure (no I/O) so it can be unit-tested without the service.
"""

import copy
import re
from typing import Any, Dict, Final, List, Optional, Tuple

DEFAULT_PASS_THROUGH_PERCENT: Final[float] = 90.0

_BALANCE_PHRASE: Final = re.compile(
    r"balance|retain(?:s|ing)?\s+funds|keep(?:s|ing)?\s+(?:the\s+)?(?:funds|money)|money\s+(?:does\s+not|doesn't)\s+stay",
    re.IGNORECASE,
)
_INCOMING_TYPE: Final = re.compile(r"deposit|inward|incoming|credit|receiv", re.IGNORECASE)
_OUTGOING_TYPE: Final = re.compile(r"withdraw|outward|outgoing|debit|transfer\s*out|payment", re.IGNORECASE)
_POPULATION_SUBJECT: Final = re.compile(r"customer|account|client|person", re.IGNORECASE)
# Wording that limits WHO a rule covers, whatever condition type the intent gave it.
_POPULATION_TERMS: Final = re.compile(
    r"politically|\bpeps?\b|high[- ]risk|risk level|individual|corporate|resident|nationalit|\baged?\b|segment|"
    r"watch ?list|sanction",
    re.IGNORECASE,
)


def auto_correct_intent(
    intent: Dict[str, Any], rejection: Dict[str, Any]
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Remove or replace the conditions PioTech refused, and describe every change.

    Args:
        intent (Dict[str, Any]): The AMLIntent that was refused (not modified).
        rejection (Dict[str, Any]): The rejection from ``piotech_report.build_rejection``
            with ``kind == "NOT_IMPLEMENTABLE"``.

    Returns:
        Tuple[Dict[str, Any], List[Dict[str, Any]]]: The corrected intent (a deep copy) and the
            list of corrections applied. Each correction has 'original' (the refused phrase),
            'action' ("removed" or "replaced"), 'replacement' (Optional[str]), 'reason' (PioTech's
            reason) and 'warning' (text for the officer). An empty list means nothing in the
            intent matched the refused conditions, so retrying would not help.
    """
    corrected = copy.deepcopy(intent)
    reasons = {_key(r.get("condition", "")): r.get("reason", "") for r in rejection.get("reasons") or []}
    blocked = [b for b in rejection.get("blocked_by") or [] if b] or [
        r.get("condition", "") for r in rejection.get("reasons") or [] if r.get("condition")
    ]

    corrections: List[Dict[str, Any]] = []
    for phrase in dict.fromkeys(blocked):
        removed = _remove_condition(corrected, phrase)
        if not removed:
            continue
        narrows_population = any(_narrows_population(item) for item in removed)
        reason = reasons.get(_key(phrase), "")

        replacement: Optional[str] = None
        if _BALANCE_PHRASE.search(phrase) and _has_in_and_out(corrected):
            replacement = _add_pass_through(corrected)

        _scrub_detection_logic(corrected, phrase, replacement)
        corrections.append(
            {
                "original": phrase,
                "action": "replaced" if replacement else "removed",
                "replacement": replacement,
                "reason": reason,
                "warning": _warning(phrase, replacement, narrows_population),
            }
        )

    if corrections:
        notes = list(corrected.get("auto_corrections") or [])
        notes += [{k: c[k] for k in ("original", "action", "replacement")} for c in corrections]
        corrected["auto_corrections"] = notes
    return corrected, corrections


def resolve_intent(
    intent: Dict[str, Any], rejection: Dict[str, Any]
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Correct an intent PioTech refused as UNRESOLVED or REQUEST_REJECTED, so it can be retried.

    UNRESOLVED (an ambiguous or contradictory request): every free-text semantic condition the
    refusal quotes is removed — the structured thresholds, window and types still carry the rule —
    and values marked as needing the user are treated as assumed defaults.

    REQUEST_REJECTED (the intent broke PioTech's contract): a time window of 0 or less becomes
    1 business day, and an invalid baseline window is dropped.

    Args:
        intent (Dict[str, Any]): The refused AMLIntent (not modified).
        rejection (Dict[str, Any]): The rejection from ``piotech_report``.

    Returns:
        Tuple[Dict[str, Any], List[Dict[str, Any]]]: The corrected intent (a deep copy) and the
            corrections applied, in the same record shape as :func:`auto_correct_intent`. An empty
            list means nothing could be changed, so retrying would not help.
    """
    corrected = copy.deepcopy(intent)
    corrections: List[Dict[str, Any]] = []
    reason = "; ".join(r.get("reason", "") for r in rejection.get("reasons") or [])[:300]

    def record(original: str, action: str, replacement: Optional[str] = None) -> None:
        corrections.append({
            "original": original, "action": action, "replacement": replacement, "reason": reason,
            "warning": f'"{original}" was automatically {action}'
                       + (f' with "{replacement}"' if replacement else "") + " to generate a valid operational query.",
        })

    if rejection.get("kind") == "UNRESOLVED":
        quoted = {_key(q) for item in rejection.get("blocked_by") or [] for q in re.findall(r'"([^"]+)"', str(item))}
        kept = []
        for cond in corrected.get("semantic_conditions") or []:
            phrase = cond.get("raw_phrase", "") if isinstance(cond, dict) else str(cond)
            if quoted and isinstance(cond, dict) and any(_matches(q, phrase, cond.get("predicate")) for q in quoted):
                record(phrase, "removed")
            else:
                kept.append(cond)
        if corrected.get("semantic_conditions") is not None:
            corrected["semantic_conditions"] = kept
        if _assume_user_values(corrected):
            record("values marked as needing your answer", "assumed")
        if corrected.get("clarifications") or corrected.get("clarification_needed"):
            corrected["clarifications"] = []
            corrected["clarification_needed"] = False
            corrected["ready_for_handoff"] = True
            record("open clarification questions", "assumed")

    elif rejection.get("kind") == "REQUEST_REJECTED":
        window = corrected.get("time_window")
        if isinstance(window, dict) and _number_or_zero(window.get("value")) <= 0:
            window["value"], window["unit"], window["provenance"] = 1, "DAYS", "assumed_default"
            record("a time window of 0", "replaced", "1 business day")
        baseline = corrected.get("baseline_window")
        if isinstance(baseline, dict) and _number_or_zero(baseline.get("duration")) <= 0:
            corrected["baseline_window"] = None
            record("an empty baseline window", "removed")

    if corrections:
        notes = list(corrected.get("auto_corrections") or [])
        notes += [{k: c[k] for k in ("original", "action", "replacement")} for c in corrections]
        corrected["auto_corrections"] = notes
    return corrected, corrections


# ─────────────────────────────── helpers ────────────────────────────────


def _number_or_zero(value: Any) -> float:
    """Parse a number, treating anything unparsable as 0.

    Args:
        value (Any): The value.

    Returns:
        float: The number, or 0.0.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _assume_user_values(intent: Dict[str, Any]) -> bool:
    """Turn every ``provenance: needs_user`` into ``assumed_default`` (modified in place).

    Args:
        intent (Dict[str, Any]): The intent being corrected.

    Returns:
        bool: Whether anything was changed.
    """
    changed = False
    items: List[Any] = list(intent.get("thresholds") or []) + list(intent.get("semantic_conditions") or [])
    items += [intent.get(k) for k in ("time_window", "baseline_window", "aggregation")]
    for item in items:
        if isinstance(item, dict) and item.get("provenance") == "needs_user":
            item["provenance"] = "assumed_default"
            changed = True
    return changed


def _key(text: str) -> str:
    """Normalize a phrase for comparison (case, quotes and spacing).

    Args:
        text (str): The phrase.

    Returns:
        str: Lower-case phrase without quotes and with single spaces.
    """
    return " ".join(str(text).lower().replace('"', "").replace("'", "").split())


def _matches(phrase: str, *candidates: Any) -> bool:
    """True when the refused phrase and any candidate text name the same condition.

    Args:
        phrase (str): The refused condition as PioTech named it.
        *candidates (Any): Texts from the intent (raw phrase, predicate, segment, field).

    Returns:
        bool: Whether one contains the other after normalization.
    """
    target = _key(phrase)
    for candidate in candidates:
        text = _key(candidate) if candidate else ""
        if text and (text == target or text in target or target in text):
            return True
    return False


def _remove_condition(intent: Dict[str, Any], phrase: str) -> List[Dict[str, Any]]:
    """Remove every intent element that states the refused condition.

    Args:
        intent (Dict[str, Any]): The intent being corrected (modified in place).
        phrase (str): The refused condition.

    Returns:
        List[Dict[str, Any]]: The removed elements, tagged with their source field.
    """
    removed: List[Dict[str, Any]] = []

    kept = []
    for cond in intent.get("semantic_conditions") or []:
        if isinstance(cond, dict) and _matches(phrase, cond.get("raw_phrase"), cond.get("predicate")):
            removed.append({"source": "semantic_conditions", **cond})
        else:
            kept.append(cond)
    if intent.get("semantic_conditions") is not None:
        intent["semantic_conditions"] = kept

    segments = intent.get("customer_segments")
    if isinstance(segments, list):
        left = [s for s in segments if not _matches(phrase, s)]
        removed += [{"source": "customer_segments", "subject": "customer", "raw_phrase": s} for s in segments if s not in left]
        intent["customer_segments"] = left or None

    kept_thresholds = []
    for th in intent.get("thresholds") or []:
        label = f"{th.get('field')} {th.get('operator')} {th.get('value_from')}"
        if isinstance(th, dict) and _matches(phrase, label, th.get("field")):
            removed.append({"source": "thresholds", **th})
        else:
            kept_thresholds.append(th)
    if intent.get("thresholds") is not None:
        intent["thresholds"] = kept_thresholds
    return removed


def _narrows_population(item: Dict[str, Any]) -> bool:
    """True when the removed element limited WHO the rule applies to (segment, PEP, risk level).

    Args:
        item (Dict[str, Any]): A removed intent element.

    Returns:
        bool: Whether removing it makes the rule apply to a wider population.
    """
    if item.get("source") == "customer_segments":
        return True
    if _POPULATION_TERMS.search(f"{item.get('raw_phrase', '')} {item.get('predicate', '')}"):
        return True
    return (
        item.get("source") == "semantic_conditions"
        and str(item.get("logical_type", "")).upper() == "STATE"
        and bool(_POPULATION_SUBJECT.search(str(item.get("subject", ""))))
        and not _BALANCE_PHRASE.search(str(item.get("raw_phrase", "")))
    )


def _types(intent: Dict[str, Any]) -> List[str]:
    """All transaction-type names the intent mentions.

    Args:
        intent (Dict[str, Any]): The intent.

    Returns:
        List[str]: Type names.
    """
    names = list(intent.get("transaction_types") or [])
    if intent.get("transaction_type"):
        names.append(str(intent["transaction_type"]))
    names += [str(th.get("transaction_type")) for th in intent.get("thresholds") or [] if th.get("transaction_type")]
    return names


def _has_in_and_out(intent: Dict[str, Any]) -> bool:
    """True when the scenario has both an incoming and an outgoing transaction type.

    Args:
        intent (Dict[str, Any]): The intent.

    Returns:
        bool: Whether a money-out vs money-in comparison is meaningful.
    """
    names = " ".join(_types(intent))
    return bool(_INCOMING_TYPE.search(names)) and bool(_OUTGOING_TYPE.search(names))


def _add_pass_through(intent: Dict[str, Any]) -> str:
    """Add a money-out vs money-in ratio threshold in the form PioTech recognises.

    Args:
        intent (Dict[str, Any]): The intent being corrected (modified in place).

    Returns:
        str: The replacement condition in words.
    """
    names = _types(intent)
    outgoing = next((n for n in names if _OUTGOING_TYPE.search(n)), "withdrawals")
    incoming = next((n for n in names if _INCOMING_TYPE.search(n)), "deposits")
    out_word = "withdrawals" if re.search(r"withdraw", outgoing, re.I) else "outgoing_transfers"
    in_word = "deposits" if re.search(r"deposit", incoming, re.I) else "incoming_funds"
    pct = DEFAULT_PASS_THROUGH_PERCENT
    thresholds = intent.setdefault("thresholds", []) or []
    thresholds.append(
        {
            "field": f"total_{out_word}_to_total_{in_word}_ratio",
            "operator": ">=",
            "value_from": pct / 100,
            "value_to": None,
            "provenance": "assumed_default",
            "target_scope": "AGGREGATE",
            "transaction_type": None,
            "explanation_codes": None,
        }
    )
    intent["thresholds"] = thresholds
    return f"total {out_word.replace('_', ' ')} at least {pct:g}% of total {in_word.replace('_', ' ')}"


def _scrub_detection_logic(intent: Dict[str, Any], phrase: str, replacement: Optional[str]) -> None:
    """Remove (or replace) the refused wording in ``detection_logic`` so it matches the SQL.

    Args:
        intent (Dict[str, Any]): The intent being corrected (modified in place).
        phrase (str): The refused condition.
        replacement (Optional[str]): The substitute wording, if any.
    """
    logic = intent.get("detection_logic")
    if not isinstance(logic, str) or not logic:
        return
    clean = phrase.strip('" ')
    pattern = re.compile(re.escape(clean), re.IGNORECASE)
    if replacement:
        # "... velocity without maintaining a balance" -> "... velocity, where total withdrawals are at least ..."
        inline = ", where " + replacement.replace(" at least ", " are at least ", 1)
        with_lead = re.compile(r"[\s,;]*" + re.escape(clean), re.IGNORECASE)   # absorb ", " before the phrase
        logic = with_lead.sub(inline, logic) if pattern.search(logic) else f"{logic.rstrip('. ')}{inline}."
    elif pattern.search(logic):
        # Keep the sentence readable: "Identify politically exposed customers whose ..." -> "Identify customers whose ..."
        head = re.search(r"(customers|accounts|clients|persons)$", clean, re.IGNORECASE)
        logic = pattern.sub(head.group(1) if head else "", logic)
    logic = re.sub(r"\s{2,}", " ", re.sub(r"\s+([,.;])", r"\1", logic)).strip(" ,;")
    intent["detection_logic"] = logic[:1].upper() + logic[1:]


def _warning(phrase: str, replacement: Optional[str], narrows_population: bool) -> str:
    """Build the officer-facing warning for one correction.

    Args:
        phrase (str): The refused condition.
        replacement (Optional[str]): The substitute, if one was used.
        narrows_population (bool): Whether the removed condition limited who the rule covers.

    Returns:
        str: The warning text.
    """
    if replacement:
        text = (
            f'The unsupported phrase "{phrase}" was automatically replaced with "{replacement}" to generate a valid '
            f"operational query. The {DEFAULT_PASS_THROUGH_PERCENT:g}% level is an assumed default, not a value you stated."
        )
    else:
        text = f'The unsupported phrase "{phrase}" was automatically bypassed to generate a valid operational query.'
    if narrows_population:
        text += (
            f' The rule no longer checks "{phrase}": it now applies to every customer or account that meets the '
            "remaining conditions, so it will flag a wider population than you asked for."
        )
    return text
