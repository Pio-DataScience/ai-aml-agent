"""
Rejection passthrough for the PioTech AI DWH Scenario Agent.

When the PioTech service cannot (or may not) generate SQL for a scenario, it streams a
``scenario_report`` event followed by an ``error`` event. The report names the condition
that blocked the scenario, the reason, and often how to get past it. Before this module the
AML Builder discarded both and returned a generic "did not return a valid SQL query", which
left the agent to guess a cause.

This module turns those two events into one structured rejection with the three things an
officer needs — what blocked the scenario, why, and what to do — plus a markdown rendering
for the UI side panel. It is pure (no I/O) so it can be unit-tested without the service.

Event shapes consumed (PioTech ``services/dwh_scenario_agent``):
    scenario_report: {"type": "scenario_report",
                      "implemented": [str],
                      "not_implemented": [{"condition": str, "reason": str}],
                      "warnings": [str],
                      "blocked_by": str,                  # optional: the single blocker
                      "implementable_without": [str]}     # optional: what the rest implements
    error:           {"type": "error", "message": "NOT_IMPLEMENTABLE: ..." | "UNRESOLVED: a | b" | str}
"""

import re
from typing import Any, Dict, Final, List, Literal, Optional

RejectionKind = Literal[
    "NOT_IMPLEMENTABLE",
    "UNRESOLVED",
    "VALIDATION_FAILED",
    "NO_SQL",
    "SERVICE_UNAVAILABLE",
]

_KIND_TITLES: Final[Dict[str, str]] = {
    "NOT_IMPLEMENTABLE": "A condition cannot be implemented on the available data",
    "UNRESOLVED": "The scenario needs your answer before SQL can be generated",
    "VALIDATION_FAILED": "The generated SQL did not pass validation",
    "NO_SQL": "The SQL service returned no SQL",
    "SERVICE_UNAVAILABLE": "The SQL service could not be reached",
}

# Sentences in a PioTech reason or warning that tell the officer what to do instead.
_ADVICE_PATTERN: Final = re.compile(
    r"\b(instead|for example|state a|to add one|rephrase|send the intent again|ask the data owner)\b",
    re.IGNORECASE,
)
# The PioTech warning that restates the single blocker; rendered by us as a suggestion instead.
_SINGLE_BLOCKER_WARNING: Final = re.compile(r'^Only ".+" blocks this scenario', re.IGNORECASE)
_SENTENCE_SPLIT: Final = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"(])")


def normalize_scenario_report(event: Dict[str, Any]) -> Dict[str, Any]:
    """Coerce a PioTech ``scenario_report`` event into a predictable shape.

    Malformed members are dropped rather than raised on, so a bad report can never hide
    the SQL or the error that came with it.

    Args:
        event (Dict[str, Any]): The decoded ``scenario_report`` SSE event.

    Returns:
        Dict[str, Any]: Keys 'implemented' (List[str]), 'not_implemented'
            (List[{'condition': str, 'reason': str}]), 'warnings' (List[str]),
            'blocked_by' (Optional[str]) and 'implementable_without' (List[str]).
    """
    not_implemented: List[Dict[str, str]] = []
    raw_not_implemented = event.get("not_implemented")
    for item in raw_not_implemented if isinstance(raw_not_implemented, list) else []:
        if isinstance(item, dict):
            not_implemented.append(
                {
                    "condition": str(item.get("condition") or "").strip(),
                    "reason": str(item.get("reason") or "").strip(),
                }
            )
        elif item:
            not_implemented.append({"condition": str(item).strip(), "reason": ""})

    blocked_by = event.get("blocked_by")
    return {
        "implemented": _str_list(event.get("implemented")),
        "not_implemented": not_implemented,
        "warnings": _str_list(event.get("warnings")),
        "blocked_by": str(blocked_by).strip() if blocked_by else None,
        "implementable_without": _str_list(event.get("implementable_without")),
    }


