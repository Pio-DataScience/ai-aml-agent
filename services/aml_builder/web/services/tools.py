"""
The autonomous tools exposed to the AML Scenario Builder's ReAct agent.

Each pipeline capability (intent extraction, plan generation, shadow testing,
governance metadata, persistence, registry lookup) is an independent @tool
with a rich docstring. The agent itself — compiled in graph.py — receives a
goal-oriented system prompt and decides tool invocation ordering, parameter
passing, and user interaction autonomously; there is no hardcoded routing here.

Notes:
- SQL generation is delegated to the external PioTech AI text-to-SQL SSE
  service (see execute_oracle_dwh_shadow_test).
- Persistence writes atomically to PIO_AML_AGENT_PRODUCTION_SCENARIOS and
  PIO_AML_AGENT_SCENARIOS via DeployService and OracleRegistryRepo.
- query_production_scenario_registry answers questions about ALREADY-persisted
  scenarios (inventory, search, detail, alert telemetry) — it is read-only and
  independent of the sequential scenario-creation workflow above.
"""

import json
import logging
from typing import Any, Dict, List, Literal, Optional
from langchain_core.runnables import RunnableConfig

from langchain_core.messages import HumanMessage, SystemMessage

from services.aml_builder.web.services.settings import settings
from services.aml_builder.web.api.codec import typed_tool as tool
from services.aml_builder.web.services.schemas import AMLIntent
from services.aml_builder.web.services.explanation_code_search import (
    select_relevant_explanation_codes,
    format_explanation_code_checkpoint,
    search_explanation_candidates,
)
from services.aml_builder.web.services.oracle import run_shadow_readonly
from services.aml_builder.web.services.io_executor import run_in_io_pool
from services.aml_builder.web.adapters.oracle_repo import OracleRegistryRepo
from services.aml_builder.web.domain.deploy_service import DeployError, DeployService
from services.aml_builder.web.services.llm_client import build_llm, safe_parse_json
from services.aml_builder.web.domain.code_binding import (
    bind_channels_and_codes_to_intent,
    specific_transaction_types,
)
from services.aml_builder.web.domain.intent_normalize import normalize_intent
from services.aml_builder.web.services.plan_renderer import render_plan
from services.aml_builder.web.services.prompts.loader import load_prompt
from services.aml_builder.web.domain.policy import authorize, get_role
from services.aml_builder.web.services.config_loader import load_catalogs
from services.aml_builder.web.domain.filter_resolution import (
    build_filter_prompt_section,
    build_service_b_intent,
    resolve_filter_mentions,
)
from services.aml_builder.web.domain.filter_applied_check import (
    check_filter_application,
)
from services.aml_builder.web.domain.dwh_scenario.contracts import ScenarioSqlRequest
from services.aml_builder.web.services.dwh_scenario.http_adapter import (
    HttpScenarioSqlGenerator,
)
from services.aml_builder.web.workflow.runtime import get_workflow_repo
from services.aml_builder.web.services.conflict_check import make_conflict_checker
from services.aml_builder.web.services.scenario_metadata import (
    seed_scenario_metadata,
    build_metadata_catalog,
    validate_scenario_metadata,
)
from services.aml_builder.web.services.scenario_registry_analytics import (
    search_scenarios_semantic,
    get_registry_statistics,
    get_alert_telemetry,
    get_scenario_full_details,
    list_scenario_versions,
    compare_scenario_versions,
)

logger = logging.getLogger(__name__)


def _shadow_count_payload(header_count: int) -> Dict[str, Any]:
    """Build existing shadow counts with explicit detail-count provenance.

    Args:
        header_count: Existing shadow query's header count.

    Returns:
        The unchanged numeric metrics plus additive estimate metadata.
    """
    detail_count = header_count * 4
    ratio = round(detail_count / max(header_count, 1), 2) if header_count > 0 else 0.0
    return {
        "header_alert_count": header_count,
        "transaction_detail_count": detail_count,
        "alert_density_ratio": ratio,
        "detail_count_is_estimate": True,
        "detail_count_basis": "header_count*4",
    }


