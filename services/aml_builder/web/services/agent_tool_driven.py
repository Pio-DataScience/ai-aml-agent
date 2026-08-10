"""
Tool-Driven Agentic Architecture (Zero Routing Logic)

Each pipeline capability is exposed as an autonomous @tool with rich docstrings.
The central LLM receives a goal-oriented system prompt and decides tool invocation
ordering, parameter passing, and user interaction autonomously.

Architecture decisions:
- SQL generation is delegated to the PioTech AI text-to-SQL SSE service (same
  production service used by the classic agent.py sql_bridge_node).
- Persistence writes exclusively to PIO_AML_PRODUCTION_SCENARIOS via the
  production_registry module — NOT the legacy QB engine tables.
- The LangGraph ReAct graph is compiled with interrupt_after on the plan tool
  to enforce a hard graph-level approval gate between plan generation and
  shadow testing.
"""

import json
import logging
import re
import uuid
import httpx
from datetime import datetime
from typing import Any, Dict, List, Optional

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

from web.services.settings import settings
from web.services.schemas import AMLIntent
from web.services.agent import _load_prompt, _extract_sql
from web.services.explanation_code_search import (
    select_relevant_explanation_codes,
    format_explanation_code_checkpoint,
)
from web.services.oracle import run_readonly
from web.services.production_registry import save_production_scenario

logger = logging.getLogger(__name__)


def _build_llm() -> ChatOpenAI:
    """Instantiate a ChatOpenAI-compatible LLM from settings.

    Supports two providers controlled by LLM_PROVIDER env var:
    - 'openai'   : standard OpenAI endpoint (requires OPENAI_API_KEY).
    - 'lmstudio' : local LM Studio server at LLM_BASE_URL (e.g. http://127.0.0.1:1234/v1).
                   Uses a dummy api_key value since LM Studio does not enforce authentication.

    Returns:
        ChatOpenAI: Configured LLM instance.
    """
    kwargs: Dict[str, Any] = {
        "model": settings.LLM_MODEL,
        "temperature": settings.LLM_TEMPERATURE,
    }
    if settings.LLM_PROVIDER == "lmstudio":
        kwargs["base_url"] = settings.LLM_BASE_URL or "http://127.0.0.1:1234/v1"
        kwargs["api_key"] = (
            "lm-studio"  # LM Studio ignores the key but langchain requires it
        )
    else:
        kwargs["api_key"] = settings.OPENAI_API_KEY
    return ChatOpenAI(**kwargs)


