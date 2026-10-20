"""Versioned, typed envelope for agent tool results."""

import json
import inspect
from enum import Enum
from functools import wraps
from typing import Any, Callable, Dict, Literal, Optional, Type

from langchain_core.tools import tool as langchain_tool
from pydantic import BaseModel, ConfigDict, Field


class ToolStatus(str, Enum):
    """Status of a tool result."""

    OK = "ok"
    ERROR = "error"
    BLOCKED = "blocked"


class ToolErrorCode(str, Enum):
    """Small stable set of machine-readable tool failure codes."""

    INVALID_INPUT = "INVALID_INPUT"
    UPSTREAM_ERROR = "UPSTREAM_ERROR"
    SHADOW_FAILED = "SHADOW_FAILED"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    PERSIST_FAILED = "PERSIST_FAILED"
    NOT_FOUND = "NOT_FOUND"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    STAGE_NOT_ALLOWED = "STAGE_NOT_ALLOWED"
    CHAT_LOCKED_DEPLOYED = "CHAT_LOCKED_DEPLOYED"
    PARAMS_UNCONFIRMED = "PARAMS_UNCONFIRMED"
    PARAMS_INCOMPLETE = "PARAMS_INCOMPLETE"
    PARAMS_INVALID = "PARAMS_INVALID"
    CARD_STALE = "CARD_STALE"
    CARD_PENDING = "CARD_PENDING"
    REVISION_STALE = "REVISION_STALE"
    PLAN_NOT_APPROVED = "PLAN_NOT_APPROVED"
    SHADOW_NOT_PASSED = "SHADOW_NOT_PASSED"
    HASH_MISMATCH = "HASH_MISMATCH"
    WORKFLOW_CONFLICT = "WORKFLOW_CONFLICT"
    FILTER_NOT_APPLIED = "FILTER_NOT_APPLIED"
    CONFLICT_BLOCKED = "CONFLICT_BLOCKED"
    CONFLICT_ACK_REQUIRED = "CONFLICT_ACK_REQUIRED"
    CONFLICT_CHECK_UNAVAILABLE = "CONFLICT_CHECK_UNAVAILABLE"
    CONFLICT_STALE = "CONFLICT_STALE"


class PayloadModel(BaseModel):
    """Base class for typed payloads that preserves legacy extra keys."""

    model_config = ConfigDict(extra="allow")


class IntentPayload(PayloadModel):
    """Intent extraction result."""

    enriched_intent: Optional[Dict[str, Any]] = None
    explanation_code_checkpoint: Optional[str] = None
    discovered_explanation_codes: Optional[list[Dict[str, Any]]] = None
    scenario_metadata: Optional[Dict[str, Any]] = None


class PlanPayload(PayloadModel):
    """Deterministic scenario plan result."""

    plan_artifact: Optional[str] = None
    plan_conditions: Optional[list[Dict[str, str]]] = None


class FilterCheckItem(PayloadModel):
    """Status for one resolved filter in generated SQL."""

    dimension: Optional[str] = None
    raw_mention: Optional[str] = None
    polarity: Optional[str] = None
    status: Literal["applied", "missing", "mismatch", "unverified"]
    detail: Optional[str] = None


class FilterCheckPayload(PayloadModel):
    """Aggregate filter-check summary and its per-filter results."""

    validation_success: bool
    filters: list[FilterCheckItem] = Field(default_factory=list)


class ShadowPayload(PayloadModel):
    """SQL generation and shadow-test result."""

    raw_sql: Optional[str] = None
    header_alert_count: Optional[int] = None
    transaction_detail_count: Optional[int] = None
    alert_density_ratio: Optional[float] = None
    validation_success: Optional[bool] = None
    filter_check: Optional[FilterCheckPayload] = None
    error_code: Optional[str] = None
    error: Optional[str] = None


class MetadataPayload(PayloadModel):
    """Governance metadata field catalog result."""

    metadata: Optional[Dict[str, Any]] = None
    fields: Optional[list[Dict[str, Any]]] = None
    missing_mandatory_fields: Optional[list[str]] = None
    ready_for_persistence: Optional[bool] = None


class PersistencePayload(PayloadModel):
    """Scenario persistence result."""

    scenario_id: Optional[str] = None
    scenario_name: Optional[str] = None
    scenario_type: Optional[str] = None
    time_window_days: Optional[int] = None
    raw_sql_preview: Optional[str] = None
    write_success: Optional[bool] = None
    write_verification: Optional[Dict[str, Any]] = None
    created_at: Optional[str] = None
    validation_errors: Optional[list[str]] = None


