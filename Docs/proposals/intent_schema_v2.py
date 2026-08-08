"""Proposed v2 schema for the Intent Analyst node.

Design principles (the finite/infinite boundary this schema enforces):
- Domain entities (subjects, predicates, metrics, grains) are OPEN-ENDED plain
  English. We never enumerate them — that space is infinite and non-standardized.
- Query *grammar* (operators, time units, provenance) IS enumerated. That space
  is small, stable, and safe to close.
- Every business value carries provenance: was it STATED by the user, safely
  DEFAULTED by the parser, or is it materially AMBIGUOUS and must be asked?
  This is what operationalizes "ask, don't silently guess." It is a
  classification of the user's text (verifiable), not the LLM narrating its
  own downstream reasoning.

Drop these into web/services/schemas.py, replacing the old AMLIntent, or import
from here while you test.
"""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field

# --- Finite, safe-to-enumerate grammar -------------------------------------

Operator = Literal[">", "<", ">=", "<=", "=", "!=", "BETWEEN", "IN"]
Provenance = Literal["stated", "assumed_default", "needs_user"]
TimeUnit = Literal["DAYS", "WEEKS", "MONTHS", "YEARS"]


# --- Open-ended business elements ------------------------------------------

class BusinessCondition(BaseModel):
    """A single business rule extracted from the request, in plain English.

    The subject/predicate are intentionally NOT constrained to enums — they are
    handed to the downstream text-to-SQL agent, which grounds them against the
    live data dictionary. This node's job is to capture WHAT the analyst means,
    not to bind it to columns.

    Args:
        raw_phrase: The user's own words this condition was extracted from, for
            traceability and for the downstream critique node to diff against.
        subject: The entity the condition applies to, as a plain English noun
            (e.g. 'Customer', 'ATM', 'Bank Teller', 'Crypto Wallet'). Never an
            enum.
        predicate: The business rule in plain English (e.g. 'is domiciled in a
            high-risk country', 'is a cash deposit').
        operator: Comparison operator, if the condition is numeric.
        value_from: Lower/only numeric bound, if numeric.
        value_to: Upper bound, only for BETWEEN.
        provenance: Whether the value was stated by the user, defaulted by the
            parser, or still needs the user to resolve it.
    """

    raw_phrase: str = Field(..., description="Verbatim user text this came from.")
    subject: str = Field(..., description="Plain English entity noun. No enums.")
    predicate: str = Field(..., description="Plain English business rule.")
    operator: Optional[Operator] = Field(
        default=None, description="Comparison operator if numeric, else null."
    )
    value_from: Optional[float] = Field(default=None)
    value_to: Optional[float] = Field(default=None, description="Only for BETWEEN.")
    provenance: Provenance = Field(
        default="stated",
        description="stated | assumed_default | needs_user.",
    )


class AggregationProfile(BaseModel):
    """How the metric is measured and at what grain.

    Grain and metric are plain English so the SQL agent can ground them. The
    only reason this is a distinct object is that grain/measure are the most
    commonly-omitted-yet-outcome-changing dimensions in AML rules, so we force
    the parser to state them (or flag them ambiguous) rather than let them hide
    inside a threshold.

    Args:
        metric: What is being measured, plain English (e.g. 'Total deposit
            amount', 'Count of distinct beneficiaries').
        function: Aggregation function in plain English (e.g. 'sum', 'count',
            'count distinct', 'max'). Null if per-row (no aggregation).
        grain: The evaluation grain, plain English (e.g. 'Per Transaction',
            'Per Customer per Day', 'Per ATM per Month').
        provenance: stated | assumed_default | needs_user.
    """

    metric: str = Field(..., description="What is measured, plain English.")
    function: Optional[str] = Field(
        default=None, description="sum | count | count distinct | max | null."
    )
    grain: str = Field(..., description="Evaluation grain, plain English.")
    provenance: Provenance = Field(default="stated")


class TimeWindow(BaseModel):
    """Observation window. Kept close to your existing TimeWindow.

    Args:
        unit: DAYS | WEEKS | MONTHS | YEARS.
        value: Numeric size of the window.
        is_rolling: True for rolling window, False for calendar-aligned.
        provenance: stated | assumed_default | needs_user.
    """

    unit: Optional[TimeUnit] = Field(default=None)
    value: Optional[int] = Field(default=None)
    is_rolling: Optional[bool] = Field(default=None)
    provenance: Provenance = Field(default="stated")


class Clarification(BaseModel):
    """One material ambiguity the parser could NOT safely resolve.

    Only created for dimensions where a wrong guess would materially change the
    result AND the parser cannot infer or safely default the answer. Everything
    else is defaulted (with provenance='assumed_default') and NOT asked.

    Args:
        dimension: The ambiguous dimension in plain English (e.g. 'transaction
            direction', 'cash vs wire').
        why_it_matters: One line on how a wrong guess changes the outcome.
        question: The business-phrased question to show the user. Never
            technical (no table/column talk).
        options: Suggested answers to offer, if a small set applies.
    """

    dimension: str = Field(...)
    why_it_matters: str = Field(...)
    question: str = Field(..., description="Business-phrased. Never technical.")
    options: Optional[List[str]] = Field(default=None)


class AMLIntent(BaseModel):
    """Structured AML detection intent produced by the Intent Analyst node.

    Contract with the downstream SQL agent: this captures the analyst's business
    intent as far as it can be known, marks every value's provenance, and lists
    only the material ambiguities that must be resolved before a trustworthy
    query can be built. It does NOT bind anything to schema — that is the SQL
    agent's job against the data dictionary.

    Args:
        scenario_name: Descriptive name.
        monitored_subject: Primary entity monitored, plain English (open-ended).
        detection_logic: One-paragraph plain English summary of the rule.
        conditions: All business conditions (filters + numeric thresholds).
        aggregation: How the metric is measured and at what grain.
        time_window: Observation window.
        exclusions: Explicit exclusions in plain English (e.g. 'exclude reversed
            transactions', 'exclude internal transfers').
        clarifications: Material ambiguities that must be asked before handoff.
        ready_for_handoff: False if any clarification blocks a trustworthy query.
        applied_defaults: Human-readable list of business defaults the parser
            applied, shown to the user for confirmation/transparency.
        expected_alert_range_min: Lower bound of expected alert volume.
        expected_alert_range_max: Upper bound of expected alert volume.
    """

    scenario_name: str = Field(...)
    monitored_subject: str = Field(
        ..., description="Primary monitored entity, plain English. No enums."
    )
    detection_logic: str = Field(..., description="Plain English rule summary.")
    conditions: List[BusinessCondition] = Field(default_factory=list)
    aggregation: Optional[AggregationProfile] = Field(default=None)
    time_window: Optional[TimeWindow] = Field(default=None)
    exclusions: List[str] = Field(
        default_factory=list, description="Plain English exclusion rules."
    )
    clarifications: List[Clarification] = Field(
        default_factory=list,
        description="Material ambiguities to resolve before handoff.",
    )
    ready_for_handoff: bool = Field(
        default=True,
        description="False if any clarification must be answered first.",
    )
    applied_defaults: List[str] = Field(
        default_factory=list,
        description="Business defaults assumed, shown to the user.",
    )
    expected_alert_range_min: Optional[int] = Field(default=None)
    expected_alert_range_max: Optional[int] = Field(default=None)
