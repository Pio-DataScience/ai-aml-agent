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
    scenario_type: str = Field(
        ..., description="Primary entity type or category this scenario monitors."
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

    All field defaults mirror the reference scenario_creation.md template,
    verified against the live PIO_AML_SCENARIO schema.

    Args:
        country_code (str): Country code (e.g. '400').
        inst_code (str): Institution code (e.g. '1').
        scenario_code (str): Unique numeric-string scenario identifier.
        scenario_des_eng (str): English description.
        scenario_des_nat_lan (str): Native language description (copied from ENG).
        active_flag (str): '1' = active (NOT 'Y').
        exclude_expl_flag (str): '0' = do not exclude explicit transactions.
        use_watchlist_flag (str): '0' = no watchlist matching.
        violation_level (str): '1' = Low severity.
        degree_risk_flag (str): 'D' = Default/Low risk.
        default_scenario_flag (str): '0' = not a default scenario.
        run_flag (str): '1' = scheduled to run.
        approval_flag (str): '1' = approved for production.
        group_by_flag (str): '1' = group alerts by customer.
        use_worldcheck_flag (str): '0' = no WorldCheck integration.
        created_by (str): Numeric-string author ID.
        created_date (datetime): Creation timestamp.
        updated_by (str): Same as created_by.
        updated_date (datetime): Same as created_date.
        trans_withouttrans_flag (str): '1' = include accounts with no transactions.
        category_code (str): '999' = generic category.
        active_threshold_curr_flag (str): '0' = use base currency thresholds.
        sce_type_code (str): '1' = standard transaction scenario type.
        class_code (str): '1' = Individual customer class.
    """

    country_code: str = Field(..., description="Country code (PIO_AML_SCENARIO.COUNTRY_CODE).")
    inst_code: str = Field(..., description="Institution code.")
    scenario_code: str = Field(..., description="Unique numeric-string scenario identifier.")
    scenario_des_eng: str = Field(..., description="English scenario description.")
    scenario_des_nat_lan: str = Field(
        default="",
        description="Native language description — copied from scenario_des_eng if blank.",
    )
    active_flag: str = Field(default="1", description="'1' = active.")
    exclude_expl_flag: str = Field(default="0", description="'0' = include explicit transactions.")
    use_watchlist_flag: str = Field(default="0", description="'0' = no watchlist matching.")
    violation_level: str = Field(default="1", description="Violation severity code from PIO_AML_SCENARIO domain.")
    degree_risk_flag: str = Field(default="D", description="Risk degree: D=Default, L=Low, M=Medium, H=High.")
    default_scenario_flag: str = Field(default="0", description="'0' = not a system-default scenario.")
    run_flag: str = Field(default="1", description="'1' = scenario is scheduled to run by the QB engine.")
    approval_flag: str = Field(default="1", description="'1' = scenario has been approved for production.")
    group_by_flag: str = Field(default="1", description="'1' = group resulting alerts by customer ID.")
    use_worldcheck_flag: str = Field(default="0", description="'0' = no WorldCheck/external list integration.")
    created_by: str = Field(default="999", description="Numeric-string creator ID.")
    created_date: datetime = Field(default_factory=datetime.utcnow, description="Creation timestamp.")
    updated_by: str = Field(default="999", description="Same as created_by on initial insert.")
    updated_date: datetime = Field(default_factory=datetime.utcnow, description="Same as created_date on initial insert.")
    trans_withouttrans_flag: str = Field(
        default="1",
        description="'1' = include accounts that have no transactions in the period.",
    )
    category_code: str = Field(default="999", description="Scenario category code. '999' = generic.")
    active_threshold_curr_flag: str = Field(
        default="0",
        description="'0' = use base currency thresholds, not active currency.",
    )
    sce_type_code: str = Field(default="1", description="Scenario type code. '1' = standard transaction scenario.")
    class_code: str = Field(default="1", description="Customer class code. '1' = Individual.")


class QBRule(BaseModel):
    """Data to INSERT into PIO_AML_RULES.

    All field defaults mirror the reference scenario_creation.md template,
    verified against the live PIO_AML_RULES schema.

    PERIOD_TYPE is a numeric code from PIO_PERIOD_TYPE lookup table:
        '0' = Last n Days, '2' = Weekly, '3' = Monthly, '4' = Quarterly,
        '5' = Half Year, '6' = Yearly.

    Args:
        country_code (str): Country code.
        inst_code (str): Institution code.
        rule_code (str): Unique rule identifier.
        rule_desc_eng (str): English description of this rule.
        rule_desc_arb (str): Arabic/native description (copied from ENG if blank).
        active_flag (str): '1' = active.
        period_type (str): Numeric code from PIO_PERIOD_TYPE. '0' = Last n Days.
        period_days (int): Number of days for the detection window.
        use_sequentially_flag (str): '0' = conditions evaluated together, not sequentially.
        sequentially_count (int): 0 when use_sequentially_flag='0'.
        default_rule_flag (str): '0' = not a system-default rule.
        exclude_days (int): Days to exclude from the period (usually 0).
        ltg_code (Optional[str]): Transaction type group code (optional).
        created_by (str): Numeric-string creator ID.
        created_date (datetime): Creation timestamp.
        updated_by (str): Same as created_by.
        updated_date (datetime): Same as created_date.
    """

    country_code: str = Field(..., description="Country code.")
    inst_code: str = Field(..., description="Institution code.")
    rule_code: str = Field(..., description="Unique rule identifier.")
    rule_desc_eng: str = Field(..., description="English rule description (PIO_AML_RULES.RULE_DESC_ENG).")
    rule_desc_arb: str = Field(
        default="",
        description="Arabic/native rule description. Copied from rule_desc_eng if blank.",
    )
    active_flag: str = Field(default="1", description="'1' = rule is active.")
    period_type: str = Field(
        default="0",
        description=(
            "Detection window type from PIO_PERIOD_TYPE lookup. "
            "'0'=Last n Days, '2'=Weekly, '3'=Monthly, '4'=Quarterly, "
            "'5'=Half Year, '6'=Yearly."
        ),
    )
    period_days: int = Field(default=30, description="Length of the detection window in days.")
    use_sequentially_flag: str = Field(
        default="0",
        description="'0' = evaluate all conditions together (not in sequence).",
    )
    sequentially_count: int = Field(
        default=0,
        description="Sequence count, used only when use_sequentially_flag='1'.",
    )
    default_rule_flag: str = Field(default="0", description="'0' = not a system-default rule.")
    exclude_days: int = Field(default=0, description="Number of days to exclude from the detection period.")
    ltg_code: Optional[str] = Field(
        default=None,
        description="Transaction type group code (PIO_LTG_DEFINITION). Optional.",
    )
    created_by: str = Field(default="999", description="Numeric-string creator ID.")
    created_date: datetime = Field(default_factory=datetime.utcnow, description="Creation timestamp.")
    updated_by: str = Field(default="999", description="Same as created_by on initial insert.")
    updated_date: datetime = Field(default_factory=datetime.utcnow, description="Same as created_date.")


class QBRuleDetail(BaseModel):
    """Data to INSERT into PIO_AML_RULES_DETAILS.

    One row per condition. PARAMETER_CODE must match a real code in
    PIO_AML_PARAMETERS — this is the key lookup the QB engine uses to
    build its dynamic detection SQL.

    Key parameter codes (from live PIO_AML_PARAMETERS catalog):
        '1'  = Transaction Type (EXPL_CODE) — use with IN operator.
        '2'  = Number of Transactions — use with >= for count thresholds.
        '5'  = Transaction Amount — use with >= for single-amount thresholds.
        '6'  = Summation of Transactions — use with >= for SUM thresholds.
        '7'  = Customer Class — use with IN.
        '104'= Individual/Corporate Indicator — use with =.

    Args:
        country_code (str): Country code.
        inst_code (str): Institution code.
        parameter_code (str): Real PIO_AML_PARAMETERS.PARAMETER_CODE.
        rule_code (str): FK to PIO_AML_RULES.RULE_CODE.
        rule_seq (str): Sequence number as string ('1', '2', '3'...).
        rule_operator (str): Operator exactly as stored in Oracle: 'IN', '>=', '>', '=', etc.
        comparison_value_from (Optional[str]): Primary/lower bound value.
        comparison_value_to (Optional[str]): Upper bound (BETWEEN only).
        comparison_value_from_des (Optional[str]): Human-readable description.
        combined_rule (Optional[str]): 'AND' or 'OR' connector to next condition. '-' for last.
        scenario_code (str): FK to PIO_AML_SCENARIO.SCENARIO_CODE.
        use_sd_flag (str): '0' = do not use standard deviation comparison.
        sd_period_type (str): '0' = no SD period type.
        same_cust_flag (str): '0' = do not restrict to same customer.
        from_param_perc (Optional[int]): Percentage of the FROM parameter (100 for SUM rows).
        created_by (str): Numeric-string creator ID.
        created_date (datetime): Creation timestamp.
        updated_by (str): '0' on initial insert (reference convention).
        updated_date (datetime): Same as created_date.
    """

    country_code: str = Field(..., description="Country code.")
    inst_code: str = Field(..., description="Institution code.")
    parameter_code: str = Field(
        ..., description="Real AML parameter code from PIO_AML_PARAMETERS.PARAMETER_CODE."
    )
    rule_code: str = Field(..., description="FK to PIO_AML_RULES.RULE_CODE.")
    rule_seq: str = Field(..., description="Condition sequence number as string ('1', '2', ...).")
    rule_operator: str = Field(
        ...,
        description="Oracle operator string: 'IN', '>=', '>', '<=', '<', '=', 'BETWEEN'.",
    )
    comparison_value_from: Optional[str] = Field(
        default=None,
        description="Primary threshold value. For IN: Oracle-quoted list e.g. \"'CHW','CAA'\".",
    )
    comparison_value_to: Optional[str] = Field(
        default=None,
        description="Upper bound value — BETWEEN operator only.",
    )
    comparison_value_from_des: Optional[str] = Field(
        default=None,
        description="Human-readable comma-separated description of the values.",
    )
    combined_rule: Optional[str] = Field(
        default="AND",
        description="Logical connector to next condition: 'AND', 'OR', or '-' for last row.",
    )
    scenario_code: str = Field(..., description="FK to PIO_AML_SCENARIO.SCENARIO_CODE.")
    use_sd_flag: str = Field(default="0", description="'0' = no standard deviation comparison.")
    sd_period_type: str = Field(default="0", description="'0' = no SD period type.")
    same_cust_flag: str = Field(default="0", description="'0' = not restricted to same customer.")
    from_param_perc: Optional[int] = Field(
        default=None,
        description="Percentage of FROM parameter. Set to 100 for SUM aggregation rows (PARAMETER_CODE='6').",
    )
    created_by: str = Field(default="0", description="Creator ID. Reference convention uses 0 for details rows.")
    created_date: datetime = Field(default_factory=datetime.utcnow, description="Creation timestamp.")
    updated_by: str = Field(default="0", description="'0' on initial insert (reference convention).")
    updated_date: datetime = Field(default_factory=datetime.utcnow, description="Same as created_date.")


class QBScenarioRule(BaseModel):
    """Data to INSERT into PIO_AML_SCENARIO_RULES.

    Links a scenario to its rule(s). RULE_TYPE must be a code from the
    PIO_AML_RULE_TYPE lookup table (filtered by COUNTRY_CODE).

    Key RULE_TYPE codes (from live PIO_AML_RULE_TYPE for country_code=400):
        '3' = '-' (standalone single rule, no relationship to other rules)
        '1' = FOLLOW BY
        '2' = WITH
        '24'= UNION

    Args:
        country_code (str): Country code.
        inst_code (str): Institution code.
        aml_rule_code (str): FK to PIO_AML_RULES.RULE_CODE.
        aml_scenario (str): FK to PIO_AML_SCENARIO.SCENARIO_CODE.
        rule_seq (str): Rule sequence within the scenario ('1', '2', ...).
        rule_type (str): Code from PIO_AML_RULE_TYPE. '3' = standalone.
        frequency_days (int): Days between re-alerts. 0 = no cooldown.
        amt_perc (float): Amount percentage threshold (domain use, usually 0).
        margin_perc (float): Margin percentage (domain use, usually 0).
        stop_period (int): Cooldown period in days after alert triggers (usually 0).
    """

    country_code: str = Field(..., description="Country code.")
    inst_code: str = Field(..., description="Institution code.")
    aml_rule_code: str = Field(..., description="FK to PIO_AML_RULES.RULE_CODE.")
    aml_scenario: str = Field(..., description="FK to PIO_AML_SCENARIO.SCENARIO_CODE.")
    rule_seq: str = Field(default="1", description="Rule sequence as string ('1', '2', ...).")
    rule_type: str = Field(
        default="3",
        description=(
            "Rule type from PIO_AML_RULE_TYPE. "
            "'3' = standalone (no relationship). See lookup table for other values."
        ),
    )
    frequency_days: int = Field(default=0, description="Days between re-alerts for this rule. 0 = no cooldown.")
    amt_perc: float = Field(default=0.0, description="Amount percentage threshold.")
    margin_perc: float = Field(default=0.0, description="Margin percentage.")
    stop_period: int = Field(default=0, description="Alert cooldown period in days.")


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