def confirmed_intent_for_tool(
    repo: Any, thread_id: str, tool_name: str, supplied_intent: Any
) -> dict[str, Any]:
    """Return the confirmed intent stored on the current workflow revision.

    Args:
        repo: Workflow repository.
        thread_id: Deterministic workflow thread ID.
        tool_name: Shadow or persistence tool name for the audit event.
        supplied_intent: Model-supplied intent argument, used only for mismatch detection.

    Returns:
        Stored confirmed intent mapping.

    Raises:
        ValueError: If the workflow has no confirmed intent.
    """
    current = repo.current(thread_id)
    if current is None or not current.card_json:
        raise ValueError(
            "No confirmed scenario intent is stored on the current revision."
        )
    try:
        card = json.loads(current.card_json)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("The current revision intent is invalid.") from exc
    intent = card.get("intent") if isinstance(card, dict) else None
    if not isinstance(intent, dict) or not intent:
        raise ValueError(
            "No confirmed scenario intent is stored on the current revision."
        )
    supplied: Any = supplied_intent
    if isinstance(supplied, str):
        try:
            supplied = json.loads(supplied)
        except json.JSONDecodeError:
            supplied = None
    if isinstance(supplied, dict):
        if isinstance(supplied.get("payload"), dict):
            supplied = supplied["payload"]
        supplied = supplied.get("enriched_intent", supplied.get("intent", supplied))
    if supplied != intent:
        repo.add_event(
            thread_id,
            "system",
            "intent_arg_mismatch",
            detail={"tool": tool_name, "revision_id": current.revision_id},
        )
        logger.warning(
            "%s ignored an LLM intent argument that differed from revision %s",
            tool_name,
            current.revision_id,
        )
    return intent


def _permission_error(config: RunnableConfig, action: str) -> str | None:
    """Return an error payload when the current caller lacks a tool action.

    Args:
        config: LangGraph run configuration carrying trusted request identity.
        action: Policy action required by the tool.

    Returns:
        JSON error payload, or None when authorized.
    """
    configurable = config.get("configurable", {})
    user_id = str(configurable.get("user_id", ""))
    user_email = str(configurable.get("user_email", "")) or None
    if authorize(user_id, action, user_email=user_email):
        return None
    return json.dumps(
        {"error": f"PERMISSION_DENIED: action {action} is not allowed for this user."}
    )


