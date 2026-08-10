"""
Deterministic markdown renderer for the Scenario Implementation Plan artifact.

Zero LLM involvement — every non-empty AMLIntent field is mapped to a fixed
markdown section so the frontend side panel always receives a structured,
predictable document regardless of which model produced the intent.
"""

import re
from typing import Optional


def _clean_explanation_checkpoint(checkpoint: Optional[str]) -> Optional[str]:
    """
    Strips interactive call-to-action prompts from the explanation codes checkpoint.

    Args:
        checkpoint (Optional[str]): Raw markdown text containing discovered explanation
            codes table and optional call-to-action text.

    Returns:
        Optional[str]: Cleaned markdown string stripped of interactive prompts,
            or None if input is empty or invalid.
    """
    if not checkpoint or not isinstance(checkpoint, str):
        return None

    cleaned_lines: list[str] = []
    # Pattern matching 'Action Needed:', 'Action Needed', 'confirm codes', etc.
    action_pattern = re.compile(
        r"(?:action\s+needed:|confirm\s+codes|reply\s+[\"'\`]?confirm)",
        re.IGNORECASE,
    )

    for line in checkpoint.splitlines():
        if action_pattern.search(line):
            continue
        cleaned_lines.append(line)

    result = "\n".join(cleaned_lines).rstrip()
    return result if result.strip() else None


