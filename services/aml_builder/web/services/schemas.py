"""
Pydantic contracts for the AML Builder agent system.

This module defines the data structures that flow through the tool-driven
AML Scenario Builder agent: the AMLIntent contract produced by the intent
tool, and the HTTP/SSE request-response contracts for the FastAPI layer.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator, model_validator


# =============================================================================
# INTENT LAYER — What the user wants
# =============================================================================


class Threshold(BaseModel):
    """A single numeric or relational threshold condition from the user's intent.

    Supports both fixed numeric literals (e.g. 50000.0) and relational field-to-field
    comparisons (e.g. comparing transaction_amount against customer_loan_amount).

    Args:
        field (str): Business field name (e.g., 'transaction_amount').
        operator (str): Comparison operator: '>', '<', '>=', '<=', '=', 'BETWEEN', 'IN'.
        value_from (Optional[Union[float, str]]): Numeric threshold value or relational field name.
        value_to (Optional[Union[float, str]]): Upper bound value, used only for BETWEEN.
    """

    field: str = Field(..., description="Business field name from the user's intent.")
    operator: Literal[">", "<", ">=", "<=", "=", "BETWEEN", "IN"] = Field(
        ..., description="Comparison operator."
    )
    value_from: Optional[Union[float, str]] = Field(
        default=None,
        description="Primary numeric threshold value or relational field name (or lower bound for BETWEEN).",
    )
    value_to: Optional[Union[float, str]] = Field(
        default=None,
        description="Upper bound value — only populated for BETWEEN operator.",
    )

    @field_validator("value_from", "value_to", mode="before")
    @classmethod
    def _normalize_numeric_value(cls, v: Any) -> Any:
        if isinstance(v, str):
            v_clean = v.strip()
            # Handle percentage strings like "300%", "90%" -> 3.0, 0.90
            pct_match = re.search(r"^(\d+(?:\.\d+)?)\s*%$", v_clean)
            if pct_match:
                return float(pct_match.group(1)) / 100.0
            # Handle string multipliers like "3.0x", "3x"
            mult_match = re.search(r"^(\d+(?:\.\d+)?)\s*x$", v_clean, re.IGNORECASE)
            if mult_match:
                return float(mult_match.group(1))
            # Try parsing direct float
            try:
                return float(v_clean)
            except ValueError:
                # Retain relational column name or expression string (e.g. 'customer_loan_amount')
                return v_clean
        return v

    provenance: Literal["stated", "assumed_default", "needs_user"] = Field(
        default="stated",
        description=(
            "Where this value came from: 'stated' (user gave it), "
            "'assumed_default' (parser defaulted it — disclose in applied_defaults), "
            "or 'needs_user' (materially ambiguous — must be clarified)."
        ),
    )
    target_scope: Literal["DETAIL", "AGGREGATE"] = Field(
        ...,
        description=(
            "MANDATORY: Scope of threshold. "
            "Use 'DETAIL' ONLY for filtering individual raw transaction amounts BEFORE aggregation (WHERE clause). "
            "Use 'AGGREGATE' for accumulated daily totals, sums, counts, or averages (HAVING clause)."
        ),
    )


class TimeWindow(BaseModel):
    """A rolling or fixed time window from the user's intent.

    Args:
        unit (str): Time unit: 'DAYS', 'WEEKS', 'MONTHS', 'YEARS'.
        value (int): Numeric size of the window.
        is_rolling (bool): True = rolling window from today. False = fixed period.
    """

    unit: Literal["DAYS", "WEEKS", "MONTHS", "YEARS"] = Field(
        ..., description="Time unit."
    )

    @field_validator("unit", mode="before")
    @classmethod
    def _normalize_unit(cls, v: Any) -> Any:
        if isinstance(v, str):
            v_upper = v.upper().strip()
            unit_map = {
                "DAY": "DAYS",
                "WEEK": "WEEKS",
                "MONTH": "MONTHS",
                "YEAR": "YEARS",
            }
            return unit_map.get(v_upper, v_upper)
        return v

    value: int = Field(..., description="Numeric size of the time window.")
    is_rolling: bool = Field(
        default=True,
        description="True = rolling from SYSDATE. False = fixed calendar period.",
    )
    provenance: Literal["stated", "assumed_default", "needs_user"] = Field(
        default="stated",
        description="stated | assumed_default | needs_user (see Threshold.provenance).",
    )


class BaselineWindow(BaseModel):
    """Dedicated historical baseline window for comparative scenarios.

    Constructed whenever a scenario compares current activity against a historical baseline
    (e.g., 'historical monthly average', '6-month average', 'prior activity profile').

    Args:
        unit (str): Time unit: 'DAYS', 'WEEKS', 'MONTHS', 'YEARS'.
        duration (int): Duration of the historical baseline period (e.g., 180 for 6 months).
        exclude_current_window (bool): True to enforce zero-overlap isolation with current observation window.
        offset_days (int): Equal to the current observation window size (e.g., 30 for 30-day window).
        sql_date_formula (Optional[str]): Explicit SQL predicate formula for non-overlapping date range.
        description (Optional[str]): Plain English description of baseline range.
    """

    unit: Literal["DAYS", "WEEKS", "MONTHS", "YEARS"] = Field(
        default="DAYS", description="Time unit."
    )

    @field_validator("unit", mode="before")
    @classmethod
    def _normalize_unit(cls, v: Any) -> Any:
        if isinstance(v, str):
            v_upper = v.upper().strip()
            unit_map = {
                "DAY": "DAYS",
                "WEEK": "WEEKS",
                "MONTH": "MONTHS",
                "YEAR": "YEARS",
            }
            return unit_map.get(v_upper, v_upper)
        return v

    duration: int = Field(
        ..., description="Duration of historical baseline window in units."
    )
    exclude_current_window: bool = Field(
        default=True,
        description="True = enforce zero-overlap isolation with current observation window.",
    )
    offset_days: int = Field(
        ...,
        description="Offset days matching current window size to exclude current period.",
    )
    sql_date_formula: Optional[str] = Field(
        default=None,
        description="SQL date formula for non-overlapping range.",
    )
    description: Optional[str] = Field(
        default=None,
        description="Plain English description of baseline window.",
    )


class AggregationProfile(BaseModel):
    """How the monitored metric is measured and at what grain.

    Grain and metric stay plain English so the downstream text-to-SQL agent can
    ground them against the data dictionary. This object exists because grain and
    measure are the most commonly-omitted yet outcome-changing dimensions in AML
    rules; forcing them out of prose makes omissions visible.

    Args:
        metric (str): What is measured, plain English (e.g. 'Total deposit
            amount', 'Count of distinct beneficiaries').
        function (Optional[str]): Aggregation function in plain English
            (e.g. 'sum', 'count', 'count distinct', 'max'). None if per-row.
        grain (str): Evaluation grain, plain English (e.g. 'Per Transaction',
            'Per Customer per Day').
        provenance (str): stated | assumed_default | needs_user.
    """

    metric: str = Field(..., description="What is measured, plain English.")
    function: Optional[str] = Field(
        default=None, description="sum | count | count distinct | max | None."
    )
    grain: str = Field(..., description="Evaluation grain, plain English.")
    provenance: Literal["stated", "assumed_default", "needs_user"] = Field(
        default="stated"
    )


class SemanticCondition(BaseModel):
    """A purely atomic, typed semantic logic filter or state condition.

    Captures logic that doesn't fit a numeric Threshold (e.g. 'is an outward transfer',
    'was dormant then became active'). By giving it a `logical_type`, we classify the
    nature of the constraint without hard-coding specific database rules.

    Args:
        raw_phrase (str): The user's own words this condition came from.
        logical_type (str): Categorizes the logic (STATE, TRANSITION, SEQUENCE, TEMPORAL, BEHAVIORAL, OTHER).
        subject (str): Entity being evaluated (e.g. 'Customer', 'Transaction', 'Account').
        predicate (str): A single, atomic plain English business rule.
        provenance (str): stated | assumed_default | needs_user.
    """

    raw_phrase: str = Field(..., description="Verbatim user text this came from.")
    logical_type: Literal[
        "STATE", "TRANSITION", "SEQUENCE", "BEHAVIORAL", "TEMPORAL", "OTHER"
    ] = Field(
        ...,
        description="The nature of the logic (e.g. 'dormant to active' = TRANSITION).",
    )
    subject: str = Field(
        ...,
        description="Plain English entity noun (e.g. 'Customer', 'Transaction', 'Account'). No enums.",
    )
    predicate: str = Field(
        ..., description="A single, atomic plain English business rule."
    )
    provenance: Literal["stated", "assumed_default", "needs_user"] = Field(
        default="stated"
    )


class Clarification(BaseModel):
    """One material ambiguity the parser could not safely resolve.

    Created only when a wrong guess would materially change which alerts fire AND
    the parser cannot infer or safely default the answer. Everything else is
    defaulted (recorded in applied_defaults) and NOT asked.

    Args:
        dimension (str): The ambiguous dimension, plain English (e.g.
            'transaction direction', 'cash vs wire').
        why_it_matters (str): One line on how a wrong guess changes the outcome.
        question (str): Business-phrased question to show the user. Never
            technical (no table/column talk).
        options (Optional[list[str]]): Suggested answers, if a small set applies.
    """

    dimension: str = Field(...)
    why_it_matters: str = Field(...)
    question: str = Field(..., description="Business-phrased. Never technical.")
    options: Optional[List[str]] = Field(default=None)


class AMLIntent(BaseModel):
    """Fully structured, unambiguous AML detection intent.

    Produced by the analyze_intent_and_discover_explanation_codes tool.
    This is the primary contract passed between the agent's tools.

    Args:
        scenario_name (str): Auto-generated descriptive name for the scenario.
        scenario_type (str): Dominant entity type: TRANSACTION, ACCOUNT, CUSTOMER.
        detection_logic (str): Plain English business logic summary.
        thresholds (list[Threshold]): All numeric conditions extracted from intent.
        time_window (Optional[TimeWindow]): Rolling or fixed observation period.
        baseline_window (Optional[BaselineWindow]): Dedicated non-overlapping baseline window.
        customer_segments (Optional[list[str]]): Target legal classifications (INDIVIDUAL, CORPORATE) or segments.
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
    transaction_type: Optional[str] = Field(
        default=None,
        description=(
            "Explicit transaction type name e.g. 'LOAN SETTLEMENT', 'CASH DEPOSIT', "
            "'OUTWARD TRANSFER', or null if all transaction types are monitored."
        ),
    )
    explanation_codes: Optional[List[str]] = Field(
        default=None,
        description="Discovered or user-selected domain explanation code strings.",
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
    baseline_window: Optional[BaselineWindow] = Field(
        default=None,
        description=(
            "Dedicated historical baseline window constructed whenever current activity is "
            "compared against a historical profile (e.g. 6-month average)."
        ),
    )
    customer_segments: Optional[List[str]] = Field(
        default=None,
        description="Target legal classifications (e.g. ['INDIVIDUAL'], ['CORPORATE']) or business segments.",
    )
    exclusions: Optional[List[str]] = Field(
        default=None,
        description="Explicit rules about what to exclude from detection.",
    )
    mapped_keywords: Optional[Dict[str, str]] = Field(
        default=None,
        description="Descriptive terms mapped to their resolved numeric thresholds.",
    )
    clarification_needed: bool = Field(
        default=False,
        description="True if the agent needs to ask the user for more information.",
    )
    clarification_questions: List[str] = Field(
        default_factory=list,
        description=(
            "Business-level questions for the user (never technical). Kept for "
            "backward compatibility; prefer the structured `clarifications` list."
        ),
    )
    aggregation: Optional[AggregationProfile] = Field(
        default=None,
        description="How the metric is measured and at what grain.",
    )
    semantic_conditions: List[SemanticCondition] = Field(
        default_factory=list,
        description="All strictly separated, atomic semantic logic constraints.",
    )
    clarifications: List[Clarification] = Field(
        default_factory=list,
        description="Material ambiguities to resolve before handoff to the SQL agent.",
    )
    applied_defaults: List[str] = Field(
        default_factory=list,
        description="Business defaults the parser assumed, shown to the user.",
    )
    ready_for_handoff: bool = Field(
        default=True,
        description=(
            "False if any clarification must be answered before a trustworthy "
            "query can be built. Mirror of (len(clarifications) == 0)."
        ),
    )
    expected_alert_range_min: Optional[int] = Field(
        default=None,
        description="Minimum expected alert count (used in validator sanity check).",
    )
    expected_alert_range_max: Optional[int] = Field(
        default=None,
        description="Maximum expected alert count (used in validator sanity check).",
    )
    anchor_date: Optional[str] = Field(
        default=None,
        description=(
            "Optional evaluation anchor date in 'YYYY-MM-DD' format. "
            "When null (default), SQL is generated with TRUNC(SYSDATE) for production. "
            "When populated (e.g. '2024-01-04'), SQL uses DATE '<value>' as the temporal "
            "anchor — used during shadow testing against seeded/historical DWH snapshots."
        ),
    )

    @model_validator(mode="after")
    def validate_and_deduplicate_thresholds(self) -> "AMLIntent":
        """Guarantees zero conflict between DETAIL and AGGREGATE scopes for cumulative scenarios.

        If a scenario is at CUSTOMER or ACCOUNT grain with SUM/COUNT aggregation, any redundant
        DETAIL threshold matching an AGGREGATE threshold value is purged so the WHERE clause
        never filters out raw transactions before summing.
        """
        if self.scenario_type in ["CUSTOMER", "ACCOUNT"] and self.aggregation:
            if self.aggregation.function in ["SUM", "COUNT"]:
                aggregate_thresholds = [
                    t for t in self.thresholds if t.target_scope == "AGGREGATE"
                ]
                detail_thresholds = [
                    t for t in self.thresholds if t.target_scope == "DETAIL"
                ]

                if aggregate_thresholds and detail_thresholds:
                    agg_values = {
                        t.value_from
                        for t in aggregate_thresholds
                        if t.value_from is not None
                    }
                    self.thresholds = [
                        t
                        for t in self.thresholds
                        if not (
                            t.target_scope == "DETAIL" and t.value_from in agg_values
                        )
                    ]
        return self


# =============================================================================
# API LAYER — HTTP request / response contracts
# =============================================================================


class ChatMessage(BaseModel):
    """A single message in the conversation history.

    Args:
        role (str): 'user' or 'assistant'.
        content (str): Message text content.
        plan_artifact (Optional[str]): Markdown execution plan, if this turn produced one.
        scenario_result (Optional[Dict[str, Any]]): Structured scenario/validation result.
        escalation_report (Optional[str]): Markdown escalation report, if this turn produced one.
    """

    role: Literal["user", "assistant"] = Field(..., description="Message role.")
    content: str = Field(..., description="Message text content.")
    plan_artifact: Optional[str] = Field(
        default=None, description="The markdown execution plan"
    )
    scenario_metadata_catalog: Optional[Dict[str, Any]] = Field(
        default=None, description="Governance metadata catalog with live options"
    )
    scenario_result: Optional[Dict[str, Any]] = Field(
        default=None, description="The scenario validation result"
    )
    escalation_report: Optional[str] = Field(
        default=None, description="The markdown escalation report"
    )


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
    metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="Routing metadata: user_id, project_id, chat_id.",
    )
    metadata_json: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Officer-modified governance metadata from the side panel.",
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
        "tool_call",
        "thinking",
        "content",
        "final_answer",
        "scenario_result",
        "plan_artifact",
        "scenario_metadata_catalog",
        "escalation_report",
        "error",
        "done",
    ] = Field(..., description="SSE event type.")
    text: Optional[str] = Field(default=None, description="Text payload.")
    tool: Optional[str] = Field(
        default=None, description="Tool name (tool_call events)."
    )
    status: Optional[str] = Field(
        default=None, description="Status string (thinking events)."
    )
    data: Optional[Any] = Field(
        default=None, description="Structured payload (scenario_result)."
    )


class ChatHistoryResponse(BaseModel):
    """Response model for loading chat history."""

    user_id: str
    project_id: str
    chat_id: str
    messages: List[ChatMessage]


class ChatSessionItem(BaseModel):
    """Individual chat session metadata for sidebar display."""

    chat_id: str
    project_id: str
    title: str
    updated_at: str
    is_pinned: bool = False
    is_deleted: bool = False


class ChatListResponse(BaseModel):
    """Response model for listing user's chat sessions."""

    chats: List[ChatSessionItem]


class RenameChatRequest(BaseModel):
    title: str = Field(..., description="New title for the chat session")


class DeleteChatResponse(BaseModel):
    status: str
    message: str
    chat_id: str


class RenameChatResponse(BaseModel):
    status: str
    message: str
    chat_id: str
    title: str


class TogglePinResponse(BaseModel):
    status: str
    message: str
    chat_id: str
    is_pinned: bool