@tool
def analyze_intent_and_discover_explanation_codes(
    user_prompt: str,
    existing_intent_json: Optional[str] = None,
    existing_metadata_json: Optional[str] = None,
    config: RunnableConfig = {},
) -> str:
    """Extract scenario intent, conditionally discover codes, and seed governance metadata.

    For specific transaction types, code discovery uses vector similarity search against the domain
    explanation-code catalog (top 70% relative cutoff). It also seeds PIO_AML_AGENT_SCENARIOS metadata
    (period, country/institution codes, description, etc.) for later persistence.

    ALL/ANY transaction scope does not need explanation-code discovery. Do not search the catalog
    for ALL/ANY or unspecified transaction types; only search for one or more specific types.

    IMPORTANT: The server builds a parameter confirmation card from this result. Do not invoke the
    plan tool while a card is pending; the typed confirm action applies confirmed values and renders
    the plan deterministically.

    Args:
        user_prompt (str): The FULL, VERBATIM message or modification request from the user
            (including all rules, constraints, entity types, demographic limits, thresholds, and
            time periods). NEVER truncate or summarize the user's message.
        existing_intent_json (Optional[str]): Serialized JSON string of prior AMLIntent payload if refining.
        existing_metadata_json (Optional[str]): Serialized JSON string of the scenario metadata
            accumulated so far (side panel clicks, prior turns). Already-set values are never
            overwritten by re-seeding — pass this on every refinement turn so officer picks persist.

    Returns:
        str: JSON object containing 'enriched_intent' dict, 'explanation_code_checkpoint' markdown
             table, and 'scenario_metadata' dict (PIO_AML_AGENT_SCENARIOS field values seeded so far).
    """
    denied = _permission_error(config, "scenario.create")
    if denied:
        return denied
    logger.info(
        "[TOOL: INTENT] Extracting scenario parameters and running vector search..."
    )
    llm = build_llm()

    catalogs = load_catalogs(settings.CATALOG_DIR)
    system_instruction = load_prompt("intent_extraction.md").replace(
        "{{FILTER_DIMENSIONS}}", build_filter_prompt_section(catalogs.filters)
    )
    if existing_intent_json:
        system_instruction += f"\n\nExisting Intent Payload:\n{existing_intent_json}\nPreserve existing stated fields."

    messages = [
        SystemMessage(content=system_instruction),
        HumanMessage(content=user_prompt),
    ]

    try:
        res = llm.invoke(messages)
        raw_text = str(res.content).strip() if res.content else ""
        if not raw_text and hasattr(res, "additional_kwargs") and res.additional_kwargs:
            raw_text = str(
                res.additional_kwargs.get("reasoning_content", "")
                or res.additional_kwargs.get("content", "")
            ).strip()

        if not raw_text:
            logger.error("[TOOL: INTENT] Sub-LLM returned empty output content.")
            return json.dumps(
                {"error": "Sub-LLM returned empty response during intent extraction."}
            )

        intent_dict = safe_parse_json(raw_text)
        if "AMLIntent" in intent_dict and isinstance(intent_dict["AMLIntent"], dict):
            intent_dict = intent_dict["AMLIntent"]
        elif "aml_intent" in intent_dict and isinstance(
            intent_dict["aml_intent"], dict
        ):
            intent_dict = intent_dict["aml_intent"]
        elif "intent" in intent_dict and isinstance(intent_dict["intent"], dict):
            intent_dict = intent_dict["intent"]

        intent_dict = normalize_intent(intent_dict, user_prompt)
        raw_filter_mentions = intent_dict.get("filters")
        if not isinstance(raw_filter_mentions, list):
            raw_filter_mentions = []
        intent_dict["filters"] = raw_filter_mentions
        tx_type = intent_dict.get("transaction_type")
        tx_types = intent_dict.get("transaction_types")

        explicit_codes = intent_dict.get("explanation_codes")

        discovered_codes = []
        explanation_code_lookup_status: Dict[str, str] = {}
        expl_checkpoint = None

        target_types = tx_types if tx_types else tx_type
        specific_types = specific_transaction_types(target_types)

        # If user explicitly confirmed/selected codes via LLM resolution, preserve them & skip re-discovery checkpoint
        if (
            explicit_codes
            and isinstance(explicit_codes, list)
            and len(explicit_codes) > 0
        ):
            logger.info(
                "[TOOL: INTENT] User/LLM confirmed explicit explanation codes: %s",
                explicit_codes,
            )
        elif specific_types:
            # Search only for explicitly named transaction types. ALL/ANY is
            # a scope choice, not a catalog query.
            search_types: Any = (
                specific_types if isinstance(target_types, list) else specific_types[0]
            )
            discovered_codes = select_relevant_explanation_codes(
                search_types, status_by_type=explanation_code_lookup_status
            )
            if discovered_codes:
                expl_checkpoint = format_explanation_code_checkpoint(
                    search_types, discovered_codes
                )
                code_strings = [
                    str(c.get("code")).strip()
                    for c in discovered_codes
                    if c.get("code")
                ]
                intent_dict["explanation_codes"] = code_strings

                # Build explanation_codes_by_type partitioning
                codes_by_type: Dict[str, List[str]] = {}
                for c in discovered_codes:
                    tx_key = (
                        c.get("channel")
                        or c.get("transaction_type")
                        or (
                            search_types[0]
                            if isinstance(search_types, list)
                            else str(search_types)
                        )
                    )
                    codes_by_type.setdefault(tx_key, []).append(
                        str(c.get("code")).strip()
                    )
                intent_dict["explanation_codes_by_type"] = codes_by_type

                # Bind channel-specific explanation codes directly to individual thresholds and semantic conditions
                bind_channels_and_codes_to_intent(
                    intent_dict, discovered_codes, codes_by_type
                )

                logger.info(
                    "[TOOL: INTENT] Vector search discovered %d codes across %d channels: %s",
                    len(code_strings),
                    len(codes_by_type),
                    list(codes_by_type.keys()),
                )

        else:
            # Do not let model-generated codes turn an ALL/ANY request into a
            # restricted transaction population. Explicit user-selected codes
            # are handled by the branch above.
            intent_dict["explanation_codes"] = None
            intent_dict["explanation_codes_by_type"] = None
            for condition_field in ("thresholds", "semantic_conditions"):
                conditions = intent_dict.get(condition_field)
                if isinstance(conditions, list):
                    for condition in conditions:
                        if isinstance(condition, dict):
                            condition["explanation_codes"] = None

        def filter_candidate_provider(
            dimension_name: str, raw_mention: str, dimension: dict[str, Any]
        ) -> list[dict[str, Any]]:
            """Adapt the existing explanation-code index to the filter resolver."""
            if dimension_name == "explanation_code":
                source = discovered_codes or search_explanation_candidates(
                    raw_mention,
                    min_similarity=float(
                        (dimension.get("resolver_config") or {}).get(
                            "match_threshold", 0.55
                        )
                    ),
                    top_k=int(
                        (dimension.get("resolver_config") or {}).get("max_options", 8)
                    ),
                )
                return [
                    {
                        "value": str(candidate.get("code", "")),
                        "label": str(
                            candidate.get("desc_eng") or candidate.get("code", "")
                        ),
                        "confidence": candidate.get("score", 1.0),
                    }
                    for candidate in source
                    if candidate.get("code")
                ]
            source = (dimension.get("resolver_config") or {}).get("candidates", [])
            return source if isinstance(source, list) else []

        resolved_filters = resolve_filter_mentions(
            raw_filter_mentions,
            catalogs.filters,
            candidate_provider=filter_candidate_provider,
        )
        code_row = next(
            (
                row
                for row in resolved_filters
                if row.get("dimension") == "explanation_code"
            ),
            None,
        )
        code_values = [str(value) for value in (explicit_codes or []) if value]
        if not code_values:
            code_values = [
                str(candidate.get("code"))
                for candidate in discovered_codes
                if candidate.get("code")
            ]
        if not code_values and code_row and code_row.get("status") == "resolved":
            code_values = [str(value) for value in code_row.get("values", [])]
        if code_values:
            if code_row is None:
                raw_code_mention = str(target_types or "selected explanation codes")
                code_row = {
                    "dimension": "explanation_code",
                    "raw_mention": raw_code_mention,
                    "polarity": "positive",
                    "level": "transaction",
                    "status": "resolved",
                    "confidence": None,
                    "provenance": "existing_explanation_code_catalog",
                    "values": list(dict.fromkeys(code_values)),
                    "options": [],
                    "target": catalogs.filters.dimensions["explanation_code"].target,
                    "polarity_allowed": False,
                }
                resolved_filters.append(code_row)
            else:
                code_row.update(
                    status="resolved", values=list(dict.fromkeys(code_values))
                )
            intent_dict["explanation_codes"] = list(dict.fromkeys(code_values))
        intent_dict["resolved_filters"] = resolved_filters

        intent = AMLIntent(**intent_dict)

        existing_metadata: Optional[Dict[str, Any]] = None
        if existing_metadata_json:
            try:
                existing_metadata = json.loads(existing_metadata_json)
            except json.JSONDecodeError:
                logger.warning(
                    "[TOOL: INTENT] Could not parse existing_metadata_json — seeding fresh."
                )
        scenario_metadata = seed_scenario_metadata(
            intent.model_dump(), existing_metadata
        )

        return json.dumps(
            {
                "enriched_intent": intent.model_dump(),
                "explanation_code_checkpoint": expl_checkpoint,
                "discovered_explanation_codes": discovered_codes,
                "explanation_code_lookup_status": explanation_code_lookup_status,
                "scenario_metadata": scenario_metadata,
            },
            ensure_ascii=False,
            indent=2,
        )
    except Exception as exc:
        logger.error("[TOOL: INTENT] Extraction failed: %s", exc)
        return json.dumps({"error": str(exc)})