class RegistryPayload(PayloadModel):
    """Dynamic scenario registry query result."""

    result: Optional[Any] = None


TOOL_PAYLOAD_MODELS: Dict[str, Type[PayloadModel]] = {
    "analyze_intent_and_discover_explanation_codes": IntentPayload,
    "generate_scenario_execution_plan": PlanPayload,
    "execute_oracle_dwh_shadow_test": ShadowPayload,
    "prepare_scenario_metadata_for_persistence": MetadataPayload,
    "persist_and_validate_scenario_in_dwh": PersistencePayload,
    "query_production_scenario_registry": RegistryPayload,
}


class ToolResult(BaseModel):
    """Versioned result envelope shared by tools and API consumers."""

    schema_version: int = Field(default=1)
    status: ToolStatus = ToolStatus.OK
    error_code: Optional[ToolErrorCode] = None
    revision_id: Optional[str] = None
    payload: Dict[str, Any] = Field(default_factory=dict)


def _infer_error_code(
    tool_name: Optional[str], payload: Dict[str, Any]
) -> Optional[ToolErrorCode]:
    """Infer a stable code from legacy-shaped tool output.

    Args:
        tool_name: Producing tool, when known.
        payload: Legacy tool payload.

    Returns:
        Error code for a failed result, otherwise None.
    """
    if (
        tool_name == "execute_oracle_dwh_shadow_test"
        and payload.get("validation_success") is False
    ):
        if payload.get("error_code") == "FILTER_NOT_APPLIED":
            return ToolErrorCode.FILTER_NOT_APPLIED
        return ToolErrorCode.SHADOW_FAILED
    if (
        tool_name == "persist_and_validate_scenario_in_dwh"
        and payload.get("write_success") is False
    ):
        return (
            ToolErrorCode.VALIDATION_FAILED
            if payload.get("validation_errors")
            else ToolErrorCode.PERSIST_FAILED
        )
    if payload.get("error"):
        message = str(payload["error"]).lower()
        if "permission_denied" in message:
            return ToolErrorCode.PERMISSION_DENIED
        if "not found" in message:
            return ToolErrorCode.NOT_FOUND
        if any(
            token in message for token in ("invalid", "required", "unknown query_type")
        ):
            return ToolErrorCode.INVALID_INPUT
        if "piotech" in message or "http error" in message:
            return ToolErrorCode.UPSTREAM_ERROR
        return ToolErrorCode.INTERNAL_ERROR
    return None


def _validate_payload(
    tool_name: Optional[str], payload: Dict[str, Any]
) -> Dict[str, Any]:
    """Validate a result payload with its tool-specific model.

    Args:
        tool_name: Producing tool name, if available.
        payload: Tool payload dictionary.

    Returns:
        Payload dictionary with all original keys retained.
    """
    model = TOOL_PAYLOAD_MODELS.get(tool_name or "")
    if model is None:
        return payload
    return model.model_validate(payload).model_dump(exclude_unset=True)


def encode_tool_result(
    tool_name: str,
    payload: Dict[str, Any],
    status: Optional[ToolStatus] = None,
    error_code: Optional[ToolErrorCode] = None,
    revision_id: Optional[str] = None,
) -> str:
    """Encode a tool payload as a JSON ToolResult envelope.

    Args:
        tool_name: Tool name used to select its typed payload model.
        payload: Legacy-shaped payload to retain inside the envelope.
        status: Explicit status override, if already known.
        error_code: Explicit error code override, if already known.

    Returns:
        Indented UTF-8 JSON envelope string.
    """
    validated_payload = _validate_payload(tool_name, payload)
    inferred_code = error_code or _infer_error_code(tool_name, validated_payload)
    result_status = status or (ToolStatus.ERROR if inferred_code else ToolStatus.OK)
    envelope = ToolResult(
        status=result_status,
        error_code=inferred_code,
        revision_id=revision_id,
        payload=validated_payload,
    )
    return envelope.model_dump_json(indent=2, exclude_none=True)


