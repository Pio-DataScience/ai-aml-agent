"""
AML Builder Agent — LangGraph StateGraph.

This module defines the full multi-node agent that converts a compliance
manager's natural language intent into a live, self-validated AML scenario
in the PioTech Oracle Query Builder engine.

Graph topology:
    orchestrator → intent_analyst → sql_bridge → decomposer
                                                       ↓
                 validator ←────────────────── qb_writer
                     ↓ (retry → decomposer)
                 orchestrator (final answer)
"""

import json
import logging
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Dict, List, Literal, Optional

import httpx
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, StateGraph, add_messages
from pydantic import BaseModel
from typing_extensions import TypedDict

from web.services.schemas import (
    AMLIntent,
    AlertSample,
    CatalogCreation,
    OrchestratorDecision,
    PlanCondition,
    QBRule,
    QBRuleDetail,
    QBScenario,
    QBScenarioRule,
    ScenarioParameters,
    SQLMetadata,
    ValidationResult,
    WriteVerification,
)
from web.services.settings import settings

logger = logging.getLogger(__name__)


class PlanDriftError(Exception):
    """Raised when generated rule_details diverge from the approved plan conditions."""


# =============================================================================
# STATE — The single source of truth passed between every node
# =============================================================================


class AMLScenarioState(TypedDict):
    """Full state of the AML scenario creation lifecycle.

    Every node reads from and writes to this state object.
    LangGraph manages its persistence via SQLite checkpointer.
    """

    # Conversation history (append-only via add_messages reducer)
    messages: Annotated[List[BaseMessage], add_messages]

    # Intent layer
    user_intent: str
    enriched_intent: Optional[Dict[str, Any]]  # Serialized AMLIntent

    # SQL bridge layer
    raw_sql: Optional[str]
    sql_metadata: Optional[Dict[str, Any]]  # Serialized SQLMetadata

    # Decomposition layer
    scenario_parameters: Optional[Dict[str, Any]]  # Serialized ScenarioParameters
    decomposition_confidence: float

    # QB execution layer
    scenario_code: Optional[str]
    scenario_write_success: bool

    # Validation layer
    validation_result: Optional[Dict[str, Any]]  # Serialized ValidationResult
    validation_retry_count: int

    # Control flow
    next_action: str  # Router signal between nodes
    iteration_count: int
    error_log: List[str]

    # Plan layer
    plan_artifact: Optional[str]  # markdown plan streamed to frontend side panel
    plan_conditions: Optional[
        List[Dict[str, Any]]
    ]  # machine-readable conditions for drift check
    plan_approved: bool  # True once user says "proceed"

    # Catalog auto-creation log
    catalog_creations: List[
        Dict[str, Any]
    ]  # one entry per auto-provisioned catalog row

    # Post-write verification
    write_verification: Optional[Dict[str, Any]]  # per-table row counts after INSERT

    # Escalation
    escalation_report: Optional[str]  # markdown escalation report on terminal failure
    failure_mode: Optional[str]  # "REDEFINE" | "ADJUST" | "ESCALATE"

    # Explanation code discovery
    discovered_explanation_codes: Optional[List[Dict[str, Any]]]
    explanation_code_checkpoint: Optional[str]  # Markdown table view for Checkpoint 1
    explanation_codes_confirmed: bool  # True once user confirms Checkpoint 1


# =============================================================================
# LLM FACTORY
# =============================================================================


def _build_llm(fast: bool = False):
    """Construct the LLM client based on settings.

    Args:
        fast (bool): If True, use the lightweight fast model. Otherwise
            use the primary reasoning model. Defaults to False.

    Returns:
        ChatOpenAI: Configured LLM client.
    """
    from langchain_openai import ChatOpenAI

    model = settings.LLM_MODEL_FAST if fast else settings.LLM_MODEL

    if settings.LLM_PROVIDER == "lmstudio":
        return ChatOpenAI(
            base_url=settings.LLM_BASE_URL or "http://127.0.0.1:1234/v1",
            api_key="lm-studio",
            model=model,
            temperature=settings.LLM_TEMPERATURE,
            max_tokens=settings.LLM_MAX_TOKENS,
        )

    return ChatOpenAI(
        model=model,
        api_key=settings.OPENAI_API_KEY,
        temperature=settings.LLM_TEMPERATURE,
        max_tokens=settings.LLM_MAX_TOKENS,
    )


# =============================================================================
# PROMPT LOADER
# =============================================================================


def _load_prompt(filename: str) -> str:
    """Load a system prompt from the prompts directory.

    Args:
        filename (str): Filename of the prompt markdown file.

    Returns:
        str: File contents as a string. Returns empty string on failure.
    """
    prompt_path = Path(__file__).parent / "prompts" / filename
    try:
        return prompt_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.warning("[PROMPT] Not found: %s — using empty prompt.", filename)
        return ""


# =============================================================================
# NODE 1 — ORCHESTRATOR
# =============================================================================