@tool
def generate_scenario_execution_plan(
    intent_json: str,
    explanation_code_checkpoint: Optional[str] = None,
    config: RunnableConfig = {},
) -> str:
    """Generate a human-readable markdown implementation plan artifact for frontend side panel display.

    This tool deterministically renders a full Scenario Implementation Plan from the
    AMLIntent contract. Every non-empty field in the intent payload gets its own section.
    The plan is structured for UI/UX readability with tables, headers, and status banners.

    AFTER calling this tool, present the plan to the user and STOP to explicitly ask
    for their approval. Do NOT call execute_oracle_dwh_shadow_test until the user
    explicitly accepts or approves this plan.

    Args:
        intent_json (str): Serialized AMLIntent payload dictionary JSON string.
        explanation_code_checkpoint (Optional[str]): Markdown table of discovered explanation
            codes from the prior analyze_intent_and_discover_explanation_codes call. Included
            in the plan if provided, so the user sees codes alongside plan details.

    Returns:
        str: JSON containing 'plan_artifact' markdown string and 'plan_conditions' list.
    """
    denied = _permission_error(config, "scenario.create")
    if denied:
        return denied
    logger.info("[TOOL: PLANNER] Generating implementation plan artifact...")

    try:
        intent = (
            json.loads(intent_json) if isinstance(intent_json, str) else intent_json
        )
        if isinstance(intent, dict):
            if isinstance(intent.get("payload"), dict):
                if not explanation_code_checkpoint:
                    explanation_code_checkpoint = intent["payload"].get(
                        "explanation_code_checkpoint"
                    )
                intent = intent["payload"]
            # Unwrap if full tool result of analyze_intent_and_discover_explanation_codes was passed directly
            if "enriched_intent" in intent and isinstance(
                intent["enriched_intent"], dict
            ):
                if not explanation_code_checkpoint and intent.get(
                    "explanation_code_checkpoint"
                ):
                    explanation_code_checkpoint = intent.get(
                        "explanation_code_checkpoint"
                    )
                intent = intent["enriched_intent"]
    except (json.JSONDecodeError, TypeError) as exc:
        logger.error("[TOOL: PLANNER] Failed to parse intent_json: %s", exc)
        return json.dumps({"error": f"Invalid intent JSON: {exc}"})

    try:
        if not isinstance(intent, dict) or not intent:
            logger.warning(
                "[TOOL: PLANNER] Received empty or invalid intent dict: %s", intent
            )

        rendered = render_plan(
            intent if isinstance(intent, dict) else {}, explanation_code_checkpoint
        )
        plan_md = rendered["plan_artifact"]
        logger.info("[TOOL: PLANNER] Plan artifact generated (%d chars).", len(plan_md))

        return json.dumps(
            {
                "plan_artifact": plan_md,
                "plan_conditions": rendered["plan_conditions"],
            },
            ensure_ascii=False,
            indent=2,
        )
    except Exception as exc:
        logger.error("[TOOL: PLANNER] Plan generation failed: %s", exc)
        return json.dumps({"error": str(exc)})


