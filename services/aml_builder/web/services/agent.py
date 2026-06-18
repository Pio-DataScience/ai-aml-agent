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
    QBRule,
    QBRuleDetail,
    QBScenario,
    QBScenarioRule,
    ScenarioParameters,
    SQLMetadata,
    ValidationResult,
)
from web.services.settings import settings

logger = logging.getLogger(__name__)


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


def orchestrator_node(state: AMLScenarioState, config: RunnableConfig) -> Dict[str, Any]:
    """Route the conversation and manage the lifecycle.

    The Orchestrator is the entry point for every user turn. It:
    - Detects the current stage and routes to the correct next node
    - Owns all final user-facing communication
    - Enforces loop limits

    Args:
        state (AMLScenarioState): Current agent state.
        config (RunnableConfig): LangGraph runtime config.

    Returns:
        Dict[str, Any]: State updates including next_action routing signal.
    """
    logger.info("[ORCHESTRATOR] Iteration %d", state.get("iteration_count", 0))

    messages = state.get("messages", [])
    iteration = state.get("iteration_count", 0) + 1
    next_action = state.get("next_action", "INTENT")

    # Guard: enforce hard iteration cap
    if iteration > settings.MAX_AGENT_ITERATIONS:
        logger.error("[ORCHESTRATOR] Max iterations reached. Halting.")
        return {
            "messages": [
                AIMessage(
                    content=(
                        "⚠️ I've reached my processing limit for this request. "
                        "Please try rephrasing or breaking the request into smaller steps."
                    )
                )
            ],
            "next_action": "END",
            "iteration_count": iteration,
        }

    # If validation succeeded → format final answer and end
    if next_action == "FINALIZE":
        return _finalize_response(state, iteration)

    # If we need clarification → pass through the question to user
    if next_action == "CLARIFY":
        intent_data = state.get("enriched_intent") or {}
        questions = intent_data.get("clarification_questions", [])
        if questions:
            numbered = "\n".join(f"{i+1}. {q}" for i, q in enumerate(questions))
            msg = (
                "To build this scenario correctly, I need a few clarifications:\n\n"
                f"{numbered}\n\n"
                "Please provide these details and I'll proceed immediately."
            )
        else:
            msg = "Could you provide more details about the scenario you'd like to create?"

        return {
            "messages": [AIMessage(content=msg)],
            "next_action": "WAIT_USER",
            "iteration_count": iteration,
        }

    # If there was a write error
    if next_action == "ERROR":
        error_log = state.get("error_log", [])
        last_error = error_log[-1] if error_log else "Unknown error occurred."
        return {
            "messages": [
                AIMessage(
                    content=(
                        f"❌ I encountered an issue while creating the scenario:\n\n"
                        f"```\n{last_error}\n```\n\n"
                        "Please check the Oracle connection or try again. "
                        "If this persists, the implementation team can investigate."
                    )
                )
            ],
            "next_action": "END",
            "iteration_count": iteration,
        }

    # Default: route to intent analyst for new user input
    return {
        "next_action": "INTENT",
        "iteration_count": iteration,
    }


def _finalize_response(state: AMLScenarioState, iteration: int) -> Dict[str, Any]:
    """Build the final user-facing success message with scenario results.

    Args:
        state (AMLScenarioState): Current agent state.
        iteration (int): Current iteration count.

    Returns:
        Dict[str, Any]: State update with final AIMessage and END signal.
    """
    val_data = state.get("validation_result") or {}
    intent_data = state.get("enriched_intent") or {}
    scenario_code = state.get("scenario_code", "N/A")
    alert_count = val_data.get("alert_count", 0)
    samples = val_data.get("sample_alerts", [])
    scenario_name = intent_data.get("scenario_name", "AML Scenario")

    # Build sample table
    sample_rows = ""
    if samples:
        sample_rows = "\n| Customer ID | Details |\n|-------------|---------|"
        for s in samples[:5]:
            cid = s.get("customer_id", "—")
            raw = s.get("raw_data", {})
            detail = ", ".join(f"{k}: {v}" for k, v in list(raw.items())[:3])
            sample_rows += f"\n| `{cid}` | {detail} |"

    confidence = val_data.get("confidence_score", 0.0)
    confidence_label = "🟢 High" if confidence >= 0.8 else "🟡 Medium" if confidence >= 0.5 else "🔴 Low"

    message = (
        f"## ✅ Scenario Created Successfully\n\n"
        f"**Scenario Code:** `{scenario_code}`  \n"
        f"**Name:** {scenario_name}\n\n"
        f"---\n\n"
        f"### 📊 Live Impact Assessment\n"
        f"- **Active Alerts:** {alert_count} customers\n"
        f"- **Confidence:** {confidence_label} ({confidence:.0%})\n"
        f"\n### 👥 Sample Matched Customers\n"
        f"{sample_rows if sample_rows else '_No sample data available._'}\n\n"
        f"---\n\n"
        f"> The scenario is now **ACTIVE** in the AML module.  \n"
        f"> Would you like to adjust thresholds, view the full alert list, or create another scenario?"
    )

    return {
        "messages": [AIMessage(content=message)],
        "next_action": "END",
        "iteration_count": iteration,
    }