def _build_state_context(state: AMLScenarioState, iteration: int) -> str:
    """Serialise current pipeline state into a readable table for the orchestrator LLM.

    Args:
        state (AMLScenarioState): Current agent state.
        iteration (int): Current iteration number (already incremented).

    Returns:
        str: Markdown table + error log block.
    """
    prev_action = state.get("next_action") or "INTENT"
    enriched_intent = state.get("enriched_intent") or {}
    plan_generated = bool(state.get("plan_artifact"))
    plan_approved = state.get("plan_approved", False)
    scenario_code = state.get("scenario_code")
    error_log = state.get("error_log", [])
    failure_mode = state.get("failure_mode")
    validation_result = state.get("validation_result") or {}
    write_verification = state.get("write_verification") or {}
    catalog_creations = state.get("catalog_creations") or []

    val_success = validation_result.get("success")
    alert_count = validation_result.get("alert_count")
    det_count = validation_result.get("det_count")
    density = validation_result.get("alert_density_ratio")
    confidence = validation_result.get("confidence_score")
    diagnosis = validation_result.get("diagnosis")
    threshold_sensitivity = validation_result.get("threshold_sensitivity") or {}
    write_ok = write_verification.get("all_pass")

    intent_name = enriched_intent.get("scenario_name", "Not captured yet")
    intent_type = enriched_intent.get("scenario_type", "")
    ready_for_handoff = enriched_intent.get("ready_for_handoff", True)
    structured_clarifications = enriched_intent.get("clarifications", []) or []
    applied_defaults = enriched_intent.get("applied_defaults", []) or []
    # Prefer structured clarifications; fall back to the legacy flat list.
    clarification_needed = (not ready_for_handoff) or enriched_intent.get(
        "clarification_needed", False
    )
    clarification_questions = [
        c.get("question", "") for c in structured_clarifications
    ] or enriched_intent.get("clarification_questions", [])

    samples = validation_result.get("sample_alerts", [])
    sample_lines = ""
    if samples:
        sample_lines = "\n\n**Sample Customers (validation output):**\n| Customer ID | Details |\n|---|---|\n"
        for s in samples[:4]:
            cid = s.get("customer_id", "—")
            raw = s.get("raw_data", {})
            detail = ", ".join(f"{k}: {v}" for k, v in list(raw.items())[:3])
            sample_lines += f"| `{cid}` | {detail} |\n"

    table = (
        f"| Field | Value |\n"
        f"|---|---|\n"
        f"| **Pipeline phase (prev)** | `{prev_action}` |\n"
        f"| **Iteration** | {iteration} / {settings.MAX_AGENT_ITERATIONS} |\n"
        f"| **Intent captured** | {intent_name} ({intent_type}) |\n"
        f"| **Clarification needed** | {clarification_needed} |\n"
        f"| **Plan generated** | {plan_generated} |\n"
        f"| **Plan approved** | {plan_approved} |\n"
        f"| **Scenario code** | {scenario_code or 'Not assigned'} |\n"
        f"| **Write success** | {state.get('scenario_write_success', False)} |\n"
        f"| **Write integrity (all 4 tables)** | {write_ok} |\n"
        f"| **Validation success** | {val_success} |\n"
        f"| **Header alert count** | {alert_count} |\n"
        f"| **Transaction detail count** | {det_count} |\n"
        f"| **Alert density ratio** | {density} |\n"
        f"| **Confidence score** | {confidence} |\n"
        f"| **Failure mode** | {failure_mode or 'None'} |\n"
        f"| **Catalog auto-provisions** | {len(catalog_creations)} new entries |\n"
    )
    if diagnosis:
        table += f"| **Validation diagnosis** | {diagnosis} |\n"
    if threshold_sensitivity.get("tested"):
        table += f"| **Threshold sensitivity** | {threshold_sensitivity.get('diagnosis', 'Available')} |\n"

    error_block = f"\n**Error log ({len(error_log)} entries):**\n"
    if error_log:
        for err in error_log[-3:]:
            short = err[:250] + "…" if len(err) > 250 else err
            error_block += f"- {short}\n"
    else:
        error_block += "- _No errors_\n"

    questions_block = ""
    if structured_clarifications:
        questions_block = "\n\n**Clarification Questions Needed from User:**\n"
        for idx, c in enumerate(structured_clarifications, 1):
            q = c.get("question", "")
            opts = c.get("options")
            opts_str = f" (Choices: {', '.join(opts)})" if opts else ""
            why = c.get("why_it_matters", "")
            why_str = f" — *{why}*" if why else ""
            questions_block += f"{idx}. {q}{opts_str}{why_str}\n"
    elif clarification_questions:
        questions_block = "\n\n**Clarification Questions Needed from User:**\n"
        for idx, q in enumerate(clarification_questions, 1):
            questions_block += f"{idx}. {q}\n"

    defaults_block = ""
    if applied_defaults:
        defaults_block = "\n\n**Defaults Assumed (confirm or correct):**\n"
        for d in applied_defaults:
            defaults_block += f"- {d}\n"

    intent_json_str = ""
    if enriched_intent:
        intent_json_str = (
            f"\n\n**CURRENT CAPTURED INTENT PAYLOAD:**\n```json\n"
            f"{json.dumps(enriched_intent, indent=2, default=str, ensure_ascii=False)}\n```"
        )

    plan_str = ""
    plan_art = state.get("plan_artifact")
    if plan_art:
        plan_str = f"\n\n**CURRENT ACTIVE SCENARIO PLAN:**\n{plan_art[:1500]}\n"

    return (
        table
        + intent_json_str
        + plan_str
        + error_block
        + questions_block
        + defaults_block
        + sample_lines
    )


def orchestrator_node(
    state: AMLScenarioState, config: RunnableConfig
) -> Dict[str, Any]:
    """Fully agentic orchestrator — LLM decides every routing and communication action.

    Builds a rich state context, passes the full conversation history and system
    state to the LLM, and interprets its structured OrchestratorDecision output.
    Python only executes mechanical side effects (field clearing, report generation).
    Zero hardcoded routing logic or message templates.

    Args:
        state (AMLScenarioState): Current agent state.
        config (RunnableConfig): LangGraph runtime config.

    Returns:
        Dict[str, Any]: State updates including next_action routing signal.
    """
    iteration = state.get("iteration_count", 0) + 1
    logger.info(
        "[ORCHESTRATOR] Iteration %d  prev_action=%s",
        iteration,
        state.get("next_action", "INTENT"),
    )

    # Hard safety cap — only non-LLM logic in this node
    if iteration > settings.MAX_AGENT_ITERATIONS:
        logger.error("[ORCHESTRATOR] Max iterations reached. Halting.")
        return {
            "messages": [
                AIMessage(
                    content=(
                        "I've reached the processing limit for this session. "
                        "Please start a new conversation to continue."
                    )
                )
            ],
            "next_action": "END",
            "iteration_count": iteration,
        }

    # Build state context for the LLM
    state_context = _build_state_context(state, iteration)

    # Build message list: system prompt + conversation history + state injection
    system_prompt = _load_prompt("orchestrator_system.md")
    history = list(state.get("messages", []))

    messages_for_llm = (
        [SystemMessage(content=system_prompt)]
        + history[-14:]
        + [
            HumanMessage(
                content=(
                    f"## CURRENT SYSTEM STATE\n\n{state_context}\n\n"
                    f"## YOUR TASK\n\n"
                    f"Read the conversation history and the system state above, "
                    f"then produce your orchestration decision."
                )
            )
        ]
    )

    # Call LLM with structured output
    llm = _build_llm(fast=False).with_structured_output(OrchestratorDecision)

    try:
        decision: OrchestratorDecision = llm.invoke(messages_for_llm)
    except Exception as exc:
        logger.error("[ORCHESTRATOR] LLM decision call failed: %s", exc, exc_info=True)
        return {
            "messages": [
                AIMessage(
                    content=(
                        "I'm experiencing a technical issue. Please try again in a moment."
                    )
                )
            ],
            "next_action": "WAIT_USER",
            "iteration_count": iteration,
        }

    # Safety check: Prevent infinite loops when clarifications are needed
    if state.get("next_action") == "CLARIFY":
        if decision.next_action != "WAIT_USER":
            logger.warning(
                "[ORCHESTRATOR] LLM tried to bypass CLARIFY phase. Overriding next_action to WAIT_USER."
            )
            decision.next_action = "WAIT_USER"

        if not decision.message_to_user:
            intent_data = state.get("enriched_intent") or {}
            clarifications = intent_data.get("clarifications") or []
            if not clarifications:
                clarifications = intent_data.get("clarification_questions") or []

            if clarifications:
                fallback_msg = "To build your scenario accurately, I need a few clarifications:\n\n"
                for idx, cl in enumerate(clarifications, 1):
                    q_text = cl.get("question") if isinstance(cl, dict) else str(cl)
                    fallback_msg += f"{idx}. **{q_text}**\n"
                decision.message_to_user = fallback_msg
    # Initial scenario request fallback: Force route to INTENT if no enriched_intent exists yet
    if not state.get("enriched_intent") and decision.next_action != "INTENT":
        logger.info("[ORCHESTRATOR] Initial scenario request detected without intent — forcing next_action to INTENT.")
        decision.next_action = "INTENT"

    updates: Dict[str, Any] = {"iteration_count": iteration}

    # Checkpoint 1: Domain Explanation Code Discovery Checkpoint
    checkpoint = state.get("explanation_code_checkpoint")
    confirmed = state.get("explanation_codes_confirmed", False)

    if checkpoint and not confirmed:
        raw_msgs = state.get("messages", [])
        last_msg_lower = (
            raw_msgs[-1].content.lower() if raw_msgs and isinstance(raw_msgs[-1], HumanMessage) else ""
        )
        confirm_keywords = ["confirm", "confirm codes", "yes", "proceed", "looks good", "ok", "use these"]
        if any(k in last_msg_lower for k in confirm_keywords):
            logger.info("[ORCHESTRATOR] User confirmed domain explanation codes!")
            updates["explanation_codes_confirmed"] = True
        else:
            logger.info("[ORCHESTRATOR] Presenting Checkpoint 1 explanation code table view.")
            decision.next_action = "WAIT_USER"
            if not decision.message_to_user or "Discovered Transaction Types" not in decision.message_to_user:
                decision.message_to_user = f"{checkpoint}"

    logger.info(
        "[ORCHESTRATOR] Decision → next_action=%s  message=%s",
        decision.next_action,
        "yes" if decision.message_to_user else "none (silent routing)",
    )

    if decision.message_to_user:
        # Persist plan_artifact and validation_result inside message additional_kwargs for history reload
        add_kwargs = {}
        # Only attach validation_result if we are displaying final results or failure recovery options
        if (
            decision.next_action in ("FINALIZE", "WAIT_USER")
            or state.get("next_action") == "VALIDATE"
        ):
            val_res = state.get("validation_result")
            if val_res:
                add_kwargs["validation_result"] = val_res

        updates["messages"] = [
            AIMessage(content=decision.message_to_user, additional_kwargs=add_kwargs)
        ]

    action = decision.next_action

    # ── Mechanical side effects based on LLM decision ─────────────────────────

    if action == "REDEFINE" or decision.clear_scenario_state:
        # Wipe all scenario-specific fields; preserve INTENT if user provided new scenario directly
        target_action = "INTENT" if action == "INTENT" else "WAIT_USER"
        updates.update(
            {
                "enriched_intent": None,
                "raw_sql": None,
                "sql_metadata": None,
                "scenario_parameters": None,
                "scenario_code": None,
                "rule_code": None,
                "plan_artifact": None,
                "plan_conditions": None,
                "plan_approved": False,
                "catalog_creations": [],
                "write_verification": None,
                "validation_result": None,
                "validation_retry_count": 0,
                "scenario_write_success": False,
                "error_log": [],
                "failure_mode": None,
                "next_action": target_action,
            }
        )

    elif action == "SQL_BRIDGE":
        updates["plan_approved"] = True
        updates["next_action"] = "SQL_BRIDGE"

    elif action == "ADJUST":
        updates["failure_mode"] = "ADJUST"
        updates["next_action"] = "WAIT_USER"

    elif action == "ESCALATE":
        report = _generate_escalation_report(state)
        updates["escalation_report"] = report
        updates["failure_mode"] = "ESCALATE"
        updates["next_action"] = "END"
        # Inject the generated report into the AIMessage's additional_kwargs for history retrieval
        if "messages" in updates and updates["messages"]:
            msg = updates["messages"][0]
            if isinstance(msg, AIMessage):
                msg.additional_kwargs["escalation_report"] = report

    elif action in ("FINALIZE", "END"):
        updates["next_action"] = "END"

    else:
        # INTENT, WAIT_USER, WAIT_APPROVAL, DECOMPOSE — pass through directly
        updates["next_action"] = action

    return updates


