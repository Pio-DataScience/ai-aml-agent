"""
Pydantic contracts for the AML Builder agent system.

This module defines every data structure that flows between agent nodes —
the single source of truth for all inter-node communication.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


# =============================================================================
# INTENT LAYER — What the user wants
# =============================================================================


class Threshold(BaseModel):
    """A single numeric threshold condition from the user's intent.

    Args:
        field (str): Business field name (e.g., 'transaction_amount').
        operator (str): Comparison operator: '>', '<', '>=', '<=', '=', 'BETWEEN'.
        value_from (float): The primary or lower bound threshold value.
        value_to (Optional[float]): Upper bound value, used only for BETWEEN.
    """

    field: str = Field(..., description="Business field name from the user's intent.")
    operator: Literal[">", "<", ">=", "<=", "=", "BETWEEN", "IN"] = Field(
        ..., description="Comparison operator."
    )
    value_from: float = Field(
        ..., description="Primary threshold value (or lower bound for BETWEEN)."
    )
    value_to: Optional[float] = Field(
        default=None,
        description="Upper bound value — only populated for BETWEEN operator.",
    )


class TimeWindow(BaseModel):
    """A rolling or fixed time window from the user's intent.

    Args:
        unit (str): Time unit: 'DAYS', 'MONTHS', 'YEARS'.
        value (int): Numeric size of the window.
        is_rolling (bool): True = rolling window from today. False = fixed period.
    """

    unit: Literal["DAYS", "MONTHS", "YEARS"] = Field(..., description="Time unit.")
    value: int = Field(..., description="Numeric size of the time window.")
    is_rolling: bool = Field(
        default=True,
        description="True = rolling from SYSDATE. False = fixed calendar period.",
    )


class AMLIntent(BaseModel):
    """Fully structured, unambiguous AML detection intent.

    Produced by the Intent Analyst node after clarifying the user's request.
    This is the primary input to the SQL Bridge node.

    Args:
        scenario_name (str): Auto-generated descriptive name for the scenario.
        scenario_type (str): Dominant entity type: TRANSACTION, ACCOUNT, CUSTOMER.
        detection_logic (str): Plain English business logic summary.
        thresholds (list[Threshold]): All numeric conditions extracted from intent.
        time_window (Optional[TimeWindow]): Rolling or fixed observation period.
        customer_segments (Optional[list[str]]): Target segments (RETAIL, CORPORATE).
        exclusions (Optional[list[str]]): Explicit exclusion rules.
        clarification_needed (bool): True if the agent must ask the user something.
        clarification_questions (list[str]): Business questions for the user (NOT technical).
        expected_alert_range_min (Optional[int]): Lower bound of expected alert volume.
        expected_alert_range_max (Optional[int]): Upper bound of expected alert volume.
    """

    scenario_name: str = Field(..., description="Descriptive name for this scenario.")
    scenario_type: Literal["TRANSACTION", "ACCOUNT", "CUSTOMER"] = Field(
        ..., description="Primary entity type this scenario monitors."
    )
    detection_logic: str = Field(
        ..., description="Plain English description of the detection logic."
    )
    thresholds: List[Threshold] = Field(
        default_factory=list,
        description="All numeric threshold conditions.",
    )
    time_window: Optional[TimeWindow] = Field(
        default=None,
        description="Observation time window (rolling or fixed).",
    )
    customer_segments: Optional[List[str]] = Field(
        default=None,
        description="Target customer segments, e.g. ['RETAIL', 'CORPORATE'].",
    )
    exclusions: Optional[List[str]] = Field(
        default=None,
        description="Explicit rules about what to exclude from detection.",
    )
    clarification_needed: bool = Field(
        default=False,
        description="True if the agent needs to ask the user for more information.",
    )
    clarification_questions: List[str] = Field(
        default_factory=list,
        description="Business-level questions for the user (never technical).",
    )
    expected_alert_range_min: Optional[int] = Field(
        default=None,
        description="Minimum expected alert count (used in validator sanity check).",
    )
    expected_alert_range_max: Optional[int] = Field(
        default=None,
        description="Maximum expected alert count (used in validator sanity check).",
    )


# =============================================================================
# SQL BRIDGE LAYER — What PioTech AI returns
# =============================================================================


class SQLMetadata(BaseModel):
    """Structured metadata extracted from the SQL returned by PioTech AI.

    Args:
        raw_sql (str): The exact SQL query as returned by PioTech AI.
        tables (list[str]): All Oracle tables referenced (schema-qualified).
        primary_table (str): The main driving table of the query.
        where_conditions (list[str]): Raw WHERE clause conditions as strings.
        group_by_fields (list[str]): Fields in the GROUP BY clause.
        having_conditions (list[str]): Raw HAVING clause conditions as strings.
        date_fields (list[str]): Date/timestamp columns used for time filtering.
        aggregations (list[str]): Aggregate functions used (COUNT, SUM, AVG).
    """

    raw_sql: str = Field(..., description="The raw SQL string from PioTech AI.")
    tables: List[str] = Field(
        default_factory=list,
        description="All schema-qualified Oracle tables in the query.",
    )
    primary_table: str = Field(
        default="",
        description="The main driving table (FROM clause, not JOIN).",
    )
    where_conditions: List[str] = Field(
        default_factory=list,
        description="Extracted WHERE clause conditions.",
    )
    group_by_fields: List[str] = Field(
        default_factory=list,
        description="Fields in the GROUP BY clause.",
    )
    having_conditions: List[str] = Field(
        default_factory=list,
        description="Extracted HAVING clause conditions.",
    )
    date_fields: List[str] = Field(
        default_factory=list,
        description="Date columns used in time-based filters.",
    )
    aggregations: List[str] = Field(
        default_factory=list,
        description="Aggregate functions present in the query.",
    )


# =============================================================================
# DECOMPOSER LAYER — QB-ready parameters
# =============================================================================


class QBScenario(BaseModel):
    """Data to INSERT into PIO_AML_SCENARIO.

    Args:
        country_code (str): Country code (default from env: '400').
        inst_code (str): Institution code (default from env: '1').
        scenario_code (str): Unique identifier for this scenario.
        scenario_des_eng (str): English description of the scenario.
        active_flag (str): 'Y' = active, 'N' = inactive.
        violation_level (str): L / M / H.
        degree_risk_flag (str): L / M / H.
        created_by (str): Author tag (default: 'AI_AGENT').
        created_date (datetime): Timestamp of creation.
    """

    country_code: str = Field(..., description="Country code (PIO_AML_SCENARIO.COUNTRY_CODE).")
    inst_code: str = Field(..., description="Institution code (PIO_AML_SCENARIO.INST_CODE).")
    scenario_code: str = Field(..., description="Unique scenario identifier.")
    scenario_des_eng: str = Field(..., description="English scenario description.")
    active_flag: str = Field(default="Y", description="Active flag: Y or N.")
    violation_level: str = Field(default="M", description="Violation severity: L, M, or H.")
    degree_risk_flag: str = Field(default="M", description="Risk degree: L, M, or H.")
    created_by: str = Field(default="AI_AGENT", description="Creator identifier.")
    created_date: datetime = Field(
        default_factory=datetime.utcnow,
        description="Creation timestamp.",
    )


class QBRule(BaseModel):
    """Data to INSERT into PIO_AML_RULES.

    Args:
        country_code (str): Country code.
        inst_code (str): Institution code.
        rule_code (str): Unique rule identifier.
        rule_des_eng (str): English description of this rule.
        active_flag (str): Y or N.
        period_type (str): D=Days, M=Months, Y=Years.
        period_days (int): Numeric length of the detection window.
        ltg_code (Optional[str]): Transaction type group code (future use).
        created_by (str): Creator identifier.
        created_date (datetime): Creation timestamp.
    """

    country_code: str = Field(..., description="Country code (PIO_AML_RULES.COUNTRY_CODE).")
    inst_code: str = Field(..., description="Institution code.")
    rule_code: str = Field(..., description="Unique rule identifier.")
    rule_des_eng: str = Field(..., description="English rule description.")
    active_flag: str = Field(default="Y", description="Active flag.")
    period_type: Literal["D", "M", "Y"] = Field(
        default="D",
        description="Period unit: D=Days, M=Months, Y=Years.",
    )
    period_days: int = Field(default=30, description="Length of the detection window.")
    ltg_code: Optional[str] = Field(
        default=None,
        description="Transaction type group code (PIO_LTG_DEFINITION). Optional.",
    )
    created_by: str = Field(default="AI_AGENT", description="Creator identifier.")
    created_date: datetime = Field(default_factory=datetime.utcnow)


class QBRuleDetail(BaseModel):
    """Data to INSERT into PIO_AML_RULES_DETAILS.

    One row per condition (WHERE clause predicate / HAVING predicate).

    Args:
        country_code (str): Country code.
        inst_code (str): Institution code.
        parameter_code (str): The AML parameter/field code from pio_aml_parameters.
        rule_code (str): FK to PIO_AML_RULES.RULE_CODE.
        rule_seq (int): Sequence number within the rule (1, 2, 3...).
        rule_operator (str): Operator code: GT, LT, GE, LE, EQ, BTW, IN, GRP, HAV_CNT.
        comparison_value_from (Optional[str]): Primary/lower bound value.
        comparison_value_to (Optional[str]): Upper bound (BETWEEN only).
        comparison_value_from_des (Optional[str]): Human-readable description.
        combined_rule (Optional[str]): AND/OR connector with next condition.
        scenario_code (str): FK to PIO_AML_SCENARIO.SCENARIO_CODE.
    """

    country_code: str = Field(..., description="Country code.")
    inst_code: str = Field(..., description="Institution code.")
    parameter_code: str = Field(
        ..., description="AML parameter code (from pio_aml_parameters)."
    )
    rule_code: str = Field(..., description="FK to PIO_AML_RULES.RULE_CODE.")
    rule_seq: int = Field(..., description="Condition sequence number within the rule.")
    rule_operator: str = Field(
        ...,
        description="Operator: GT, LT, GE, LE, EQ, BTW, IN, GRP, HAV_CNT.",
    )
    comparison_value_from: Optional[str] = Field(
        default=None,
        description="Primary threshold value (stored as string for Oracle compatibility).",
    )
    comparison_value_to: Optional[str] = Field(
        default=None,
        description="Upper bound value — BETWEEN operator only.",
    )
    comparison_value_from_des: Optional[str] = Field(
        default=None,
        description="Human-readable description of the value.",
    )
    combined_rule: Optional[str] = Field(
        default="AND",
        description="Logical connector to next condition: AND or OR.",
    )
    scenario_code: str = Field(
        ..., description="FK to PIO_AML_SCENARIO.SCENARIO_CODE."
    )


class QBScenarioRule(BaseModel):
    """Data to INSERT into PIO_AML_SCENARIO_RULES.

    Links a scenario to its rules.

    Args:
        country_code (str): Country code.
        inst_code (str): Institution code.
        aml_rule_code (str): FK to PIO_AML_RULES.RULE_CODE.
        aml_scenario (str): FK to PIO_AML_SCENARIO.SCENARIO_CODE.
        rule_seq (int): Rule sequence within the scenario.
        rule_type (str): Type code for this rule (domain-specific).
        amt_perc (Optional[float]): Amount percentage threshold (domain use).
        margin_perc (Optional[float]): Margin percentage (domain use).
        stop_period (Optional[int]): Cooldown period after alert triggers.
    """

    country_code: str = Field(..., description="Country code.")
    inst_code: str = Field(..., description="Institution code.")
    aml_rule_code: str = Field(..., description="FK to PIO_AML_RULES.")
    aml_scenario: str = Field(..., description="FK to PIO_AML_SCENARIO.")
    rule_seq: int = Field(default=1, description="Rule sequence in scenario.")
    rule_type: str = Field(
        default="T",
        description="Rule type code (domain-specific, default 'T' for Transaction).",
    )
    amt_perc: Optional[float] = Field(
        default=None, description="Amount percentage threshold."
    )
    margin_perc: Optional[float] = Field(
        default=None, description="Margin percentage."
    )
    stop_period: Optional[int] = Field(
        default=None, description="Alert cooldown period in days."
    )


class ScenarioParameters(BaseModel):
    """Complete QB-ready parameter bundle — output of the Decomposer node.

    This is everything needed to write the scenario into Oracle.

    Args:
        scenario (QBScenario): The scenario header row.
        rules (list[QBRule]): One or more rule definitions.
        scenario_rules (list[QBScenarioRule]): Scenario-rule linkage rows.
        rule_details (list[QBRuleDetail]): All individual condition rows.
        decomposition_confidence (float): Self-assessed accuracy 0.0–1.0.
        decomposition_notes (list[str]): Reasoning log from the Decomposer.
    """

    scenario: QBScenario = Field(..., description="Scenario header data.")
    rules: List[QBRule] = Field(..., description="Rule definition rows.")
    scenario_rules: List[QBScenarioRule] = Field(
        ..., description="Scenario-rule linkage rows."
    )
    rule_details: List[QBRuleDetail] = Field(
        ..., description="Individual condition rows for PIO_AML_RULES_DETAILS."
    )
    decomposition_confidence: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Confidence score: how well the SQL maps to QB parameters.",
    )
    decomposition_notes: List[str] = Field(
        default_factory=list,
        description="Internal reasoning log from the Decomposer for debugging.",
    )


# =============================================================================
# VALIDATION LAYER — Did it work?
# =============================================================================


class AlertSample(BaseModel):
    """A single sample alert from PIO_AML_CUSTOMERS.

    Args:
        customer_id (str): Customer identifier.
        raw_data (Dict[str, Any]): Full row data as returned from Oracle.
    """

    customer_id: str = Field(..., description="Customer identifier.")
    raw_data: Dict[str, Any] = Field(
        default_factory=dict, description="Full Oracle row as a dict."
    )


class ValidationResult(BaseModel):
    """Result of the Scenario Validator node's self-check.

    Args:
        success (bool): True if scenario is ACTIVE and alert count is reasonable.
        scenario_status (str): Oracle scenario status: ACTIVE, ERROR, DRAFT.
        alert_count (Optional[int]): Number of customers matching the scenario.
        sample_alerts (list[AlertSample]): Up to 5 sample matching customers.
        diagnosis (Optional[str]): Human-readable explanation of failure.
        suggested_fix (Optional[str]): Recommended corrective action.
        retry_count (int): How many self-correction cycles have been attempted.
        confidence_score (float): Quality score 0.0–1.0.
    """

    success: bool = Field(..., description="True if the scenario validated successfully.")
    scenario_status: str = Field(
        default="UNKNOWN",
        description="Status of the scenario in Oracle.",
    )
    alert_count: Optional[int] = Field(
        default=None, description="Number of alerts produced by this scenario."
    )
    sample_alerts: List[AlertSample] = Field(
        default_factory=list,
        description="Sample of up to 5 matching customers.",
    )
    diagnosis: Optional[str] = Field(
        default=None,
        description="Explanation of why validation failed.",
    )
    suggested_fix: Optional[str] = Field(
        default=None,
        description="Recommended corrective action for the Decomposer.",
    )
    retry_count: int = Field(
        default=0, description="Number of self-correction cycles attempted."
    )
    confidence_score: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Overall quality score.",
    )


# =============================================================================
# API LAYER — HTTP request / response contracts
# =============================================================================


class ChatMessage(BaseModel):
    """A single message in the conversation history.

    Args:
        role (str): 'user' or 'assistant'.
        content (str): Message text content.
    """

    role: Literal["user", "assistant"] = Field(..., description="Message role.")
    content: str = Field(..., description="Message text content.")


class ChatRequest(BaseModel):
    """Incoming chat request from the client.

    Args:
        messages (list[ChatMessage]): Full conversation history.
        metadata (Dict[str, str]): user_id, project_id, chat_id for thread routing.
        reasoning_mode (str): 'instant' (fast) or 'thinking' (deep).
    """

    messages: List[ChatMessage] = Field(
        ..., description="Conversation history (at minimum the latest user message)."
    )
    metadata: Dict[str, str] = Field(
        default_factory=dict,
        description="Routing metadata: user_id, project_id, chat_id.",
    )
    reasoning_mode: Literal["instant", "thinking"] = Field(
        default="instant",
        description="Reasoning depth: instant=fast, thinking=deep deliberation.",
    )


class SSEEvent(BaseModel):
    """A single Server-Sent Event payload.

    Args:
        type (str): Event category for client-side routing.
        text (Optional[str]): Text content for 'content' and 'final_answer' events.
        tool (Optional[str]): Tool name for 'tool_call' events.
        status (Optional[str]): Status string for 'thinking' events.
        data (Optional[Any]): Arbitrary payload for 'scenario_result' events.
    """

    type: Literal[
        "tool_call", "thinking", "content", "final_answer", "scenario_result", "error", "done"
    ] = Field(..., description="SSE event type.")
    text: Optional[str] = Field(default=None, description="Text payload.")
    tool: Optional[str] = Field(default=None, description="Tool name (tool_call events).")
    status: Optional[str] = Field(default=None, description="Status string (thinking events).")
    data: Optional[Any] = Field(default=None, description="Structured payload (scenario_result).")