@tool
async def execute_oracle_dwh_shadow_test(
    intent_json: str, config: RunnableConfig = {}
) -> str:
    """Generate production-grade Oracle DWH SQL via PioTech AI text-to-SQL service and execute
    a live shadow test against Oracle BI_DWH to compute real alert volume metrics.

    ONLY call this tool AFTER the user has explicitly confirmed, accepted, or approved the
    implementation plan generated by generate_scenario_execution_plan.

    Args:
        intent_json (str): Serialized AMLIntent payload dictionary JSON string.

    Returns:
        str: JSON containing 'raw_sql', 'header_alert_count', 'transaction_detail_count',
             'alert_density_ratio', and 'validation_success'.
    """
    denied = _permission_error(config, "scenario.create")
    if denied:
        return denied
    configurable = config.get("configurable", {})
    thread_id = str(configurable.get("thread_id") or "")
    if not thread_id:
        return json.dumps({"error": "Authenticated workflow identity is required."})
    try:
        workflow_repo = await run_in_io_pool(get_workflow_repo)
        intent_dict = await run_in_io_pool(
            confirmed_intent_for_tool,
            workflow_repo,
            thread_id,
            "execute_oracle_dwh_shadow_test",
            intent_json,
        )
    except ValueError as exc:
        return json.dumps({"error": str(exc)})
    logger.info("[TOOL: SQL_BRIDGE] Requesting SQL generation from PioTech AI...")

    trace_metadata = config.get("metadata", {})
    request_id = str(trace_metadata.get("request_id") or "")
    correlation_id = str(trace_metadata.get("correlation_id") or "")
    catalogs = load_catalogs(settings.CATALOG_DIR)
    service_intent = build_service_b_intent(intent_dict, catalogs.filters)
    try:
        request = ScenarioSqlRequest(
            intent=AMLIntent.model_validate(intent_dict),
            confirmed_filters=service_intent.get("resolved_filters", []),
            version_context={
                "request_id": request_id,
                "correlation_id": correlation_id,
            },
        )
    except ValueError as exc:
        return json.dumps({"error": str(exc)})

    sql_result = await HttpScenarioSqlGenerator().generate(request)
    if sql_result.error:
        if sql_result.error.startswith("PioTech AI did not return"):
            logger.error(
                "[TOOL: SQL_BRIDGE] PioTech AI did not return a valid SQL query."
            )
        return json.dumps({"error": sql_result.error})
    sql = sql_result.sql or ""
    if not sql:
        logger.error("[TOOL: SQL_BRIDGE] PioTech AI did not return a valid SQL query.")
        return json.dumps(
            {
                "error": "PioTech AI did not return a valid SQL query. Try refining the scenario intent."
            }
        )

    logger.info(
        "[TOOL: SQL_BRIDGE] SQL extracted (%d chars). Executing shadow test...",
        len(sql),
    )

    filter_policy = catalogs.policy.filter_check.unverifiable
    requested_types = intent_dict.get("transaction_types") or intent_dict.get(
        "transaction_type"
    )
    transaction_scope = (
        "SPECIFIC"
        if specific_transaction_types(requested_types)
        else "ALL" if requested_types else None
    )
    filter_check = check_filter_application(
        sql,
        [
            item.model_dump(mode="python", exclude_none=True)
            for item in request.confirmed_filters
        ],
        unverifiable=filter_policy,
        transaction_scope=transaction_scope,
    )
    if not filter_check["validation_success"]:
        return json.dumps(
            {
                "raw_sql": sql,
                "validation_success": False,
                "error_code": "FILTER_NOT_APPLIED",
                "error": "One or more resolved filters were not verified in the generated SQL.",
                "filter_check": filter_check,
                "dropped_filters": [
                    item
                    for item in filter_check["filters"]
                    if item["status"] in {"missing", "mismatch", "unverified"}
                ],
            },
            ensure_ascii=False,
            indent=2,
        )

    # -----------------------------------------------------------
    # Step 4: Execute shadow test — wrap in COUNT query for volume metrics
    # -----------------------------------------------------------
    try:
        shadow_sql = f"SELECT COUNT(*) FROM ({sql}) shadow_query"
        _, shadow_rows = await run_in_io_pool(run_shadow_readonly, shadow_sql)
        header_count = shadow_rows[0][0] if shadow_rows else 0
        count_metrics = _shadow_count_payload(header_count)

        logger.info(
            "[TOOL: SQL_BRIDGE] Shadow test complete. header_alerts=%d, detail_txns=%d, ratio=%.2f",
            count_metrics["header_alert_count"],
            count_metrics["transaction_detail_count"],
            count_metrics["alert_density_ratio"],
        )
        return json.dumps(
            {
                "raw_sql": sql,
                **count_metrics,
                "validation_success": True,
                "filter_check": filter_check,
            },
            ensure_ascii=False,
            indent=2,
        )
    except Exception as exc:
        logger.error("[TOOL: SQL_BRIDGE] Shadow execution failed: %s", exc)
        # Return the generated SQL even if the COUNT wrapper fails — useful for debugging
        return json.dumps(
            {
                "raw_sql": sql,
                "header_alert_count": None,
                "transaction_detail_count": None,
                "alert_density_ratio": None,
                "validation_success": False,
                "filter_check": filter_check,
                "error": str(exc),
            },
            ensure_ascii=False,
            indent=2,
        )