def _generate_escalation_report(state: AMLScenarioState) -> str:
    """Build a structured markdown escalation report for the compliance & DWH engineering team.

    Includes scenario intent, generated production SQL, error logs, and DWH shadow testing metrics.
    """
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    intent_dict = state.get("enriched_intent") or {}
    scenario_code = state.get("scenario_code", "PENDING")
    error_log = state.get("error_log", [])
    validation_result = state.get("validation_result") or {}

    errors_text = "\n".join(f"- {e}" for e in error_log) or "_No errors logged._"

    raw_sql = state.get("raw_sql")
    sql_text = f"```sql\n{raw_sql}\n```" if raw_sql else "_No SQL query was generated._"

    report_content = (
        f"# AML Scenario Technical Escalation Report\n\n"
        f"**Generated:** {now}  \n"
        f"**Scenario Code:** `{scenario_code}`  \n"
        f"**Engine:** PioTech AML Standalone Production Engine\n\n"
        f"---\n\n"
        f"## 1. Scenario Intent & Parameters\n\n"
        f"```json\n{json.dumps(intent_dict, indent=2, default=str, ensure_ascii=False)}\n```\n\n"
        f"---\n\n"
        f"## 2. Generated ANSI Oracle SQL Query\n\n"
        f"{sql_text}\n\n"
        f"---\n\n"
        f"## 3. System Error & Diagnostic Log\n\n"
        f"{errors_text}\n\n"
        f"---\n\n"
        f"## 4. DWH Shadow Testing & Validation Metrics\n\n"
        f"```json\n{json.dumps(validation_result, indent=2, default=str, ensure_ascii=False)}\n```\n\n"
        f"---\n\n"
        f"_This report was generated automatically by the AML Scenario Agent._  \n"
        f"_Reference Scenario Code `{scenario_code}` in all support communications._"
    )

    try:
        from web.services.persister import DatePartitionedFilePersister

        persister = DatePartitionedFilePersister()
        persister.persist(scenario_code, report_content)
    except Exception as exc:
        logger.error("[PERSISTER] Failed to save escalation report: %s", exc)

    return report_content


# =============================================================================
# NODE 2 — INTENT ANALYST
# =============================================================================