def build_plan_markdown(intent: dict, explanation_code_checkpoint: Optional[str]) -> str:
    """Deterministic markdown renderer that mirrors every AMLIntent field into a plan.

    Builds the full Scenario Implementation Plan as a structured markdown document
    directly from the parsed intent dict — no LLM involved. This guarantees consistent
    structure regardless of which model is running and eliminates nonsense freeform output.

    Args:
        intent (dict): Parsed AMLIntent payload as a Python dict.
        explanation_code_checkpoint (Optional[str]): Raw markdown table string of discovered
            explanation codes. Rendered verbatim into the plan's explanation codes section.

    Returns:
        str: Full markdown plan string ready for frontend side panel rendering.
    """
    lines: list[str] = []

    # ─────────────────────────────────────────
    # STATUS BANNER
    # ─────────────────────────────────────────
    ready = intent.get("ready_for_handoff", True)
    has_clarifications = bool(intent.get("clarifications") or intent.get("clarification_questions"))
    if ready and not has_clarifications:
        lines.append("> ✅ **Status: Ready for Handoff** — All required parameters are captured. Approve this plan to proceed to shadow testing.")
    else:
        lines.append("> ⚠️ **Status: Clarification Required** — One or more parameters are ambiguous. Resolve the open questions below before proceeding.")
    lines.append("")

    # ─────────────────────────────────────────
    # SECTION 1 — SCENARIO OVERVIEW
    # ─────────────────────────────────────────
    lines.append("---")
    lines.append("")
    lines.append("## 1. Scenario Overview")
    lines.append("")
    lines.append("| Field | Value |")
    lines.append("|---|---|")
    lines.append(f"| **Scenario Name** | {intent.get('scenario_name', '—')} |")
    lines.append(f"| **Scenario Type** | `{intent.get('scenario_type', '—')}` |")
    tx_type = intent.get("transaction_type")
    if tx_type:
        lines.append(f"| **Transaction Type** | {tx_type} |")

    segs = intent.get("customer_segments")
    if segs:
        lines.append(f"| **Customer Segments** | {', '.join(f'`{s}`' for s in segs)} |")

    anchor = intent.get("anchor_date")
    lines.append(f"| **Evaluation Mode** | {'🕐 Shadow Test — Anchor: `' + anchor + '`' if anchor else '🟢 Production (SYSDATE)'} |")
    lines.append("")

    # ─────────────────────────────────────────
    # SECTION 2 — DETECTION LOGIC
    # ─────────────────────────────────────────
    lines.append("---")
    lines.append("")
    lines.append("## 2. Detection Logic")
    lines.append("")
    detection_logic = intent.get("detection_logic", "—")
    lines.append(f"> {detection_logic}")
    lines.append("")

    # ─────────────────────────────────────────
    # SECTION 3 — OBSERVATION WINDOW
    # ─────────────────────────────────────────
    lines.append("---")
    lines.append("")
    lines.append("## 3. Observation Window & Time Parameters")
    lines.append("")
    tw = intent.get("time_window")
    if tw:
        rolling_label = "Rolling (from SYSDATE)" if tw.get("is_rolling") else "Fixed Calendar Period"
        provenance_tw = tw.get("provenance", "stated")
        prov_badge = " *(assumed default)*" if provenance_tw == "assumed_default" else ""
        lines.append(f"- **Window Size:** `{tw.get('value')} {tw.get('unit')}`{prov_badge}")
        lines.append(f"- **Window Type:** {rolling_label}")
    else:
        lines.append("- **Window Size:** — *(not specified)*")
    lines.append("")

    bw = intent.get("baseline_window")
    if bw:
        lines.append("### Historical Baseline Window")
        lines.append("")
        lines.append("| Parameter | Value |")
        lines.append("|---|---|")
        lines.append(f"| **Duration** | `{bw.get('duration')} {bw.get('unit')}` |")
        lines.append(f"| **Isolates Current Window** | `{bw.get('exclude_current_window', True)}` |")
        lines.append(f"| **Offset Days** | `{bw.get('offset_days', '—')}` |")
        if bw.get("description"):
            lines.append(f"| **Description** | {bw.get('description')} |")
        if bw.get("sql_date_formula"):
            lines.append(f"| **SQL Formula** | `{bw.get('sql_date_formula')}` |")
        lines.append("")

    # ─────────────────────────────────────────
    # SECTION 4 — AGGREGATION PROFILE
    # ─────────────────────────────────────────
    lines.append("---")
    lines.append("")
    lines.append("## 4. Aggregation Profile")
    lines.append("")
    agg = intent.get("aggregation")
    if agg:
        prov_agg = agg.get("provenance", "stated")
        prov_badge = " *(assumed default)*" if prov_agg == "assumed_default" else ""
        lines.append(f"- **Metric:** `{agg.get('metric', '—')}`{prov_badge}")
        lines.append(f"- **Function:** `{agg.get('function', 'NONE')}`")
        lines.append(f"- **Grain:** `{agg.get('grain', '—')}`")
    else:
        lines.append("- **Aggregation:** — *(per-row, no grouping)*")
    lines.append("")

    # ─────────────────────────────────────────
    # SECTION 5 — THRESHOLDS & ALERT CONDITIONS
    # ─────────────────────────────────────────
    lines.append("---")
    lines.append("")
    lines.append("## 5. Thresholds & Alert Conditions")
    lines.append("")
    thresholds = intent.get("thresholds", [])
    if thresholds:
        detail_thresholds = [t for t in thresholds if t.get("target_scope") == "DETAIL"]
        aggregate_thresholds = [t for t in thresholds if t.get("target_scope") == "AGGREGATE"]

        if detail_thresholds:
            lines.append("### 5.1 Detail-Level Filters *(SQL: `WHERE` clause)*")
            lines.append("")
            lines.append("| # | Field | Operator | Value | Provenance |")
            lines.append("|---|---|---|---|---|")
            for i, t in enumerate(detail_thresholds, 1):
                val = f"`{t.get('value_from')}`"
                if t.get("operator") == "BETWEEN" and t.get("value_to") is not None:
                    val = f"`{t.get('value_from')}` — `{t.get('value_to')}`"
                prov = t.get("provenance", "stated")
                prov_label = "✅ Stated" if prov == "stated" else "⚙️ Assumed" if prov == "assumed_default" else "❓ Needs User"
                lines.append(f"| {i} | `{t.get('field')}` | `{t.get('operator')}` | {val} | {prov_label} |")
            lines.append("")

        if aggregate_thresholds:
            lines.append("### 5.2 Aggregate-Level Conditions *(SQL: `HAVING` clause)*")
            lines.append("")
            lines.append("| # | Field | Operator | Value | Provenance |")
            lines.append("|---|---|---|---|---|")
            for i, t in enumerate(aggregate_thresholds, 1):
                val = f"`{t.get('value_from')}`"
                if t.get("operator") == "BETWEEN" and t.get("value_to") is not None:
                    val = f"`{t.get('value_from')}` — `{t.get('value_to')}`"
                prov = t.get("provenance", "stated")
                prov_label = "✅ Stated" if prov == "stated" else "⚙️ Assumed" if prov == "assumed_default" else "❓ Needs User"
                lines.append(f"| {i} | `{t.get('field')}` | `{t.get('operator')}` | {val} | {prov_label} |")
            lines.append("")
    else:
        lines.append("*No numeric thresholds extracted.*")
        lines.append("")

    # ─────────────────────────────────────────
    # SECTION 6 — SEMANTIC CONDITIONS
    # ─────────────────────────────────────────
    semantic_conditions = intent.get("semantic_conditions", [])
    if semantic_conditions:
        lines.append("---")
        lines.append("")
        lines.append("## 6. Behavioral & Semantic Rules")
        lines.append("")
        lines.append("*Non-numeric rules, state transitions, and behavioral patterns captured from your description.*")
        lines.append("")
        lines.append("| # | Type | Subject | Business Rule |")
        lines.append("|---|---|---|---|")
        for i, sc in enumerate(semantic_conditions, 1):
            type_badge = {
                "STATE": "🔵 State",
                "TRANSITION": "🔄 Transition",
                "SEQUENCE": "📋 Sequence",
                "BEHAVIORAL": "🧠 Behavioral",
                "TEMPORAL": "⏱️ Temporal",
                "OTHER": "📌 Other",
            }.get(sc.get("logical_type", "OTHER"), "📌 Other")
            lines.append(f"| {i} | {type_badge} | `{sc.get('subject', '—')}` | {sc.get('predicate', '—')} |")
        lines.append("")

    # ─────────────────────────────────────────
    # SECTION 7 — EXCLUSIONS
    # ─────────────────────────────────────────
    exclusions = intent.get("exclusions")
    if exclusions:
        lines.append("---")
        lines.append("")
        lines.append("## 7. Exclusion Rules")
        lines.append("")
        for exc in exclusions:
            lines.append(f"- 🚫 {exc}")
        lines.append("")

    # ─────────────────────────────────────────
    # SECTION 8 — EXPLANATION CODES
    # ─────────────────────────────────────────
    expl_codes = intent.get("explanation_codes")
    cleaned_checkpoint = _clean_explanation_checkpoint(explanation_code_checkpoint)
    if expl_codes or cleaned_checkpoint:
        lines.append("---")
        lines.append("")
        lines.append("## 8. Explanation Codes")
        lines.append("")
        if expl_codes:
            lines.append(f"**Selected Codes:** {', '.join(f'`{c}`' for c in expl_codes)}")
            lines.append("")
        if cleaned_checkpoint:
            lines.append("**Discovered Codes (from DWH catalog):**")
            lines.append("")
            lines.append(cleaned_checkpoint)
            lines.append("")

    # ─────────────────────────────────────────
    # SECTION 9 — APPLIED DEFAULTS
    # ─────────────────────────────────────────
    applied_defaults = intent.get("applied_defaults", [])
    if applied_defaults:
        lines.append("---")
        lines.append("")
        lines.append("## 9. Applied Defaults")
        lines.append("")
        lines.append("*The following parameters were not explicitly stated and were inferred by the system. Please confirm or correct them.*")
        lines.append("")
        for d in applied_defaults:
            lines.append(f"- ⚙️ {d}")
        lines.append("")

    # ─────────────────────────────────────────
    # SECTION 10 — OPEN CLARIFICATIONS
    # ─────────────────────────────────────────
    clarifications = intent.get("clarifications", [])
    cq = intent.get("clarification_questions", [])
    if clarifications or cq:
        lines.append("---")
        lines.append("")
        lines.append("## 10. ⚠️ Open Clarifications Required")
        lines.append("")
        lines.append("*The following questions must be answered before this scenario can be built into SQL.*")
        lines.append("")
        if clarifications:
            for i, c in enumerate(clarifications, 1):
                lines.append(f"**{i}. {c.get('dimension', 'Ambiguity')}**")
                lines.append(f"   - ❓ *{c.get('question', '')}*")
                lines.append(f"   - 📌 Why it matters: {c.get('why_it_matters', '')}")
                opts = c.get("options")
                if opts:
                    lines.append(f"   - Options: {', '.join(f'`{o}`' for o in opts)}")
                lines.append("")
        elif cq:
            for i, q in enumerate(cq, 1):
                lines.append(f"{i}. ❓ {q}")
            lines.append("")

    # ─────────────────────────────────────────
    # SECTION 11 — SHADOW TESTING SCOPE
    # ─────────────────────────────────────────
    lines.append("---")
    lines.append("")
    lines.append("## 11. Shadow Testing Scope")
    lines.append("")
    if anchor:
        lines.append(f"- **Mode:** 🕐 Historical Shadow Test")
        lines.append(f"- **Anchor Date:** `{anchor}`")
        lines.append(f"- **SQL Temporal Anchor:** `DATE '{anchor}'` *(replaces `TRUNC(SYSDATE)`)*")
    else:
        lines.append("- **Mode:** 🟢 Production (live data, `TRUNC(SYSDATE)`)")
    exp_min = intent.get("expected_alert_range_min")
    exp_max = intent.get("expected_alert_range_max")
    if exp_min is not None and exp_max is not None:
        lines.append(f"- **Expected Alert Range:** `{exp_min}` — `{exp_max}` alerts")
    lines.append("")

    return "\n".join(lines)
