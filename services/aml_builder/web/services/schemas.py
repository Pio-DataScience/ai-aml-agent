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
            "Business evaluation scope, not a required SQL clause. DETAIL applies "
            "to one record/entity observation; AGGREGATE applies to a grouped or "
            "computed metric. Service B chooses the SQL implementation."
        ),
    )
    transaction_type: Optional[str] = Field(
        default=None,
        description="Specific transaction channel/type this threshold applies to (e.g. 'OUTWARD TRANSFER', 'CASH DEPOSIT').",
    )
    explanation_codes: Optional[List[str]] = Field(
        default=None,
        description="Specific compliance explanation codes bound directly to this threshold filter.",
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
    transaction_type: Optional[str] = Field(
        default=None,
        description="Specific transaction channel/type this condition applies to.",
    )
    explanation_codes: Optional[List[str]] = Field(
        default=None,
        description="Specific compliance explanation codes bound directly to this semantic condition.",
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


class GrainDefinition(BaseModel):
    """Business grain at which a scenario is evaluated or returned."""

    entity: str = Field(..., description="Business entity, such as CUSTOMER or ACCOUNT.")
    keys: List[str] = Field(
        default_factory=list,
        description="Business keys required at this grain, such as CUS_NUM.",
    )
    period: Optional[str] = Field(
        default=None,
        description="Optional business period component, such as DAY or ROLLING_WINDOW.",
    )
    description: Optional[str] = None


class BusinessPredicate(BaseModel):
    """A business filter with explicit applicability and evaluation phase."""

    predicate_id: str
    subject: str
    field: str
    operator: Literal[
        ">", "<", ">=", "<=", "=", "!=", "BETWEEN", "IN", "NOT_IN", "IS_NULL", "IS_NOT_NULL"
    ]
    value_from: Optional[Union[float, str, bool, List[str]]] = None
    value_to: Optional[Union[float, str]] = None
    evaluation_phase: Literal["RECORD", "GROUP", "METRIC", "OUTPUT"]
    applies_to: Literal["GLOBAL", "POPULATION", "METRIC"] = "GLOBAL"
    population_ids: List[str] = Field(default_factory=list)
    metric_ids: List[str] = Field(default_factory=list)
    transaction_types: List[str] = Field(default_factory=list)
    explanation_codes: List[str] = Field(default_factory=list)
    raw_phrase: Optional[str] = None
    provenance: Literal["stated", "assumed_default", "needs_user"] = "stated"

    @model_validator(mode="after")
    def validate_target_references(self) -> "BusinessPredicate":
        """Require references when a predicate is population- or metric-specific."""
        if self.applies_to == "POPULATION" and not self.population_ids:
            raise ValueError("POPULATION predicate requires population_ids")
        if self.applies_to == "METRIC" and not self.metric_ids:
            raise ValueError("METRIC predicate requires metric_ids")
        if self.operator == "BETWEEN" and self.value_to is None:
            raise ValueError("BETWEEN predicate requires value_to")
        return self


class PopulationDefinition(BaseModel):
    """Explicit population used by a metric, ratio, or comparison."""

    population_id: str
    role: Literal["BASE", "NUMERATOR", "DENOMINATOR", "COMPARISON"]
    entity: str
    description: str
    filters: List[BusinessPredicate] = Field(default_factory=list)
    transaction_types: List[str] = Field(default_factory=list)
    explanation_codes: List[str] = Field(default_factory=list)
    time_window_id: Optional[str] = None


class MetricComparison(BaseModel):
    """Business comparison applied to a computed metric."""

    operator: Literal[">", "<", ">=", "<=", "=", "!=", "BETWEEN"]
    value_from: Union[float, str]
    value_to: Optional[Union[float, str]] = None

    @model_validator(mode="after")
    def validate_between(self) -> "MetricComparison":
        """Require an upper bound for BETWEEN comparisons."""
        if self.operator == "BETWEEN" and self.value_to is None:
            raise ValueError("BETWEEN comparison requires value_to")
        return self


class MetricDefinition(BaseModel):
    """One explicitly defined business metric and its source populations."""

    metric_id: str
    name: str
    metric_kind: Literal[
        "AMOUNT", "COUNT", "DISTINCT_COUNT", "AVERAGE", "MINIMUM", "MAXIMUM", "RATIO", "PERCENTAGE", "OTHER"
    ]
    measure: Optional[str] = None
    population_id: Optional[str] = None
    numerator_population_id: Optional[str] = None
    denominator_population_id: Optional[str] = None
    grain: GrainDefinition
    filters: List[BusinessPredicate] = Field(default_factory=list)
    comparison: Optional[MetricComparison] = None
    zero_denominator_policy: Optional[
        Literal["EXCLUDE", "RETURN_ZERO", "RETURN_NULL", "ERROR", "NEEDS_USER"]
    ] = None
    null_measure_policy: Optional[
        Literal["EXCLUDE", "TREAT_AS_ZERO", "PROPAGATE_NULL", "NEEDS_USER"]
    ] = None

    @model_validator(mode="after")
    def validate_population_shape(self) -> "MetricDefinition":
        """Require explicit numerator/denominator semantics for ratio metrics."""
        if self.metric_kind in ("RATIO", "PERCENTAGE"):
            if not self.numerator_population_id or not self.denominator_population_id:
                raise ValueError(
                    "RATIO/PERCENTAGE metric requires numerator_population_id and "
                    "denominator_population_id"
                )
            if self.zero_denominator_policy is None:
                raise ValueError(
                    "RATIO/PERCENTAGE metric requires zero_denominator_policy"
                )
        elif not self.population_id:
            raise ValueError("Non-ratio metric requires population_id")
        return self


class TimeWindowSemantics(BaseModel):
    """Business time-window semantics independent of an Oracle implementation."""

    window_id: str
    purpose: Literal["OBSERVATION", "BASELINE", "COMPARISON", "EVIDENCE"]
    window_type: Literal["ROLLING", "CALENDAR", "FIXED", "RELATIVE"]
    unit: Optional[Literal["MINUTES", "HOURS", "DAYS", "WEEKS", "MONTHS", "YEARS"]] = None
    value: Optional[int] = Field(default=None, gt=0)
    offset_value: int = Field(default=0, ge=0)
    offset_unit: Optional[
        Literal["MINUTES", "HOURS", "DAYS", "WEEKS", "MONTHS", "YEARS"]
    ] = None
    fixed_start: Optional[str] = None
    fixed_end: Optional[str] = None
    anchor: Literal["EVALUATION_TIME", "EVALUATION_DATE", "EXPLICIT", "EVENT_TIME"]
    anchor_value: Optional[str] = None
    lower_inclusive: bool
    upper_inclusive: bool
    boundary_precision: Literal["CALENDAR_DAY", "BUSINESS_DAY", "INSTANT"]
    timezone: Optional[str] = None

    @model_validator(mode="after")
    def validate_window_shape(self) -> "TimeWindowSemantics":
        """Require values appropriate to relative and fixed window types."""
        if self.window_type in ("ROLLING", "RELATIVE", "CALENDAR"):
            if self.value is None or self.unit is None:
                raise ValueError(f"{self.window_type} window requires value and unit")
        if self.window_type == "FIXED" and (not self.fixed_start or not self.fixed_end):
            raise ValueError("FIXED window requires fixed_start and fixed_end")
        if self.anchor == "EXPLICIT" and not self.anchor_value:
            raise ValueError("EXPLICIT anchor requires anchor_value")
        return self


class EntityRelationship(BaseModel):
    """Business relationship required to evaluate the scenario."""

    relationship_id: str
    from_entity: str
    to_entity: str
    relationship: str
    cardinality: Literal["ONE_TO_ONE", "ONE_TO_MANY", "MANY_TO_ONE", "MANY_TO_MANY", "UNKNOWN"]
    required: bool = True
    purpose: str


class EvidenceRequirement(BaseModel):
    """Required output and transaction evidence for downstream alert creation."""

    required: bool
    output_grain: GrainDefinition
    required_fields: List[str] = Field(default_factory=list)
    include_matching_transactions: bool = False
    transaction_fields: List[str] = Field(default_factory=list)


class AmbiguityMarker(BaseModel):
    """A semantic gap that must remain visible instead of being guessed."""

    code: str
    field_path: str
    why_it_matters: str
    question: str
    options: Optional[List[str]] = None
    blocking: bool = True


class UnsupportedRequirement(BaseModel):
    """A requested business requirement not safely supported by the current contract."""

    requirement: str
    reason: str
    blocking: bool = True


class SemanticContract(BaseModel):
    """Versioned business-semantic contract consumed by the SQL generator."""

    contract_version: Literal["1.0"] = "1.0"
    evaluation_grain: GrainDefinition
    output_grain: GrainDefinition
    populations: List[PopulationDefinition] = Field(default_factory=list)
    metrics: List[MetricDefinition] = Field(default_factory=list)
    global_filters: List[BusinessPredicate] = Field(default_factory=list)
    exclusions: List[BusinessPredicate] = Field(default_factory=list)
    time_windows: List[TimeWindowSemantics] = Field(default_factory=list)
    relationships: List[EntityRelationship] = Field(default_factory=list)
    evidence: EvidenceRequirement
    ambiguities: List[AmbiguityMarker] = Field(default_factory=list)
    unsupported_requirements: List[UnsupportedRequirement] = Field(
        default_factory=list
    )

    @model_validator(mode="after")
    def validate_references(self) -> "SemanticContract":
        """Reject dangling population, metric, and time-window references."""
        population_ids = [population.population_id for population in self.populations]
        metric_ids = [metric.metric_id for metric in self.metrics]
        window_ids = [window.window_id for window in self.time_windows]
        if len(population_ids) != len(set(population_ids)):
            raise ValueError("population_id values must be unique")
        if len(metric_ids) != len(set(metric_ids)):
            raise ValueError("metric_id values must be unique")
        if len(window_ids) != len(set(window_ids)):
            raise ValueError("window_id values must be unique")

        known_populations = set(population_ids)
        known_metrics = set(metric_ids)
        known_windows = set(window_ids)
        for population in self.populations:
            if population.time_window_id and population.time_window_id not in known_windows:
                raise ValueError(
                    f"Population '{population.population_id}' references unknown time window"
                )
        for metric in self.metrics:
            references = {
                metric.population_id,
                metric.numerator_population_id,
                metric.denominator_population_id,
            } - {None}
            missing = references - known_populations
            if missing:
                raise ValueError(
                    f"Metric '{metric.metric_id}' references unknown populations: {sorted(missing)}"
                )
        predicates = [*self.global_filters, *self.exclusions]
        for population in self.populations:
            predicates.extend(population.filters)
        for metric in self.metrics:
            predicates.extend(metric.filters)
        for predicate in predicates:
            if set(predicate.population_ids) - known_populations:
                raise ValueError(
                    f"Predicate '{predicate.predicate_id}' references unknown populations"
                )
            if set(predicate.metric_ids) - known_metrics:
                raise ValueError(
                    f"Predicate '{predicate.predicate_id}' references unknown metrics"
                )
        unresolved_values = any(
            metric.zero_denominator_policy == "NEEDS_USER"
            or metric.null_measure_policy == "NEEDS_USER"
            for metric in self.metrics
        ) or any(predicate.provenance == "needs_user" for predicate in predicates)
        unresolved_relationships = any(
            relationship.required and relationship.cardinality == "UNKNOWN"
            for relationship in self.relationships
        )
        if (unresolved_values or unresolved_relationships) and not any(
            ambiguity.blocking for ambiguity in self.ambiguities
        ):
            raise ValueError(
                "NEEDS_USER or required UNKNOWN semantics require a blocking ambiguity"
            )
        return self


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
        semantic_contract (Optional[SemanticContract]): Versioned explicit business semantics.
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
    transaction_types: Optional[List[str]] = Field(
        default=None,
        description="List of explicit transaction types if multiple are present e.g. ['OUTWARD TRANSFER', 'CASH DEPOSIT'].",
    )
    explanation_codes: Optional[List[str]] = Field(
        default=None,
        description="Discovered or user-selected domain explanation code strings (flat list).",
    )
    explanation_codes_by_type: Optional[Dict[str, List[str]]] = Field(
        default=None,
        description="Discovered explanation codes partitioned/mapped by transaction type name e.g. {'OUTWARD TRANSFER': ['1414'], 'CASH DEPOSIT': ['110']}.",
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
    semantic_contract: Optional[SemanticContract] = Field(
        default=None,
        description=(
            "Versioned explicit populations, metrics, grains, time boundaries, "
            "relationships, evidence, and ambiguity markers. Optional for legacy intents."
        ),
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

    @model_validator(mode="before")
    @classmethod
    def _normalize_transaction_types(cls, data: Any) -> Any:
        if isinstance(data, dict):
            tx_type = data.get("transaction_type")
            tx_types = data.get("transaction_types")
            if isinstance(tx_type, list):
                data["transaction_types"] = [str(x).strip() for x in tx_type if str(x).strip()]
                data["transaction_type"] = ", ".join(data["transaction_types"])
            elif isinstance(tx_types, list) and tx_types:
                data["transaction_types"] = [str(x).strip() for x in tx_types if str(x).strip()]
                if not tx_type:
                    data["transaction_type"] = ", ".join(data["transaction_types"])
            elif isinstance(tx_type, str) and tx_type.strip():
                # Split comma/semicolon/OR/AND separated phrases if multiple types listed in single string
                parts = re.split(r"[,;]|\b(?:or|and)\b", tx_type, flags=re.IGNORECASE)
                cleaned = [p.strip() for p in parts if p.strip() and len(p.strip()) > 2 and p.strip().lower() not in ("or", "and")]
                if len(cleaned) > 1 and not tx_types:
                    data["transaction_types"] = cleaned
        return data

    @model_validator(mode="after")
    def validate_and_deduplicate_thresholds(self) -> "AMLIntent":
        """Apply the legacy duplicate-threshold heuristic only to legacy intents.

        If a scenario is at CUSTOMER or ACCOUNT grain with SUM/COUNT aggregation, any redundant
        DETAIL threshold matching an AGGREGATE threshold value is removed. Explicit semantic
        contracts preserve all predicates because equal values can target distinct populations.
        """
        if (
            self.semantic_contract is None
            and self.scenario_type in ["CUSTOMER", "ACCOUNT"]
            and self.aggregation
        ):
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
        blocking_semantics = bool(
            self.semantic_contract
            and (
                any(item.blocking for item in self.semantic_contract.ambiguities)
                or any(
                    item.blocking
                    for item in self.semantic_contract.unsupported_requirements
                )
            )
        )
        if self.clarifications or blocking_semantics:
            self.clarification_needed = True
            self.ready_for_handoff = False
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