def build_rejection(
    upstream_error: Optional[str],
    scenario_report: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Combine the PioTech error message and report into one structured rejection.

    Args:
        upstream_error (Optional[str]): The ``message`` of the PioTech ``error`` event, if any.
        scenario_report (Optional[Dict[str, Any]]): The report, already passed through
            :func:`normalize_scenario_report`, if one was sent.

    Returns:
        Dict[str, Any]: Keys 'kind' (RejectionKind), 'title' (str), 'blocked_by' (List[str]),
            'reasons' (List[{'condition': str, 'reason': str}]), 'suggestions' (List[str]),
            'implementable_without' (List[str]), 'warnings' (List[str]) and 'message' (str,
            the upstream error verbatim, or "" if none).
    """
    report = scenario_report or normalize_scenario_report({})
    message = (upstream_error or "").strip()
    kind = _classify(message, report)

    reasons = [dict(entry) for entry in report["not_implemented"] if entry["condition"] or entry["reason"]]
    unresolved = _unresolved_items(message) if kind == "UNRESOLVED" else []

    if report["blocked_by"]:
        blocked_by = [report["blocked_by"]]
    elif unresolved:
        blocked_by = unresolved
    else:
        blocked_by = [entry["condition"] for entry in reasons if entry["condition"]]

    if not reasons:
        if unresolved:
            reasons = [{"condition": item, "reason": "The intent leaves this open."} for item in unresolved]
        elif message:
            reasons = [{"condition": "the scenario as a whole", "reason": _strip_kind_prefix(message)}]

    warnings = [
        w
        for w in report["warnings"]
        if not _SINGLE_BLOCKER_WARNING.match(w) and not w.lower().startswith("unresolved:")
    ]

    return {
        "kind": kind,
        "title": _KIND_TITLES[kind],
        "blocked_by": blocked_by,
        "reasons": reasons,
        "suggestions": _suggestions(kind, report, reasons),
        "implementable_without": list(report["implementable_without"]),
        "warnings": warnings,
        "message": message,
    }


def build_unavailable_rejection(url: str, detail: str) -> Dict[str, Any]:
    """Build the rejection for a PioTech service that could not be reached at all.

    Args:
        url (str): The PioTech endpoint that was called (``settings.PIOTECH_AI_URL``).
        detail (str): The transport error text, e.g. ``[WinError 10061] ...``.

    Returns:
        Dict[str, Any]: A rejection in the same shape as :func:`build_rejection`, kind
            ``SERVICE_UNAVAILABLE``. The scenario itself was never evaluated.
    """
    return {
        "kind": "SERVICE_UNAVAILABLE",
        "title": _KIND_TITLES["SERVICE_UNAVAILABLE"],
        "blocked_by": ["the PioTech DWH scenario service"],
        "reasons": [
            {
                "condition": "the PioTech DWH scenario service",
                "reason": f"No response from {url}: {detail}. The scenario itself was not evaluated.",
            }
        ],
        "suggestions": [
            "Start the PioTech DWH scenario service (services.dwh_scenario_agent, port 8006) "
            "and run the shadow test again. Nothing needs to change in the scenario."
        ],
        "implementable_without": [],
        "warnings": [],
        "message": detail,
    }


def render_rejection_markdown(rejection: Dict[str, Any]) -> str:
    """Render a rejection as markdown for the UI side panel.

    Args:
        rejection (Dict[str, Any]): Output of :func:`build_rejection` or
            :func:`build_unavailable_rejection`.

    Returns:
        str: Markdown with the sections Blocked by, Reason, Suggestion, and — when
            present — What can still be generated and Warnings.
    """
    lines = [
        "## Scenario not generated",
        "",
        f"**{rejection['title']}** (`{rejection['kind']}`)",
        "",
        "### Blocked by",
    ]
    lines += [f"- {item}" for item in rejection["blocked_by"]] or ["- (not stated by the SQL service)"]

    lines += ["", "### Reason"]
    if rejection["reasons"]:
        for entry in rejection["reasons"]:
            condition = entry.get("condition") or ""
            reason = entry.get("reason") or "(no reason given)"
            lines.append(f"- **{condition}**: {reason}" if condition else f"- {reason}")
    else:
        lines.append("- (no reason given by the SQL service)")

    lines += ["", "### Suggestion"]
    lines += [f"- {item}" for item in rejection["suggestions"]]

    if rejection["implementable_without"]:
        lines += ["", "### What can still be generated without it"]
        lines += [f"- {item}" for item in rejection["implementable_without"]]

    if rejection["warnings"]:
        lines += ["", "### Warnings"]
        lines += [f"- {item}" for item in rejection["warnings"]]

    return "\n".join(lines)


def summarize_rejection(rejection: Dict[str, Any]) -> str:
    """Build a one-line summary of a rejection for the tool's ``error`` field and logs.

    Args:
        rejection (Dict[str, Any]): Output of :func:`build_rejection` or
            :func:`build_unavailable_rejection`.

    Returns:
        str: E.g. ``NOT_IMPLEMENTABLE — blocked by "without maintaining a balance"``.
    """
    blockers = ", ".join(f'"{item}"' for item in rejection["blocked_by"])
    return f"{rejection['kind']} — blocked by {blockers}" if blockers else rejection["kind"]


# ─────────────────────────────── helpers ────────────────────────────────


def _str_list(value: Any) -> List[str]:
    """Return the non-empty string members of a list, or [] for anything else.

    Args:
        value (Any): A value expected to be a list of strings.

    Returns:
        List[str]: The stripped, non-empty members as strings.
    """
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if item is not None and str(item).strip()]


def _classify(message: str, report: Dict[str, Any]) -> RejectionKind:
    """Classify a failed PioTech request from its error message and report.

    Args:
        message (str): The upstream error message ("" if none was sent).
        report (Dict[str, Any]): The normalized scenario report.

    Returns:
        RejectionKind: The rejection kind.
    """
    upper = message.upper()
    if upper.startswith("UNRESOLVED"):
        return "UNRESOLVED"
    if upper.startswith("NOT_IMPLEMENTABLE"):
        return "NOT_IMPLEMENTABLE"
    if message or report["not_implemented"]:
        return "VALIDATION_FAILED"
    return "NO_SQL"


def _strip_kind_prefix(message: str) -> str:
    """Remove a leading ``NOT_IMPLEMENTABLE:`` / ``UNRESOLVED:`` tag from a message.

    Args:
        message (str): The upstream error message.

    Returns:
        str: The message without its kind prefix.
    """
    return re.sub(r"^(NOT_IMPLEMENTABLE|UNRESOLVED)\s*:\s*", "", message, flags=re.IGNORECASE).strip()


def _unresolved_items(message: str) -> List[str]:
    """Split an ``UNRESOLVED: a | b`` message into its open items.

    Args:
        message (str): The upstream error message.

    Returns:
        List[str]: The open items, in order.
    """
    body = _strip_kind_prefix(message)
    return [part.strip() for part in body.split("|") if part.strip()]


def _suggestions(
    kind: RejectionKind,
    report: Dict[str, Any],
    reasons: List[Dict[str, str]],
) -> List[str]:
    """Derive what the officer can do next, using only what PioTech said.

    The service's own advice sentences are kept verbatim; a generic fallback is added only
    when the service gave none.

    Args:
        kind (RejectionKind): The rejection kind.
        report (Dict[str, Any]): The normalized scenario report.
        reasons (List[Dict[str, str]]): The condition/reason pairs.

    Returns:
        List[str]: Suggestions, most specific first, without duplicates.
    """
    out: List[str] = []

    if report["blocked_by"] and report["implementable_without"]:
        out.append(
            f'Only "{report["blocked_by"]}" blocks this scenario. If leaving it out is acceptable, '
            "send the scenario again without it; everything listed under "
            '"What can still be generated without it" would then be implemented. '
            "Leaving a condition out is your decision; no reduced SQL was generated."
        )

    # PioTech's own single-blocker warning is restated by the first suggestion above.
    advice_sources = [entry["reason"] for entry in reasons] + [
        w for w in report["warnings"] if not _SINGLE_BLOCKER_WARNING.match(w)
    ]
    for text in advice_sources:
        for sentence in _SENTENCE_SPLIT.split(text):
            sentence = sentence.strip()
            if _SINGLE_BLOCKER_WARNING.match(sentence):
                continue
            if _ADVICE_PATTERN.search(sentence) and sentence not in out:
                out.append(sentence)

    if kind == "UNRESOLVED":
        out.append("Answer the open point(s) listed under \"Blocked by\", then run the shadow test again.")

    if not out:
        if kind == "NOT_IMPLEMENTABLE":
            out.append(
                "The data cannot support this condition as written. Remove or rephrase it, "
                "or ask the data owner to make the needed data available."
            )
        else:
            out.append(
                "Rephrase the scenario and try again. If it keeps failing, share the reason above "
                "with the PioTech team."
            )
    return out