def intent_analyst_node(
    state: AMLScenarioState, config: RunnableConfig
) -> Dict[str, Any]:
    """Parse and enrich the user's AML detection goal.

    Converts natural language into a structured AMLIntent object.
    Identifies ambiguities and generates business-level clarification questions.
    Never asks technical questions — only business decisions.

    Args:
        state (AMLScenarioState): Current agent state.
        config (RunnableConfig): LangGraph runtime config.

    Returns:
        Dict[str, Any]: State updates with enriched_intent and next_action.
    """
    logger.info("[INTENT_ANALYST] Analyzing user intent.")

    messages = state.get("messages", [])
    last_user_msg = next(
        (m.content for m in reversed(messages) if isinstance(m, HumanMessage)), ""
    )

    system_prompt = _load_prompt("intent_analyst_system_v2.md")
    llm = _build_llm(fast=False)

    # Compile the full system instructions, including schemas.
    instruction_prompt = f"""{system_prompt}

---

## TASK
Analyze the entire conversation history below to extract the final compiled AML scenario intent matching the schema, taking all user clarifications into account.
Return ONLY valid JSON. No explanation, no markdown fences.

Schema:
{{
  "scenario_name": "string",
  "scenario_type": "string",
  "transaction_type": "string (e.g. 'LOAN SETTLEMENT', 'CASH DEPOSIT', 'OUTWARD TRANSFER')" | null,
  "detection_logic": "string",
  "thresholds": [{{"field":"string","operator":"string","value_from":number,"value_to":null,"provenance":"stated|assumed_default|needs_user"}}],
  "time_window": {{"unit":"DAYS|MONTHS|YEARS","value":number,"is_rolling":true,"provenance":"stated|assumed_default|needs_user"}} | null,
  "baseline_window": {{"unit":"DAYS|MONTHS|YEARS","duration":number,"exclude_current_window":true,"offset_days":number,"sql_date_formula":"string","description":"string"}} | null,
  "customer_segments": ["string"] | null,
  "exclusions": ["string"] | null,
  "mapped_keywords": {{"descriptive_qualifier": "threshold_field_expression"}} | null,
  "aggregation": {{"metric":"string (plain English)","function":"sum|count|count distinct|max|null","grain":"string (e.g. 'Per Customer per Day')","provenance":"stated|assumed_default|needs_user"}} | null,
  "semantic_conditions": [{{"raw_phrase":"string","logical_type":"STATE|TRANSITION|SEQUENCE|BEHAVIORAL|TEMPORAL|OTHER","subject":"string (plain English noun)","predicate":"string (plain English rule)","provenance":"stated|assumed_default|needs_user"}}],
  "clarifications": [{{"dimension":"string","why_it_matters":"string","question":"business-phrased, never technical","options":["string"] | null}}],
  "applied_defaults": ["string (plain English default you assumed)"],
  "ready_for_handoff": true|false,
  "clarification_needed": true|false,
  "clarification_questions": ["string"],
  "expected_alert_range_min": number | null,
  "expected_alert_range_max": number | null
}}

RULES:
- Set "transaction_type" to the explicit uppercase transaction category if specified (e.g. 'LOAN SETTLEMENT', 'CASH DEPOSIT', 'OUTWARD TRANSFER', 'ATM WITHDRAWAL', 'WIRE TRANSFER'). Do NOT place transaction categories inside "semantic_conditions".
- INTENT EXTRACTION RULE: HISTORICAL BASELINE ISOLATION:
  * When extracting intent for scenarios that compare current activity against a historical baseline (e.g., "historical monthly average", "6-month average", "prior activity profile"):
    1. EXPLICITLY GENERATE 'baseline_window': You MUST construct a dedicated 'baseline_window' JSON object alongside the 'time_window' object.
    2. ENFORCE ZERO-OVERLAP ISOLATION: Set 'exclude_current_window' to true and 'offset_days' equal to the current window's value (e.g., offset_days = 30 for a 30-day rolling scenario).
    3. DEFINE DATES CLEARLY: Specify exact offset boundaries so downstream SQL generators do not include current-period transactions inside baseline averages.
- Set "provenance" on each value: "stated" if the user gave it, "assumed_default" if you defaulted it (and add a plain sentence to "applied_defaults"), or "needs_user" if it is materially ambiguous.
- For every "needs_user" value, add a matching entry to "clarifications".
- "ready_for_handoff" MUST be false whenever "clarifications" is non-empty. Keep "clarification_needed"/"clarification_questions" in sync (mirror of clarifications) for backward compatibility.
- DYNAMIC BASELINES & COMPARATIVE METRICS (<X> vs <Y>):
  * When a condition compares a metric <X> against another field or dynamic baseline <Y> (e.g., <Metric_X> > <Metric_Y>, <Metric_X> >= <Ratio_K> * <Baseline_Y>, or <Metric_X> compared to <Historical_Period_Y>), <Y> is a database-computed calculation performed dynamically in SQL.
  * NEVER set "provenance": "needs_user" or ask a clarification question requesting a hardcoded numeric value for <Y>.
  * Map the comparison under "semantic_conditions" or map the explicit multiplier/threshold <Ratio_K> under "thresholds" with "provenance": "stated".
- NO RE-ASKING STATED DETAILS:
  * Check the user's input thoroughly. If the user provided the timeframe, thresholds, transaction types, or baseline rules in their prompt, NEVER ask a clarification question for them.
  * If all material scenario requirements are provided in the user's prompt, "clarifications" MUST be empty [] and "ready_for_handoff" MUST be true.
- Never bind terms to tables/columns. Keep subjects and predicates as business English.
- Deduplicate resolved semantic conditions: If a semantic logic term (e.g. "unexpected", "suspicious", "large", "high value") is clarified by the user to mean a specific numeric threshold (e.g. > 10,000), set the numeric threshold under "thresholds" and remove the resolved term from the "semantic_conditions" list. Do not keep it in both. Populate the "mapped_keywords" dictionary mapping the term to its threshold field/condition.
- STRICT NUMERIC TYPES: Under "thresholds", the fields "value_from" and "value_to" MUST always be numeric values (integers or floats) or null. If the value is unknown or needs user clarification, set it to null and set "provenance": "needs_user". NEVER output a string (like a field name or descriptive text) in "value_from" or "value_to".
- TIME WINDOW IS MANDATORY FOR ALL SCENARIOS. Never leave "time_window" null.
  * If the user does not specify a window:
    - For transaction-level/detail rules (e.g. single transaction > 10k), default to a 1-day rolling window (unit: DAYS, value: 1, is_rolling: true) and add a plain sentence to "applied_defaults" (e.g. "Assumed a 1-day rolling window (not stated).").
    - For cumulative/velocity/aggregate rules, if the window is not obvious, do not guess: set "time_window.provenance": "needs_user", add a "clarifications" entry asking for the observation period, and set "ready_for_handoff": false.
- AGGREGATION IS MANDATORY WHENEVER A NUMERIC THRESHOLD EXISTS. Never leave "aggregation" null if "thresholds" is non-empty. You MUST decide the grain, because it changes what the number means:
  * If the amount/count applies to EACH single transaction (e.g. "a cash deposit over 10k", "any transaction above X"), set "aggregation" with "function": null and "grain": "Per Transaction" and "provenance": "stated". This is a per-transaction rule, NOT a sum.
  * If it applies to a CUMULATIVE total across rows (e.g. "total deposits over 10k in a day", "more than 5 transactions"), set "function" to "sum"/"count"/etc. and "grain" to the correct level (e.g. "Per Customer per Day").
  * If single-transaction vs cumulative is genuinely unclear, DO NOT GUESS: set "aggregation.provenance": "needs_user", add a "clarifications" entry (e.g. "Should this flag a single deposit over 10,000, or total deposits over 10,000 within a period?"), and set "ready_for_handoff": false.
- Singular phrasing ("a deposit", "a transaction") implies per-transaction; plural/accumulating phrasing ("total", "sum of", "combined", "over a period") implies an aggregate.
"""

    existing_intent = state.get("enriched_intent")
    if existing_intent:
        instruction_prompt += (
            "\n\nCRITICAL SCENARIO MODIFICATION & DELTA PRESERVATION INSTRUCTION:\n"
            "An existing AMLIntent payload has already been captured and validated for this session:\n"
            f"```json\n{json.dumps(existing_intent, indent=2, default=str, ensure_ascii=False)}\n```\n"
            "The user is requesting a MODIFICATION or REFINEMENT to this existing scenario.\n"
            "1. You MUST PRESERVE all previously stated fields, customer_segments, transaction_types, thresholds, and conditions.\n"
            "2. Apply ONLY the user's requested delta modification (e.g. updating time_window, threshold value, or filter condition).\n"
            "3. DO NOT re-raise clarification questions or set ready_for_handoff=false for dimensions that were already stated or defaulted in the existing intent.\n"
            "4. Ensure ready_for_handoff is set to true unless the user's modification request itself is completely ambiguous.\n"
        )

    llm_messages = [SystemMessage(content=instruction_prompt)]
    # Append the native conversation history
    for m in messages:
        if isinstance(m, (HumanMessage, AIMessage)):
            llm_messages.append(m)

    # Append a final instruction to enforce the JSON task on the whole history
    final_instruction = SystemMessage(
        content="Compile the final scenario parameters from the chat above. Return ONLY the JSON object. Do not include any markdown styling."
    )
    llm_messages.append(final_instruction)

    try:
        response = llm.invoke(llm_messages)
        raw_content = response.content.strip()

        # Clean JSON fences if present
        match = re.search(r"```(?:json)?\s*([\s\S]+?)```", raw_content, re.IGNORECASE)
        if match:
            raw_content = match.group(1).strip()
        else:
            start = raw_content.find("{")
            end = raw_content.rfind("}")
            if start != -1 and end != -1:
                raw_content = raw_content[start : end + 1].strip()

        intent_dict = json.loads(raw_content)

        tx_type = intent_dict.get("transaction_type")
        explanation_checkpoint = None
        discovered_codes = state.get("discovered_explanation_codes") or []
        if tx_type:
            try:
                from web.services.explanation_code_search import (
                    select_relevant_explanation_codes,
                    format_explanation_code_checkpoint,
                )

                if not discovered_codes:
                    discovered_codes = select_relevant_explanation_codes(tx_type)
                if discovered_codes:
                    explanation_checkpoint = format_explanation_code_checkpoint(
                        tx_type, discovered_codes
                    )
                    logger.info(
                        "[INTENT_ANALYST] Generated explanation code discovery checkpoint with %d codes.",
                        len(discovered_codes),
                    )
            except Exception as exc:
                logger.error("[INTENT_ANALYST] Failed explanation code vector search: %s", exc)

        # Extract code strings and assign to intent_dict
        code_strings = [str(c.get("code")).strip() for c in discovered_codes if isinstance(c, dict) and c.get("code")]
        if code_strings:
            mentioned_codes = re.findall(r"\b\d{3,6}\b", last_user_msg)
            matched_subset = [c for c in mentioned_codes if c in code_strings]
            if matched_subset:
                logger.info("[INTENT_ANALYST] User specified code subset: %s", matched_subset)
                intent_dict["explanation_codes"] = matched_subset
            else:
                intent_dict["explanation_codes"] = code_strings
        elif state.get("enriched_intent", {}).get("explanation_codes"):
            intent_dict["explanation_codes"] = state["enriched_intent"]["explanation_codes"]

        # Validate with Pydantic contract
        intent = AMLIntent(**intent_dict)
        enriched_dict = intent.model_dump()

        logger.info(
            "[INTENT_ANALYST] Enriched intent ready. transaction_type=%s, explanation_codes=%s",
            intent.transaction_type,
            intent.explanation_codes,
        )

        needs_clarify = (not intent.ready_for_handoff) or intent.clarification_needed or bool(intent.clarifications)
        next_action = "CLARIFY" if needs_clarify else "SQL_BRIDGE"

        return {
            "enriched_intent": enriched_dict,
            "user_intent": last_user_msg,
            "next_action": next_action,
            "discovered_explanation_codes": discovered_codes,
            "explanation_code_checkpoint": explanation_checkpoint,
        }

    except (json.JSONDecodeError, Exception) as exc:
        logger.error("[INTENT_ANALYST] Failed to parse intent: %s", exc, exc_info=True)
        return {
            "next_action": "ERROR",
            "error_log": state.get("error_log", []) + [f"Intent parsing failed: {exc}"],
        }


