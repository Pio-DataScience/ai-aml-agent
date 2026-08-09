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

    # Domain Explanation Codes Discovery Layer
    explanation_code_checkpoint: Optional[str]
    discovered_explanation_codes: Optional[List[Dict[str, Any]]]
    explanation_codes_confirmed: bool


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

    expl_checkpoint = state.get("explanation_code_checkpoint")
    expl_block = ""
    if expl_checkpoint and not state.get("explanation_codes_confirmed", False):
        expl_block = f'\n\n**DISCOVERED DOMAIN EXPLANATION CODES TABLE (Present this full formatted table to the user for review in your message_to_user):**\n{expl_checkpoint}\n'

    return (
        table
        + expl_block
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
            else:
                decision.message_to_user = (
                    "Please provide more details about the scenario you want to build."
                )

    logger.info(
        "[ORCHESTRATOR] Decision → next_action=%s  message=%s",
        decision.next_action,
        "yes" if decision.message_to_user else "none (silent routing)",
    )

    updates: Dict[str, Any] = {"iteration_count": iteration}

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

    checkpoint = state.get("explanation_code_checkpoint")
    confirmed = state.get("explanation_codes_confirmed", False)

    if checkpoint and not confirmed:
        raw_msgs = state.get("messages", [])
        last_msg_lower = (
            raw_msgs[-1].content.lower()
            if raw_msgs and isinstance(raw_msgs[-1], HumanMessage)
            else ""
        )
        confirm_keywords = [
            "confirm",
            "confirm codes",
            "yes",
            "proceed",
            "looks good",
            "ok",
            "use these",
        ]
        if any(k in last_msg_lower for k in confirm_keywords):
            logger.info("[ORCHESTRATOR] User confirmed domain explanation codes!")
            updates["explanation_codes_confirmed"] = True
        else:
            logger.info(
                "[ORCHESTRATOR] Presenting Checkpoint 1 explanation code table view."
            )
            decision.next_action = "WAIT_USER"
            if (
                not decision.message_to_user
                or "Discovered Transaction Types" not in decision.message_to_user
            ):
                decision.message_to_user = f"{checkpoint}"

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
    """Build a structured markdown escalation report for the implementation team.

    Includes everything needed to reproduce and diagnose the failure:
    scenario intent, generated parameters, Oracle errors, catalog provisions,
    write and validation results, and timestamps.

    Args:
        state (AMLScenarioState): Current agent state at time of escalation.

    Returns:
        str: Full markdown escalation report.
    """
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    intent_dict = state.get("enriched_intent") or {}
    scenario_code = state.get("scenario_code", "Not generated")
    error_log = state.get("error_log", [])
    catalog_creations = state.get("catalog_creations") or []
    params_dict = state.get("scenario_parameters") or {}
    write_verification = state.get("write_verification") or {}
    validation_result = state.get("validation_result") or {}

    errors_text = "\n".join(f"- {e}" for e in error_log) or "_No errors logged._"
    catalog_text = (
        "\n".join(
            f"- **{c.get('entity_type')}** `{c.get('code')}`: "
            f"{c.get('name')} ({c.get('business_name')})"
            for c in catalog_creations
        )
        or "_None — all parameters were pre-existing in the catalog._"
    )

    raw_sql = state.get("raw_sql")
    sql_text = f"```sql\n{raw_sql}\n```" if raw_sql else "_No SQL query was generated._"

    report_content = (
        f"# AML Scenario Escalation Report\n\n"
        f"**Generated:** {now}  \n"
        f"**Scenario Code:** `{scenario_code}`  \n"
        f"**System:** PioTech AML Builder — Automated Agent\n\n"
        f"---\n\n"
        f"## Intent Submitted\n\n"
        f"```json\n{json.dumps(intent_dict, indent=2, default=str, ensure_ascii=False)}\n```\n\n"
        f"---\n\n"
        f"## Generated SQL Query\n\n"
        f"{sql_text}\n\n"
        f"---\n\n"
        f"## Error Log (Chronological)\n\n"
        f"{errors_text}\n\n"
        f"---\n\n"
        f"## Catalog Auto-Provisions Attempted\n\n"
        f"{catalog_text}\n\n"
        f"---\n\n"
        f"## Scenario Parameters Generated\n\n"
        f"```json\n{json.dumps(params_dict, indent=2, default=str, ensure_ascii=False)}\n```\n\n"
        f"---\n\n"
        f"## Write Verification Results\n\n"
        f"```json\n{json.dumps(write_verification, indent=2, default=str, ensure_ascii=False)}\n```\n\n"
        f"---\n\n"
        f"## Validation Results\n\n"
        f"```json\n{json.dumps(validation_result, indent=2, default=str, ensure_ascii=False)}\n```\n\n"
        f"---\n\n"
        f"_This report was generated automatically by the AML Builder agent._  \n"
        f"_Please reference Scenario Code `{scenario_code}` in all correspondence._"
    )

    # Persist the escalation report dynamically using the SOLID DatePartitionedFilePersister
    try:
        from web.services.persister import DatePartitionedFilePersister

        persister = DatePartitionedFilePersister()
        persister.persist(scenario_code, report_content)
    except Exception as exc:
        logger.error("[CATALOG] Failed to persistently save escalation report: %s", exc)

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

        # Strip any accidental markdown fences
        raw_content = re.sub(r"^```(?:json)?\s*", "", raw_content)
        raw_content = re.sub(r"\s*```$", "", raw_content)

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
        elif (state.get("enriched_intent") or {}).get("explanation_codes"):
            intent_dict["explanation_codes"] = (state.get("enriched_intent") or {})["explanation_codes"]

        # Validate with Pydantic
        intent = AMLIntent(**intent_dict)

        # `ready_for_handoff` is the primary gate; fall back to the legacy
        # `clarification_needed` flag if the model only populated the old field.
        needs_clarify = (not intent.ready_for_handoff) or intent.clarification_needed
        if bool(intent.clarifications) and intent.ready_for_handoff:
            # Structured clarifications present but flag not set — trust the list.
            needs_clarify = True

        logger.info(
            "[INTENT_ANALYST] Intent parsed. scenario_type=%s ready_for_handoff=%s "
            "clarifications=%d",
            intent.scenario_type,
            intent.ready_for_handoff,
            len(intent.clarifications),
        )

        next_action = "CLARIFY" if needs_clarify else "SQL_BRIDGE"

        return {
            "enriched_intent": intent.model_dump(),
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
# NODE 4 — DECOMPOSER
# =============================================================================


def decomposer_node(state: AMLScenarioState, config: RunnableConfig) -> Dict[str, Any]:
    """Map SQL metadata to QB Oracle table parameters.

    Converts the structured SQLMetadata into a complete ScenarioParameters
    object ready for insertion into the 4 Oracle AML tables.

    Uses a live PIO_AML_PARAMETERS catalog lookup + LLM-assisted mapping
    to select the correct PARAMETER_CODE for each condition. All field
    defaults match the reference scenario_creation.md template.

    Args:
        state (AMLScenarioState): Current agent state.
        config (RunnableConfig): LangGraph runtime config.

    Returns:
        Dict[str, Any]: State updates with scenario_parameters and next_action.
    """
    logger.info("[DECOMPOSER] Mapping SQL to QB parameters.")

    try:
        intent_dict = state.get("enriched_intent") or {}
        sql_meta_dict = state.get("sql_metadata") or {}
        if not sql_meta_dict:
            raise ValueError(
                "SQL metadata is missing or empty. PioTech AI may have failed to return a valid SQL query."
            )

        intent = AMLIntent(**intent_dict)
        sql_meta = SQLMetadata(**sql_meta_dict)

        # Reuse codes if they already exist in the state (validation retry loop)
        scenario_code = state.get("scenario_code")
        rule_code = None

        scenario_parameters = state.get("scenario_parameters")
        if scenario_parameters and "rules" in scenario_parameters:
            rules = scenario_parameters["rules"]
            if rules and len(rules) > 0:
                rule_code = rules[0].get("rule_code")

        import random

        if not scenario_code:
            import time

            epoch_ms = int(time.time() * 1000)
            rand_suffix = random.randint(100, 999)
            scenario_code = f"{epoch_ms}{rand_suffix}"

        if not rule_code:
            timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
            rule_code = f"{timestamp}{random.randint(100, 999)}"

        # Query period types from PIO_PERIOD_TYPE
        period_type_rows = []
        try:
            from web.services.oracle import run_readonly

            _, period_rows = run_readonly(
                "SELECT PERIOD_CODE, DES_ENG, NO_OF_DAYS FROM PIO_PERIOD_TYPE WHERE CB_CODE = :ic OR CB_CODE IS NULL",
                {"ic": settings.AML_INST_CODE},
            )
            for row in period_rows:
                period_type_rows.append(
                    {
                        "period_code": str(row[0]).strip(),
                        "des_eng": str(row[1]).strip(),
                        "no_of_days": int(row[2]) if row[2] is not None else None,
                    }
                )
        except Exception as exc:
            logger.error("[DECOMPOSER] Failed to query PIO_PERIOD_TYPE: %s", exc)

        # Query risk degrees from PIO_AML_DEGREE_RISK
        risk_level_rows = []
        try:
            from web.services.oracle import run_readonly

            _, risk_rows = run_readonly(
                "SELECT DEGREE_CODE, DESC_ENG FROM PIO_AML_DEGREE_RISK WHERE COUNTRY_CODE = :cc AND INST_CODE = :ic",
                {"cc": settings.AML_COUNTRY_CODE, "ic": settings.AML_INST_CODE},
            )
            for row in risk_rows:
                risk_level_rows.append(
                    {
                        "degree_code": str(row[0]).strip(),
                        "desc_eng": str(row[1]).strip(),
                    }
                )
        except Exception as exc:
            logger.error("[DECOMPOSER] Failed to query PIO_AML_DEGREE_RISK: %s", exc)

        # Determine PERIOD_TYPE (PIO_PERIOD_TYPE code) and PERIOD_DAYS from intent.
        period_type = "0"  # default: Last n Days
        period_days = 30  # sensible default
        degree_code = settings.AML_DEFAULT_VIOLATION_LEVEL or "H"

        if period_type_rows and risk_level_rows:
            try:
                tw_unit = intent.time_window.unit if intent.time_window else "DAYS"
                tw_value = intent.time_window.value if intent.time_window else 30
                tw_rolling = (
                    intent.time_window.is_rolling if intent.time_window else True
                )

                period_types_str = "\n".join(
                    [
                        f"- Code: {p['period_code']}, Name: {p['des_eng']}, Default Days: {p['no_of_days']}"
                        for p in period_type_rows
                    ]
                )
                risk_degrees_str = "\n".join(
                    [
                        f"- Code: {r['degree_code']}, Description: {r['desc_eng']}"
                        for r in risk_level_rows
                    ]
                )

                llm = _build_llm(fast=True)
                prompt = (
                    "You are an expert system mapping Compliance Scenario settings to database lookup codes.\n\n"
                    "1. TIME WINDOW SYSTEM:\n"
                    f"Target Time Window: Unit={tw_unit}, Value={tw_value}, Rolling={tw_rolling}\n"
                    "Available Period Types:\n"
                    f"{period_types_str}\n\n"
                    "2. RISK LEVEL SYSTEM:\n"
                    f"Scenario Name: {intent.scenario_name}\n"
                    f"Detection Logic: {intent.detection_logic}\n"
                    "Available Risk Degrees:\n"
                    f"{risk_degrees_str}\n\n"
                    "INSTRUCTIONS:\n"
                    "- Match the Target Time Window to the most appropriate 'period_code' in the Available Period Types.\n"
                    "  - If it matches a specific frequency (Daily, Weekly, Monthly, Yearly), use that code.\n"
                    "  - Otherwise, default to '0' (Last n Days) or a custom code if more appropriate.\n"
                    "  - Compute 'period_days' as the number of days represented by the window (e.g., 30 for 1 Month, 365 for 1 Year, or the value itself if DAYS).\n"
                    "- Analyze the Scenario Name and Detection Logic to determine the risk level (e.g. HIGH, MEDIUM, LOW) and match it to 'degree_code' from the Available Risk Degrees.\n"
                    "  - If not explicitly mentioned, default to High ('H') or Medium ('M') contextually.\n"
                    "3. Respond ONLY with a valid JSON object matching the following schema. Do not write any explanations, markdown code blocks, or extra text:\n"
                    "{\n"
                    '  "period_code": "<matching period_code>",\n'
                    '  "period_days": <integer number of days>,\n'
                    '  "degree_code": "<matching degree_code>",\n'
                    '  "reasoning": "<brief explanation of your decision>"\n'
                    "}\n"
                )

                response = llm.invoke(prompt)
                response_text = str(response.content).strip()

                def _extract_json(text: str) -> Optional[dict]:
                    match = re.search(
                        r"```(?:json)?\s*([\s\S]+?)```", text, re.IGNORECASE
                    )
                    if match:
                        try:
                            return json.loads(match.group(1).strip())
                        except Exception:
                            pass
                    try:
                        return json.loads(text.strip())
                    except Exception:
                        pass
                    start = text.find("{")
                    end = text.rfind("}")
                    if start != -1 and end != -1:
                        try:
                            return json.loads(text[start : end + 1].strip())
                        except Exception:
                            pass
                    return None

                res_json = _extract_json(response_text)
                if res_json:
                    p_code = res_json.get("period_code")
                    if p_code is not None:
                        period_type = str(p_code).strip()

                    p_days = res_json.get("period_days")
                    if p_days is not None:
                        try:
                            period_days = int(p_days)
                        except Exception:
                            pass

                    d_code = res_json.get("degree_code")
                    if d_code is not None:
                        degree_code = str(d_code).strip()

                    logger.info(
                        "[DECOMPOSER] Grounded Period Type to '%s' (%s days) and Degree Code to '%s'. Reasoning: %s",
                        period_type,
                        str(period_days),
                        degree_code,
                        res_json.get("reasoning"),
                    )
            except Exception as exc:
                logger.error(
                    "[DECOMPOSER] LLM grounding lookup failed: %s. Using defaults.", exc
                )
        else:
            # Simple python mapping fallback
            if intent.time_window:
                tw = intent.time_window
                period_days = tw.value
                unit_upper = tw.unit.upper()
                if unit_upper == "DAYS":
                    period_type = "0"
                elif unit_upper == "MONTHS":
                    period_type = "3"
                    period_days = tw.value * 30
                elif unit_upper == "YEARS":
                    period_type = "6"
                    period_days = tw.value * 365

        now = datetime.utcnow()

        # Build scenario header — every column in PIO_AML_SCENARIO.
        scenario = QBScenario(
            country_code=settings.AML_COUNTRY_CODE,
            inst_code=settings.AML_INST_CODE,
            scenario_code=scenario_code,
            scenario_des_eng=intent.scenario_name,
            scenario_des_nat_lan=intent.scenario_name,  # duplicate ENG for native lang
            active_flag=settings.AML_DEFAULT_ACTIVE_FLAG,
            exclude_expl_flag="0",
            use_watchlist_flag="0",
            violation_level=degree_code,
            degree_risk_flag=settings.AML_DEFAULT_DEGREE_RISK_FLAG,
            default_scenario_flag="0",
            run_flag=settings.AML_RUN_FLAG,
            approval_flag=settings.AML_APPROVAL_FLAG,
            group_by_flag=settings.AML_GROUP_BY_FLAG,
            use_worldcheck_flag="0",
            created_by=settings.AML_CREATED_BY,
            created_date=now,
            updated_by=settings.AML_CREATED_BY,
            updated_date=now,
            trans_withouttrans_flag=settings.AML_TRANS_WITHOUTTRANS_FLAG,
            category_code=settings.AML_CATEGORY_CODE,
            active_threshold_curr_flag="0",
            sce_type_code=settings.AML_SCE_TYPE_CODE,
            class_code=settings.AML_CLASS_CODE,
        )

        desc = f"Rule for {intent.scenario_name}"

        # Build rule — every column in PIO_AML_RULES.
        rule = QBRule(
            country_code=settings.AML_COUNTRY_CODE,
            inst_code=settings.AML_INST_CODE,
            rule_code=rule_code,
            rule_desc_eng=desc,
            rule_desc_arb=desc,  # duplicate ENG for Arabic column
            active_flag=settings.AML_DEFAULT_ACTIVE_FLAG,
            period_type=period_type,
            period_days=period_days,
            use_sequentially_flag="0",
            sequentially_count=0,
            default_rule_flag="0",
            exclude_days=0,
            ltg_code=None,
            created_by=settings.AML_CREATED_BY,
            created_date=now,
            updated_by=settings.AML_CREATED_BY,
            updated_date=now,
        )

        # Build scenario-rule link — every column in PIO_AML_SCENARIO_RULES.
        scenario_rule = QBScenarioRule(
            country_code=settings.AML_COUNTRY_CODE,
            inst_code=settings.AML_INST_CODE,
            aml_rule_code=rule_code,
            aml_scenario=scenario_code,
            rule_seq="1",
            rule_type=settings.AML_RULE_TYPE,  # '3' = standalone
            frequency_days=0,
            amt_perc=0.0,
            margin_perc=0.0,
            stop_period=0,
        )

        # Use live PIO_AML_PARAMETERS catalog + auto-provisioning to map conditions.
        existing_creations: List[Dict[str, Any]] = list(
            state.get("catalog_creations") or []
        )
        rule_details = _fetch_and_map_parameters(
            intent=intent,
            sql_meta=sql_meta,
            rule_code=rule_code,
            scenario_code=scenario_code,
            created_date=now,
            catalog_creations=existing_creations,
        )

        if not rule_details:
            raise ValueError(
                "Could not map any conditions to Query Builder parameters. "
                "The scenario must define at least one valid threshold or condition "
                "that maps to the PIO_AML_PARAMETERS catalog."
            )

        # Verify auto-provisioned catalog entries are reachable via QB join path
        if existing_creations:
            catalog_ok = _verify_catalog_integrity(existing_creations)
            if not catalog_ok:
                raise ValueError(
                    "Catalog integrity check failed: one or more auto-created parameters "
                    "are not reachable via the PIO_AML_PARAMETERS → PIO_AML_COLUMNS join path. "
                    "Check the auto-provisioned PIO_AML_COLUMNS entries."
                )

        # Self-assess decomposition confidence
        confidence = _assess_confidence(intent, rule_details)

        notes = [
            f"Scenario code: {scenario_code}",
            f"Rule code: {rule_code}",
            f"Period type: {period_type} ({period_days} days)",
            f"Conditions mapped: {len(rule_details)}",
            f"Confidence: {confidence:.2f}",
        ]

        params = ScenarioParameters(
            scenario=scenario,
            rules=[rule],
            scenario_rules=[scenario_rule],
            rule_details=rule_details,
            decomposition_confidence=confidence,
            decomposition_notes=notes,
        )

        logger.info(
            "[DECOMPOSER] Parameters built. scenario_code=%s confidence=%.2f conditions=%d",
            scenario_code,
            confidence,
            len(rule_details),
        )

        return {
            "scenario_parameters": params.model_dump(mode="json"),
            "decomposition_confidence": confidence,
            "scenario_code": scenario_code,
            "catalog_creations": existing_creations,
            "next_action": "QB_WRITE",
        }

    except Exception as exc:
        logger.error("[DECOMPOSER] Failed to decompose: %s", exc, exc_info=True)
        return {
            "next_action": "FAILURE",
            "error_log": state.get("error_log", []) + [f"Decomposition failed: {exc}"],
        }


def _query_oracle_column_type(table_name: str, column_name: str) -> Optional[str]:
    """Query Oracle's ALL_TAB_COLUMNS for the physical data type of a column.

    COLUMN_TYPE must never be LLM-inferred — it is always read from the DB schema.
    Returns None if the column is not found.

    Args:
        table_name (str): Physical Oracle table name (e.g., 'PIO_TRANSACTIONS').
        column_name (str): Physical column name (e.g., 'TRA_AMT').

    Returns:
        Optional[str]: Oracle DATA_TYPE string (e.g., 'NUMBER', 'VARCHAR2(40)', 'DATE')
            or None if not found in ALL_TAB_COLUMNS.
    """
    from web.services.oracle import run_readonly

    try:
        _, rows = run_readonly(
            """
            SELECT DATA_TYPE, DATA_LENGTH
            FROM ALL_TAB_COLUMNS
            WHERE UPPER(TABLE_NAME) = UPPER(:table_name)
              AND UPPER(COLUMN_NAME) = UPPER(:column_name)
            """,
            {"table_name": table_name, "column_name": column_name},
        )
        if rows:
            data_type = str(rows[0][0]).strip()
            data_length = rows[0][1]
            if data_length is not None and str(data_length) not in ("None", "0"):
                return f"{data_type}({data_length})"
            return data_type
        logger.warning(
            "[CATALOG] Column %s.%s not found in ALL_TAB_COLUMNS.",
            table_name,
            column_name,
        )
        return None
    except Exception as exc:
        logger.error("[CATALOG] ALL_TAB_COLUMNS query failed: %s", exc)
        return None


def _infer_column_metadata(
    col_name: str, col_type: str, table_name: str
) -> Dict[str, Any]:
    """Use the LLM to infer business metadata for a new catalog column.

    Infers: COLUMN_BUSINESS_NAME, COLUMN_BUSINESS_NAME_NAT (Arabic),
    AGGREGATION_CODE, SD_USED_FLAG, BALANCE_FLAG.
    COLUMN_TYPE is explicitly excluded — it is passed in, never inferred.

    Args:
        col_name (str): Physical column name (e.g., 'DAILY_LIMIT').
        col_type (str): Actual Oracle data type from ALL_TAB_COLUMNS.
        table_name (str): Table this column belongs to.

    Returns:
        Dict[str, Any]: Inferred metadata keys with safe defaults for any LLM failure.
    """
    llm = _build_llm(fast=True)
    prompt = (
        f"You are an AML compliance metadata expert for a banking system.\n\n"
        f"A new column needs to be registered in the AML parameter catalog.\n\n"
        f"Column: {col_name}\n"
        f"Table: {table_name}\n"
        f"Oracle Type: {col_type}\n\n"
        f"Return a JSON object with EXACTLY these fields:\n{{\n"
        f'  "column_business_name": "<clear English business name, max 40 chars>",\n'
        f'  "column_business_name_nat": "<Arabic translation, max 200 chars>",\n'
        f'  "aggregation_code": "<1=None/Direct, 2=Count, 3=Sum, 4=Average, 5=StdDev — '
        f'pick based on column type and name>",\n'
        f'  "sd_used_flag": "<0 or 1 — 1 only if this is a numeric amount/balance column>",\n'
        f'  "balance_flag": "<0 or 1 — 1 only if this column represents an account balance>"\n'
        f"}}\n\nReturn ONLY the JSON. No explanation. No markdown fences."
    )

    defaults: Dict[str, Any] = {
        "column_business_name": col_name.replace("_", " ").title()[:40],
        "column_business_name_nat": col_name.replace("_", " ").title()[:200],
        "aggregation_code": "1",
        "sd_used_flag": "0",
        "balance_flag": "0",
    }

    try:
        response = llm.invoke([HumanMessage(content=prompt)])
        raw = response.content.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        inferred = json.loads(raw)
        return {**defaults, **inferred}
    except Exception as exc:
        logger.warning(
            "[CATALOG] LLM metadata inference failed for %s: %s — using defaults.",
            col_name,
            exc,
        )
        return defaults


def _provision_catalog_entry(
    col_name: str,
    table_name: str,
    catalog_creations: List[Dict[str, Any]],
) -> Optional[tuple]:
    """Self-provision a missing column into the AML catalog and return its codes.

    Three sequential steps:
      1. Check / INSERT PIO_AML_TABLES for the source table.
      2. Query ALL_TAB_COLUMNS for the physical data type (never LLM-inferred).
      3. Check / INSERT PIO_AML_COLUMNS for the column.
      4. Check / INSERT PIO_AML_PARAMETERS for the parameter.

    Each step uses individual commits so partial catalog state is preserved
    on retry. The QB writer's atomic transaction covers scenario/rule writes,
    not catalog provisioning.

    Args:
        col_name (str): Physical column name to provision (e.g., 'DAILY_LIMIT').
        table_name (str): Physical Oracle table name (e.g., 'PIO_TRANSACTIONS').
        catalog_creations (List[Dict]): Mutable list — entries appended for each new row.

    Returns:
        Optional[Tuple[str, str]]: (parameter_code, aggregation_code) on success,
            or None if provisioning fails (e.g., column not in ALL_TAB_COLUMNS).
    """
    # Strip table alias prefix (e.g., 'T.DEB_CRE_IND' -> 'DEB_CRE_IND')
    if "." in col_name:
        col_name = col_name.split(".", 1)[1]

    from web.services.oracle import run_readonly, run_write, get_next_numeric_code

    now = datetime.utcnow()

    logger.info(
        "[CATALOG] Provisioning catalog for col=%s table=%s", col_name, table_name
    )

    # ── Step 1: Ensure PIO_AML_TABLES has the source table ────────────────────
    _, table_rows = run_readonly(
        "SELECT TABLE_CODE FROM PIO_AML_TABLES WHERE UPPER(TABLE_NAME) = UPPER(:tn)",
        {"tn": table_name},
    )
    if table_rows:
        table_code = str(table_rows[0][0]).strip()
        logger.info("[CATALOG] TABLE already registered: TABLE_CODE=%s", table_code)
    else:
        table_code = get_next_numeric_code("PIO_AML_TABLES", "TABLE_CODE")
        biz_name = table_name.replace("PIO_", "").replace("_", " ").title()[:40]
        run_write(
            """INSERT INTO PIO_AML_TABLES
               (TABLE_CODE, TABLE_NAME, BUSINESS_NAME, BUSINESS_NAME_NAT)
               VALUES (:tc, :tn, :bn, :bnn)""",
            {"tc": table_code, "tn": table_name[:40], "bn": biz_name, "bnn": biz_name},
        )
        catalog_creations.append(
            CatalogCreation(
                entity_type="TABLE",
                code=table_code,
                name=table_name,
                business_name=biz_name,
            ).model_dump(mode="json")
        )
        logger.info(
            "[CATALOG] Inserted PIO_AML_TABLES: TABLE_CODE=%s NAME=%s",
            table_code,
            table_name,
        )

    # ── Step 2: Query ALL_TAB_COLUMNS for actual data type (NEVER inferred) ───
    col_type = _query_oracle_column_type(table_name, col_name)
    if col_type is None:
        logger.error(
            "[CATALOG] Column %s not found in ALL_TAB_COLUMNS for table %s. Cannot provision.",
            col_name,
            table_name,
        )
        return None

    # ── Step 3: Ensure PIO_AML_COLUMNS has the column ────────────────────────
    _, col_rows = run_readonly(
        """SELECT COLUMN_CODE FROM PIO_AML_COLUMNS
           WHERE UPPER(COLUMN_NAME) = UPPER(:cn) AND TABLE_CODE = :tc""",
        {"cn": col_name, "tc": table_code},
    )
    if col_rows:
        col_code = str(col_rows[0][0]).strip()
        logger.info("[CATALOG] COLUMN already registered: COLUMN_CODE=%s", col_code)
    else:
        col_code = get_next_numeric_code("PIO_AML_COLUMNS", "COLUMN_CODE")
        meta = _infer_column_metadata(col_name, col_type, table_name)

        # Map physical type to legacy code: 1 = Number, 2 = String, 3 = Date
        ct_upper = str(col_type).upper()
        if any(
            k in ct_upper
            for k in ("NUMBER", "FLOAT", "INT", "DECIMAL", "DOUBLE", "NUMERIC")
        ):
            catalog_col_type = "1"
        elif any(k in ct_upper for k in ("DATE", "TIME", "TIMESTAMP")):
            catalog_col_type = "3"
        else:
            catalog_col_type = "2"

        run_write(
            """INSERT INTO PIO_AML_COLUMNS
               (COLUMN_CODE, COLUMN_TYPE, TABLE_CODE, COLUMN_NAME,
                COLUMN_BUSINESS_NAME, COLUMN_BUSINESS_NAME_NAT,
                LOOKUP_FLAG, SD_USED_FLAG, BALANCE_FLAG, HIS_FLAG)
               VALUES (:cc, :ct, :tc, :cn, :cbn, :cbnnat, '0', :sd, :bal, '0')""",
            {
                "cc": col_code,
                "ct": catalog_col_type,
                "tc": table_code,
                "cn": col_name[:40],
                "cbn": meta["column_business_name"][:200],
                "cbnnat": meta["column_business_name_nat"][:200],
                "sd": meta["sd_used_flag"],
                "bal": meta["balance_flag"],
            },
        )
        catalog_creations.append(
            CatalogCreation(
                entity_type="COLUMN",
                code=col_code,
                name=col_name,
                business_name=meta["column_business_name"],
            ).model_dump(mode="json")
        )
        logger.info(
            "[CATALOG] Inserted PIO_AML_COLUMNS: COLUMN_CODE=%s NAME=%s TYPE=%s",
            col_code,
            col_name,
            col_type,
        )

    # ── Step 4: Create PARAMETER_CODE in PIO_AML_PARAMETERS ──────────────────
    _, param_rows = run_readonly(
        """SELECT PARAMETER_CODE, AGGREGATION_CODE FROM PIO_AML_PARAMETERS
           WHERE TABLE_CODE = :tc AND COLUMN_CODE = :cc""",
        {"tc": table_code, "cc": col_code},
    )
    if param_rows:
        p_code = str(param_rows[0][0]).strip()
        agg_code = str(param_rows[0][1]).strip() if param_rows[0][1] else "1"
        logger.info("[CATALOG] PARAMETER already registered: PARAMETER_CODE=%s", p_code)
        return p_code, agg_code

    meta = _infer_column_metadata(col_name, col_type, table_name)
    agg_code = str(meta.get("aggregation_code", "1"))
    p_code = get_next_numeric_code("PIO_AML_PARAMETERS", "PARAMETER_CODE")
    param_element = meta["column_business_name"][:400]
    run_write(
        """INSERT INTO PIO_AML_PARAMETERS
           (PARAMETER_CODE, PARAMETER_ELEMENT, TABLE_CODE, COLUMN_CODE,
            AGGREGATION_CODE, PARAMETER_PERC_FLAG, PARAMETER_ELEMENT_NAT,
            LGM_SCENARIO_BASED_FLAG, LGM_GROUP_BASED_FLAG,
            CREATED_BY, CREATED_DATE, UPDATED_BY, UPDATED_DATE)
           VALUES (:pc, :pe, :tc, :cc, :ac, '0', :penat, '0', '0',
                   :cb, :cd, :ub, :ud)""",
        {
            "pc": p_code,
            "pe": param_element,
            "tc": table_code,
            "cc": col_code,
            "ac": agg_code,
            "penat": meta["column_business_name_nat"][:400],
            "cb": 999,
            "cd": now,
            "ub": 999,
            "ud": now,
        },
    )
    catalog_creations.append(
        CatalogCreation(
            entity_type="PARAMETER",
            code=p_code,
            name=col_name,
            business_name=param_element,
            aggregation_code=agg_code,
        ).model_dump(mode="json")
    )
    logger.info(
        "[CATALOG] Inserted PIO_AML_PARAMETERS: PARAMETER_CODE=%s for %s.%s",
        p_code,
        table_name,
        col_name,
    )
    return p_code, agg_code


def _verify_catalog_integrity(catalog_creations: List[Dict[str, Any]]) -> bool:
    """Verify that auto-created catalog entries are reachable via the QB join path.

    Checks that each new PARAMETER_CODE can be found via:
    PIO_AML_PARAMETERS JOIN PIO_AML_COLUMNS ON TABLE_CODE + COLUMN_CODE.

    Args:
        catalog_creations (List[Dict]): Entries logged by _provision_catalog_entry.

    Returns:
        bool: True if all checks pass, False if any new entry is orphaned.
    """
    from web.services.oracle import run_readonly

    param_codes = [
        c["code"] for c in catalog_creations if c.get("entity_type") == "PARAMETER"
    ]
    if not param_codes:
        return True

    try:
        for p_code in param_codes:
            _, rows = run_readonly(
                """
                SELECT P.PARAMETER_CODE
                FROM PIO_AML_PARAMETERS P
                JOIN PIO_AML_COLUMNS C
                    ON P.TABLE_CODE = C.TABLE_CODE AND P.COLUMN_CODE = C.COLUMN_CODE
                WHERE P.PARAMETER_CODE = :pc
                """,
                {"pc": p_code},
            )
            if not rows:
                logger.error(
                    "[CATALOG] Integrity FAILED for PARAMETER_CODE=%s — "
                    "not reachable via PIO_AML_PARAMETERS ⟶ PIO_AML_COLUMNS join.",
                    p_code,
                )
                return False
            logger.info(
                "[CATALOG] Integrity check PASSED for PARAMETER_CODE=%s", p_code
            )
        return True
    except Exception as exc:
        logger.error("[CATALOG] Integrity check query failed: %s", exc)
        return False


def _fetch_and_map_parameters(
    intent: AMLIntent,
    sql_meta: SQLMetadata,
    rule_code: str,
    scenario_code: str,
    created_date: datetime,
    catalog_creations: Optional[List[Dict[str, Any]]] = None,
) -> List[QBRuleDetail]:
    """Map intent thresholds and SQL conditions to QBRuleDetail rows.

    Queries the live database catalog to map physical SQL column names to
    Query Builder PARAMETER_CODEs dynamically.

    Args:
        intent (AMLIntent): Enriched user intent.
        sql_meta (SQLMetadata): Parsed SQL metadata.
        rule_code (str): The rule code to link details to.
        scenario_code (str): The scenario code to link details to.
        created_date (datetime): Timestamp to stamp on all rows.

    Returns:
        List[QBRuleDetail]: List of condition rows for PIO_AML_RULES_DETAILS.
    """
    if catalog_creations is None:
        catalog_creations = []

    details: List[QBRuleDetail] = []
    seq = 1

    # Load dynamic catalog mappings from database
    detail_column_map: Dict[str, tuple] = {}
    aggregate_column_map: Dict[str, tuple] = {}
    parameter_to_column: Dict[str, str] = {}
    all_catalog_params: List[Dict[str, Any]] = []

    try:
        from web.services.oracle import run_readonly

        _, catalog_rows = run_readonly(
            """
            SELECT DISTINCT P.PARAMETER_CODE, UPPER(C.COLUMN_NAME), P.AGGREGATION_CODE, P.PARAMETER_ELEMENT, P.TABLE_CODE
            FROM PIO_AML_PARAMETERS P
            JOIN PIO_AML_COLUMNS C ON P.TABLE_CODE = C.TABLE_CODE AND P.COLUMN_CODE = C.COLUMN_CODE
            """,
            {},
        )
        for row in catalog_rows:
            p_code = str(row[0]).strip()
            col_name_upper = str(row[1]).strip().upper()
            agg_code = str(row[2]).strip() if row[2] else "1"
            p_element = str(row[3]).strip()
            t_code = str(row[4]).strip()

            cp = {
                "parameter_code": p_code,
                "column_name": col_name_upper,
                "aggregation_code": agg_code,
                "parameter_element": p_element,
                "table_code": t_code,
            }
            all_catalog_params.append(cp)

            # Prevent overwriting transaction amount mapping (Param 5)
            # with count (Param 2) or sum (Param 6) which also reference EQU_TRA_AMT.
            if col_name_upper == "EQU_TRA_AMT" and p_code in ("2", "6"):
                continue

            if agg_code == "1":
                detail_column_map[col_name_upper] = (p_code, agg_code)
            else:
                aggregate_column_map[col_name_upper] = (p_code, agg_code)

            parameter_to_column[p_code] = col_name_upper

        logger.info(
            "[DECOMPOSER] Loaded %d dynamic parameter mappings from catalog.",
            len(detail_column_map) + len(aggregate_column_map),
        )
    except Exception as exc:
        logger.error("[DECOMPOSER] Failed to query live parameter catalog: %s.", exc)

    def _get_parameter_for_column(col_name: str, is_aggregate: bool) -> Optional[tuple]:
        if is_aggregate:
            return aggregate_column_map.get(col_name) or detail_column_map.get(col_name)
        else:
            return detail_column_map.get(col_name) or aggregate_column_map.get(col_name)

    # Helper: build a QBRuleDetail with all required fields.
    def _make_detail(
        param_code: str,
        operator: str,
        value_from: Optional[str],
        value_des: Optional[str],
        combined: str = "AND",
        value_to: Optional[str] = None,
        from_param_perc: Optional[int] = None,
        use_sd_flag: str = "0",
        sd_period_type: str = "0",
    ) -> QBRuleDetail:
        return QBRuleDetail(
            country_code=settings.AML_COUNTRY_CODE,
            inst_code=settings.AML_INST_CODE,
            parameter_code=param_code,
            rule_code=rule_code,
            rule_seq=str(seq),
            rule_operator=operator,
            comparison_value_from=value_from,
            comparison_value_to=value_to,
            comparison_value_from_des=value_des,
            combined_rule=combined,
            scenario_code=scenario_code,
            use_sd_flag=use_sd_flag,
            sd_period_type=sd_period_type,
            same_cust_flag="0",
            from_param_perc=from_param_perc,
            created_by="0",
            created_date=created_date,
            updated_by="0",
            updated_date=created_date,
        )

    # ------------------------------------------------------------------ #
    # Step 1: Map WHERE conditions dynamically from SQL                  #
    # ------------------------------------------------------------------ #
    for cond in sql_meta.where_conditions:
        # Strip SQL comments
        cond_clean = re.sub(r"--.*$", "", cond)
        cond_clean = re.sub(r"/\*.*?\*/", "", cond_clean).strip()

        # Match: COLUMN_NAME Operator VALUE with strict word boundaries for verbal operators
        match = re.search(
            r"(\w+(?:\.\w+)?)\s*([><=!]+|\bLIKE\b|\bIN\b)\s*(.*)",
            cond_clean,
            re.IGNORECASE,
        )
        if not match:
            continue

        raw_col = match.group(1).strip()
        op = match.group(2).strip().upper()
        raw_val = match.group(3).strip()

        # Strip table alias (e.g. C.CUS_CLASS -> CUS_CLASS)
        col_name = raw_col.split(".")[-1].upper()

        # Map equivalent physical columns to their registered catalog names
        col_aliases = {
            "TRA_AMT": "EQU_TRA_AMT",
            "TRANS_TYPE": "EXPL_CODE",
            "TXN_TYPE": "EXPL_CODE",
            "TRANSACTION_TYPE": "EXPL_CODE",
            "TXN_CODE": "EXPL_CODE",
            "TRANS_DATE": "TRA_DATE",
        }
        col_name = col_aliases.get(col_name, col_name)

        # Skip standard DWH system columns that aren't rule conditions
        if col_name in (
            "CUS_STATUS",
            "DAY_DATE",
            "COUNTRY_CODE",
            "INST_CODE",
            "UPDATED_DATE",
            "CREATED_DATE",
            "STATUS_CODE",
            "TRA_DATE",
            "TRANS_DATE",
        ):
            continue

        p_res = _get_parameter_for_column(col_name, is_aggregate=False)
        if not p_res:
            # Column not in catalog — attempt auto-provisioning
            logger.info(
                "[DECOMPOSER] Column '%s' not in catalog — attempting auto-provisioning.",
                col_name,
            )
            source_table = (
                (sql_meta.primary_table or "PIO_TRANSACTIONS").split(".")[-1].upper()
            )
            provision_result = _provision_catalog_entry(
                col_name, source_table, catalog_creations
            )
            if provision_result is None:
                raise ValueError(
                    f"Column '{raw_col}' (table: {source_table}) could not be auto-provisioned. "
                    f"It does not exist in ALL_TAB_COLUMNS. "
                    f"Verify the column name is correct or register it manually in PIO_AML_COLUMNS."
                )
            p_new, agg_new = provision_result
            # Add to maps
            all_catalog_params.append(
                {
                    "parameter_code": p_new,
                    "column_name": col_name,
                    "aggregation_code": agg_new,
                    "parameter_element": col_name,
                    "table_code": "unknown",
                }
            )
            if agg_new == "1":
                detail_column_map[col_name] = (p_new, agg_new)
            else:
                aggregate_column_map[col_name] = (p_new, agg_new)
            parameter_to_column[p_new] = col_name
            p_res = (p_new, agg_new)

        p_code, agg_code = p_res

        # Extract comparison values
        if op == "IN":
            # Extract comma-separated values inside parenthesis, stripping quotes
            val_match = re.search(r"\(([^)]+)\)", raw_val)
            if val_match:
                codes = [
                    c.strip().strip("'").strip('"')
                    for c in val_match.group(1).split(",")
                ]
                oracle_in_val = ",".join(f"''{c}''" for c in codes)
                des = ",".join(codes)
                details.append(_make_detail(p_code, "IN", oracle_in_val, des))
                seq += 1
        else:
            # Single value comparison
            val = raw_val.strip().rstrip(";").strip("'").strip('"')
            details.append(_make_detail(p_code, op, val, f"{col_name} {op} {val}"))
            seq += 1

    # ------------------------------------------------------------------ #
    # Step 2: Map HAVING conditions dynamically from SQL (Aggregates)    #
    # ------------------------------------------------------------------ #
    for having in sql_meta.having_conditions:
        # Strip SQL comments
        having_clean = re.sub(r"--.*$", "", having)
        having_clean = re.sub(r"/\*.*?\*/", "", having_clean).strip()

        # Check for Standard Deviation first in aggregate functions
        # e.g., SUM(TRA_AMT) > AVG(TRA_AMT) + 0.1 * STDDEV(TRA_AMT)
        if "STDDEV" in having_clean.upper() or "STDDEV_SAMP" in having_clean.upper():
            # Parse multiplier of STDDEV, e.g. 0.1 * STDDEV or STDDEV * 0.1
            mult_match = re.search(
                r"([\d\.]+)\s*\*\s*STDDEV", having_clean, re.IGNORECASE
            )
            if not mult_match:
                mult_match = re.search(
                    r"STDDEV\s*\([^)]+\)\s*\*\s*([\d\.]+)", having_clean, re.IGNORECASE
                )

            mult = float(mult_match.group(1)) if mult_match else 1.0
            from_perc = int(mult * 100)

            # Map operator
            op_raw = ">="
            for op in (">=", "<=", ">", "<", "="):
                if op in having_clean:
                    op_raw = op
                    break

            des = f"Standard Deviation threshold multiplier: {from_perc}%"
            p_res = _get_parameter_for_column("EQU_TRA_AMT", is_aggregate=True)
            p_code = p_res[0] if p_res else "6"
            details.append(
                _make_detail(
                    param_code=p_code,
                    operator=op_raw,
                    value_from="0",  # Engine evaluates dynamically when USE_SD_FLAG=1
                    value_des=des,
                    from_param_perc=from_perc,
                    use_sd_flag="1",
                    sd_period_type="1",  # Default 1 year history
                )
            )
            seq += 1
            continue

        # SUM(...) >= N  -> Parameter 6 (Summation of Transactions)
        sum_match = re.search(
            r"SUM\s*\([^)]+\)\s*([><=!]+)\s*([\d,\.]+)", having_clean, re.IGNORECASE
        )
        if sum_match:
            op_raw = sum_match.group(1).strip()
            val = sum_match.group(2).replace(",", "").strip()
            des = f"Sum of transactions {op_raw} {val}"
            p_res = _get_parameter_for_column("EQU_TRA_AMT", is_aggregate=True)
            p_code = p_res[0] if p_res else "6"
            details.append(_make_detail(p_code, op_raw, val, des, from_param_perc=100))
            seq += 1
            continue

        # COUNT(DISTINCT COLUMN) >= N  -> Parameter for distinct count
        count_dist_match = re.search(
            r"COUNT\s*\(\s*DISTINCT\s+(\w+(?:\.\w+)?)\s*\)\s*([><=!]+)\s*(\d+)",
            having_clean,
            re.IGNORECASE,
        )
        if count_dist_match:
            raw_col = count_dist_match.group(1).strip()
            op_raw = count_dist_match.group(2).strip()
            val = count_dist_match.group(3).strip()
            col_name = raw_col.split(".")[-1].upper()

            p_res = _get_parameter_for_column(col_name, is_aggregate=True)
            p_code = p_res[0] if p_res else "117"
            des = f"Distinct {col_name} count {op_raw} {val}"
            details.append(_make_detail(p_code, op_raw, val, des))
            seq += 1
            continue

        # COUNT(...) >= N  -> Parameter 2 (Number of Transactions)
        count_match = re.search(
            r"COUNT\s*\(\s*[^)]*\s*\)\s*([><=!]+)\s*(\d+)", having, re.IGNORECASE
        )
        if count_match:
            op_raw = count_match.group(1).strip()
            val = count_match.group(2).strip()
            des = f"Number of transactions {op_raw} {val}"
            p_code = "2"
            for cp in all_catalog_params:
                if cp["column_name"] == "EQU_TRA_AMT" and cp["aggregation_code"] == "6":
                    p_code = cp["parameter_code"]
                    break
            details.append(_make_detail(p_code, op_raw, val, des))
            seq += 1

    # ------------------------------------------------------------------ #
    # Step 3: LLM-Based Intent Grounding (Zero Hardcoded Fallbacks)       #
    # ------------------------------------------------------------------ #
    # Compile lists of mapped details
    mapped_param_codes = {d.parameter_code for d in details}
    mapped_columns = {
        parameter_to_column.get(p)
        for p in mapped_param_codes
        if p in parameter_to_column
    }

    # Find the table_code of the primary table to filter the catalog parameters list
    primary_table_upper = (
        (sql_meta.primary_table or "PIO_TRANSACTIONS").split(".")[-1].upper()
    )
    primary_table_code = None
    try:
        from web.services.oracle import run_readonly

        _, table_rows = run_readonly(
            "SELECT TABLE_CODE FROM PIO_AML_TABLES WHERE UPPER(TABLE_NAME) = UPPER(:tn)",
            {"tn": primary_table_upper},
        )
        if table_rows:
            primary_table_code = str(table_rows[0][0]).strip()
    except Exception:
        pass

    # Build filtered catalog list and context string
    catalog_list = []
    for cp in all_catalog_params:
        if primary_table_code and cp.get("table_code") != primary_table_code:
            continue
        catalog_list.append(cp)

    catalog_params_str = ""
    for cp in catalog_list:
        catalog_params_str += (
            f"- Parameter Code: {cp['parameter_code']}, "
            f"Element: {cp['parameter_element']}, "
            f"Column: {cp['column_name']}, "
            f"Aggregation Code: {cp['aggregation_code']}\n"
        )

    already_mapped_str = ""
    for d in details:
        already_mapped_str += (
            f"- Mapped Parameter: {d.parameter_code} ({d.comparison_value_from_des})\n"
        )

    intent_thresholds_str = ""
    for t in intent.thresholds:
        intent_thresholds_str += (
            f"- Field: {t.field}, Operator: {t.operator}, Value: {t.value_from}\n"
        )

    if intent.thresholds and catalog_list:
        try:
            llm = _build_llm(fast=True)
            prompt = (
                "You are an expert database catalog grounding system for an AML compliance transaction monitoring database.\n\n"
                "Your task is to map plain-English intent thresholds to parameter codes in the PIO_AML_PARAMETERS catalog.\n\n"
                "DATABASE CATALOG PARAMETERS:\n"
                f"{catalog_params_str}\n"
                "ALREADY MAPPED PARAMETERS (from SQL WHERE/HAVING):\n"
                f"{already_mapped_str if already_mapped_str else 'None'}\n"
                "INTENT THRESHOLDS TO GROUND:\n"
                f"{intent_thresholds_str}\n\n"
                "INSTRUCTIONS:\n"
                "1. For each intent threshold, determine if it is already covered by the SQL query's mapped parameters.\n"
                "   If a threshold is already covered (e.g. the SQL query already filtered on transaction amount or type), set its parameter_code to null.\n"
                "2. If it is NOT covered, look up the most appropriate parameter code in the DATABASE CATALOG PARAMETERS list.\n"
                "   - Map Detail-level thresholds (e.g. per-transaction amount) to parameters with aggregation_code='1'.\n"
                "   - Map Aggregated thresholds (e.g. summation, count, monthly frequency) to aggregate parameter codes (aggregation_code != '1').\n"
                "3. Respond ONLY with a valid JSON object matching the following schema. Do not write any explanations, markdown code blocks, or extra text:\n"
                "{\n"
                '  "mappings": [\n'
                "    {\n"
                '      "field": "<field name>",\n'
                '      "parameter_code": "<parameter code or null>",\n'
                '      "reasoning": "<explanation>"\n'
                "    }\n"
                "  ]\n"
                "}\n"
            )

            response = llm.invoke(prompt)
            response_text = str(response.content).strip()

            def _extract_json(text: str) -> Optional[dict]:
                match = re.search(r"```(?:json)?\s*([\s\S]+?)```", text, re.IGNORECASE)
                if match:
                    try:
                        return json.loads(match.group(1).strip())
                    except Exception:
                        pass
                try:
                    return json.loads(text.strip())
                except Exception:
                    pass
                start = text.find("{")
                end = text.rfind("}")
                if start != -1 and end != -1:
                    try:
                        return json.loads(text[start : end + 1].strip())
                    except Exception:
                        pass
                return None

            json_data = _extract_json(response_text)
            if json_data and "mappings" in json_data:
                for item in json_data["mappings"]:
                    p_code = str(item.get("parameter_code") or "").strip()
                    field_name = str(item.get("field") or "").strip()

                    if p_code and p_code.lower() != "null" and p_code != "none":
                        underlying_col = parameter_to_column.get(p_code)
                        if p_code in mapped_param_codes or (
                            underlying_col and underlying_col in mapped_columns
                        ):
                            logger.info(
                                "[DECOMPOSER] Skipping grounded threshold '%s' (Parameter %s) as it is already mapped.",
                                field_name,
                                p_code,
                            )
                            continue

                        threshold = None
                        for t in intent.thresholds:
                            if t.field == field_name:
                                threshold = t
                                break

                        if threshold:
                            value_from = (
                                str(int(threshold.value_from))
                                if threshold.value_from == int(threshold.value_from)
                                else str(threshold.value_from)
                            )
                            value_to = (
                                str(int(threshold.value_to))
                                if threshold.value_to is not None
                                and threshold.value_to == int(threshold.value_to)
                                else (
                                    str(threshold.value_to)
                                    if threshold.value_to is not None
                                    else None
                                )
                            )
                            des = f"{threshold.field} {threshold.operator} {threshold.value_from}"

                            details.append(
                                _make_detail(
                                    p_code.strip(),
                                    threshold.operator,
                                    value_from,
                                    des,
                                    value_to=value_to,
                                )
                            )
                            seq += 1
                            mapped_param_codes.add(p_code)
                            if underlying_col:
                                mapped_columns.add(underlying_col)
                            logger.info(
                                "[DECOMPOSER] Grounded fallback threshold '%s' to PARAMETER_CODE=%s",
                                field_name,
                                p_code,
                            )
        except Exception as exc:
            logger.error(
                "[DECOMPOSER] LLM intent grounding failed: %s. Bypassing fallback.", exc
            )

    # ------------------------------------------------------------------ #
    # Step 4: Determine dynamic AND / OR / - connectors                  #
    # ------------------------------------------------------------------ #
    _determine_combined_connectors(details, intent.detection_logic, sql_meta.raw_sql)

    logger.info(
        "[DECOMPOSER] Mapped %d rule detail rows. Codes used: %s",
        len(details),
        [d.parameter_code for d in details],
    )
    return details


def _assess_confidence(intent: AMLIntent, details: List[QBRuleDetail]) -> float:
    """Self-assess how completely the intent maps to QB parameters.

    Args:
        intent (AMLIntent): The enriched AML intent.
        details (List[QBRuleDetail]): The generated rule detail rows.

    Returns:
        float: Confidence score between 0.0 and 1.0.
    """
    score = 0.0

    # Has at least one condition mapped
    if details:
        score += 0.4

    # All intent thresholds are covered
    if len(details) >= len(intent.thresholds):
        score += 0.3

    # Time window is defined
    if intent.time_window:
        score += 0.2

    # Has a meaningful scenario name
    if intent.scenario_name and len(intent.scenario_name) > 5:
        score += 0.1

    return min(score, 1.0)


# =============================================================================
# NODE 5 — QB WRITER (helpers + atomic node)
# =============================================================================


def _assert_plan_drift(
    plan_conditions: List[Dict[str, Any]],
    rule_details: List[QBRuleDetail],
) -> None:
    """Assert that generated rule_details match every approved plan condition.

    Checks that each PlanCondition has a corresponding QBRuleDetail with the
    same operator and value_from. Extra rule_details generate a WARNING only.

    Args:
        plan_conditions (List[Dict]): Serialized PlanCondition dicts from state.
        rule_details (List[QBRuleDetail]): Generated condition rows from decomposer.

    Raises:
        PlanDriftError: If any approved plan condition has no matching rule_detail.
    """
    if not plan_conditions:
        logger.warning(
            "[DRIFT] No plan conditions to check — drift assertion skipped. "
            "Planner may not have emitted a CONDITIONS_BLOCK."
        )
        return

    # Build lookup: (operator_upper, value_from_stripped)
    generated_sigs = {
        (d.rule_operator.upper().strip(), str(d.comparison_value_from or "").strip())
        for d in rule_details
    }

    drifted = []
    for cond in plan_conditions:
        op = str(cond.get("operator", "")).upper().strip()
        val = str(cond.get("value_from", "")).strip()

        # Parse value_from as a float. If it's not a numeric threshold (e.g. 'cash deposit'),
        # bypass strict value drift comparison since DWH uses code lookups.
        try:
            val_float = float(val)
            matched = False
            for gen_op, gen_val in generated_sigs:
                if gen_op == op:
                    try:
                        if abs(float(gen_val) - val_float) < 1e-9:
                            matched = True
                            break
                    except ValueError:
                        continue
            if not matched:
                drifted.append(
                    f"  • [{cond.get('condition_id')}] {cond.get('description')} "
                    f"— expected {op} {val}"
                )
        except ValueError:
            logger.info(
                "[DRIFT] Bypassing strict value check for non-numeric condition: %s",
                cond.get("description"),
            )

    if drifted:
        drift_detail = "\n".join(drifted)
        logger.error(
            "[DRIFT] %d plan condition(s) unmatched in generated parameters:\n%s",
            len(drifted),
            drift_detail,
        )
        raise PlanDriftError(
            f"Execution halted — {len(drifted)} approved plan condition(s) "
            f"are absent from the generated scenario parameters:\n\n"
            f"{drift_detail}\n\n"
            f"The scenario was NOT written to Oracle. "
            f"Please review the plan and resubmit."
        )

    # Warn about extra rule_details with no plan counterpart
    plan_sigs = {
        (
            str(c.get("operator", "")).upper().strip(),
            str(c.get("value_from", "")).strip(),
        )
        for c in plan_conditions
    }
    extras = [
        d
        for d in rule_details
        if (d.rule_operator.upper().strip(), str(d.comparison_value_from or "").strip())
        not in plan_sigs
    ]
    if extras:
        logger.warning(
            "[DRIFT] %d extra rule_detail(s) have no corresponding plan condition "
            "(will be written): %s",
            len(extras),
            [(d.rule_operator, d.comparison_value_from) for d in extras],
        )


def _verify_write_integrity(
    scenario_code: str,
    rule_code: str,
    expected_detail_rows: int,
) -> WriteVerification:
    """SELECT COUNT from all 4 Oracle tables to verify atomic write landed correctly.

    Args:
        scenario_code (str): The scenario code written.
        rule_code (str): The rule code written.
        expected_detail_rows (int): Number of QBRuleDetail rows that were inserted.

    Returns:
        WriteVerification: Per-table counts, pass/fail, and discrepancy descriptions.
    """
    from web.services.oracle import run_readonly

    discrepancies: List[str] = []

    def _count(sql: str, params: dict) -> int:
        try:
            _, rows = run_readonly(sql, params)
            return int(rows[0][0]) if rows else 0
        except Exception as exc:
            logger.error("[WRITER] Count query failed: %s", exc)
            return -1

    sce_rows = _count(
        "SELECT COUNT(*) FROM PIO_AML_SCENARIO WHERE SCENARIO_CODE = :sc AND COUNTRY_CODE = :cc AND INST_CODE = :ic",
        {
            "sc": scenario_code,
            "cc": settings.AML_COUNTRY_CODE,
            "ic": settings.AML_INST_CODE,
        },
    )
    rule_rows = _count(
        "SELECT COUNT(*) FROM PIO_AML_RULES WHERE RULE_CODE = :rc AND COUNTRY_CODE = :cc AND INST_CODE = :ic",
        {
            "rc": rule_code,
            "cc": settings.AML_COUNTRY_CODE,
            "ic": settings.AML_INST_CODE,
        },
    )
    sr_rows = _count(
        "SELECT COUNT(*) FROM PIO_AML_SCENARIO_RULES WHERE AML_SCENARIO = :sc AND COUNTRY_CODE = :cc AND INST_CODE = :ic",
        {
            "sc": scenario_code,
            "cc": settings.AML_COUNTRY_CODE,
            "ic": settings.AML_INST_CODE,
        },
    )
    det_rows = _count(
        "SELECT COUNT(*) FROM PIO_AML_RULES_DETAILS WHERE RULE_CODE = :rc AND COUNTRY_CODE = :cc AND INST_CODE = :ic",
        {
            "rc": rule_code,
            "cc": settings.AML_COUNTRY_CODE,
            "ic": settings.AML_INST_CODE,
        },
    )

    if sce_rows != 1:
        discrepancies.append(f"PIO_AML_SCENARIO: expected 1, found {sce_rows}")
    if rule_rows != 1:
        discrepancies.append(f"PIO_AML_RULES: expected 1, found {rule_rows}")
    if sr_rows != 1:
        discrepancies.append(f"PIO_AML_SCENARIO_RULES: expected 1, found {sr_rows}")
    if det_rows != expected_detail_rows:
        discrepancies.append(
            f"PIO_AML_RULES_DETAILS: expected {expected_detail_rows}, found {det_rows}"
        )

    return WriteVerification(
        scenario_rows=sce_rows,
        rule_rows=rule_rows,
        scenario_rule_rows=sr_rows,
        rule_detail_rows=det_rows,
        expected_detail_rows=expected_detail_rows,
        all_pass=len(discrepancies) == 0,
        discrepancies=discrepancies,
    )


def _extract_table_from_oracle_error(error_str: str) -> str:
    """Extract a table name from an Oracle error string for precise diagnostics.

    Args:
        error_str (str): The string representation of the Oracle exception.

    Returns:
        str: Matching table name, or 'unknown table'.
    """
    for table in (
        "PIO_AML_SCENARIO",
        "PIO_AML_RULES",
        "PIO_AML_SCENARIO_RULES",
        "PIO_AML_RULES_DETAILS",
        "PIO_AML_PARAMETERS",
        "PIO_AML_COLUMNS",
        "PIO_AML_TABLES",
    ):
        if table in error_str.upper():
            return table
    return "unknown table"


def qb_writer_node(state: AMLScenarioState, config: RunnableConfig) -> Dict[str, Any]:
    """Write the decomposed parameters into Oracle AML tables — atomically.

    All 4 INSERTs are wrapped in a SINGLE Oracle transaction using
    atomic_connection(). Any exception triggers a full rollback — no
    partial scenarios can be left in the database.

    Pre-flight: performs plan drift assertion before opening any connection.
    If drift is detected, raises PlanDriftError and exits without writing.

    Post-write: runs write integrity verification (SELECT COUNT on all 4 tables).

    Args:
        state (AMLScenarioState): Current agent state.
        config (RunnableConfig): LangGraph runtime config.

    Returns:
        Dict[str, Any]: State updates with scenario_write_success, write_verification,
            and next_action.
    """
    logger.info("[QB_WRITER] Starting atomic write sequence.")

    params_dict = state.get("scenario_parameters") or {}
    params = ScenarioParameters(**params_dict)
    plan_conditions: List[Dict[str, Any]] = state.get("plan_conditions") or []

    # ── Pre-flight: drift assertion (before touching Oracle) ──────────────────
    try:
        _assert_plan_drift(plan_conditions, params.rule_details)
        logger.info(
            "[QB_WRITER] Drift assertion passed — %d plan condition(s) verified.",
            len(plan_conditions),
        )
    except PlanDriftError as drift_exc:
        logger.error(
            "[QB_WRITER] Drift assertion FAILED — aborting write. %s", drift_exc
        )
        return {
            "scenario_write_success": False,
            "next_action": "FAILURE",
            "error_log": state.get("error_log", []) + [str(drift_exc)],
        }

    from web.services.oracle import atomic_connection

    try:
        # ── Single atomic transaction: delete + 4 INSERTs ────────────────────
        with atomic_connection() as conn:
            cursor = conn.cursor()

            # Deactivate all other scenarios to prevent them from crashing the validation stored procedure
            cursor.execute(
                """UPDATE PIO_AML_SCENARIO 
                   SET ACTIVE_FLAG = '0' 
                   WHERE COUNTRY_CODE = :cc AND INST_CODE = :ic 
                     AND SCENARIO_CODE <> :sc""",
                {
                    "cc": int(settings.AML_COUNTRY_CODE),
                    "ic": int(settings.AML_INST_CODE),
                    "sc": params.scenario.scenario_code,
                },
            )

            # Cleanup any previous attempt for this scenario_code
            _delete_scenario_atomic(cursor, params.scenario.scenario_code)

            # 1. Scenario header
            _insert_scenario_cursor(cursor, params.scenario)

            # 2. Rule definitions
            for rule in params.rules:
                _insert_rule_cursor(cursor, rule)

            # 3. Scenario-rule links
            for sr in params.scenario_rules:
                _insert_scenario_rule_cursor(cursor, sr)

            # 4. Rule details (batch)
            _insert_rule_details_cursor(cursor, params.rule_details)

        logger.info(
            "[QB_WRITER] Atomic commit successful. scenario_code=%s",
            params.scenario.scenario_code,
        )

    except Exception as exc:
        table_hint = _extract_table_from_oracle_error(str(exc))
        detailed_error = (
            f"Oracle write failed on {table_hint}. "
            f"Error type: {type(exc).__name__}. "
            f"Detail: {exc}. "
            f"All writes were rolled back — the database is clean."
        )
        logger.error("[QB_WRITER] %s", detailed_error, exc_info=True)
        return {
            "scenario_write_success": False,
            "next_action": "FAILURE",
            "error_log": state.get("error_log", []) + [detailed_error],
        }

    # ── Post-write integrity verification ────────────────────────────────────
    rule_code = params.rules[0].rule_code if params.rules else ""
    verification = _verify_write_integrity(
        scenario_code=params.scenario.scenario_code,
        rule_code=rule_code,
        expected_detail_rows=len(params.rule_details),
    )

    if not verification.all_pass:
        disc_text = "; ".join(verification.discrepancies)
        logger.error("[QB_WRITER] Write integrity check FAILED: %s", disc_text)
        return {
            "scenario_write_success": False,
            "write_verification": verification.model_dump(mode="json"),
            "next_action": "FAILURE",
            "error_log": state.get("error_log", [])
            + [f"Write integrity check failed after commit: {disc_text}"],
        }

    logger.info("[QB_WRITER] Write integrity verified — all row counts match.")
    return {
        "scenario_write_success": True,
        "scenario_code": params.scenario.scenario_code,
        "write_verification": verification.model_dump(mode="json"),
        "next_action": "VALIDATE",
    }


def _delete_scenario_atomic(cursor: Any, scenario_code: str) -> None:
    """Delete all DB records for scenario_code using a shared cursor (within atomic tx).

    Runs inside the atomic_connection transaction — no individual commit.
    Ensures retry loops never hit primary key violations.

    Args:
        cursor: An open Oracle cursor from atomic_connection.
        scenario_code (str): The unique scenario identifier to clean up.
    """
    logger.info(
        "[QB_WRITER] Cleaning existing rows for scenario_code=%s", scenario_code
    )

    cursor.execute(
        """DELETE FROM PIO_AML_RULES_DETAILS
           WHERE COUNTRY_CODE = :cc AND INST_CODE = :ic
           AND RULE_CODE IN (
               SELECT AML_RULE_CODE FROM PIO_AML_SCENARIO_RULES
               WHERE AML_SCENARIO = :sc AND COUNTRY_CODE = :cc AND INST_CODE = :ic
           )""",
        {
            "sc": scenario_code,
            "cc": settings.AML_COUNTRY_CODE,
            "ic": settings.AML_INST_CODE,
        },
    )
    cursor.execute(
        """DELETE FROM PIO_AML_RULES
           WHERE COUNTRY_CODE = :cc AND INST_CODE = :ic
           AND RULE_CODE IN (
               SELECT AML_RULE_CODE FROM PIO_AML_SCENARIO_RULES
               WHERE AML_SCENARIO = :sc AND COUNTRY_CODE = :cc AND INST_CODE = :ic
           )""",
        {
            "sc": scenario_code,
            "cc": settings.AML_COUNTRY_CODE,
            "ic": settings.AML_INST_CODE,
        },
    )
    cursor.execute(
        "DELETE FROM PIO_AML_SCENARIO_RULES WHERE AML_SCENARIO = :sc AND COUNTRY_CODE = :cc AND INST_CODE = :ic",
        {
            "sc": scenario_code,
            "cc": settings.AML_COUNTRY_CODE,
            "ic": settings.AML_INST_CODE,
        },
    )
    cursor.execute(
        "DELETE FROM PIO_AML_SCENARIO WHERE SCENARIO_CODE = :sc AND COUNTRY_CODE = :cc AND INST_CODE = :ic",
        {
            "sc": scenario_code,
            "cc": settings.AML_COUNTRY_CODE,
            "ic": settings.AML_INST_CODE,
        },
    )


def _insert_scenario_cursor(cursor: Any, scenario: QBScenario) -> None:
    """INSERT a row into PIO_AML_SCENARIO using a shared cursor.

    Args:
        cursor: Open Oracle cursor from atomic_connection.
        scenario (QBScenario): Scenario header data.
    """
    cursor.execute(
        """INSERT INTO PIO_AML_SCENARIO
               (COUNTRY_CODE, INST_CODE, SCENARIO_CODE, SCENARIO_DES_ENG,
                SCENARIO_DES_NAT_LAN, ACTIVE_FLAG, EXCLUDE_EXPL_FLAG,
                USE_WATCHLIST_FLAG, VIOLATION_LEVEL, DEGREE_RISK_FLAG,
                DEFAULT_SCENARIO_FLAG, RUN_FLAG, APPROVAL_FLAG,
                GROUP_BY_FLAG, USE_WORLDCHECK_FLAG,
                CREATED_BY, CREATED_DATE, UPDATED_BY, UPDATED_DATE,
                TRANS_WITHOUTTRANS_FLAG, CATEGORY_CODE,
                ACTIVE_THRESHOLD_CURR_FLAG, SCE_TYPE_CODE, CLASS_CODE)
           VALUES
               (:country_code, :inst_code, :scenario_code, :scenario_des_eng,
                :scenario_des_nat_lan, :active_flag, :exclude_expl_flag,
                :use_watchlist_flag, :violation_level, :degree_risk_flag,
                :default_scenario_flag, :run_flag, :approval_flag,
                :group_by_flag, :use_worldcheck_flag,
                :created_by, :created_date, :updated_by, :updated_date,
                :trans_withouttrans_flag, :category_code,
                :active_threshold_curr_flag, :sce_type_code, :class_code)""",
        {
            "country_code": int(scenario.country_code),
            "inst_code": int(scenario.inst_code),
            "scenario_code": scenario.scenario_code,
            "scenario_des_eng": scenario.scenario_des_eng,
            "scenario_des_nat_lan": scenario.scenario_des_nat_lan
            or scenario.scenario_des_eng,
            "active_flag": scenario.active_flag,
            "exclude_expl_flag": scenario.exclude_expl_flag,
            "use_watchlist_flag": scenario.use_watchlist_flag,
            "violation_level": scenario.violation_level,
            "degree_risk_flag": scenario.degree_risk_flag,
            "default_scenario_flag": scenario.default_scenario_flag,
            "run_flag": scenario.run_flag,
            "approval_flag": scenario.approval_flag,
            "group_by_flag": scenario.group_by_flag,
            "use_worldcheck_flag": scenario.use_worldcheck_flag,
            "created_by": int(scenario.created_by),
            "created_date": scenario.created_date,
            "updated_by": int(scenario.updated_by),
            "updated_date": scenario.updated_date,
            "trans_withouttrans_flag": scenario.trans_withouttrans_flag,
            "category_code": scenario.category_code,
            "active_threshold_curr_flag": scenario.active_threshold_curr_flag,
            "sce_type_code": scenario.sce_type_code,
            "class_code": scenario.class_code,
        },
    )
    logger.debug("[QB_WRITER] PIO_AML_SCENARIO staged: %s", scenario.scenario_code)


def _insert_rule_cursor(cursor: Any, rule: QBRule) -> None:
    """INSERT a row into PIO_AML_RULES using a shared cursor.

    Args:
        cursor: Open Oracle cursor from atomic_connection.
        rule (QBRule): Rule definition data.
    """
    cursor.execute(
        """INSERT INTO PIO_AML_RULES
               (COUNTRY_CODE, INST_CODE, RULE_CODE, RULE_DESC_ENG, RULE_DESC_ARB,
                ACTIVE_FLAG, PERIOD_TYPE, PERIOD_DAYS,
                USE_SEQUENTIALLY_FLAG, SEQUENTIALLY_COUNT,
                DEFAULT_RULE_FLAG, EXCLUDE_DAYS,
                LTG_CODE, UPDATED_BY, UPDATED_DATE)
           VALUES
               (:country_code, :inst_code, :rule_code, :rule_desc_eng, :rule_desc_arb,
                :active_flag, :period_type, :period_days,
                :use_sequentially_flag, :sequentially_count,
                :default_rule_flag, :exclude_days,
                :ltg_code, :updated_by, :updated_date)""",
        {
            "country_code": int(rule.country_code),
            "inst_code": int(rule.inst_code),
            "rule_code": rule.rule_code,
            "rule_desc_eng": rule.rule_desc_eng,
            "rule_desc_arb": rule.rule_desc_arb or rule.rule_desc_eng,
            "active_flag": rule.active_flag,
            "period_type": rule.period_type,
            "period_days": rule.period_days,
            "use_sequentially_flag": rule.use_sequentially_flag,
            "sequentially_count": rule.sequentially_count,
            "default_rule_flag": rule.default_rule_flag,
            "exclude_days": rule.exclude_days,
            "ltg_code": rule.ltg_code,
            "updated_by": int(rule.updated_by),
            "updated_date": rule.updated_date,
        },
    )
    logger.debug("[QB_WRITER] PIO_AML_RULES staged: %s", rule.rule_code)


def _insert_scenario_rule_cursor(cursor: Any, sr: QBScenarioRule) -> None:
    """INSERT a row into PIO_AML_SCENARIO_RULES using a shared cursor.

    Args:
        cursor: Open Oracle cursor from atomic_connection.
        sr (QBScenarioRule): Scenario-rule linkage data.
    """
    cursor.execute(
        """INSERT INTO PIO_AML_SCENARIO_RULES
               (COUNTRY_CODE, INST_CODE, AML_RULE_CODE, AML_SCENARIO,
                RULE_SEQ, RULE_TYPE, FREQUENCY_DAYS,
                AMT_PERC, MARGIN_PERC, STOP_PERIOD)
           VALUES
               (:country_code, :inst_code, :aml_rule_code, :aml_scenario,
                :rule_seq, :rule_type, :frequency_days,
                :amt_perc, :margin_perc, :stop_period)""",
        {
            "country_code": int(sr.country_code),
            "inst_code": int(sr.inst_code),
            "aml_rule_code": sr.aml_rule_code,
            "aml_scenario": sr.aml_scenario,
            "rule_seq": sr.rule_seq,
            "rule_type": sr.rule_type,
            "frequency_days": sr.frequency_days,
            "amt_perc": sr.amt_perc,
            "margin_perc": sr.margin_perc,
            "stop_period": sr.stop_period,
        },
    )
    logger.debug("[QB_WRITER] PIO_AML_SCENARIO_RULES staged.")


def _determine_combined_connectors(
    details: List[QBRuleDetail], detection_logic: str, raw_sql: str
) -> None:
    """Determine dynamic AND / OR / - connectors for QBRuleDetail rows based on logic and SQL syntax."""
    if not details:
        return

    raw_upper = (raw_sql or "").upper()
    logic_upper = (detection_logic or "").upper()

    for idx, d in enumerate(details):
        d.rule_seq = str(idx + 1)
        if idx == len(details) - 1:
            d.combined_rule = "-"
            continue

        connector = "AND"
        curr_val = str(d.comparison_value_from or "").strip()
        next_d = details[idx + 1]
        next_val = str(next_d.comparison_value_from or "").strip()

        # Check 1: Value comparison in logic or SQL (e.g. 50000 OR 2)
        if curr_val and next_val:
            pattern = (
                re.escape(curr_val) + r"[\s\S]*?\bOR\b[\s\S]*?" + re.escape(next_val)
            )
            if re.search(pattern, logic_upper, re.IGNORECASE) or re.search(
                pattern, raw_upper, re.IGNORECASE
            ):
                connector = "OR"

        # Check 2: Direct OR clause detection between descriptions in raw SQL or detection logic
        if connector == "AND":
            curr_des = str(d.comparison_value_from_des or "").upper()
            next_des = str(next_d.comparison_value_from_des or "").upper()
            if curr_des and next_des:
                des_pattern = (
                    re.escape(curr_des)
                    + r"[\s\S]*?\bOR\b[\s\S]*?"
                    + re.escape(next_des)
                )
                if re.search(des_pattern, logic_upper, re.IGNORECASE) or re.search(
                    des_pattern, raw_upper, re.IGNORECASE
                ):
                    connector = "OR"

        d.combined_rule = connector


def _insert_rule_details_cursor(cursor: Any, details: List[QBRuleDetail]) -> None:
    """Batch INSERT rows into PIO_AML_RULES_DETAILS using a shared cursor.

    Args:
        cursor: Open Oracle cursor from atomic_connection.
        details (List[QBRuleDetail]): List of condition rows to insert.
    """
    if not details:
        logger.debug("[QB_WRITER] No rule details to stage.")
        return

    sql = """INSERT INTO PIO_AML_RULES_DETAILS
                 (COUNTRY_CODE, INST_CODE, PARAMETER_CODE, RULE_CODE,
                  RULE_SEQ, RULE_OPERATOR, COMPARISON_VALUE_FROM,
                  COMBINED_RULE, COMPARISON_VALUE_FROM_DES,
                  USE_SD_FLAG, SD_PERIOD_TYPE, SAME_CUST_FLAG,
                  FROM_PARAM_PERC,
                  CREATED_BY, CREATED_DATE, UPDATED_BY, UPDATED_DATE)
             VALUES
                 (:country_code, :inst_code, :parameter_code, :rule_code,
                  :rule_seq, :rule_operator, :comparison_value_from,
                  :combined_rule, :comparison_value_from_des,
                  :use_sd_flag, :sd_period_type, :same_cust_flag,
                  :from_param_perc,
                  :created_by, :created_date, :updated_by, :updated_date)"""

    params_list = [
        {
            "country_code": int(d.country_code),
            "inst_code": int(d.inst_code),
            "parameter_code": d.parameter_code,
            "rule_code": d.rule_code,
            "rule_seq": d.rule_seq,
            "rule_operator": d.rule_operator,
            "comparison_value_from": d.comparison_value_from,
            "combined_rule": d.combined_rule,
            "comparison_value_from_des": d.comparison_value_from_des,
            "use_sd_flag": d.use_sd_flag,
            "sd_period_type": d.sd_period_type,
            "same_cust_flag": d.same_cust_flag,
            "from_param_perc": d.from_param_perc,
            "created_by": int(d.created_by),
            "created_date": d.created_date,
            "updated_by": int(d.updated_by),
            "updated_date": d.updated_date,
        }
        for d in details
    ]
    cursor.executemany(sql, params_list)
    logger.debug("[QB_WRITER] PIO_AML_RULES_DETAILS staged: %d rows", len(details))


# =============================================================================
# NODE 6 — SCENARIO VALIDATOR
# =============================================================================


def _check_threshold_sensitivity(rule_code: str) -> Dict[str, Any]:
    """Diagnose zero-alert scenarios by reporting ±20% threshold adjustments.

    Reads numeric COMPARISON_VALUE_FROM values from PIO_AML_RULES_DETAILS and
    calculates what a 20% loosening would look like. Does NOT modify any data —
    purely advisory output for the failure menu.

    Args:
        rule_code (str): The rule code whose details to inspect.

    Returns:
        Dict[str, Any]: Sensitivity report with per-parameter adjustment suggestions.
    """
    from web.services.oracle import run_readonly

    try:
        cols, rows = run_readonly(
            """SELECT PARAMETER_CODE, RULE_OPERATOR, COMPARISON_VALUE_FROM, RULE_SEQ
               FROM PIO_AML_RULES_DETAILS
               WHERE RULE_CODE = :rule_code
               AND REGEXP_LIKE(COMPARISON_VALUE_FROM, '^[0-9.]+$')
               ORDER BY RULE_SEQ""",
            {"rule_code": rule_code},
        )
        adjustments = []
        for row in rows:
            row_d = dict(zip(cols, row))
            try:
                current = float(row_d["comparison_value_from"])
                # For >= or > operators, loosening means reducing the threshold
                # For <= or < operators, loosening means raising the threshold
                op = row_d.get("rule_operator", ">=")
                if op in (">=", ">"):
                    looser = round(current * 0.8, 2)
                    tighter = round(current * 1.2, 2)
                else:
                    looser = round(current * 1.2, 2)
                    tighter = round(current * 0.8, 2)
                adjustments.append(
                    {
                        "parameter_code": row_d["parameter_code"],
                        "operator": op,
                        "current_value": current,
                        "looser_20pct": looser,
                        "tighter_20pct": tighter,
                        "suggestion": f"Change threshold from {current} to {looser} to capture more events",
                    }
                )
            except (ValueError, TypeError):
                pass

        return {
            "tested": len(adjustments) > 0,
            "adjustments": adjustments,
            "diagnosis": (
                "Zero alerts detected. Loosening the thresholds below by ~20% may capture events."
                if adjustments
                else "No numeric thresholds found to adjust."
            ),
        }
    except Exception as exc:
        logger.warning("[VALIDATOR] Sensitivity check failed: %s", exc)
        return {
            "tested": False,
            "adjustments": [],
            "diagnosis": f"Sensitivity check unavailable: {exc}",
        }


def validator_node(state: AMLScenarioState, config: RunnableConfig) -> Dict[str, Any]:
    """Agentic self-validation — hunt the scenario's own output.

    After writing the scenario to Oracle:
    1. Runs FILL_PIO_AML_CUSTOMERS to trigger all scenarios
    2. Queries PIO_AML_CUSTOMERS (header alerts) + PIO_AML_CUSTOMERS_DET (transactions)
    3. Calculates alert_density_ratio (det rows / header rows)
    4. Runs threshold sensitivity diagnostic when alert_count == 0
    5. Verifies write integrity from state
    6. Self-corrects up to MAX_VALIDATION_RETRIES if conditions not met

    Args:
        state (AMLScenarioState): Current agent state.
        config (RunnableConfig): LangGraph runtime config.

    Returns:
        Dict[str, Any]: State updates with validation_result and next_action.
    """
    retry_count = state.get("validation_retry_count", 0)
    scenario_code = state.get("scenario_code", "")
    rule_code = state.get("rule_code", "")
    intent_dict = state.get("enriched_intent") or {}

    if not state.get("scenario_write_success", False):
        logger.warning(
            "[VALIDATOR] Scenario write failed in previous step. Skipping database validation."
        )
        diagnosis = "The scenario parameters could not be written to Oracle. Check the error log for details."
        return {
            "validation_result": {
                "success": False,
                "scenario_status": "ERROR",
                "alert_count": 0,
                "sample_alerts": [],
                "diagnosis": diagnosis,
                "suggested_fix": "Verify that all configuration parameters and database column mappings are correct.",
                "retry_count": retry_count,
                "confidence_score": 0.0,
                "catalog_integrity": True,
            },
            "next_action": "FAILURE",
            "error_log": state.get("error_log", []) + [diagnosis],
        }

    logger.info(
        "[VALIDATOR] Validating scenario_code=%s (attempt %d/%d)",
        scenario_code,
        retry_count + 1,
        settings.MAX_VALIDATION_RETRIES,
    )

    from web.services.oracle import run_readonly, get_connection
    import oracledb

    try:
        # Step 1: Run the QB engine to process all active scenarios
        logger.info("[VALIDATOR] Calling FILL_PIO_AML_CUSTOMERS...")
        with get_connection() as conn:
            cursor = conn.cursor()
            p_status = cursor.var(oracledb.NUMBER)
            cursor.execute(
                """
                BEGIN
                    FILL_PIO_AML_CUSTOMERS(
                        COUNTRYCODE => :country_code,
                        INSTCODE => :inst_code,
                        P_STATUS => :p_status
                    );
                END;
                """,
                {
                    "country_code": int(settings.AML_COUNTRY_CODE),
                    "inst_code": int(settings.AML_INST_CODE),
                    "p_status": p_status,
                },
            )
            logger.info("[VALIDATOR] Procedure done. P_STATUS=%s", p_status.getvalue())

        # Step 2: Count header alerts (PIO_AML_CUSTOMERS)
        _, rows = run_readonly(
            "SELECT COUNT(*) FROM PIO_AML_CUSTOMERS WHERE AML_SCENARIO_CODE = :scenario_code",
            {"scenario_code": scenario_code},
        )
        alert_count = int(rows[0][0]) if rows else 0
        logger.info("[VALIDATOR] PIO_AML_CUSTOMERS count: %d", alert_count)

        # Step 3: Count + sample transaction detail (PIO_AML_CUSTOMERS_DET)
        _, det_count_rows = run_readonly(
            "SELECT COUNT(*) FROM PIO_AML_CUSTOMERS_DET WHERE AML_SCENARIO_CODE = :scenario_code",
            {"scenario_code": scenario_code},
        )
        det_count = int(det_count_rows[0][0]) if det_count_rows else 0
        logger.info("[VALIDATOR] PIO_AML_CUSTOMERS_DET count: %d", det_count)

        det_samples: List[Dict[str, Any]] = []
        if det_count > 0:
            with get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """SELECT * FROM PIO_AML_CUSTOMERS_DET
                       WHERE AML_SCENARIO_CODE = :scenario_code
                       AND ROWNUM <= 5""",
                    {"scenario_code": scenario_code},
                )
                det_cols = [c[0].lower() for c in cursor.description]
                det_samples = [dict(zip(det_cols, r)) for r in cursor.fetchall()]

        # Step 4: Density ratio — how many detail rows per header alert
        alert_density_ratio: Optional[float] = (
            round(det_count / alert_count, 2) if alert_count > 0 else None
        )
        logger.info("[VALIDATOR] Alert density ratio: %s", alert_density_ratio)

        # Step 5: Sanity check — both tables must have records for success
        intent = AMLIntent(**intent_dict)
        diagnosis: Optional[str] = None
        suggested_fix: Optional[str] = None
        threshold_sensitivity: Optional[Dict[str, Any]] = None

        if alert_count == 0:
            threshold_sensitivity = _check_threshold_sensitivity(rule_code)
            diagnosis = (
                "Zero header alerts generated. The scenario conditions may be too restrictive, "
                "the time window may be too narrow, or the parameter codes may need adjustment."
            )
            suggested_fix = (
                "Try widening the threshold values or extending the time window. "
                "Verify that PARAMETER_CODE values match PIO_AML_PARAMETERS."
            )
        elif det_count == 0:
            diagnosis = (
                f"Header alerts found ({alert_count:,}) but PIO_AML_CUSTOMERS_DET has zero "
                "transaction rows. The QB engine may not have populated detail records."
            )
            suggested_fix = "Check that FILL_PIO_AML_CUSTOMERS populates PIO_AML_CUSTOMERS_DET for this scenario type."
        elif (
            intent.expected_alert_range_max
            and alert_count > intent.expected_alert_range_max
        ):
            diagnosis = (
                f"Alert volume ({alert_count:,}) exceeds expected maximum "
                f"({intent.expected_alert_range_max:,}). Scenario may be too broad."
            )
            suggested_fix = "Tighten the threshold values or add exclusion criteria."

        # Step 6: Pull write_verification from state and check all_pass
        write_verification_dict: Optional[Dict[str, Any]] = state.get(
            "write_verification"
        )
        write_ok = (
            write_verification_dict.get("all_pass", False)
            if write_verification_dict
            else True  # if no verification data, don't block (backward compat)
        )

        # Step 7: Catalog integrity from state
        catalog_integrity: bool = state.get("catalog_integrity", True)

        # Step 8: Pull header samples
        sample_alerts: List[AlertSample] = []
        if alert_count > 0:
            sample_cols, sample_rows = run_readonly(
                """SELECT * FROM PIO_AML_CUSTOMERS
                   WHERE AML_SCENARIO_CODE = :scenario_code
                   AND ROWNUM <= 5""",
                {"scenario_code": scenario_code},
            )
            for row in sample_rows:
                row_dict = dict(zip(sample_cols, row))
                customer_id = str(
                    row_dict.get("cus_num", row_dict.get("customer_id", "—"))
                )
                sample_alerts.append(
                    AlertSample(customer_id=customer_id, raw_data=row_dict)
                )

        # Step 9: Success gate — both tables ≥1 AND write integrity passes
        success = alert_count >= 1 and det_count >= 1 and write_ok and diagnosis is None

        # Step 10: Assemble WriteVerification for the result if available
        from web.services.schemas import WriteVerification

        write_integrity_obj: Optional[WriteVerification] = None
        if write_verification_dict:
            try:
                write_integrity_obj = WriteVerification(**write_verification_dict)
            except Exception:
                pass

        confidence = 1.0 if success else max(0.0, 1.0 - (retry_count * 0.3))

        result = ValidationResult(
            success=success,
            scenario_status="ACTIVE" if success else "REVIEW_NEEDED",
            scenario_code=scenario_code,
            raw_sql=state.get("raw_sql"),
            scenario_name=intent.scenario_name,
            alert_count=alert_count,
            sample_alerts=sample_alerts,
            diagnosis=diagnosis,
            suggested_fix=suggested_fix,
            retry_count=retry_count,
            confidence_score=confidence,
            det_count=det_count,
            det_samples=det_samples,
            alert_density_ratio=alert_density_ratio,
            write_integrity=write_integrity_obj,
            catalog_integrity=catalog_integrity,
            threshold_sensitivity=threshold_sensitivity,
        )

        if success:
            return {
                "validation_result": result.model_dump(mode="json"),
                "next_action": "FINALIZE",
                "validation_retry_count": retry_count,
            }

        # Failed — should we retry?
        if retry_count < settings.MAX_VALIDATION_RETRIES - 1:
            logger.warning("[VALIDATOR] Validation failed. Triggering self-correction.")
            return {
                "validation_result": result.model_dump(mode="json"),
                "next_action": "DECOMPOSE",
                "validation_retry_count": retry_count + 1,
                "error_log": state.get("error_log", [])
                + [f"Validation attempt {retry_count + 1}: {diagnosis}"],
            }

        # Max retries hit — surface failure menu
        logger.error("[VALIDATOR] Max retries reached. Routing to FAILURE.")
        return {
            "validation_result": result.model_dump(mode="json"),
            "next_action": "FAILURE",
            "validation_retry_count": retry_count + 1,
            "error_log": state.get("error_log", [])
            + [f"Max validation retries reached. Last diagnosis: {diagnosis}"],
        }

    except Exception as exc:
        logger.error("[VALIDATOR] Error during validation: %s", exc, exc_info=True)
        return {
            "next_action": "FAILURE",
            "error_log": state.get("error_log", []) + [f"Validation error: {exc}"],
        }


# =============================================================================
# ROUTING FUNCTIONS
# =============================================================================


def route_after_orchestrator(state: AMLScenarioState) -> str:
    """Route after the agentic orchestrator.

    The orchestrator resolves all internal states (REDEFINE, ADJUST, ESCALATE,
    FINALIZE, FAILURE, ERROR, CLARIFY) before returning, so this function only
    needs to handle the six clean output actions the orchestrator can produce.

    Args:
        state (AMLScenarioState): Current state.

    Returns:
        str: LangGraph node name or END.
    """
    action = state.get("next_action", "INTENT")
    route_map = {
        "INTENT": "intent_analyst",
        "SQL_BRIDGE": "sql_bridge",
        "DECOMPOSE": "decomposer",
        "WAIT_USER": END,
        "WAIT_APPROVAL": END,
        "END": END,
    }
    return route_map.get(action, END)


def route_after_intent(state: AMLScenarioState) -> str:
    """Route after Intent Analyst node."""
    action = state.get("next_action", "SQL_BRIDGE")
    checkpoint = state.get("explanation_code_checkpoint")
    confirmed = state.get("explanation_codes_confirmed", False)

    if checkpoint and not confirmed:
        logger.info(
            "[ROUTER] Domain explanation code checkpoint active — routing to orchestrator."
        )
        return "orchestrator"

    if action in ("CLARIFY", "CONFIRM_DOMAIN", "ERROR"):
        return "orchestrator"

    return "planner"  # always plan first before sql_bridge


def route_after_planner(state: AMLScenarioState) -> str:
    """Route after the Planner node.

    On success: returns END to pause execution and wait for user approval.
    On error: routes to orchestrator for failure handling.

    Args:
        state (AMLScenarioState): Current state.

    Returns:
        str: END or 'orchestrator'.
    """
    action = state.get("next_action", "WAIT_APPROVAL")
    if action == "ERROR":
        return "orchestrator"
    return END  # Pause execution and wait for user approval


def route_after_sql_bridge(state: AMLScenarioState) -> str:
    """Route after SQL Bridge node.

    Args:
        state (AMLScenarioState): Current state.

    Returns:
        str: Next node name — orchestrator on any failure, decomposer otherwise.
    """
    action = state.get("next_action", "DECOMPOSE")
    if action in ("ERROR", "FAILURE"):
        return "orchestrator"
    return "decomposer"


def route_after_decomposer(state: AMLScenarioState) -> str:
    """Route after Decomposer node.

    Args:
        state (AMLScenarioState): Current state.

    Returns:
        str: Next node name — orchestrator on any failure, qb_writer otherwise.
    """
    action = state.get("next_action", "QB_WRITE")
    if action in ("ERROR", "FAILURE"):
        return "orchestrator"
    return "qb_writer"


def route_after_validator(state: AMLScenarioState) -> str:
    """Route after Validator node.

    DECOMPOSE → retry loop back to decomposer.
    Everything else (FINALIZE, FAILURE, ERROR) → orchestrator to handle.

    Args:
        state (AMLScenarioState): Current state.

    Returns:
        str: Next node name.
    """
    action = state.get("next_action", "FINALIZE")
    if action == "DECOMPOSE":
        return "decomposer"
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
    graph.add_node("decomposer", decomposer_node)
    graph.add_node("qb_writer", qb_writer_node)
    graph.add_node("validator", validator_node)

    # Entry point
    graph.set_entry_point("orchestrator")

    # Edges with conditional routing
    graph.add_conditional_edges("orchestrator", route_after_orchestrator)
    graph.add_conditional_edges("intent_analyst", route_after_intent)
    graph.add_conditional_edges("planner", route_after_planner)
    graph.add_conditional_edges("sql_bridge", route_after_sql_bridge)
    graph.add_conditional_edges("decomposer", route_after_decomposer)
    graph.add_edge("qb_writer", "validator")
    graph.add_conditional_edges("validator", route_after_validator)

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