@tool
def prepare_scenario_metadata_for_persistence(
    metadata_json: str, config: RunnableConfig = {}
) -> str:
    """Build the live PIO_AML_AGENT_SCENARIOS governance field catalog for the side panel / chat fallback.

    Call this ONLY after the user has approved the shadow test results and before calling
    persist_and_validate_scenario_in_dwh. It attaches the currently valid options for every
    lookup-driven field (e.g. risk degree, category) by querying Oracle live, and reports which
    mandatory fields are still missing.

    If 'ready_for_persistence' is False in the response, present the missing mandatory fields
    to the user in chat as a fallback to the side panel — list each field's description and its
    valid options. NEVER invent or guess a value for a mandatory field yourself; only the officer's
    explicit pick (via panel click or chat) may fill it. Re-call this tool whenever the user
    supplies new field values in chat, passing the merged metadata_json back in.

    CRITICAL WORKFLOW RULE: AFTER calling this tool, you MUST STOP and output a plain-text
    conversational message to the user asking them to review/fill the governance fields in the side panel.
    DO NOT call `persist_and_validate_scenario_in_dwh` in the same turn! Wait for the user's
    explicit confirmation in a subsequent turn before calling persistence.

    Args:
        metadata_json (str): Serialized JSON of the scenario metadata accumulated so far (from
            the 'scenario_metadata' field of analyze_intent_and_discover_explanation_codes, merged
            with any side panel clicks or chat-provided values).

    Returns:
        str: JSON containing 'metadata' (current values), 'fields' (full catalog with live
             lookup options), 'missing_mandatory_fields', and 'ready_for_persistence'.
    """
    denied = _permission_error(config, "scenario.create")
    if denied:
        return denied
    logger.info("[TOOL: METADATA] Building PIO_AML_AGENT_SCENARIOS field catalog...")

    try:
        metadata = json.loads(metadata_json) if metadata_json else {}
    except json.JSONDecodeError as exc:
        logger.error("[TOOL: METADATA] Invalid metadata_json: %s", exc)
        return json.dumps({"error": f"Invalid metadata JSON: {exc}"})

    try:
        catalog = build_metadata_catalog(metadata)
        logger.info(
            "[TOOL: METADATA] Catalog built. missing=%s ready=%s",
            catalog["missing_mandatory_fields"],
            catalog["ready_for_persistence"],
        )
        return json.dumps(catalog, ensure_ascii=False, indent=2, default=str)
    except Exception as exc:
        logger.error("[TOOL: METADATA] Catalog build failed: %s", exc)
        return json.dumps({"error": str(exc)})