# =============================================================================
# NODE 2b — PLANNER
# =============================================================================


def planner_node(state: AMLScenarioState, config: RunnableConfig) -> Dict[str, Any]:
    """Generate and emit the human-readable execution plan for user approval.

    Reads the enriched AMLIntent and uses the LLM to produce:
    1. A structured markdown plan artifact (for the frontend side panel).
    2. A machine-readable CONDITIONS_BLOCK JSON array (for drift assertion).

    Sets next_action to WAIT_APPROVAL. Execution halts until the user
    sends a message containing an approval keyword ("proceed", "yes",
    "approve", "go ahead", "confirm", "start", "execute").

    Args:
        state (AMLScenarioState): Current agent state with enriched_intent set.
        config (RunnableConfig): LangGraph runtime config.

    Returns:
        Dict[str, Any]: State updates with plan_artifact, plan_conditions, next_action.
    """
    logger.info("[PLANNER] Generating execution plan for user approval.")

    intent_dict = state.get("enriched_intent") or {}
    try:
        intent = AMLIntent(**intent_dict)
    except Exception as exc:
        logger.error("[PLANNER] Could not deserialize AMLIntent: %s", exc)
        return {
            "next_action": "ERROR",
            "error_log": state.get("error_log", [])
            + [f"Planner failed to read intent: {exc}"],
        }

    system_prompt = _load_prompt("planner_system.md")
    llm = _build_llm(fast=False)

    user_prompt = (
        f"Generate the AML Scenario Execution Plan for the following intent:\n\n"
        f"{json.dumps(intent.model_dump(), indent=2, default=str, ensure_ascii=False)}\n\n"
        f"Follow the OUTPUT FORMAT exactly. "
        f"Produce the full markdown plan AND the CONDITIONS_BLOCK JSON."
    )

    try:
        response = llm.invoke(
            [
                SystemMessage(content=system_prompt),
                HumanMessage(content=user_prompt),
            ]
        )
        raw = response.content.strip()

        # Extract the CONDITIONS_BLOCK JSON from the response
        conditions_match = re.search(
            r"## CONDITIONS_BLOCK\s*```json\s*([\s\S]+?)```",
            raw,
            re.IGNORECASE,
        )
        plan_conditions: List[Dict[str, Any]] = []
        if conditions_match:
            try:
                conditions_raw = json.loads(conditions_match.group(1).strip())
                plan_conditions = [
                    PlanCondition(**c).model_dump() for c in conditions_raw
                ]
                logger.info(
                    "[PLANNER] Extracted %d plan conditions.", len(plan_conditions)
                )
            except Exception as exc:
                logger.warning("[PLANNER] Could not parse CONDITIONS_BLOCK: %s", exc)
        else:
            logger.warning("[PLANNER] No CONDITIONS_BLOCK found in planner response.")

        logger.info(
            "[PLANNER] Plan generated. length=%d chars conditions=%d.",
            len(raw),
            len(plan_conditions),
        )

        plan_intro = (
            "I've generated the Scenario Implementation Plan based on your requirements. "
            "Please review the plan and let me know if you would like to proceed or make any adjustments."
        )
        msg = AIMessage(
            content=plan_intro,
            additional_kwargs={
                "plan_artifact": raw,
                "timestamp": datetime.utcnow().isoformat(),
            },
        )

        return {
            "plan_artifact": raw,
            "plan_conditions": plan_conditions,
            "plan_approved": False,
            "next_action": "WAIT_APPROVAL",
            "messages": [msg],
        }

    except Exception as exc:
        logger.error("[PLANNER] LLM call failed: %s", exc, exc_info=True)
        return {
            "next_action": "ERROR",
            "error_log": state.get("error_log", []) + [f"Planner LLM failed: {exc}"],
        }


# =============================================================================
# NODE 3 — SQL BRIDGE
# =============================================================================