# =============================================================================
# NODE 2 — INTENT ANALYST
# =============================================================================


def intent_analyst_node(state: AMLScenarioState, config: RunnableConfig) -> Dict[str, Any]:
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

    system_prompt = _load_prompt("intent_analyst_system.md")
    llm = _build_llm(fast=False)

    # Request structured JSON output
    structured_prompt = f"""{system_prompt}

---

## TASK
Analyze the user's request and return a JSON object matching the AMLIntent schema.

USER REQUEST: "{last_user_msg}"

CONVERSATION HISTORY (last 3 turns):
{_format_history(messages, n=3)}

Return ONLY valid JSON. No explanation, no markdown fences.
Schema:
{{
  "scenario_name": "string",
  "scenario_type": "TRANSACTION|ACCOUNT|CUSTOMER",
  "detection_logic": "string",
  "thresholds": [{{"field":"string","operator":"string","value_from":number,"value_to":null}}],
  "time_window": {{"unit":"DAYS|MONTHS|YEARS","value":number,"is_rolling":true}} | null,
  "customer_segments": ["string"] | null,
  "exclusions": ["string"] | null,
  "clarification_needed": true|false,
  "clarification_questions": ["string"],
  "expected_alert_range_min": number | null,
  "expected_alert_range_max": number | null
}}
"""

    try:
        response = llm.invoke([SystemMessage(content=structured_prompt)])
        raw_content = response.content.strip()

        # Strip any accidental markdown fences
        raw_content = re.sub(r"^```(?:json)?\s*", "", raw_content)
        raw_content = re.sub(r"\s*```$", "", raw_content)

        intent_dict = json.loads(raw_content)
        # Validate with Pydantic
        intent = AMLIntent(**intent_dict)

        logger.info(
            "[INTENT_ANALYST] Intent parsed. scenario_type=%s clarification_needed=%s",
            intent.scenario_type,
            intent.clarification_needed,
        )

        next_action = "CLARIFY" if intent.clarification_needed else "SQL_BRIDGE"

        return {
            "enriched_intent": intent.model_dump(),
            "user_intent": last_user_msg,
            "next_action": next_action,
        }

    except (json.JSONDecodeError, Exception) as exc:
        logger.error("[INTENT_ANALYST] Failed to parse intent: %s", exc, exc_info=True)
        return {
            "next_action": "ERROR",
            "error_log": state.get("error_log", []) + [f"Intent parsing failed: {exc}"],
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

    # Construct a precise, context-rich prompt for the DWH agent
    thresholds_text = "\n".join(
        f"  - {t.field} {t.operator} {t.value_from}"
        + (f" AND {t.value_to}" if t.value_to else "")
        for t in intent.thresholds
    )

    time_text = "No time window specified."
    if intent.time_window:
        tw = intent.time_window
        time_text = f"Rolling {tw.value} {tw.unit}" if tw.is_rolling else f"Fixed {tw.value} {tw.unit}"

    segments_text = ", ".join(intent.customer_segments) if intent.customer_segments else "All segments"
    exclusions_text = "\n".join(f"  - {e}" for e in intent.exclusions) if intent.exclusions else "None"

    aml_prompt = (
        f"<AML_SCENARIO_REQUEST>\n"
        f"SCENARIO_NAME: {intent.scenario_name}\n"
        f"SCENARIO_TYPE: {intent.scenario_type}\n"
        f"DETECTION_LOGIC: {intent.detection_logic}\n\n"
        f"THRESHOLDS:\n{thresholds_text}\n\n"
        f"TIME_WINDOW: {time_text}\n"
        f"CUSTOMER_SEGMENTS: {segments_text}\n"
        f"EXCLUSIONS:\n{exclusions_text}\n\n"
        f"TASK: Write a SQL SELECT query that identifies {intent.scenario_type.lower()}s "
        f"matching this AML detection scenario. The query must:\n"
        f"1. Return the primary entity (customer/account/transaction identifier)\n"
        f"2. Use GROUP BY if counting occurrences\n"
        f"3. Use HAVING for aggregate filters\n"
        f"4. Apply time filters using SYSDATE arithmetic\n"
        f"5. Use ONLY BI_DWH schema tables\n\n"
        f"RETURN_FORMAT: Return ONLY the SQL query. No explanation. No markdown.\n"
        f"</AML_SCENARIO_REQUEST>"
    )

    chat_id = f"aml_builder_{uuid.uuid4().hex[:8]}"

    payload = {
        "messages": [{"role": "user", "content": aml_prompt}],
        "metadata": {
            "user_id": settings.PIOTECH_AI_USER_ID,
            "project_id": settings.PIOTECH_AI_PROJECT_ID,
            "chat_id": chat_id,
        },
        "reasoning_mode": "instant",
    }

    collected_text = []

    try:
        with httpx.Client(timeout=settings.PIOTECH_AI_TIMEOUT_SECONDS) as client:
            with client.stream(
                "POST",
                settings.PIOTECH_AI_URL,
                json=payload,
                headers={"Accept": "text/event-stream", "Content-Type": "application/json"},
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
                        if event_type in ("content", "final_answer"):
                            collected_text.append(event.get("text", ""))
                    except json.JSONDecodeError:
                        continue

        full_response = "".join(collected_text).strip()
        logger.info("[SQL_BRIDGE] PioTech AI response received (%d chars).", len(full_response))

        # Extract SQL from the response
        sql = _extract_sql(full_response)

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
            "[SQL_BRIDGE] SQL extracted. tables=%s", metadata.tables
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
            "error_log": state.get("error_log", [])
            + [f"SQL Bridge HTTP error: {exc}"],
        }


def _extract_sql(text: str) -> str:
    """Extract a clean SQL query from LLM response text.

    Args:
        text (str): Raw text response from PioTech AI.

    Returns:
        str: The extracted SQL query, or empty string if none found.
    """
    # Try code block first
    match = re.search(r"```(?:sql)?\s*([\s\S]+?)```", text, re.IGNORECASE)
    if match:
        return match.group(1).strip()

    # Look for SELECT / WITH statement
    match = re.search(r"((?:SELECT|WITH)\s+[\s\S]+?)(?:\n\n|$)", text, re.IGNORECASE)
    if match:
        return match.group(1).strip()

    # If the entire response looks like SQL
    stripped = text.strip()
    if re.match(r"^(SELECT|WITH)\s+", stripped, re.IGNORECASE):
        return stripped

    return ""


def _parse_sql_metadata(sql: str) -> SQLMetadata:
    """Parse a SQL query to extract structural metadata.

    Uses regex-based parsing (compatible without sqlglot as dependency).
    Extracts tables, WHERE conditions, GROUP BY fields, HAVING conditions.

    Args:
        sql (str): The SQL query string.

    Returns:
        SQLMetadata: Populated metadata object.
    """
    # Extract tables (FROM and JOIN clauses)
    tables = re.findall(
        r"(?:FROM|JOIN)\s+(BI_DWH\.\w+|\w+\.\w+|\w+)",
        sql,
        re.IGNORECASE,
    )
    tables = list(dict.fromkeys(tables))  # deduplicate preserving order
    primary_table = tables[0] if tables else ""

    # Extract WHERE conditions (simplified: split by AND/OR)
    where_match = re.search(
        r"WHERE\s+([\s\S]+?)(?:GROUP BY|HAVING|ORDER BY|$)",
        sql,
        re.IGNORECASE,
    )
    where_conditions = []
    if where_match:
        raw_where = where_match.group(1).strip()
        where_conditions = [c.strip() for c in re.split(r"\bAND\b|\bOR\b", raw_where, flags=re.IGNORECASE)]
        where_conditions = [c for c in where_conditions if c]

    # Extract GROUP BY fields
    group_match = re.search(r"GROUP BY\s+([\s\S]+?)(?:HAVING|ORDER BY|$)", sql, re.IGNORECASE)
    group_by_fields = []
    if group_match:
        group_by_fields = [f.strip() for f in group_match.group(1).split(",")]

    # Extract HAVING conditions
    having_match = re.search(r"HAVING\s+([\s\S]+?)(?:ORDER BY|$)", sql, re.IGNORECASE)
    having_conditions = []
    if having_match:
        raw_having = having_match.group(1).strip()
        having_conditions = [c.strip() for c in re.split(r"\bAND\b|\bOR\b", raw_having, flags=re.IGNORECASE)]
        having_conditions = [c for c in having_conditions if c]

    # Detect date fields
    date_fields = re.findall(r"\b(\w*DATE\w*|\w*TIME\w*|\w*DT\b)\b", sql, re.IGNORECASE)
    date_fields = list(set(date_fields))

    # Detect aggregations
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

    Uses an LLM to handle the semantic mapping of SQL constructs to
    AML parameter codes, guided by strict rules.

    Args:
        state (AMLScenarioState): Current agent state.
        config (RunnableConfig): LangGraph runtime config.

    Returns:
        Dict[str, Any]: State updates with scenario_parameters and next_action.
    """
    logger.info("[DECOMPOSER] Mapping SQL to QB parameters.")

    intent_dict = state.get("enriched_intent") or {}
    sql_meta_dict = state.get("sql_metadata") or {}
    intent = AMLIntent(**intent_dict)
    sql_meta = SQLMetadata(**sql_meta_dict)

    # Generate unique codes
    # SCENARIO_CODE is VARCHAR2(40) in header, but NUMBER in rules details.
    # Therefore, generate a purely numeric string for the scenario code.
    import time
    import random
    epoch_ms = int(time.time() * 1000)
    rand_suffix = random.randint(100, 999)
    scenario_code = f"{epoch_ms}{rand_suffix}"

    timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    short_id = uuid.uuid4().hex[:4].upper()
    rule_code = f"RL_{timestamp}_{short_id}"

    # Build scenario header
    scenario = QBScenario(
        country_code=settings.AML_COUNTRY_CODE,
        inst_code=settings.AML_INST_CODE,
        scenario_code=scenario_code,
        scenario_des_eng=intent.scenario_name,
        active_flag=settings.AML_DEFAULT_ACTIVE_FLAG,
        violation_level=settings.AML_DEFAULT_VIOLATION_LEVEL,
        degree_risk_flag=settings.AML_DEFAULT_DEGREE_RISK_FLAG,
        created_by=settings.AML_CREATED_BY,
        created_date=datetime.utcnow(),
    )

    # Determine period from intent time window
    period_type = "D"
    period_days = 30  # sensible default
    if intent.time_window:
        tw = intent.time_window
        period_days = tw.value
        period_type = tw.unit[0]  # D / M / Y

    # Build rule
    rule = QBRule(
        country_code=settings.AML_COUNTRY_CODE,
        inst_code=settings.AML_INST_CODE,
        rule_code=rule_code,
        rule_des_eng=f"Rule for {intent.scenario_name}",
        active_flag="Y",
        period_type=period_type,  # type: ignore[arg-type]
        period_days=period_days,
        created_by=settings.AML_CREATED_BY,
        created_date=datetime.utcnow(),
    )

    # Build scenario-rule link
    scenario_rule = QBScenarioRule(
        country_code=settings.AML_COUNTRY_CODE,
        inst_code=settings.AML_INST_CODE,
        aml_rule_code=rule_code,
        aml_scenario=scenario_code,
        rule_seq=1,
        rule_type="T",
    )

    # Use LLM to map thresholds → rule details (PARAMETER_CODE lookup)
    rule_details = _map_thresholds_to_rule_details(
        intent=intent,
        sql_meta=sql_meta,
        rule_code=rule_code,
        scenario_code=scenario_code,
    )

    # Self-assess decomposition confidence
    confidence = _assess_confidence(intent, rule_details)

    notes = [
        f"Scenario code: {scenario_code}",
        f"Rule code: {rule_code}",
        f"Period: {period_days} {period_type}",
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
        "next_action": "QB_WRITE",
    }


def _map_thresholds_to_rule_details(
    intent: AMLIntent,
    sql_meta: SQLMetadata,
    rule_code: str,
    scenario_code: str,
) -> List[QBRuleDetail]:
    """Map intent thresholds and SQL conditions to QBRuleDetail rows.

    Uses a direct mapping from intent thresholds + SQL HAVING conditions.
    The PARAMETER_CODE is derived from the field name — this will be
    enriched further once pio_aml_parameters catalog is queryable.

    Args:
        intent (AMLIntent): Enriched user intent.
        sql_meta (SQLMetadata): Parsed SQL metadata.
        rule_code (str): The rule code to link details to.
        scenario_code (str): The scenario code to link details to.

    Returns:
        List[QBRuleDetail]: List of condition rows for PIO_AML_RULES_DETAILS.
    """
    details = []
    seq = 1

    operator_map = {
        ">": "GT", "<": "LT", ">=": "GE", "<=": "LE",
        "=": "EQ", "BETWEEN": "BTW", "IN": "IN",
    }

    # Map each intent threshold
    for threshold in intent.thresholds:
        op_code = operator_map.get(threshold.operator, "EQ")
        param_code = _derive_parameter_code(threshold.field)

        detail = QBRuleDetail(
            country_code=settings.AML_COUNTRY_CODE,
            inst_code=settings.AML_INST_CODE,
            parameter_code=param_code,
            rule_code=rule_code,
            rule_seq=seq,
            rule_operator=op_code,
            comparison_value_from=str(threshold.value_from),
            comparison_value_to=str(threshold.value_to) if threshold.value_to else None,
            comparison_value_from_des=f"{threshold.field} {threshold.operator} {threshold.value_from}",
            combined_rule="AND",
            scenario_code=scenario_code,
        )
        details.append(detail)
        seq += 1

    # Map HAVING conditions (aggregate thresholds e.g. COUNT(*) > 5)
    for having in sql_meta.having_conditions:
        count_match = re.search(r"COUNT\s*\(\s*\*?\s*\)\s*([><=!]+)\s*(\d+)", having, re.IGNORECASE)
        if count_match:
            op_raw = count_match.group(1).strip()
            val = count_match.group(2).strip()
            op_code = operator_map.get(op_raw, "GT")

            detail = QBRuleDetail(
                country_code=settings.AML_COUNTRY_CODE,
                inst_code=settings.AML_INST_CODE,
                parameter_code="TXN_COUNT",  # aggregate count parameter
                rule_code=rule_code,
                rule_seq=seq,
                rule_operator="HAV_CNT",
                comparison_value_from=val,
                comparison_value_from_des=f"Transaction count {op_raw} {val}",
                combined_rule="AND",
                scenario_code=scenario_code,
            )
            details.append(detail)
            seq += 1

    return details


def _derive_parameter_code(field_name: str) -> str:
    """Derive a plausible AML parameter code from a business field name.

    This is a best-effort mapping. The authoritative source is
    pio_aml_parameters which will be queried during integration.

    Args:
        field_name (str): Business field name from the intent.

    Returns:
        str: Uppercased parameter code.
    """
    mapping = {
        "amount": "TXN_AMT",
        "transaction_amount": "TXN_AMT",
        "balance": "ACC_BAL",
        "account_balance": "ACC_BAL",
        "count": "TXN_COUNT",
        "transaction_count": "TXN_COUNT",
        "date": "TXN_DATE",
        "transaction_date": "TXN_DATE",
        "customer": "CUS_NUM",
        "customer_id": "CUS_NUM",
        "type": "TXN_TYPE",
        "transaction_type": "TXN_TYPE",
    }
    normalized = field_name.lower().strip().replace(" ", "_")
    return mapping.get(normalized, normalized.upper())


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
# NODE 5 — QB WRITER
# =============================================================================


def qb_writer_node(state: AMLScenarioState, config: RunnableConfig) -> Dict[str, Any]:
    """Write the decomposed parameters into Oracle AML tables.

    Inserts rows into:
    1. PIO_AML_SCENARIO (scenario header)
    2. PIO_AML_RULES (rule definition)
    3. PIO_AML_SCENARIO_RULES (linkage)
    4. PIO_AML_RULES_DETAILS (conditions)

    Args:
        state (AMLScenarioState): Current agent state.
        config (RunnableConfig): LangGraph runtime config.

    Returns:
        Dict[str, Any]: State updates with scenario_write_success and next_action.
    """
    logger.info("[QB_WRITER] Writing scenario to Oracle.")

    params_dict = state.get("scenario_parameters") or {}
    params = ScenarioParameters(**params_dict)

    from web.services.oracle import run_write, run_write_many

    try:
        # 1. Insert scenario header
        _insert_scenario(params.scenario)

        # 2. Insert rules
        for rule in params.rules:
            _insert_rule(rule)

        # 3. Insert scenario-rule links
        for sr in params.scenario_rules:
            _insert_scenario_rule(sr)

        # 4. Insert rule details (batch)
        _insert_rule_details(params.rule_details)

        logger.info(
            "[QB_WRITER] All rows written. scenario_code=%s",
            params.scenario.scenario_code,
        )

        return {
            "scenario_write_success": True,
            "scenario_code": params.scenario.scenario_code,
            "next_action": "VALIDATE",
        }

    except Exception as exc:
        logger.error("[QB_WRITER] Oracle write failed: %s", exc, exc_info=True)
        return {
            "scenario_write_success": False,
            "next_action": "ERROR",
            "error_log": state.get("error_log", []) + [f"QB Write failed: {exc}"],
        }


def _insert_scenario(scenario: QBScenario) -> None:
    """INSERT a row into PIO_AML_SCENARIO.

    Args:
        scenario (QBScenario): Scenario header data.
    """
    from web.services.oracle import run_write

    sql = """
        INSERT INTO PIO_AML_SCENARIO
            (COUNTRY_CODE, INST_CODE, SCENARIO_CODE, SCENARIO_DES_ENG,
             ACTIVE_FLAG, VIOLATION_LEVEL, DEGREE_RISK_FLAG,
             CREATED_BY, CREATED_DATE)
        VALUES
            (:country_code, :inst_code, :scenario_code, :scenario_des_eng,
             :active_flag, :violation_level, :degree_risk_flag,
             :created_by, :created_date)
    """
    run_write(sql, {
        "country_code": scenario.country_code,
        "inst_code": scenario.inst_code,
        "scenario_code": scenario.scenario_code,
        "scenario_des_eng": scenario.scenario_des_eng,
        "active_flag": scenario.active_flag,
        "violation_level": scenario.violation_level,
        "degree_risk_flag": scenario.degree_risk_flag,
        "created_by": scenario.created_by,
        "created_date": scenario.created_date,
    })
    logger.debug("[QB_WRITER] PIO_AML_SCENARIO inserted: %s", scenario.scenario_code)


def _insert_rule(rule: QBRule) -> None:
    """INSERT a row into PIO_AML_RULES.

    Args:
        rule (QBRule): Rule definition data.
    """
    from web.services.oracle import run_write

    sql = """
        INSERT INTO PIO_AML_RULES
            (COUNTRY_CODE, INST_CODE, RULE_CODE, RULE_DESC_ENG,
             ACTIVE_FLAG, PERIOD_TYPE, PERIOD_DAYS,
             LTG_CODE, CREATED_BY, CREATED_DATE)
        VALUES
            (:country_code, :inst_code, :rule_code, :rule_des_eng,
             :active_flag, :period_type, :period_days,
             :ltg_code, :created_by, :created_date)
    """
    run_write(sql, {
        "country_code": rule.country_code,
        "inst_code": rule.inst_code,
        "rule_code": rule.rule_code,
        "rule_des_eng": rule.rule_des_eng,
        "active_flag": rule.active_flag,
        "period_type": rule.period_type,
        "period_days": rule.period_days,
        "ltg_code": rule.ltg_code,
        "created_by": rule.created_by,
        "created_date": rule.created_date,
    })
    logger.debug("[QB_WRITER] PIO_AML_RULES inserted: %s", rule.rule_code)


def _insert_scenario_rule(sr: QBScenarioRule) -> None:
    """INSERT a row into PIO_AML_SCENARIO_RULES.

    Args:
        sr (QBScenarioRule): Scenario-rule linkage data.
    """
    from web.services.oracle import run_write

    sql = """
        INSERT INTO PIO_AML_SCENARIO_RULES
            (COUNTRY_CODE, INST_CODE, AML_RULE_CODE, AML_SCENARIO,
             RULE_SEQ, RULE_TYPE, AMT_PERC, MARGIN_PERC, STOP_PERIOD)
        VALUES
            (:country_code, :inst_code, :aml_rule_code, :aml_scenario,
             :rule_seq, :rule_type, :amt_perc, :margin_perc, :stop_period)
    """
    run_write(sql, {
        "country_code": sr.country_code,
        "inst_code": sr.inst_code,
        "aml_rule_code": sr.aml_rule_code,
        "aml_scenario": sr.aml_scenario,
        "rule_seq": sr.rule_seq,
        "rule_type": sr.rule_type,
        "amt_perc": sr.amt_perc,
        "margin_perc": sr.margin_perc,
        "stop_period": sr.stop_period,
    })
    logger.debug("[QB_WRITER] PIO_AML_SCENARIO_RULES inserted.")


def _insert_rule_details(details: List[QBRuleDetail]) -> None:
    """Batch INSERT rows into PIO_AML_RULES_DETAILS.

    Args:
        details (List[QBRuleDetail]): List of condition rows to insert.
    """
    from web.services.oracle import run_write_many

    sql = """
        INSERT INTO PIO_AML_RULES_DETAILS
            (COUNTRY_CODE, INST_CODE, PARAMETER_CODE, RULE_CODE,
             RULE_SEQ, RULE_OPERATOR, COMPARISON_VALUE_FROM,
             COMPARISON_VALUE_TO, COMPARISON_VALUE_FROM_DES,
             COMBINED_RULE, SCENARIO_CODE)
        VALUES
            (:country_code, :inst_code, :parameter_code, :rule_code,
             :rule_seq, :rule_operator, :comparison_value_from,
             :comparison_value_to, :comparison_value_from_des,
             :combined_rule, :scenario_code)
    """
    params_list = [
        {
            "country_code": d.country_code,
            "inst_code": d.inst_code,
            "parameter_code": d.parameter_code,
            "rule_code": d.rule_code,
            "rule_seq": d.rule_seq,
            "rule_operator": d.rule_operator,
            "comparison_value_from": d.comparison_value_from,
            "comparison_value_to": d.comparison_value_to,
            "comparison_value_from_des": d.comparison_value_from_des,
            "combined_rule": d.combined_rule,
            "scenario_code": d.scenario_code,
        }
        for d in details
    ]
    run_write_many(sql, params_list)
    logger.debug("[QB_WRITER] PIO_AML_RULES_DETAILS batch inserted: %d rows", len(details))


# =============================================================================
# NODE 6 — SCENARIO VALIDATOR
# =============================================================================


def validator_node(state: AMLScenarioState, config: RunnableConfig) -> Dict[str, Any]:
    """Agentic self-validation — hunt the scenario's own output.

    After writing the scenario to Oracle:
    1. Runs FILL_PIO_AML_CUSTOMERS to trigger all scenarios
    2. Queries PIO_AML_CUSTOMERS for alerts generated by our scenario
    3. Sanity-checks the alert count against expected range
    4. Pulls sample alerts for user review
    5. Self-corrects up to MAX_VALIDATION_RETRIES if something is wrong

    Args:
        state (AMLScenarioState): Current agent state.
        config (RunnableConfig): LangGraph runtime config.

    Returns:
        Dict[str, Any]: State updates with validation_result and next_action.
    """
    retry_count = state.get("validation_retry_count", 0)
    scenario_code = state.get("scenario_code", "")
    intent_dict = state.get("enriched_intent") or {}

    if not state.get("scenario_write_success", False):
        logger.warning("[VALIDATOR] Scenario write failed in previous step. Skipping database validation.")
        return {
            "validation_result": {
                "success": False,
                "scenario_status": "ERROR",
                "alert_count": 0,
                "sample_alerts": [],
                "diagnosis": "The scenario parameters could not be written to Oracle. Check the error log for details.",
                "suggested_fix": "Verify that all configuration parameters and database column mappings are correct.",
                "retry_count": retry_count,
                "confidence_score": 0.0,
            },
            "next_action": "ORCHESTRATOR",
        }

    logger.info(
        "[VALIDATOR] Validating scenario_code=%s (attempt %d/%d)",
        scenario_code,
        retry_count + 1,
        settings.MAX_VALIDATION_RETRIES,
    )

    from web.services.oracle import call_procedure, run_readonly

    try:
        # Step 1: Run the QB engine to process all active scenarios
        logger.info("[VALIDATOR] Calling FILL_PIO_AML_CUSTOMERS...")
        call_procedure("FILL_PIO_AML_CUSTOMERS")
        logger.info("[VALIDATOR] Procedure completed.")

        # Step 2: Count alerts for our scenario
        cols, rows = run_readonly(
            """
            SELECT COUNT(*) AS alert_count
            FROM PIO_AML_CUSTOMERS
            WHERE AML_SCENARIO_CODE = :scenario_code
            """,
            {"scenario_code": scenario_code},
        )
        alert_count = int(rows[0][0]) if rows else 0
        logger.info("[VALIDATOR] Alert count for %s: %d", scenario_code, alert_count)

        # Step 3: Sanity check
        intent = AMLIntent(**intent_dict)
        diagnosis = None
        suggested_fix = None
        success = True

        if alert_count == 0:
            success = False
            diagnosis = (
                "Zero alerts generated. The scenario conditions may be too restrictive, "
                "the time window may be too narrow, or the parameter codes may need adjustment."
            )
            suggested_fix = (
                "Try widening the threshold values or extending the time window. "
                "Verify that PARAMETER_CODE values match pio_aml_parameters."
            )

        elif intent.expected_alert_range_max and alert_count > intent.expected_alert_range_max:
            success = False
            diagnosis = (
                f"Alert volume ({alert_count:,}) exceeds expected maximum "
                f"({intent.expected_alert_range_max:,}). Scenario may be too broad."
            )
            suggested_fix = "Tighten the threshold values or add exclusion criteria."

        # Step 4: Pull sample alerts
        sample_alerts: List[AlertSample] = []
        if alert_count > 0:
            sample_cols, sample_rows = run_readonly(
                """
                SELECT *
                FROM PIO_AML_CUSTOMERS
                WHERE AML_SCENARIO_CODE = :scenario_code
                AND ROWNUM <= 5
                """,
                {"scenario_code": scenario_code},
            )
            for row in sample_rows:
                row_dict = dict(zip(sample_cols, row))
                customer_id = str(row_dict.get("cus_num", row_dict.get("customer_id", "—")))
                sample_alerts.append(AlertSample(customer_id=customer_id, raw_data=row_dict))

        # Step 5: Calculate confidence
        confidence = 1.0 if success else max(0.0, 1.0 - (retry_count * 0.3))

        result = ValidationResult(
            success=success,
            scenario_status="ACTIVE" if success else "REVIEW_NEEDED",
            alert_count=alert_count,
            sample_alerts=sample_alerts,
            diagnosis=diagnosis,
            suggested_fix=suggested_fix,
            retry_count=retry_count,
            confidence_score=confidence,
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
                "next_action": "DECOMPOSE",  # loop back to decomposer
                "validation_retry_count": retry_count + 1,
                "error_log": state.get("error_log", []) + [f"Validation attempt {retry_count + 1}: {diagnosis}"],
            }

        # Max retries hit — surface to user
        logger.error("[VALIDATOR] Max retries reached. Escalating to user.")
        return {
            "validation_result": result.model_dump(mode="json"),
            "next_action": "FINALIZE",  # surface what we have with explanation
            "validation_retry_count": retry_count + 1,
        }

    except Exception as exc:
        logger.error("[VALIDATOR] Error during validation: %s", exc, exc_info=True)
        return {
            "next_action": "ERROR",
            "error_log": state.get("error_log", []) + [f"Validation error: {exc}"],
        }


# =============================================================================
# ROUTING FUNCTIONS
# =============================================================================


def route_after_orchestrator(state: AMLScenarioState) -> str:
    """Determine which node the orchestrator routes to.

    Args:
        state (AMLScenarioState): Current state.

    Returns:
        str: LangGraph node name or END.
    """
    action = state.get("next_action", "INTENT")
    route_map = {
        "INTENT": "intent_analyst",
        "CLARIFY": "orchestrator",   # Orchestrator formats and returns clarification
        "WAIT_USER": END,
        "END": END,
        "ERROR": "orchestrator",
    }
    return route_map.get(action, "intent_analyst")


def route_after_intent(state: AMLScenarioState) -> str:
    """Route after Intent Analyst node.

    Args:
        state (AMLScenarioState): Current state.

    Returns:
        str: Next node name.
    """
    action = state.get("next_action", "SQL_BRIDGE")
    if action == "CLARIFY":
        return "orchestrator"
    if action == "ERROR":
        return "orchestrator"
    return "sql_bridge"


def route_after_decomposer(state: AMLScenarioState) -> str:
    """Route after Decomposer node.

    Args:
        state (AMLScenarioState): Current state.

    Returns:
        str: Next node name.
    """
    action = state.get("next_action", "QB_WRITE")
    if action == "ERROR":
        return "orchestrator"
    return "qb_writer"


def route_after_validator(state: AMLScenarioState) -> str:
    """Route after Validator node.

    Args:
        state (AMLScenarioState): Current state.

    Returns:
        str: Next node name — either decomposer (retry) or orchestrator (finalize).
    """
    action = state.get("next_action", "FINALIZE")
    if action == "DECOMPOSE":
        return "decomposer"
    if action == "ERROR":
        return "orchestrator"
    return "orchestrator"  # FINALIZE → orchestrator formats final message


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
    graph.add_node("sql_bridge", sql_bridge_node)
    graph.add_node("decomposer", decomposer_node)
    graph.add_node("qb_writer", qb_writer_node)
    graph.add_node("validator", validator_node)

    # Entry point
    graph.set_entry_point("orchestrator")

    # Edges with conditional routing
    graph.add_conditional_edges("orchestrator", route_after_orchestrator)
    graph.add_conditional_edges("intent_analyst", route_after_intent)
    graph.add_edge("sql_bridge", "decomposer")
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
            logger.error("[AGENT] Error closing checkpointer database connection: %s", exc)
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