@tool
def persist_and_validate_scenario_in_dwh(
    intent_json: str, raw_sql: str, metadata_json: str, config: RunnableConfig = {}
) -> str:
    """Persist the confirmed, shadow-tested AML scenario atomically into both
    PIO_AML_AGENT_PRODUCTION_SCENARIOS (for the automated daily ETL runner) and
    PIO_AML_AGENT_SCENARIOS (business metadata for downstream compliance UI/workflow modules).

    CRITICAL: ONLY call this tool when the user in a SUBSEQUENT turn explicitly instructs
    you to persist/save the scenario after reviewing the governance metadata in the side panel.
    NEVER call this tool autonomously immediately after prepare_scenario_metadata_for_persistence!

    Performs an idempotent versioned insert on both tables in one Oracle transaction.
    A prior deployment for the same thread is returned unchanged. Only call after shadow test results have been
    reviewed AND prepare_scenario_metadata_for_persistence reports ready_for_persistence=True
    (or the user explicitly confirms proceeding despite missing fields).

    Every mandatory PIO_AML_AGENT_SCENARIOS field is re-validated here against its live lookup table
    or allowed-value set before anything is written — if validation fails, NOTHING is written
    to either table and the problems are returned for you to relay to the user.

    Args:
        intent_json (str): Serialized AMLIntent payload dictionary JSON string.
        raw_sql (str): Production-grade Oracle SQL statement from the shadow test.
        metadata_json (str): Serialized PIO_AML_AGENT_SCENARIOS metadata JSON — the merged output of
            prepare_scenario_metadata_for_persistence plus any officer picks.

    Returns:
        str: JSON containing scenario ID, write status, verification fields, and receipt.
    """
    denied = _permission_error(config, "scenario.create")
    if denied:
        return denied
    logger.info(
        "[TOOL: PERSISTER] Writing confirmed scenario to PIO_AML_AGENT_PRODUCTION_SCENARIOS + PIO_AML_AGENT_SCENARIOS..."
    )

    try:
        configurable = config.get("configurable", {})
        thread_id = str(configurable.get("thread_id") or "")
        if not thread_id:
            raise ValueError("Authenticated workflow identity is required.")
        confirmed_intent_for_tool(
            get_workflow_repo(),
            thread_id,
            "persist_and_validate_scenario_in_dwh",
            intent_json,
        )
    except ValueError as exc:
        logger.error("[TOOL: PERSISTER] Confirmed intent unavailable: %s", exc)
        return json.dumps(
            {
                "write_success": False,
                "error_code": "PARAMS_UNCONFIRMED",
                "error": str(exc),
            }
        )

    try:
        metadata_dict = json.loads(metadata_json) if metadata_json else {}
    except json.JSONDecodeError as exc:
        logger.error("[TOOL: PERSISTER] Invalid metadata JSON: %s", exc)
        return json.dumps({"error": f"Invalid metadata JSON: {exc}"})

    configurable = config.get("configurable", {})
    thread_id = str(configurable.get("thread_id") or "")
    actor_id = str(configurable.get("user_id") or "")
    if not thread_id or not actor_id:
        return json.dumps(
            {
                "write_success": False,
                "error_code": "PERMISSION_DENIED",
                "error": "Authenticated workflow identity is required.",
            }
        )
    workflow_repo = get_workflow_repo()
    workflow = workflow_repo.get_workflow(thread_id)
    revision = workflow_repo.current(thread_id) if workflow else None
    if revision is None:
        return json.dumps(
            {
                "write_success": False,
                "error_code": "STAGE_NOT_ALLOWED",
                "error": "No current workflow revision is available.",
            }
        )
    policy = load_catalogs(settings.CATALOG_DIR).policy
    actor_email = str(configurable.get("user_email") or "") or None
    service = DeployService(
        oracle_repo=OracleRegistryRepo(),
        workflow_repo=workflow_repo,
        metadata_validator=validate_scenario_metadata,
        authorizer=lambda user_id, action: authorize(
            user_id, action, user_email=actor_email
        ),
        enforcement=policy.workflow.enforcement.model_dump(),
        conflict_check_required=policy.workflow.conflict_check_required,
        conflict_on_unavailable=policy.conflict_check.on_unavailable,
        conflict_gate=make_conflict_checker(workflow_repo),
        numeric_created_by_default=settings.AML_CREATED_BY,
    )
    try:
        result = service.deploy(
            thread_id=thread_id,
            revision_id=revision.revision_id,
            actor_id=actor_id,
            actor_role=get_role(actor_id, actor_email),
            metadata=metadata_dict,
            raw_sql=raw_sql,
        )
    except (DeployError, RuntimeError) as exc:
        logger.warning("[TOOL: PERSISTER] Deployment refused: %s", exc)
        return json.dumps(
            {
                "write_success": False,
                "error_code": getattr(exc, "code", "DEPLOY_FAILED"),
                "error": str(exc),
                "validation_errors": getattr(exc, "details", None),
            },
            ensure_ascii=False,
        )

    logger.info(
        "[TOOL: PERSISTER] Scenario persisted. SCENARIO_ID=%s", result.scenario_id
    )
    return json.dumps(
        {
            "scenario_id": result.scenario_id,
            "scenario_name": result.scenario_name,
            "write_success": True,
            "write_verification": {
                "production_scenarios_table": "PIO_AML_AGENT_PRODUCTION_SCENARIOS",
                "scenario_metadata_table": "PIO_AML_AGENT_SCENARIOS",
                "is_active": 1,
            },
            "receipt": result.receipt,
            "idempotent_retry": result.repeated,
        },
        ensure_ascii=False,
        indent=2,
    )