def sql_bridge_node(state: AMLScenarioState, config: RunnableConfig) -> Dict[str, Any]:
    """Delegate SQL generation to PioTech AI text-to-SQL agent.

    Constructs a precise AML-context prompt, calls the DWH agent's
    streaming endpoint, and extracts the generated SQL + metadata.

    Args:
        state (AMLScenarioState): Current agent state.
        config (RunnableConfig): LangGraph runtime config.

    Returns:
        Dict[str, Any]: State updates with raw_sql, sql_metadata, next_action.
    """
    logger.info("[SQL_BRIDGE] Calling PioTech AI for SQL generation.")

    intent_dict = state.get("enriched_intent") or {}
    intent = AMLIntent(**intent_dict)

    # Construct a precise, context-rich prompt payload for the DWH agent
    # sending the raw serialized JSON intent for a truly domain-agnostic approach.
    payload_content = json.dumps(
        {"request_type": "AML_SCENARIO_GENERATION", "intent": intent_dict},
        indent=2,
        ensure_ascii=False,
    )

    chat_id = f"aml_builder_{uuid.uuid4().hex[:8]}"

    payload = {
        "messages": [{"role": "user", "content": payload_content}],
        "metadata": {
            "user_id": settings.PIOTECH_AI_USER_ID,
            "project_id": settings.PIOTECH_AI_PROJECT_ID,
            "chat_id": chat_id,
        },
        "reasoning_mode": "instant",
    }

    collected_text = []
    final_answer_text = None

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

        # Extract SQL: First attempt from final_answer_text, fallback to full collected stream content
        sql = ""
        if final_answer_text and final_answer_text.strip():
            sql = _extract_sql(final_answer_text)

        if not sql:
            full_stream_text = "".join(collected_text).strip()
            sql = _extract_sql(full_stream_text)

        if not sql:
            logger.error("[SQL_BRIDGE] No SQL found in PioTech AI response.")
            return {
                "next_action": "ERROR",
                "error_log": state.get("error_log", [])
                + ["SQL Bridge: PioTech AI did not return a valid SQL query."],
            }

        # Parse SQL metadata
        metadata = _parse_sql_metadata(sql)
        logger.info(
            "[SQL_BRIDGE] SQL extracted (%d chars). tables=%s",
            len(sql),
            metadata.tables,
        )

        return {
            "raw_sql": sql,
            "sql_metadata": metadata.model_dump(),
            "next_action": "DECOMPOSE",
        }

    except httpx.HTTPError as exc:
        logger.error("[SQL_BRIDGE] HTTP error calling PioTech AI: %s", exc)
        return {
            "next_action": "ERROR",
            "error_log": state.get("error_log", []) + [f"SQL Bridge HTTP error: {exc}"],
        }


def _is_real_sql(candidate: str) -> bool:
    """Helper to verify if a candidate string represents a genuine SQL query."""
    candidate_upper = candidate.upper()
    if "SELECT" not in candidate_upper or "FROM" not in candidate_upper:
        return False
    if candidate_upper.startswith("WITH"):
        # Ensure it matches common SQL CTE patterns, e.g. WITH cte_name AS
        if not re.search(r"\bWITH\s+[a-zA-Z0-9_\"#]+\s+AS\b", candidate, re.IGNORECASE):
            return False
    return True


def _extract_sql(text: str) -> str:
    """Extract a clean SQL query from LLM response text.

    Args:
        text (str): Raw text response from PioTech AI.

    Returns:
        str: The extracted SQL query, or empty string if none found.
    """
    if not text or not isinstance(text, str):
        return ""

    # 1. Find all code blocks delimited by ```sql ... ``` or ``` ... ```
    blocks = re.findall(r"```(?:sql)?\s*([\s\S]+?)```", text, re.IGNORECASE)
    if blocks:
        valid_blocks = []
        for block in blocks:
            block_clean = block.strip().rstrip(";").strip()
            if re.search(r"\b(SELECT|WITH)\b", block_clean, re.IGNORECASE):
                if _is_real_sql(block_clean):
                    valid_blocks.append(block_clean)
        if valid_blocks:
            # Pick the latest (last) valid block to ensure we get the final approved query
            return valid_blocks[-1]

    # 2. Fallback: if no code blocks found, extract raw WITH or SELECT statement
    # If WITH exists in text, prefer starting from WITH to capture full CTEs
    with_match = re.search(r"\bWITH\b", text, re.IGNORECASE)
    select_match = re.search(r"\bSELECT\b", text, re.IGNORECASE)

    start_idx = -1
    if with_match and select_match:
        start_idx = min(with_match.start(), select_match.start())
    elif with_match:
        start_idx = with_match.start()
    elif select_match:
        start_idx = select_match.start()

    if start_idx != -1:
        candidate = text[start_idx:].strip()

        # Stop at double newline after semicolon, markdown code block end, or end of string
        end_match = re.search(r";(?:\s*\n\s*\n|\s*$|\s*```)", candidate)
        if end_match:
            candidate = candidate[: end_match.start() + 1].strip()
        else:
            # Stop if explanation text starts after semicolon
            trailing_exp = re.search(r";\s*\n\s*[A-Za-z]{3,}\b", candidate)
            if trailing_exp:
                candidate = candidate[: trailing_exp.start() + 1].strip()

        candidate_clean = candidate.rstrip(";").strip()
        if _is_real_sql(candidate_clean):
            return candidate_clean

    return ""


def _remove_parenthesized_subqueries(sql_str: str) -> str:
    """Iteratively replace nested parenthesized expressions with a placeholder.

    This produces a simplified outer query skeleton so regex searches for
    outer WHERE, GROUP BY, and HAVING clauses do not get confused by inner
    subqueries (e.g. scalar subqueries in SELECT or HAVING).
    """
    prev = ""
    curr = sql_str
    while prev != curr:
        prev = curr
        curr = re.sub(r"\([^()]*\)", "__SUBQUERY__", curr)
    return curr


def _parse_sql_metadata(sql: str) -> SQLMetadata:
    """Parse a SQL query to extract structural metadata.

    Uses regex-based parsing (compatible without sqlglot as dependency).
    Extracts tables, WHERE conditions, GROUP BY fields, HAVING conditions.

    Args:
        sql (str): The SQL query string.

    Returns:
        SQLMetadata: Populated metadata object.
    """
    # Strip any trailing semicolons or whitespace from the query
    sql = sql.strip().rstrip(";").strip()

    # Create simplified skeleton to isolate outer query clauses from inner subqueries
    simplified_sql = _remove_parenthesized_subqueries(sql)

    # Extract tables (FROM and JOIN clauses)
    tables = re.findall(
        r"(?:FROM|JOIN)\s+(BI_DWH\.\w+|\w+\.\w+|\w+)",
        simplified_sql,
        re.IGNORECASE,
    )
    tables = list(dict.fromkeys(tables))  # deduplicate preserving order
    primary_table = tables[0] if tables else ""

    # Extract WHERE conditions
    where_match = re.search(
        r"WHERE\s+([\s\S]+?)(?:GROUP BY|HAVING|ORDER BY|$)",
        simplified_sql,
        re.IGNORECASE,
    )
    where_conditions = []
    if where_match:
        raw_where = where_match.group(1).strip()
        where_conditions = [
            c.strip()
            for c in re.split(r"\bAND\b|\bOR\b", raw_where, flags=re.IGNORECASE)
        ]
        where_conditions = [c for c in where_conditions if c and c != "__SUBQUERY__"]

    # Extract GROUP BY fields
    group_match = re.search(
        r"GROUP BY\s+([\s\S]+?)(?:HAVING|ORDER BY|$)", simplified_sql, re.IGNORECASE
    )
    group_by_fields = []
    if group_match:
        group_by_fields = [
            f.strip() for f in group_match.group(1).split(",") if f.strip()
        ]

    # Extract HAVING conditions
    having_match = re.search(
        r"HAVING\s+([\s\S]+?)(?:ORDER BY|$)", simplified_sql, re.IGNORECASE
    )
    having_conditions = []
    if having_match:
        raw_having = having_match.group(1).strip()
        having_conditions = [
            c.strip()
            for c in re.split(r"\bAND\b|\bOR\b", raw_having, flags=re.IGNORECASE)
        ]
        having_conditions = [c for c in having_conditions if c]

    # Detect date fields from original sql
    date_fields = re.findall(r"\b(\w*DATE\w*|\w*TIME\w*|\w*DT\b)\b", sql, re.IGNORECASE)
    date_fields = list(set(date_fields))

    # Detect aggregations from original sql
    aggregations = re.findall(r"\b(COUNT|SUM|AVG|MAX|MIN)\s*\(", sql, re.IGNORECASE)
    aggregations = list(set(agg.upper() for agg in aggregations))

    return SQLMetadata(
        raw_sql=sql,
        tables=tables,
        primary_table=primary_table,
        where_conditions=where_conditions,
        group_by_fields=group_by_fields,
        having_conditions=having_conditions,
        date_fields=date_fields,
        aggregations=aggregations,
    )