def decode_tool_result(content: Any, tool_name: Optional[str] = None) -> ToolResult:
    """Decode a current envelope or wrap a legacy tool message.

    Args:
        content: JSON string, dictionary, or message content.
        tool_name: Producing tool name for payload typing and legacy errors.

    Returns:
        Decoded, versioned ToolResult.

    Raises:
        ValueError: When content is not valid JSON or an object.
    """
    try:
        decoded = json.loads(content) if isinstance(content, str) else content
    except json.JSONDecodeError as exc:
        raise ValueError("Tool result content is not valid JSON.") from exc
    if not isinstance(decoded, dict):
        raise ValueError("Tool result content must decode to a JSON object.")
    if "schema_version" not in decoded:
        payload = _validate_payload(tool_name, decoded)
        error_code = _infer_error_code(tool_name, payload)
        return ToolResult(
            status=ToolStatus.ERROR if error_code else ToolStatus.OK,
            error_code=error_code,
            payload=payload,
        )
    return ToolResult.model_validate(decoded)


def typed_tool(function: Callable[..., Any]) -> Any:
    """Wrap a LangChain tool's JSON result in the common typed envelope.

    Args:
        function: Existing tool function returning JSON text.

    Returns:
        LangChain tool with the same name and argument contract.
    """

    def _prepare(config: Any) -> tuple[Any, str | None, str | None]:
        """Load the current workflow and enforce the shared tool guard."""
        configurable = (
            config.get("configurable", {}) if isinstance(config, dict) else {}
        )
        thread_id = configurable.get("thread_id")
        repo = None
        current_revision_id = None
        if thread_id:
            from services.aml_builder.web.domain.policy import authorize
            from services.aml_builder.web.workflow.guards import guard_tool
            from services.aml_builder.web.workflow.runtime import get_workflow_repo

            repo = get_workflow_repo()
            current = repo.current(str(thread_id))
            current_revision_id = current.revision_id if current else None
            action = (
                "chat.read"
                if function.__name__ == "query_production_scenario_registry"
                else "scenario.create"
            )
            user_id = str(configurable.get("user_id", ""))
            user_email = str(configurable.get("user_email", "")) or None
            authorized = authorize(
                user_id,
                action,
                {"user_id": user_id},
                user_email=user_email,
            )
            verdict = guard_tool(
                repo,
                str(thread_id),
                function.__name__,
                current_revision_id,
                authorized=authorized,
            )
            if not verdict.allowed:
                blocked = encode_tool_result(
                    function.__name__,
                    {"error": f"{verdict.code.value}: {verdict.message}"},
                    status=ToolStatus.BLOCKED,
                    error_code=ToolErrorCode(verdict.code.value),
                    revision_id=current_revision_id,
                )
                return repo, current_revision_id, blocked
        return repo, current_revision_id, None

    def _encode_result(
        raw_result: Any,
        repo: Any,
        current_revision_id: str | None,
        configurable: dict[str, Any],
    ) -> str:
        """Advance workflow state and encode a tool payload."""
        try:
            payload = json.loads(raw_result)
        except (json.JSONDecodeError, TypeError):
            payload = {"error": str(raw_result)}
        if not isinstance(payload, dict):
            payload = {"result": payload}
        thread_id = configurable.get("thread_id")
        if repo is not None and thread_id and "error" not in payload:
            actor = str(configurable.get("user_id", "system"))
            from services.aml_builder.web.workflow.runtime import record_tool_success

            current_revision_id = record_tool_success(
                repo, str(thread_id), function.__name__, payload, actor=actor
            )
        return encode_tool_result(
            function.__name__, payload, revision_id=current_revision_id
        )

    if inspect.iscoroutinefunction(function):
        @wraps(function)
        async def wrapped_async(*args: Any, **kwargs: Any) -> str:
            """Guard, await, advance, and encode an async workflow-aware tool."""
            config = kwargs.get("config", {})
            configurable = (
                config.get("configurable", {}) if isinstance(config, dict) else {}
            )
            from services.aml_builder.web.services.io_executor import run_in_io_pool

            repo, current_revision_id, blocked = await run_in_io_pool(
                _prepare, config
            )
            if blocked is not None:
                return blocked
            raw_result = await function(*args, **kwargs)
            return await run_in_io_pool(
                _encode_result,
                raw_result,
                repo,
                current_revision_id,
                configurable,
            )

        return langchain_tool(wrapped_async)

    @wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> str:
        """Guard, run, advance, and encode one synchronous workflow-aware tool."""
        config = kwargs.get("config", {})
        configurable = (
            config.get("configurable", {}) if isinstance(config, dict) else {}
        )
        repo, current_revision_id, blocked = _prepare(config)
        if blocked is not None:
            return blocked
        raw_result = function(*args, **kwargs)
        return _encode_result(raw_result, repo, current_revision_id, configurable)

    return langchain_tool(wrapped)
