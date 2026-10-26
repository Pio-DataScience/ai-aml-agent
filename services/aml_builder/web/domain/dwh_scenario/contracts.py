"""Typed request and result contracts for the DWH SQL boundary."""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from services.aml_builder.web.services.schemas import (
    AMLIntent,
    AggregationProfile,
    BaselineWindow,
    Clarification,
    FilterMention,
    SemanticCondition,
    Threshold,
    TimeWindow,
)


class ConfirmedFilter(BaseModel):
    """Catalog-resolved filter selected for the Service B handoff."""

    model_config = ConfigDict(extra="forbid")

    dimension: str
    raw_mention: str = ""
    polarity: str = "positive"
    level: str = "unknown"
    status: str = "resolved"
    options: list[Any] = Field(default_factory=list)
    values: list[str]
    confidence: float | None = None
    provenance: str = "catalog_resolver"
    audit_choice: str | None = None
    acknowledged_without_filter: bool | None = None
    target: dict[str, Any] = Field(default_factory=dict)
    match: str = "any_of"
    polarity_allowed: bool = False


class ScenarioVersionContext(BaseModel):
    """Optional scenario version and request tracing context."""

    model_config = ConfigDict(extra="forbid")

    scenario_id: str | None = None
    version: int | None = Field(default=None, ge=1)
    parent_scenario_id: str | None = None
    parent_version: int | None = Field(default=None, ge=1)
    request_id: str | None = None
    correlation_id: str | None = None


class ScenarioSqlRequest(BaseModel):
    """Validated AML intent and confirmed context sent to a SQL generator."""

    model_config = ConfigDict(extra="forbid")

    intent: AMLIntent
    confirmed_filters: list[ConfirmedFilter] = Field(default_factory=list)
    version_context: ScenarioVersionContext = Field(
        default_factory=ScenarioVersionContext
    )

    @field_validator("intent", mode="before")
    @classmethod
    def reject_unknown_intent_fields(cls, value: Any) -> Any:
        """Reject ignored extras before AMLIntent's compatibility parsing."""
        if not isinstance(value, dict):
            return value
        _reject_extra_keys(value, AMLIntent.model_fields, "intent")
        nested_fields = {
            "thresholds": Threshold,
            "semantic_conditions": SemanticCondition,
            "time_window": TimeWindow,
            "baseline_window": BaselineWindow,
            "aggregation": AggregationProfile,
            "clarifications": Clarification,
            "filters": FilterMention,
        }
        for field_name, model in nested_fields.items():
            nested = value.get(field_name)
            entries = nested if isinstance(nested, list) else [nested]
            for entry in entries:
                if isinstance(entry, dict):
                    _reject_extra_keys(
                        entry, model.model_fields, f"intent.{field_name}"
                    )
        return value

    @model_validator(mode="after")
    def validate_handoff(self) -> "ScenarioSqlRequest":
        """Reject intents and filters that are not ready for generation."""
        if not self.intent.ready_for_handoff or self.intent.clarification_needed:
            raise ValueError("AML intent is not ready for SQL generation")
        if self.intent.clarifications:
            raise ValueError("AML intent has unresolved clarification requirements")
        unresolved = [
            item
            for item in self.intent.resolved_filters
            if isinstance(item, dict)
            and item.get("status") != "resolved"
            and item.get("status") not in {"removed", "disabled"}
        ]
        if unresolved:
            names = ", ".join(
                str(item.get("dimension", "unknown")) for item in unresolved
            )
            raise ValueError(f"Unresolved required filters: {names}")
        for item in self.confirmed_filters:
            if item.status != "resolved" or not item.values:
                raise ValueError("Confirmed filters must be resolved and have values")
        return self


def _reject_extra_keys(
    value: dict[str, Any], fields: dict[str, Any], path: str
) -> None:
    """Raise when a contract mapping contains fields outside its typed model."""
    unknown = set(value) - set(fields)
    if unknown:
        raise ValueError(f"Unknown {path} field(s): {', '.join(sorted(unknown))}")


class ExecutionEvidence(BaseModel):
    """Execution evidence returned by a SQL generator, when available."""

    model_config = ConfigDict(extra="forbid")

    columns: list[str] = Field(default_factory=list)
    row_count: int | None = Field(default=None, ge=0)
    truncated: bool = False
    error: str | None = None


class ScenarioSqlResult(BaseModel):
    """Generated SQL with approval, evidence, validation, and trace metadata."""

    model_config = ConfigDict(extra="forbid")

    sql: str | None = None
    approved: bool = False
    execution: ExecutionEvidence = Field(default_factory=ExecutionEvidence)
    validation_findings: list[dict[str, Any]] = Field(default_factory=list)
    provider: str | None = None
    model: str | None = None
    trace_id: str | None = None
    error: str | None = None


class ScenarioSqlGenerator(Protocol):
    """Generate SQL from the canonical typed request."""

    async def generate(self, request: ScenarioSqlRequest) -> ScenarioSqlResult:
        """Generate SQL asynchronously for one AML scenario."""
        ...