# =============================================================================
# NODE 4 — DIRECT SHADOW EXECUTOR (Production DWH Customer-Hash Testing)
# =============================================================================


def build_shadow_query(clean_sql: str, key_col: str = "CUS_NUM") -> str:
    """Build a robust shadow count wrapper query for Oracle DWH.

    Uses inline-view pattern when clean_sql starts with WITH to avoid ORA-32034.
    Uses single CTE pattern when clean_sql is a standard SELECT statement.
    """
    clean_sql = clean_sql.strip().rstrip(";")
    if clean_sql.upper().startswith("WITH"):
        return f"""
        SELECT 
            (SELECT COUNT(DISTINCT {key_col}) FROM ( {clean_sql} ) Raw_Scenario) AS header_alert_count,
            (SELECT COUNT(*) FROM ( {clean_sql} ) Raw_Scenario) AS detail_record_count
        FROM DUAL
        """
    else:
        return f"""
        WITH Raw_Scenario AS (
            {clean_sql}
        )
        SELECT 
            (SELECT COUNT(DISTINCT {key_col}) FROM Raw_Scenario) AS header_alert_count,
            (SELECT COUNT(*) FROM Raw_Scenario) AS detail_record_count
        FROM DUAL
        """


def direct_shadow_executor_node(
    state: AMLScenarioState, config: RunnableConfig
) -> Dict[str, Any]:
    """Execute production SQL query directly on Oracle DWH with Customer-Hash Sampling."""
    raw_sql = state.get("raw_sql")
    if not raw_sql or not raw_sql.strip():
        logger.error("[SHADOW_EXECUTOR] Raw SQL is missing or empty.")
        return {
            "next_action": "ERROR",
            "error_log": state.get("error_log", [])
            + ["Shadow executor received empty SQL query."],
        }

    logger.info("[SHADOW_EXECUTOR] Preparing customer-hash shadow test query.")

    clean_sql = raw_sql.strip()
    clean_sql = re.sub(r"^```(?:sql)?\s*", "", clean_sql, flags=re.IGNORECASE)
    clean_sql = re.sub(r"\s*```$", "", clean_sql)
    clean_sql = clean_sql.rstrip(";").strip()

    try:
        from web.services.oracle import get_connection, run_readonly
        from web.services.schemas import AlertSample, ValidationResult

        # 1. Dynamically inspect column names of the generated query (using inline-view to avoid ORA-32034)
        test_col_query = f"SELECT * FROM ( {clean_sql} ) Raw_Scenario WHERE ROWNUM <= 1"
        entity_col = "CUS_NUM"
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(test_col_query)
            if cursor.description:
                available_cols = [c[0].upper() for c in cursor.description]
                possible_matches = ["CUS_NUM", "CUSTOMER_ID", "CUS_ID", "CUST_ID", "CUSTOMER_NUMBER", "CUS_NO"]
                matched = next((c for c in possible_matches if c in available_cols), None)
                if matched:
                    entity_col = matched
                elif available_cols:
                    entity_col = available_cols[0]

        logger.info("[SHADOW_EXECUTOR] Using entity column '%s' for header counting.", entity_col)

        # 2. Run shadow count query using build_shadow_query
        shadow_count_query = build_shadow_query(clean_sql, entity_col)

        _, count_rows = run_readonly(shadow_count_query)
        sample_header_count = int(count_rows[0][0]) if count_rows and count_rows[0][0] is not None else 0
        detail_count = int(count_rows[0][1]) if count_rows and count_rows[0][1] is not None else 0

        extrapolated_alerts = sample_header_count * 20
        alert_density = (
            round(detail_count / sample_header_count, 2)
            if sample_header_count > 0
            else 0.0
        )

        logger.info(
            "[SHADOW_EXECUTOR] Shadow test completed. Sample Customers: %d, Extrapolated Alerts: %d, Details: %d, Density: %s",
            sample_header_count,
            extrapolated_alerts,
            detail_count,
            alert_density,
        )

        # 3. Retrieve top 5 sample alert records (using inline-view to avoid ORA-32034)
        sample_records_query = f"SELECT * FROM ( {clean_sql} ) Raw_Scenario WHERE ROWNUM <= 5"
        sample_alerts: List[AlertSample] = []
        try:
            with get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(sample_records_query)
                if cursor.description:
                    cols = [c[0].lower() for c in cursor.description]
                    for r in cursor.fetchall():
                        row_dict = dict(zip(cols, r))
                        cus_id = str(
                            row_dict.get(entity_col.lower(), row_dict.get("cus_num", row_dict.get("customer_id", "—")))
                        )
                        sample_alerts.append(
                            AlertSample(customer_id=cus_id, raw_data=row_dict)
                        )
        except Exception as sample_exc:
            logger.warning("[SHADOW_EXECUTOR] Could not fetch sample alert rows: %s", sample_exc)

        val_result = ValidationResult(
            success=True if sample_header_count >= 1 else False,
            scenario_status="SHADOW_TESTED" if sample_header_count >= 1 else "REVIEW_NEEDED",
            scenario_code=state.get("scenario_code") or "PENDING",
            raw_sql=clean_sql,
            header_alert_count=sample_header_count,
            transaction_detail_count=detail_count,
            alert_density_ratio=alert_density if sample_header_count > 0 else None,
            sample_alerts=sample_alerts,
            diagnosis=(
                None
                if sample_header_count >= 1
                else "Zero alerts generated in shadow test. Consider widening threshold values or extending time window."
            ),
            suggested_fix=(
                None
                if sample_header_count >= 1
                else "Widen threshold values or extend time window in scenario request."
            ),
        ).model_dump(mode="json")

        return {
            "validation_result": val_result,
            "scenario_write_success": True,
            "next_action": "WAIT_USER",
        }

    except Exception as exc:
        err_msg = str(exc)
        logger.error(
            "[SHADOW_EXECUTOR] Direct shadow query execution failed: %s",
            err_msg,
            exc_info=True,
        )
        failed_val_result = ValidationResult(
            success=False,
            scenario_status="ERROR",
            scenario_code=state.get("scenario_code") or "PENDING",
            raw_sql=clean_sql,
            diagnosis=f"Oracle DWH Execution Error: {err_msg}",
            suggested_fix="Review generated SQL syntax, table joins, or column definitions.",
        ).model_dump(mode="json")

        return {
            "validation_result": failed_val_result,
            "next_action": "FAILURE",
            "error_log": state.get("error_log", []) + [f"Shadow execution failed: {err_msg}"],
        }


# =============================================================================
# NODE 5 — PRODUCTION SCENARIO PERSISTER
# =============================================================================