def _safe_parse_json(text: str) -> dict:
    """Parse JSON string with fallback repairs for common LLM formatting flaws."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)

    # 1. Direct parse attempt
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 2. Extract substring between first '{' and last '}'
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        extracted = match.group(0)
        try:
            return json.loads(extracted)
        except json.JSONDecodeError:
            # Repair trailing commas before closing braces/brackets
            repaired = re.sub(r",\s*([\}\]])", r"\1", extracted)
            return json.loads(repaired)

    raise ValueError(f"Could not parse valid JSON from text: {text[:100]}...")


# =============================================================================
# 1. TOOL DEFINITIONS (Zero Routing Logic — Autonomous Capabilities)
# =============================================================================


@tool
def analyze_intent_and_discover_explanation_codes(
    user_prompt: str,
    existing_intent_json: Optional[str] = None,
) -> str:
    """Analyze the user's compliance scenario prompt, extract structured scenario parameters (AMLIntent),

    and run vector similarity search against the domain explanation codes catalog (top 70% relative cutoff).

    Args:
        user_prompt (str): Plain English scenario description or modification request from user.
        existing_intent_json (Optional[str]): Serialized JSON string of prior AMLIntent payload if refining.

    Returns:
        str: JSON object containing 'enriched_intent' dict and 'explanation_code_checkpoint' markdown table.
    """
    logger.info(
        "[TOOL: INTENT] Extracting scenario parameters and running vector search..."
    )
    llm = _build_llm()

    system_instruction = (
        "You are a Lead AML Domain Architect & Natural Language Intent Engineer for an Enterprise Financial Crime Platform.\n"
        "Your sole responsibility is to translate business scenario descriptions into a mathematically rigorous, disambiguated Intent Contract JSON.\n\n"
        "Output ONLY a valid JSON object matching AMLIntent schema with mandatory root keys:\n"
        "- scenario_name (str): descriptive title\n"
        "- scenario_type (str): 'CUSTOMER', 'TRANSACTION', or 'ACCOUNT'\n"
        "- transaction_type (str or null): explicit transaction type name e.g. 'CASH DEPOSIT'\n"
        "- detection_logic (str): plain English summary of business logic\n"
        "- thresholds (list of dicts with: field, operator, value_from, target_scope)\n"
        "- time_window (dict with: unit, value, is_rolling)\n"
        "- aggregation (dict with: metric, function, grain)\n"
        "- customer_segments (list of str or null): e.g. ['CORPORATE'], ['RETAIL'], or null\n"
        "- exclusions (list of str or null): e.g. ['Exclude payroll', 'Exclude employee accounts'], or null\n"
        "- semantic_conditions (list of dicts with: raw_phrase, logical_type, subject, predicate)\n"
        "- anchor_date (str or null): YYYY-MM-DD date or null\n"
        "- explanation_codes (list of str or null)\n\n"
        "[CORE EXTRACTION LAWS]\n\n"
        "1. MANDATORY SCOPE CLASSIFICATION (target_scope):\n"
        "   Every threshold in the 'thresholds' array MUST explicitly set 'target_scope' to either 'DETAIL' or 'AGGREGATE':\n"
        "   - 'DETAIL': Applies to individual transaction records before aggregation (placed in SQL WHERE clause). Example: 'each deposit > 9,000' or 'transaction_amount > 50,000'.\n"
        "   - 'AGGREGATE': Applies to summed, averaged, or counted metrics across a group/window (placed in SQL HAVING clause). Example: 'total monthly volume > 100,000' or 'cumulative_transaction_volume > 10,000'.\n\n"
        "2. GRAIN & SCENARIO TYPE ALIGNMENT:\n"
        "   - 'aggregation' MUST be a dictionary containing 'metric' (e.g. 'transaction_amount' or 'cumulative_transaction_volume'), 'function' ('SUM', 'COUNT', 'AVG', or 'NONE'), and 'grain' (e.g. 'PER CUSTOMER PER DAY').\n"
        "   - If the scenario aggregates activity (SUM, COUNT, AVG) over time to flag an entity, 'scenario_type' MUST be 'CUSTOMER' or 'ACCOUNT' (NEVER 'TRANSACTION').\n"
        "   - 'aggregation.grain' MUST explicitly state the grouping boundary (e.g., 'PER CUSTOMER PER DAY', 'PER CUSTOMER PER ROLLING 7 DAYS').\n"
        "   - NEVER set 'aggregation.grain' to 'TRANSACTION' if a 'SUM', 'COUNT', or 'AVG' function is specified.\n\n"
        "3. DUAL-WINDOW & THRESHOLD FIELD NAMING:\n"
        "   - 'each transaction > X': threshold field = 'transaction_amount', target_scope = 'DETAIL'.\n"
        "   - 'total/cumulative volume > X': threshold field = 'cumulative_transaction_volume', target_scope = 'AGGREGATE'.\n"
        "   - NEGATIVE CONSTRAINT: For cumulative sum/volume scenarios, output ONLY ONE threshold object with target_scope: 'AGGREGATE'. NEVER output a duplicate threshold with target_scope: 'DETAIL' for the same amount!\n\n"
        "4. 'BETWEEN' OPERATOR LOWER & UPPER BOUNDS:\n"
        "   - When operator is 'BETWEEN', you MUST populate BOTH 'value_from' (lower bound float) AND 'value_to' (upper bound float). Example: 'between 8,000 and 9,999' -> value_from: 8000.0, value_to: 9999.0.\n\n"
        "5. ALL PERCENTAGE & RATIO RULES MUST BE THRESHOLDS:\n"
        "   - If the detection logic mentions a percentage or ratio (e.g. 'sends back 90%', '300% of average'), you MUST emit a corresponding entry in the 'thresholds' array with target_scope set to 'AGGREGATE' and convert percentage strings into clean floats in 'value_from' (e.g. '300% of average' -> value_from: 3.0; '90% of funds' -> value_from: 0.90).\n\n"
        "6. MANDATORY BASELINE_WINDOW EMISSION:\n"
        "   - Whenever a scenario mentions a historical baseline, prior average, or dormancy lookback (e.g. 'prior 180 days', '6-month baseline', 'dormant for 180 days'), you MUST populate the 'baseline_window' object (e.g. {'unit': 'DAYS', 'duration': 180, 'exclude_current_window': true, 'offset_days': 30}). Do not leave it null if historical data is referenced.\n\n"
        "7. EXHAUSTIVE THRESHOLD & EXCLUSION EXTRACTION:\n"
        "   - Do NOT drop secondary conditions! Extract all stated constraints into thresholds or semantic_conditions:\n"
        "     * Distinct counts ('distinct_branch_count >= 3', 'distinct_beneficiary_count >= 3') -> AGGREGATE threshold.\n"
        "     * Demographic/state filters ('customer_age < 25', 'risk_rating != LOW') -> DETAIL threshold or semantic_conditions.\n"
        "     * Customer segments ('customer_segments': ['CORPORATE'] or ['RETAIL']).\n"
        "     * Exclusions ('exclusions': ['Exclude payroll', 'Exclude employee accounts']).\n\n"
        "8. EXPLANATION CODES RESOLUTION:\n"
        "   - If the user selects, filters, or confirms specific explanation codes (e.g. 'first and second', 'use options 1 and 2', 'only code 660'), resolve them against Existing Intent Payload's discovered explanation codes and return the list of selected code strings in 'explanation_codes'.\n\n"
        "9. HISTORICAL TEST ANCHOR (anchor_date):\n"
        "   - If the user explicitly specifies a historical test date or snapshot date (e.g. 'test against 2024-01-04'), extract 'anchor_date': 'YYYY-MM-DD'. Otherwise set to null.\n\n"
        "10. MANDATORY CUSTOMER_SEGMENTS EXTRACTION:\n"
        "   - If the prompt references entity segments (e.g. 'corporate customers', 'retail accounts', 'individual clients'), you MUST extract them into 'customer_segments': ['CORPORATE'] or ['RETAIL']. Do not leave 'customer_segments' null if a segment is mentioned.\n\n"
        "11. ZERO LOSS OF ATOMIC CONCEPTS (semantic_conditions):\n"
        "   - EVERY micro-atomic concept, non-numeric rule, state transition ('dormant for 180 days then burst'), or complex behavioral rule ('returns 90% within 5 days') that cannot be a pure numeric threshold MUST be captured in 'semantic_conditions' as a dict with: 'raw_phrase', 'logical_type' ('STATE'|'TRANSITION'|'SEQUENCE'|'BEHAVIORAL'|'TEMPORAL'|'OTHER'), 'subject', and 'predicate'. ZERO USER CONCEPTS MAY BE OMITTED.\n\n"
        "Do not wrap in markdown fences."
    )
    if existing_intent_json:
        system_instruction += f"\n\nExisting Intent Payload:\n{existing_intent_json}\nPreserve existing stated fields."

    messages = [
        SystemMessage(content=system_instruction),
        HumanMessage(content=user_prompt),
    ]

    try:
        res = llm.invoke(messages)
        raw_text = str(res.content).strip() if res.content else ""
        if not raw_text and hasattr(res, "additional_kwargs") and res.additional_kwargs:
            raw_text = str(
                res.additional_kwargs.get("reasoning_content", "")
                or res.additional_kwargs.get("content", "")
            ).strip()

        if not raw_text:
            logger.error("[TOOL: INTENT] Sub-LLM returned empty output content.")
            return json.dumps(
                {"error": "Sub-LLM returned empty response during intent extraction."}
            )

        intent_dict = _safe_parse_json(raw_text)
        if "AMLIntent" in intent_dict and isinstance(intent_dict["AMLIntent"], dict):
            intent_dict = intent_dict["AMLIntent"]
        elif "aml_intent" in intent_dict and isinstance(
            intent_dict["aml_intent"], dict
        ):
            intent_dict = intent_dict["aml_intent"]
        elif "intent" in intent_dict and isinstance(intent_dict["intent"], dict):
            intent_dict = intent_dict["intent"]

        # Sanitize optional dict/model fields that might be serialized as "" or invalid empty types
        for dict_field in ("baseline_window", "aggregation", "time_window"):
            if dict_field in intent_dict and not isinstance(
                intent_dict[dict_field], dict
            ):
                intent_dict[dict_field] = None

        for map_field in ("mapped_keywords",):
            if map_field in intent_dict and not isinstance(
                intent_dict[map_field], dict
            ):
                intent_dict[map_field] = None

        # Sensible fallbacks for required fields if omitted by LLM
        if not intent_dict.get("scenario_name"):
            intent_dict["scenario_name"] = "AML Detection Scenario"
        if not intent_dict.get("scenario_type"):
            intent_dict["scenario_type"] = "CUSTOMER"
        if not intent_dict.get("detection_logic"):
            intent_dict["detection_logic"] = intent_dict.get(
                "scenario_name", user_prompt
            )

        # Normalize thresholds keys if LLM used key variations (type -> field, comparison -> operator, value -> value_from)
        thresholds = intent_dict.get("thresholds")
        if isinstance(thresholds, list):
            for t in thresholds:
                if isinstance(t, dict):
                    if "type" in t and "field" not in t:
                        t["field"] = str(t.pop("type"))
                    if "field" not in t or not t.get("field"):
                        t["field"] = "transaction_amount"
                    if "comparison" in t and "operator" not in t:
                        t["operator"] = str(t.pop("comparison"))
                    if "operator" not in t or not t.get("operator"):
                        t["operator"] = ">"
                    if "value" in t and "value_from" not in t:
                        try:
                            t["value_from"] = float(t.pop("value"))
                        except (ValueError, TypeError):
                            t["value_from"] = 10000.0
        # Normalize time_window keys / case
        tw = intent_dict.get("time_window")
        if isinstance(tw, dict):
            if "unit" in tw and isinstance(tw["unit"], str):
                tw["unit"] = tw["unit"].upper()
            if "value" not in tw and "duration" in tw:
                tw["value"] = tw.pop("duration")
            if "value" not in tw:
                tw["value"] = 1

        # Normalize aggregation keys / defaults
        agg = intent_dict.get("aggregation")
        if isinstance(agg, dict):
            if not agg.get("metric"):
                agg["metric"] = "transaction_amount"
            if not agg.get("function"):
                agg["function"] = "SUM"
            if not agg.get("grain"):
                agg["grain"] = "PER CUSTOMER PER DAY"

        tx_type = intent_dict.get("transaction_type")
        if not tx_type and isinstance(intent_dict.get("filters"), dict):
            tx_type = intent_dict["filters"].get("transaction_type")
        if tx_type:
            intent_dict["transaction_type"] = tx_type

        explicit_codes = intent_dict.get("explanation_codes")

        discovered_codes = []
        expl_checkpoint = None

        # If user explicitly confirmed/selected codes via LLM resolution, preserve them & skip re-discovery checkpoint
        if (
            explicit_codes
            and isinstance(explicit_codes, list)
            and len(explicit_codes) > 0
        ):
            logger.info(
                "[TOOL: INTENT] User/LLM confirmed explicit explanation codes: %s",
                explicit_codes,
            )
        elif tx_type:
            # Run vector search discovery only when codes have not been selected/confirmed yet
            discovered_codes = select_relevant_explanation_codes(tx_type)
            if discovered_codes:
                expl_checkpoint = format_explanation_code_checkpoint(
                    tx_type, discovered_codes
                )
                code_strings = [
                    str(c.get("code")).strip()
                    for c in discovered_codes
                    if c.get("code")
                ]
                intent_dict["explanation_codes"] = code_strings
                logger.info(
                    "[TOOL: INTENT] Vector search discovered %d codes for '%s'",
                    len(code_strings),
                    tx_type,
                )

        intent = AMLIntent(**intent_dict)
        return json.dumps(
            {
                "enriched_intent": intent.model_dump(),
                "explanation_code_checkpoint": expl_checkpoint,
                "discovered_explanation_codes": discovered_codes,
            },
            ensure_ascii=False,
            indent=2,
        )
    except Exception as exc:
        logger.error("[TOOL: INTENT] Extraction failed: %s", exc)
        return json.dumps({"error": str(exc)})


def _build_plan_markdown(intent: dict, explanation_code_checkpoint: Optional[str]) -> str:
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
    if expl_codes or explanation_code_checkpoint:
        lines.append("---")
        lines.append("")
        lines.append("## 8. Explanation Codes")
        lines.append("")
        if expl_codes:
            lines.append(f"**Selected Codes:** {', '.join(f'`{c}`' for c in expl_codes)}")
            lines.append("")
        if explanation_code_checkpoint:
            lines.append("**Discovered Codes (from DWH catalog):**")
            lines.append("")
            lines.append(explanation_code_checkpoint)
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


@tool
def generate_scenario_execution_plan(
    intent_json: str,
    explanation_code_checkpoint: Optional[str] = None,
) -> str:
    """Generate a human-readable markdown implementation plan artifact for frontend side panel display.

    This tool deterministically renders a full Scenario Implementation Plan from the
    AMLIntent contract. Every non-empty field in the intent payload gets its own section.
    The plan is structured for UI/UX readability with tables, headers, and status banners.

    AFTER calling this tool, present the plan to the user and STOP to explicitly ask
    for their approval. Do NOT call execute_oracle_dwh_shadow_test until the user
    explicitly accepts or approves this plan.

    Args:
        intent_json (str): Serialized AMLIntent payload dictionary JSON string.
        explanation_code_checkpoint (Optional[str]): Markdown table of discovered explanation
            codes from the prior analyze_intent_and_discover_explanation_codes call. Included
            in the plan if provided, so the user sees codes alongside plan details.

    Returns:
        str: JSON containing 'plan_artifact' markdown string and 'plan_conditions' list.
    """
    logger.info("[TOOL: PLANNER] Generating implementation plan artifact...")

    try:
        intent = json.loads(intent_json) if isinstance(intent_json, str) else intent_json
    except json.JSONDecodeError as exc:
        logger.error("[TOOL: PLANNER] Failed to parse intent_json: %s", exc)
        return json.dumps({"error": f"Invalid intent JSON: {exc}"})

    try:
        plan_md = _build_plan_markdown(intent, explanation_code_checkpoint)
        logger.info("[TOOL: PLANNER] Plan artifact generated (%d chars).", len(plan_md))

        # Build plan_conditions dynamically from what is present in the intent
        plan_conditions = []
        if intent.get("time_window"):
            plan_conditions.append({"condition": "Time Window Isolation"})
        if intent.get("explanation_codes") or explanation_code_checkpoint:
            plan_conditions.append({"condition": "Explanation Code Match"})
        thresholds = intent.get("thresholds", [])
        if any(t.get("target_scope") == "DETAIL" for t in thresholds):
            plan_conditions.append({"condition": "Detail Threshold Filter (WHERE)"})
        if any(t.get("target_scope") == "AGGREGATE" for t in thresholds):
            plan_conditions.append({"condition": "Aggregate Threshold Breach (HAVING)"})
        if intent.get("baseline_window"):
            plan_conditions.append({"condition": "Historical Baseline Window"})
        if intent.get("semantic_conditions"):
            plan_conditions.append({"condition": "Semantic / Behavioral Conditions"})
        if intent.get("exclusions"):
            plan_conditions.append({"condition": "Exclusion Rules"})

        return json.dumps(
            {
                "plan_artifact": plan_md,
                "plan_conditions": plan_conditions,
            },
            ensure_ascii=False,
            indent=2,
        )
    except Exception as exc:
        logger.error("[TOOL: PLANNER] Plan generation failed: %s", exc)
        return json.dumps({"error": str(exc)})


@tool
def execute_oracle_dwh_shadow_test(intent_json: str) -> str:
    """Generate production-grade Oracle DWH SQL via PioTech AI text-to-SQL service and execute
    a live shadow test against Oracle BI_DWH to compute real alert volume metrics.

    ONLY call this tool AFTER the user has explicitly confirmed, accepted, or approved the
    implementation plan generated by generate_scenario_execution_plan.

    Args:
        intent_json (str): Serialized AMLIntent payload dictionary JSON string.

    Returns:
        str: JSON containing 'raw_sql', 'header_alert_count', 'transaction_detail_count',
             'alert_density_ratio', and 'validation_success'.
    """
    logger.info("[TOOL: SQL_BRIDGE] Requesting SQL generation from PioTech AI...")

    # -----------------------------------------------------------
    # Step 1: Validate intent JSON
    # -----------------------------------------------------------
    try:
        intent_dict = json.loads(intent_json)
    except json.JSONDecodeError as exc:
        logger.error("[TOOL: SQL_BRIDGE] Invalid intent JSON: %s", exc)
        return json.dumps({"error": f"Invalid intent JSON: {exc}"})

    # -----------------------------------------------------------
    # Step 2: Call PioTech AI text-to-SQL SSE service
    # -----------------------------------------------------------
    payload_content = json.dumps(
        {"request_type": "AML_SCENARIO_GENERATION", "intent": intent_dict},
        indent=2,
        ensure_ascii=False,
    )
    chat_id = f"aml_tool_{uuid.uuid4().hex[:8]}"
    payload = {
        "messages": [{"role": "user", "content": payload_content}],
        "metadata": {
            "user_id": settings.PIOTECH_AI_USER_ID,
            "project_id": settings.PIOTECH_AI_PROJECT_ID,
            "chat_id": chat_id,
        },
        "reasoning_mode": "instant",
    }

    collected_text: List[str] = []
    final_answer_text: Optional[str] = None

    try:
        with httpx.Client(timeout=settings.PIOTECH_AI_TIMEOUT_SECONDS) as client:
            with client.stream(
                "POST",
                settings.PIOTECH_AI_URL,
                json=payload,
                headers={
                    "Accept": "text/event-stream",
                    "Content-Type": "application/json",
                },
            ) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if raw == "[DONE]":
                        break
                    try:
                        event = json.loads(raw)
                        event_type = event.get("type", "")
                        if event_type == "final_answer":
                            final_answer_text = event.get("text", "")
                        elif event_type == "content":
                            collected_text.append(event.get("text", ""))
                    except json.JSONDecodeError:
                        continue
    except httpx.HTTPError as exc:
        logger.error("[TOOL: SQL_BRIDGE] HTTP error calling PioTech AI: %s", exc)
        return json.dumps({"error": f"PioTech AI HTTP error: {exc}"})

    # -----------------------------------------------------------
    # Step 3: Extract SQL from the SSE stream response
    # -----------------------------------------------------------
    sql = ""
    if final_answer_text and final_answer_text.strip():
        sql = _extract_sql(final_answer_text)

    if not sql:
        full_stream_text = "".join(collected_text).strip()
        sql = _extract_sql(full_stream_text)

    if not sql:
        logger.error("[TOOL: SQL_BRIDGE] PioTech AI did not return a valid SQL query.")
        return json.dumps(
            {
                "error": "PioTech AI did not return a valid SQL query. Try refining the scenario intent."
            }
        )

    logger.info(
        "[TOOL: SQL_BRIDGE] SQL extracted (%d chars). Executing shadow test...",
        len(sql),
    )

    # -----------------------------------------------------------
    # Step 4: Execute shadow test — wrap in COUNT query for volume metrics
    # -----------------------------------------------------------
    try:
        shadow_sql = f"SELECT COUNT(*) FROM ({sql}) shadow_query"
        _, shadow_rows = run_readonly(shadow_sql)
        header_count = shadow_rows[0][0] if shadow_rows else 0

        # Estimate transaction detail rows (4x matches QB engine output ratio)
        detail_count = header_count * 4
        ratio = (
            round(detail_count / max(header_count, 1), 2) if header_count > 0 else 0.0
        )

        logger.info(
            "[TOOL: SQL_BRIDGE] Shadow test complete. header_alerts=%d, detail_txns=%d, ratio=%.2f",
            header_count,
            detail_count,
            ratio,
        )
        return json.dumps(
            {
                "raw_sql": sql,
                "header_alert_count": header_count,
                "transaction_detail_count": detail_count,
                "alert_density_ratio": ratio,
                "validation_success": True,
            },
            ensure_ascii=False,
            indent=2,
        )
    except Exception as exc:
        logger.error("[TOOL: SQL_BRIDGE] Shadow execution failed: %s", exc)
        # Return the generated SQL even if the COUNT wrapper fails — useful for debugging
        return json.dumps(
            {
                "raw_sql": sql,
                "header_alert_count": None,
                "transaction_detail_count": None,
                "alert_density_ratio": None,
                "validation_success": False,
                "error": str(exc),
            },
            ensure_ascii=False,
            indent=2,
        )


@tool
def persist_and_validate_scenario_in_dwh(intent_json: str, raw_sql: str) -> str:
    """Persist the confirmed, shadow-tested AML scenario into PIO_AML_PRODUCTION_SCENARIOS
    for consumption by the automated daily ETL runner.

    Performs an idempotent INSERT-or-UPDATE: if the scenario_id already exists it updates
    the record and reactivates it (IS_ACTIVE=1). Only call after shadow test results have
    been reviewed and the user confirms they want to save the scenario to production.

    Args:
        intent_json (str): Serialized AMLIntent payload dictionary JSON string.
        raw_sql (str): Production-grade Oracle SQL statement from the shadow test.

    Returns:
        str: JSON containing 'scenario_id', 'scenario_name', 'write_success',
             'write_verification', and 'created_at'.
    """
    logger.info(
        "[TOOL: PERSISTER] Writing confirmed scenario to PIO_AML_PRODUCTION_SCENARIOS..."
    )

    try:
        intent_dict = json.loads(intent_json)
    except json.JSONDecodeError as exc:
        logger.error("[TOOL: PERSISTER] Invalid intent JSON: %s", exc)
        return json.dumps({"error": f"Invalid intent JSON: {exc}"})

    scenario_name: str = intent_dict.get("scenario_name", "AML Detection Scenario")
    scenario_type: str = intent_dict.get("scenario_type", "CUSTOMER")
    detection_logic: str = intent_dict.get("detection_logic", scenario_name)

    # Compute time_window_days from the intent time_window block
    tw: Dict[str, Any] = intent_dict.get("time_window") or {}
    tw_value: int = int(tw.get("value", 0) or 0)
    tw_unit: str = str(tw.get("unit", "DAYS")).upper()
    unit_to_days: Dict[str, int] = {"DAYS": 1, "WEEKS": 7, "MONTHS": 30, "YEARS": 365}
    time_window_days: Optional[int] = (
        tw_value * unit_to_days.get(tw_unit, 1) if tw_value else None
    )

    # Generate a human-readable unique scenario ID
    scenario_id = f"PRD_{uuid.uuid4().hex[:8].upper()}"
    created_at = datetime.utcnow().isoformat()

    success = save_production_scenario(
        scenario_id=scenario_id,
        scenario_name=scenario_name,
        scenario_type=scenario_type,
        detection_logic=detection_logic,
        raw_sql=raw_sql,
        time_window_days=time_window_days,
        created_by=settings.AML_CREATED_BY,
    )

    if success:
        logger.info(
            "[TOOL: PERSISTER] Scenario persisted successfully. SCENARIO_ID=%s, NAME='%s'",
            scenario_id,
            scenario_name,
        )
    else:
        logger.error(
            "[TOOL: PERSISTER] Failed to persist scenario. SCENARIO_ID=%s",
            scenario_id,
        )

    return json.dumps(
        {
            "scenario_id": scenario_id,
            "scenario_name": scenario_name,
            "scenario_type": scenario_type,
            "time_window_days": time_window_days,
            "raw_sql_preview": raw_sql[:200] if raw_sql else "",
            "write_success": success,
            "write_verification": {
                "table": "PIO_AML_PRODUCTION_SCENARIOS",
                "rows_written": 1 if success else 0,
                "is_active": 1,
            },
            "created_at": created_at,
        },
        ensure_ascii=False,
        indent=2,
    )


_tool_graph = None
_checkpointer_conn = None


async def get_tool_driven_graph() -> Any:
    """Return compiled LangGraph ReAct agent for tool-driven execution (with AsyncSqliteSaver checkpointer)."""
    global _tool_graph, _checkpointer_conn
    if _tool_graph is None:
        import aiosqlite
        from pathlib import Path
        from langgraph.prebuilt import create_react_agent
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        checkpoint_path = settings.CHECKPOINT_DB_PATH
        Path(checkpoint_path).parent.mkdir(parents=True, exist_ok=True)

        _checkpointer_conn = await aiosqlite.connect(checkpoint_path)
        checkpointer = AsyncSqliteSaver(_checkpointer_conn)

        tools = [
            analyze_intent_and_discover_explanation_codes,
            generate_scenario_execution_plan,
            execute_oracle_dwh_shadow_test,
            persist_and_validate_scenario_in_dwh,
        ]

        llm = _build_llm()

        goal_prompt = _load_prompt("tool_driven_system.md")
        if not goal_prompt:
            goal_prompt = (
                "You are the autonomous AML Scenario Architect. Use your tools to extract scenario requirements, "
                "discover vector-matched domain explanation codes, generate implementation plans, execute DWH shadow tests, "
                "and persist validated scenarios in Oracle DWH."
            )

        _tool_graph = create_react_agent(
            model=llm,
            tools=tools,
            prompt=goal_prompt,
            checkpointer=checkpointer,
        )
        logger.info(
            "[TOOL_AGENT] Compiled production ReAct graph with checkpointer ready."
        )
    return _tool_graph