@tool
def query_production_scenario_registry(
    query_type: Literal[
        "semantic_search", "get_statistics", "get_alert_metrics", "get_scenario_detail",
        "list_versions", "compare_versions",
    ],
    search_text: Optional[str] = None,
    scenario_id: Optional[str] = None,
    compare_to_scenario_id: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    customer_number: Optional[str] = None,
    limit: int = 5,
    config: RunnableConfig = {},
) -> str:
    """Query this agent's own persisted scenario registry — inventory, search, detail, and alert telemetry.

    Use this tool whenever the compliance officer asks about EXISTING scenarios rather than building a
    new one — e.g. "how many scenarios do we have?", "do we already have a rule for structuring?",
    "show me scenario PRD_XXXX", "how many alerts has this scenario generated?". Never answer these
    questions from memory or by guessing — always call this tool. This is independent of the sequential
    scenario-creation workflow and can be called at any point in the conversation.

    Four query_type modes:
      - 'semantic_search': Natural language concept search over scenario name + detection logic
        (e.g. "structuring under 10k", "minors with high turnover", "cash deposits"). Computes
        embeddings fresh on every call against the live registry — always up to date, no stale
        cache. Requires `search_text`.
      - 'get_statistics': Deterministic inventory KPIs — total/active/inactive counts, breakdown by
        risk degree and category, lifetime alert total. No parameters needed.
      - 'get_alert_metrics': Live alert telemetry from PIO_AML_CUSTOMERS/PIO_AML_CUSTOMERS_DET —
        per-scenario firing counts, alert volume by date, or (if `customer_number` is given) all
        alerts for a specific customer. Optionally filter by `scenario_id` and/or `date_from`/
        `date_to` ('YYYY-MM-DD').
      - 'get_scenario_detail': Full record for one scenario — requires `scenario_id`
        (e.g. 'PRD_50FC063C').
      - 'list_versions': All versions for the key containing `scenario_id`, newest first.
      - 'compare_versions': Deterministic stored-JSON diff between `scenario_id` and
        `compare_to_scenario_id` (both versions must share a stable key).

    Args:
        query_type (str): Which of the 4 modes to run.
        search_text (Optional[str]): Natural language query — required for 'semantic_search'.
        scenario_id (Optional[str]): Exact scenario identifier — required for 'get_scenario_detail',
            optional filter for 'get_alert_metrics'.
        date_from (Optional[str]): Inclusive lower bound 'YYYY-MM-DD', for 'get_alert_metrics'.
        date_to (Optional[str]): Inclusive upper bound 'YYYY-MM-DD', for 'get_alert_metrics'.
        customer_number (Optional[str]): Customer number to list individual alerts for, for
            'get_alert_metrics'.
        limit (int): Max results/rows to return (clamped to a sane range per mode). Default 5.

    Returns:
        str: JSON object with the mode-specific result, or {'error': ...} if the request was invalid.
    """
    denied = _permission_error(config, "chat.read")
    if denied:
        return denied
    logger.info("[TOOL: REGISTRY] query_type=%s", query_type)

    try:
        if query_type == "semantic_search":
            if not search_text:
                return json.dumps(
                    {
                        "error": "search_text is required for query_type='semantic_search'."
                    }
                )
            result: Any = search_scenarios_semantic(search_text, limit=limit or 5)

        elif query_type == "get_statistics":
            result = get_registry_statistics()

        elif query_type == "get_alert_metrics":
            result = get_alert_telemetry(
                scenario_id=scenario_id,
                date_from=date_from,
                date_to=date_to,
                customer_number=customer_number,
                limit=limit or 10,
            )

        elif query_type == "get_scenario_detail":
            if not scenario_id:
                return json.dumps(
                    {
                        "error": "scenario_id is required for query_type='get_scenario_detail'."
                    }
                )
            detail = get_scenario_full_details(scenario_id)
            result = (
                detail
                if detail is not None
                else {"error": f"No scenario found with scenario_id='{scenario_id}'."}
            )

        elif query_type == "list_versions":
            if not scenario_id:
                return json.dumps({"error": "scenario_id is required for query_type='list_versions'."})
            result = list_scenario_versions(scenario_id)

        elif query_type == "compare_versions":
            if not scenario_id or not compare_to_scenario_id:
                return json.dumps({"error": "scenario_id and compare_to_scenario_id are required for query_type='compare_versions'."})
            result = compare_scenario_versions(scenario_id, compare_to_scenario_id)

        else:
            return json.dumps({"error": f"Unknown query_type: '{query_type}'."})

        return json.dumps(result, ensure_ascii=False, indent=2, default=str)
    except ValueError as exc:
        # Bad date format, etc. — a client-correctable input error, not a system failure.
        logger.warning("[TOOL: REGISTRY] Invalid input: %s", exc)
        return json.dumps({"error": str(exc)})
    except Exception as exc:
        logger.error(
            "[TOOL: REGISTRY] query_type=%s failed: %s", query_type, exc, exc_info=True
        )
        return json.dumps({"error": str(exc)})