def production_scenario_persister_node(
    state: AMLScenarioState, config: RunnableConfig
) -> Dict[str, Any]:
    """Persist confirmed scenario to PIO_AML_PRODUCTION_SCENARIOS table for daily ETL runner."""
    raw_sql = state.get("raw_sql")
    intent_dict = state.get("enriched_intent") or {}

    if not raw_sql:
        logger.error("[PRODUCTION_PERSISTER] Cannot persist scenario: Raw SQL is missing.")
        return {
            "next_action": "ERROR",
            "error_log": state.get("error_log", [])
            + ["Production persister received missing SQL."],
        }

    try:
        from web.services.schemas import AMLIntent

        intent = AMLIntent(**intent_dict)
    except Exception:
        intent = None

    import uuid

    scenario_code = state.get("scenario_code") or f"PRD_{uuid.uuid4().hex[:8].upper()}"
    scenario_name = intent.scenario_name if intent else "AML Scenario"
    scenario_type = intent.scenario_type if intent else "CUSTOMER"
    detection_logic = intent.detection_ideology if intent else ""
    time_window_days = (
        intent.time_window.value if (intent and intent.time_window) else 30
    )

    from web.services.production_registry import save_production_scenario

    success = save_production_scenario(
        scenario_id=scenario_code,
        scenario_name=scenario_name,
        scenario_type=scenario_type,
        detection_logic=detection_logic,
        raw_sql=raw_sql,
        time_window_days=time_window_days,
        created_by="COMPLIANCE_OFFICER",
    )

    if success:
        logger.info(
            "[PRODUCTION_PERSISTER] Scenario %s committed to production ETL registry.",
            scenario_code,
        )
        return {
            "scenario_code": scenario_code,
            "scenario_write_success": True,
            "next_action": "FINALIZE",
        }
    else:
        logger.error(
            "[PRODUCTION_PERSISTER] Failed to commit scenario %s.", scenario_code
        )
        return {
            "scenario_write_success": False,
            "next_action": "FAILURE",
            "error_log": state.get("error_log", [])
            + [f"Failed to persist scenario {scenario_code} in production registry."],
        }
# =============================================================================
# ROUTING FUNCTIONS
# =============================================================================


def route_after_orchestrator(state: AMLScenarioState) -> str:
    """Route after the agentic orchestrator.

    Args:
        state (AMLScenarioState): Current state.

    Returns:
        str: LangGraph node name or END.
    """
    action = state.get("next_action", "INTENT")
    route_map = {
        "INTENT": "intent_analyst",
        "SQL_BRIDGE": "sql_bridge",
        "PERSIST": "production_scenario_persister",
        "WAIT_USER": END,
        "WAIT_APPROVAL": END,
        "END": END,
    }
    return route_map.get(action, END)


def route_after_intent(state: AMLScenarioState) -> str:
    """Route after Intent Analyst node.

    If domain explanation codes were discovered and checkpoint is active: route to orchestrator first!
    Routes to planner on success (user must approve the plan before execution).
    Routes to orchestrator on clarification requests or errors.
    """
    action = state.get("next_action", "SQL_BRIDGE")
    checkpoint = state.get("explanation_code_checkpoint")
    confirmed = state.get("explanation_codes_confirmed", False)

    if checkpoint and not confirmed:
        logger.info("[ROUTER] Domain explanation code checkpoint active — routing to orchestrator.")
        return "orchestrator"

    if action in ("CLARIFY", "CONFIRM_DOMAIN", "ERROR"):
        return "orchestrator"

    return "planner"


def route_after_planner(state: AMLScenarioState) -> str:
    """Route after the Planner node.

    On success: returns END to pause execution and wait for user approval.
    On error: routes to orchestrator for failure handling.
    """
    action = state.get("next_action", "WAIT_APPROVAL")
    if action == "ERROR":
        return "orchestrator"
    return END  # Pause execution and wait for user approval


def route_after_sql_bridge(state: AMLScenarioState) -> str:
    """Route after SQL Bridge node.

    Routes to direct_shadow_executor for customer-hash shadow testing on DWH.
    """
    action = state.get("next_action", "SHADOW_TEST")
    if action in ("ERROR", "FAILURE"):
        return "orchestrator"
    return "direct_shadow_executor"


def route_after_shadow_executor(state: AMLScenarioState) -> str:
    """Route after Direct Shadow Executor node back to orchestrator."""
    return "orchestrator"


def route_after_persister(state: AMLScenarioState) -> str:
    """Route after Production Scenario Persister node back to orchestrator."""
    return "orchestrator"


# =============================================================================
# GRAPH BUILDER
# =============================================================================


# Module-level singletons for graph and checkpointer connection
_graph = None
_checkpointer_conn = None


async def build_graph() -> Any:
    """Construct and compile the AML Scenario StateGraph.

    Returns:
        CompiledGraph: The compiled LangGraph ready for invocation.
    """
    import aiosqlite
    from pathlib import Path

    global _checkpointer_conn

    # Ensure checkpoint directory exists
    checkpoint_path = settings.CHECKPOINT_DB_PATH
    Path(checkpoint_path).parent.mkdir(parents=True, exist_ok=True)

    # Use aiosqlite for async checkpointing
    _checkpointer_conn = await aiosqlite.connect(checkpoint_path)
    checkpointer = AsyncSqliteSaver(_checkpointer_conn)

    graph = StateGraph(AMLScenarioState)

    # Register nodes
    graph.add_node("orchestrator", orchestrator_node)
    graph.add_node("intent_analyst", intent_analyst_node)
    graph.add_node("planner", planner_node)
    graph.add_node("sql_bridge", sql_bridge_node)
    graph.add_node("direct_shadow_executor", direct_shadow_executor_node)
    graph.add_node("production_scenario_persister", production_scenario_persister_node)

    # Entry point
    graph.set_entry_point("orchestrator")

    # Edges with conditional routing
    graph.add_conditional_edges("orchestrator", route_after_orchestrator)
    graph.add_conditional_edges("intent_analyst", route_after_intent)
    graph.add_conditional_edges("planner", route_after_planner)
    graph.add_conditional_edges("sql_bridge", route_after_sql_bridge)
    graph.add_conditional_edges("direct_shadow_executor", route_after_shadow_executor)
    graph.add_conditional_edges("production_scenario_persister", route_after_persister)

    return graph.compile(checkpointer=checkpointer)


async def get_graph() -> Any:
    """Return the singleton compiled graph (lazy init).

    Returns:
        CompiledGraph: The compiled LangGraph.
    """
    global _graph
    if _graph is None:
        _graph = await build_graph()
        logger.info("[AGENT] LangGraph compiled and ready.")
    return _graph


async def close_checkpointer() -> None:
    """Close the SQLite checkpointer connection if open."""
    global _checkpointer_conn
    if _checkpointer_conn is not None:
        try:
            await _checkpointer_conn.close()
            logger.info("[AGENT] Checkpointer database connection closed.")
        except Exception as exc:
            logger.error(
                "[AGENT] Error closing checkpointer database connection: %s", exc
            )
        _checkpointer_conn = None


# =============================================================================
# HELPERS
# =============================================================================


def _format_history(messages: List[BaseMessage], n: int = 3) -> str:
    """Format the last N messages for inclusion in prompts.

    Args:
        messages (List[BaseMessage]): Full message history.
        n (int): Number of recent messages to include. Defaults to 3.

    Returns:
        str: Formatted string of recent conversation turns.
    """
    recent = messages[-n:] if len(messages) > n else messages
    lines = []
    for m in recent:
        role = "User" if isinstance(m, HumanMessage) else "Assistant"
        content = m.content if isinstance(m.content, str) else str(m.content)
        lines.append(f"{role}: {content[:300]}")
    return "\n".join(lines) if lines else "No prior history."
