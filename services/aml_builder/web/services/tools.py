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
- Persistence writes atomically to PIO_AML_PRODUCTION_SCENARIOS and
  PIO_AML_SCENARIO via the production_registry module.
- query_production_scenario_registry answers questions about ALREADY-persisted
  scenarios (inventory, search, detail, alert telemetry) — it is read-only and
  independent of the sequential scenario-creation workflow above.
"""

import re
import copy
import json
import logging
import uuid
import httpx
from datetime import date, datetime
from typing import Annotated, Any, Dict, Final, List, Literal, Optional, Tuple

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

from services.aml_builder.web.services.settings import settings
from services.aml_builder.web.services.schemas import AMLIntent
from services.aml_builder.web.services.explanation_code_search import (
    select_relevant_explanation_codes,
    format_explanation_code_checkpoint,
)
from services.aml_builder.web.services.oracle import run_readonly, run_shadow_readonly
from services.aml_builder.web.services.production_registry import (
    DuplicateScenarioError,
    save_production_scenario,
)
from services.aml_builder.web.services.llm_client import build_llm, safe_parse_json
from services.aml_builder.web.services.sql_extraction import extract_sql
from services.aml_builder.web.services.intent_autocorrect import auto_correct_intent, resolve_intent
from services.aml_builder.web.services.business_dates import (
    apply_fixed_period,
    clean_for_sql_service,
    period_of,
    to_date,
)
from services.aml_builder.web.services.sql_dates import anchor_sql, bind_values, pin_period, slide_window_over_period
from services.aml_builder.web.services.clarification import (
    answered_fields,
    apply_answers,
    all_types_if_any,
    clarification_payload,
    default_entity,
    describe_answers,
    drop_placeholder_types,
    find_gaps,
    pending_clarification,
    apply_ratio_conditions,
    balance_capability_refusal,
    for_sql_service,
    invalid_rule_message,
    parse_ratio_conditions,
    plain_text,
    validate_conditions,
)
from services.aml_builder.web.services.attribute_filters import (
    apply_attribute_filters,
    drop_covered_transaction_types,
    resolve_attribute_filters,
    strip_attribute_phrases,
    top_level_clauses,
    validate_filters,
)
from services.aml_builder.web.services.piotech_report import (
    build_http_rejection,
    build_rejection,
    build_unavailable_rejection,
    normalize_scenario_report,
    summarize_rejection,
)
from services.aml_builder.web.services.plan_renderer import build_plan_markdown
from services.aml_builder.web.services.prompts.loader import load_prompt
from services.aml_builder.web.services.scenario_metadata import (
    seed_scenario_metadata,
    build_metadata_catalog,
    validate_scenario_metadata,
)
from services.aml_builder.web.services.scenario_registry_analytics import (
    find_similar_scenarios,
    search_scenarios_semantic,
    get_registry_statistics,
    get_alert_telemetry,
    get_scenario_full_details,
)

logger = logging.getLogger(__name__)

_ALL_TRANSACTION_SCOPE_LABELS = {
    "ALL",
    "ALL TRANSACTION",
    "ALL TRANSACTIONS",
    "ALL TYPES",
    "ALL TRANSACTION TYPES",
    "ANY",
    "ANY TRANSACTION",
    "ANY TRANSACTIONS",
    "ANY TYPE",
    "ANY TYPES",
    "ANY TRANSACTION TYPE",
    "ANY TRANSACTION TYPES",
}


def _specific_transaction_types(transaction_type: Any) -> List[str]:
    """Return specific transaction types, excluding all/any scope labels.

    Args:
        transaction_type: One transaction type or a list of transaction types.

    Returns:
        Specific transaction types that can be used for code discovery.
    """
    values = (
        transaction_type if isinstance(transaction_type, list) else [transaction_type]
    )
    return [
        normalized
        for value in values
        if value and (normalized := " ".join(str(value).strip().upper().split()))
        and normalized not in _ALL_TRANSACTION_SCOPE_LABELS
    ]


# =============================================================================
def _bind_channels_and_codes_to_intent(
    intent_dict: Dict[str, Any],
    discovered_codes: List[Dict[str, Any]],
    codes_by_type: Dict[str, List[str]],
) -> None:
    """Bind channel-specific explanation codes directly to individual thresholds and semantic conditions."""
    if not codes_by_type or len(codes_by_type) <= 0:
        return

    # 1. Bind to semantic_conditions
    sc_list = intent_dict.get("semantic_conditions")
    if isinstance(sc_list, list):
        for sc in sc_list:
            if not isinstance(sc, dict):
                continue
            text = f"{sc.get('raw_phrase', '')} {sc.get('predicate', '')}".lower()
            for tx_key, codes in codes_by_type.items():
                tx_lower = tx_key.lower()
                tx_words = [w for w in tx_lower.split() if len(w) > 3]
                if any(w in text for w in tx_words) or tx_lower in text:
                    if not sc.get("transaction_type"):
                        sc["transaction_type"] = tx_key
                    if not sc.get("explanation_codes"):
                        sc["explanation_codes"] = codes
                    break

    # 2. Bind to thresholds
    thresholds = intent_dict.get("thresholds")
    if isinstance(thresholds, list):
        for idx, t in enumerate(thresholds):
            if not isinstance(t, dict):
                continue
            existing_tx = t.get("transaction_type")
            if existing_tx and existing_tx in codes_by_type:
                t["explanation_codes"] = codes_by_type[existing_tx]
                continue

            matched_tx = None

            # Check correlation with semantic conditions that already have transaction_type
            if isinstance(sc_list, list):
                for sc in sc_list:
                    if isinstance(sc, dict) and sc.get("transaction_type"):
                        raw_sc = str(sc.get("raw_phrase", "")).lower()
                        pred_sc = str(sc.get("predicate", "")).lower()
                        val_str = str(t.get("value_from", ""))
                        if (
                            val_str in raw_sc
                            or val_str in pred_sc
                            or (">" in str(t.get("operator", "")) and ("exceed" in pred_sc or "over" in raw_sc))
                            or ("<" in str(t.get("operator", "")) and ("below" in pred_sc or "under" in raw_sc))
                        ):
                            matched_tx = sc["transaction_type"]
                            break

            # Fallback by index alignment if threshold count matches types count
            if not matched_tx and len(thresholds) == len(codes_by_type):
                types_keys = list(codes_by_type.keys())
                if idx < len(types_keys):
                    matched_tx = types_keys[idx]

            if matched_tx and matched_tx in codes_by_type:
                t["transaction_type"] = matched_tx
                t["explanation_codes"] = codes_by_type[matched_tx]


@tool
def analyze_intent_and_discover_explanation_codes(
    user_prompt: str,
    existing_intent_json: Optional[str] = None,
    existing_metadata_json: Optional[str] = None,
    state: Annotated[Optional[Dict[str, Any]], InjectedState] = None,
    config: RunnableConfig = None,  # type: ignore[assignment]
) -> str:
    """Extract scenario intent, conditionally discover codes, and seed governance metadata.

    For specific transaction types, code discovery uses vector similarity search against the domain
    explanation-code catalog (top 70% relative cutoff). It also seeds PIO_AML_SCENARIO metadata
    (period, country/institution codes, description, etc.) for later persistence.

    ALL/ANY transaction scope does not need explanation-code discovery. Do not search the catalog
    for ALL/ANY or unspecified transaction types; only search for one or more specific types.

    IMPORTANT: Always invoke `generate_scenario_execution_plan` immediately after this tool in the
    same turn to render the implementation plan artifact in the side panel before responding to the user.

    Args:
        user_prompt (str): The FULL, VERBATIM message or modification request from the user
            (including all rules, constraints, entity types, demographic limits, thresholds, and
            time periods). NEVER truncate or summarize the user's message.
        existing_intent_json (Optional[str]): Serialized JSON string of prior AMLIntent payload if refining.
        existing_metadata_json (Optional[str]): Serialized JSON string of the scenario metadata
            accumulated so far (side panel clicks, prior turns). Already-set values are never
            overwritten by re-seeding — pass this on every refinement turn so officer picks persist.

        state: Injected agent state (never passed by the model); used to find an open clarification.
        config: Injected run config (never passed by the model); carries the clarification-form
            answers of this turn under configurable.clarification_answers.

    Returns:
        str: JSON object containing 'status': 'COMPLETE', 'enriched_intent' dict,
             'explanation_code_checkpoint' markdown table, and 'scenario_metadata' dict
             (PIO_AML_SCENARIO field values seeded so far). When a mandatory parameter (target
             entity, threshold amount, timeframe, channels) is not stated: 'status':
             'NEEDS_CLARIFICATION' with 'understood_summary', 'missing_fields' and 'questions';
             then STOP and do not call any other tool.
    """
    logger.info(
        "[TOOL: INTENT] Extracting scenario parameters and running vector search..."
    )
    # Clarification answers (from the form) complete the request that asked for them.
    answers = ((config or {}).get("configurable") or {}).get("clarification_answers") or None
    pending = pending_clarification((state or {}).get("messages", [])) if answers else None
    original_request = user_prompt
    known_intent: Optional[Dict[str, Any]] = None
    if pending:
        earlier = str(pending.get("original_request") or "").strip()
        if earlier and earlier not in user_prompt:
            original_request = earlier
            user_prompt = earlier + "\n\n" + describe_answers(answers)
        if not existing_intent_json and pending.get("partial_intent"):
            existing_intent_json = json.dumps(pending["partial_intent"], ensure_ascii=False)
    elif existing_intent_json:
        try:
            known_intent = json.loads(existing_intent_json)
        except json.JSONDecodeError:
            known_intent = None
    # Answer turn: the request was already parsed; the answers fill its slots (no new LLM guess).
    base_intent = (copy.deepcopy(pending["partial_intent"])
                   if pending and isinstance(pending.get("partial_intent"), dict) else None)
    llm = build_llm()

    system_instruction = load_prompt("intent_extraction.md")
    if existing_intent_json:
        system_instruction += f"\n\nExisting Intent Payload:\n{existing_intent_json}\nPreserve existing stated fields."

    messages = [
        SystemMessage(content=system_instruction),
        HumanMessage(content=user_prompt),
    ]

    try:
        if base_intent is not None:
            intent_dict = base_intent
        else:
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

        # Sanitize optional dict/model fields that might be serialized as "" or invalid empty types
        for dict_field in ("baseline_window", "aggregation", "time_window"):
            if dict_field in intent_dict and not isinstance(
                intent_dict[dict_field], dict
            ):
                intent_dict[dict_field] = None

        for map_field in ("mapped_keywords",):
            if map_field in intent_dict and not isinstance(
                intent_dict[map_field], dict
            ):
                intent_dict[map_field] = None

        # Sensible fallbacks for required fields if omitted by LLM
        if not intent_dict.get("scenario_name"):
            intent_dict["scenario_name"] = "AML Detection Scenario"
        if not intent_dict.get("scenario_type"):
            intent_dict["scenario_type"] = "CUSTOMER"
        if not intent_dict.get("detection_logic"):
            intent_dict["detection_logic"] = intent_dict.get(
                "scenario_name", user_prompt
            )

        # Normalize thresholds keys if LLM used key variations (type -> field, comparison -> operator, value -> value_from)
        thresholds = intent_dict.get("thresholds")
        if isinstance(thresholds, list):
            for t in thresholds:
                if isinstance(t, dict):
                    if "type" in t and "field" not in t:
                        t["field"] = str(t.pop("type"))
                    if "field" not in t or not t.get("field"):
                        t["field"] = "transaction_amount"
                    if "comparison" in t and "operator" not in t:
                        t["operator"] = str(t.pop("comparison"))
                    if "operator" not in t or not t.get("operator"):
                        t["operator"] = ">"
                    if "value" in t and "value_from" not in t:
                        val = t.pop("value")
                        try:
                            t["value_from"] = float(val)
                        except (ValueError, TypeError):
                            t["value_from"] = str(val) if val is not None else None
        # Normalize time_window keys / case
        tw = intent_dict.get("time_window")
        if isinstance(tw, dict):
            if "unit" in tw and isinstance(tw["unit"], str):
                tw["unit"] = tw["unit"].upper()
            if "value" not in tw and "duration" in tw:
                tw["value"] = tw.pop("duration")
            if "value" not in tw:
                tw["value"] = 1

        # Normalize aggregation keys / defaults
        agg = intent_dict.get("aggregation")
        if isinstance(agg, dict):
            if not agg.get("metric"):
                agg["metric"] = "transaction_amount"
            if not agg.get("function"):
                agg["function"] = "SUM"
            if not agg.get("grain"):
                agg["grain"] = "PER CUSTOMER PER DAY"

        tx_type = intent_dict.get("transaction_type")
        tx_types = intent_dict.get("transaction_types")
        if not tx_type and isinstance(intent_dict.get("filters"), dict):
            tx_type = intent_dict["filters"].get("transaction_type")
            tx_types = intent_dict["filters"].get("transaction_types")

        # Intelligent fallback: If transaction_type and transaction_types are not directly populated, scan semantic_conditions
        if not tx_type and not tx_types and isinstance(intent_dict.get("semantic_conditions"), list):
            discovered_from_sc = []
            for sc in intent_dict["semantic_conditions"]:
                if isinstance(sc, dict):
                    pred = str(sc.get("predicate", ""))
                    raw = str(sc.get("raw_phrase", ""))
                    # Pattern 1: "transaction_type is outward transfer" or "transaction type is cash deposit"
                    m = re.search(r"transaction_?type\s+is\s+([^,;.]+)", pred, re.IGNORECASE)
                    if m:
                        val = m.group(1).strip()
                        if val and val.lower() not in [d.lower() for d in discovered_from_sc]:
                            discovered_from_sc.append(val)
                    # Pattern 2: behavioral subject = transaction
                    elif sc.get("logical_type") == "BEHAVIORAL" and str(sc.get("subject", "")).lower() == "transaction":
                        if raw and raw.lower() not in [d.lower() for d in discovered_from_sc] and raw.lower() not in ("or", "and"):
                            discovered_from_sc.append(raw)
            if discovered_from_sc:
                tx_types = discovered_from_sc
                tx_type = ", ".join(discovered_from_sc)
                intent_dict["transaction_types"] = tx_types
                intent_dict["transaction_type"] = tx_type
                logger.info("[TOOL: INTENT] Recovered transaction types from semantic_conditions: %s", tx_types)

        if tx_types and isinstance(tx_types, list):
            intent_dict["transaction_types"] = [str(x).strip() for x in tx_types if str(x).strip()]
            if not tx_type:
                intent_dict["transaction_type"] = ", ".join(intent_dict["transaction_types"])
        elif tx_type:
            intent_dict["transaction_type"] = tx_type

        # A "type" that names no channel ("Flag transaction", "suspicious activity") is not one: it is
        # neither searched as codes nor shown, and the form asks for the transaction type.
        cleaned = drop_placeholder_types(intent_dict)
        if cleaned is not intent_dict:
            logger.info("[TOOL: INTENT] Dropped transaction types that name no channel: %s -> %s",
                        intent_dict.get("transaction_types") or intent_dict.get("transaction_type"),
                        cleaned.get("transaction_types"))
            intent_dict = cleaned
            tx_types = intent_dict.get("transaction_types")
            tx_type = intent_dict.get("transaction_type")

        # Attribute filters first ("dormant accounts", "corporate customers", "credit transactions"),
        # resolved against the lookup tables. They take priority over explanation codes: a "type" that is
        # only a filter's value (CREDIT, DEBIT) is handled by the filter and never searched as codes.
        attribute_filters = resolve_attribute_filters(user_prompt, *_period_texts(intent_dict))
        intent_dict["attribute_filters"] = attribute_filters
        intent_dict, dropped_types = drop_covered_transaction_types(intent_dict, attribute_filters)
        if dropped_types:
            tx_types = intent_dict.get("transaction_types")
            tx_type = intent_dict.get("transaction_type")

        # "X% of Y" is ONE ratio condition, not an amount plus an empty comparison.
        intent_dict = apply_ratio_conditions(intent_dict, parse_ratio_conditions(original_request))
        # Data capability: a rule on the account balance needs balance history on this database.
        refusal = balance_capability_refusal(intent_dict, original_request)
        if refusal:
            logger.info("[TOOL: INTENT] Refused: needs balance_history, not available on %s", refusal["database"])
            return json.dumps(refusal, ensure_ascii=False, indent=2)

        # Clarification flow: never guess a mandatory parameter the user did not state.
        if answers:
            intent_dict = apply_answers(intent_dict, answers, questions=(pending or {}).get("questions"))
            tx_types = intent_dict.get("transaction_types")
            tx_type = intent_dict.get("transaction_type")
        if settings.AML_CLARIFICATION_FLOW:
            # Business decision: what is monitored is per customer unless the request says otherwise.
            intent_dict = default_entity(intent_dict, user_prompt)
            intent_dict = all_types_if_any(intent_dict, user_prompt)
            gaps = find_gaps(intent_dict, user_prompt, known=known_intent, answered=answered_fields(answers))
            if gaps:
                logger.info("[TOOL: INTENT] Clarification needed for: %s", gaps)
                return json.dumps(
                    clarification_payload(intent_dict, gaps, original_request),
                    ensure_ascii=False, indent=2, default=str,
                )

        explicit_codes = intent_dict.get("explanation_codes")

        discovered_codes = []
        expl_checkpoint = None

        target_types = tx_types if tx_types else tx_type
        specific_types = _specific_transaction_types(target_types)

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
            discovered_codes = select_relevant_explanation_codes(search_types)
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
                    tx_key = c.get("channel") or c.get("transaction_type") or (search_types[0] if isinstance(search_types, list) else str(search_types))
                    codes_by_type.setdefault(tx_key, []).append(str(c.get("code")).strip())
                intent_dict["explanation_codes_by_type"] = codes_by_type

                # Bind channel-specific explanation codes directly to individual thresholds and semantic conditions
                _bind_channels_and_codes_to_intent(intent_dict, discovered_codes, codes_by_type)

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

        # An explicit period ("between 2016 and 2022", "in 2019") sets the evaluation date to its
        # last day and the window to its exact length, instead of a window back from today.
        intent_dict, fixed_period = apply_fixed_period(intent_dict, user_prompt, *_period_texts(intent_dict))
        if fixed_period:
            logger.info("[TOOL: INTENT] Fixed period %s to %s", fixed_period[0], fixed_period[1])
            # A shorter window inside the period slides over it; the plan says so.
            if period_of(intent_dict) != fixed_period:
                intent_dict["evaluation_period"] = {"start": fixed_period[0].isoformat(), "end": fixed_period[1].isoformat()}

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

        plan_md = build_plan_markdown(intent.model_dump(), expl_checkpoint)
        logger.info(
            "[TOOL: INTENT] Plan artifact generated alongside intent (%d chars).",
            len(plan_md),
        )

        return json.dumps(
            {
                "status": "COMPLETE",
                "enriched_intent": intent.model_dump(),
                "explanation_code_checkpoint": expl_checkpoint,
                "discovered_explanation_codes": discovered_codes,
                "scenario_metadata": scenario_metadata,
                "plan_artifact": plan_md,
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
    logger.info("[TOOL: PLANNER] Generating implementation plan artifact...")

    try:
        intent = (
            json.loads(intent_json) if isinstance(intent_json, str) else intent_json
        )
        if isinstance(intent, dict):
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

        plan_md = build_plan_markdown(
            intent if isinstance(intent, dict) else {}, explanation_code_checkpoint
        )
        logger.info("[TOOL: PLANNER] Plan artifact generated (%d chars).", len(plan_md))

        # Build plan_conditions dynamically from what is present in the intent
        plan_conditions = []
        if intent.get("time_window"):
            plan_conditions.append({"condition": "Time Window Isolation"})
        if intent.get("explanation_codes") or explanation_code_checkpoint:
            plan_conditions.append({"condition": "Explanation Code Match"})
        thresholds = intent.get("thresholds", [])
        if any(t.get("target_scope") == "DETAIL" for t in thresholds):
            plan_conditions.append({"condition": "Detail Threshold Filter (WHERE)"})
        if any(t.get("target_scope") == "AGGREGATE" for t in thresholds):
            plan_conditions.append({"condition": "Aggregate Threshold Breach (HAVING)"})
        if intent.get("baseline_window"):
            plan_conditions.append({"condition": "Historical Baseline Window"})
        if intent.get("semantic_conditions"):
            plan_conditions.append({"condition": "Semantic / Behavioral Conditions"})
        if intent.get("exclusions"):
            plan_conditions.append({"condition": "Exclusion Rules"})

        return json.dumps(
            {
                "plan_artifact": plan_md,
                "plan_conditions": plan_conditions,
            },
            ensure_ascii=False,
            indent=2,
        )
    except Exception as exc:
        logger.error("[TOOL: PLANNER] Plan generation failed: %s", exc)
        return json.dumps({"error": str(exc)})


_latest_transaction_date: Optional[date] = None


# Per-customer count columns a rule aggregated per customer may return.
_RESULT_COUNT_COLUMNS: Final = ("EVENT_COUNT", "TXN_COUNT", "TRANSACTION_COUNT", "TRA_COUNT")
# Columns that only a per-transaction result has.
_TRANSACTION_COLUMNS: Final = ("TRA_SEQ1", "TRA_SEQ2", "TRA_DATE")


def _shadow_metrics(sql: str, binds: Dict[str, Any]) -> Dict[str, Any]:
    """Measure what a scenario's SQL matches: transaction rows and distinct customers.

    The result is either one row per transaction (it has TRA_DATE / TRA_SEQ1) or one row per
    customer with a count column such as EVENT_COUNT. The alert engine raises one header alert per
    distinct customer, so ``header_alert_count`` is the distinct customer count.

    Args:
        sql (str): The anchored scenario SQL.
        binds (Dict[str, Any]): Its business-date bind values.

    Returns:
        Dict[str, Any]: 'matched_transaction_count' (None when a per-customer result carries no
            count column), 'alerted_customer_count' (None without CUS_NUM), 'result_row_count',
            'header_alert_count', 'transaction_detail_count', 'alert_density_ratio' (transactions
            per customer) and 'metrics_summary' (e.g. '120 transactions across 65 distinct customers').

    Raises:
        Exception: Any database error from the read-only queries.
    """
    columns, _ = run_shadow_readonly(f"SELECT * FROM ({sql}) shadow_query WHERE 1 = 0", binds)
    names = {str(c).upper() for c in columns}
    count_column = next((c for c in _RESULT_COUNT_COLUMNS if c in names), None)
    per_transaction = any(c in names for c in _TRANSACTION_COLUMNS)

    selects = ["COUNT(*)"]
    if "CUS_NUM" in names:
        selects.append("COUNT(DISTINCT shadow_query.CUS_NUM)")
    if not per_transaction and count_column:
        selects.append(f"SUM(shadow_query.{count_column})")
    _, rows = run_shadow_readonly(f"SELECT {', '.join(selects)} FROM ({sql}) shadow_query", binds)
    values = list(rows[0]) if rows else [0] * len(selects)

    row_count = int(values[0] or 0)
    customers = int(values[1] or 0) if "CUS_NUM" in names else None
    transactions: Optional[int] = None
    if per_transaction:
        transactions = row_count
    elif count_column:
        transactions = int(values[-1] or 0)
    elif "CUS_NUM" in names:
        # One row per customer and no count column: count the rule's own rows for those customers.
        count_sql = _transactions_behind_customers_sql(sql)
        if count_sql:
            _, count_rows = run_shadow_readonly(count_sql, binds)
            transactions = int(count_rows[0][0] or 0) if count_rows else 0

    if transactions is not None and customers is not None:
        summary = f"{transactions:,} transactions across {customers:,} distinct customers"
    elif customers is not None:
        summary = f"{customers:,} distinct customers (the rule returns one row per customer, without a transaction count)"
    else:
        summary = f"{row_count:,} matching rows"
    ratio = round(transactions / customers, 2) if transactions is not None and customers else None

    return {
        "matched_transaction_count": transactions,
        "alerted_customer_count": customers,
        "result_row_count": row_count,
        "header_alert_count": customers if customers is not None else row_count,
        "transaction_detail_count": transactions,
        "alert_density_ratio": ratio,
        "metrics_summary": summary,
    }


def _transactions_behind_customers_sql(sql: str) -> Optional[str]:
    """Build a count of the rule's own rows for the customers a per-customer rule returns.

    ``SELECT t.CUS_NUM FROM T t WHERE <filters> GROUP BY ... [HAVING ...]`` becomes
    ``SELECT COUNT(*) FROM T t WHERE <filters> AND t.CUS_NUM IN (SELECT CUS_NUM FROM (<rule>))``:
    the transactions that put each alerted customer in the result.

    Args:
        sql (str): The anchored scenario SQL, aggregated per customer.

    Returns:
        Optional[str]: The count SQL, or None when the rule has no top-level GROUP BY on a
            CUS_NUM column, or is not a single-table SELECT.
    """
    clauses = top_level_clauses(sql)
    if not clauses or "GROUP BY" not in clauses:
        return None
    group_start, group_end = clauses["GROUP BY"]
    customer = next(
        (e.strip() for e in sql[group_start + len("GROUP BY"):group_end].split(",") if re.search(r"\bCUS_NUM\s*$", e.strip(), re.I)),
        None,
    )
    if not customer:
        return None
    from_start = clauses["FROM"][0]
    body = sql[from_start:clauses["WHERE"][1]] if "WHERE" in clauses else sql[from_start:clauses["FROM"][1]]
    link = "AND" if "WHERE" in clauses else "WHERE"
    return (
        f"SELECT COUNT(*) {body.rstrip()} {link} {customer} IN "
        f"(SELECT shadow_query.CUS_NUM FROM ({sql}) shadow_query)"
    )


def _period_texts(intent: Dict[str, Any]) -> List[str]:
    """Free text of an intent that can state an explicit period.

    Args:
        intent (Dict[str, Any]): The scenario intent.

    Returns:
        List[str]: The detection logic, then the scenario name, then each condition phrase.
    """
    texts = [str(intent.get("detection_logic") or ""), str(intent.get("scenario_name") or "")]
    for condition in intent.get("semantic_conditions") or []:
        if isinstance(condition, dict):
            texts.append(str(condition.get("raw_phrase") or ""))
    return texts


def _shadow_anchor(intent: Dict[str, Any]) -> Tuple[date, str]:
    """Choose the business date a shadow test is evaluated for.

    Order: the scenario's ``anchor_date``; the ``SHADOW_ANCHOR_DATE`` setting; the latest
    transaction date in PIO_TRANSACTIONS for the configured country and institution (looked
    up once per process, read-only); today, only if no transaction date can be found.

    Args:
        intent (Dict[str, Any]): The scenario intent sent to PioTech.

    Returns:
        Tuple[date, str]: The business date and a short description of where it came from.
    """
    global _latest_transaction_date
    for value, source in ((intent.get("anchor_date"), "scenario anchor_date"),
                          (settings.SHADOW_ANCHOR_DATE, "SHADOW_ANCHOR_DATE setting")):
        try:
            parsed = to_date(value)
        except ValueError:
            logger.warning("[TOOL: SQL_BRIDGE] Ignoring invalid %s: %r", source, value)
            parsed = None
        if parsed:
            return parsed, source
    if _latest_transaction_date is None:
        try:
            _, rows = run_shadow_readonly(
                "SELECT MAX(TRA_DATE) FROM BI_DWH.PIO_TRANSACTIONS WHERE COUNTRY_CODE = :cc AND INST_CODE = :ic",
                {"cc": settings.AML_COUNTRY_CODE, "ic": settings.AML_INST_CODE},
            )
            _latest_transaction_date = to_date(rows[0][0]) if rows and rows[0][0] else None
        except Exception as exc:  # any lookup failure falls back to today rather than failing the test
            logger.warning("[TOOL: SQL_BRIDGE] Could not read the latest transaction date: %s", exc)
    if _latest_transaction_date:
        return _latest_transaction_date, "latest transaction date in the data"
    return date.today(), "today (no transaction date found)"


def _request_piotech_sql(intent_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Send one AML_SCENARIO_GENERATION request to PioTech AI and collect its answer.

    Args:
        intent_dict (Dict[str, Any]): The AMLIntent to generate SQL for.

    Returns:
        Dict[str, Any]: 'sql' (str, "" if none), 'report' (normalized scenario_report or
            None) and 'rejection' (structured rejection when no SQL came back, else None).
    """
    # -----------------------------------------------------------
    # Step 2: Call PioTech AI text-to-SQL SSE service
    # -----------------------------------------------------------
    payload_content = json.dumps(
        {"request_type": "AML_SCENARIO_GENERATION", "intent": intent_dict},
        indent=2,
        ensure_ascii=False,
    )
    chat_id = f"aml_tool_{uuid.uuid4().hex[:8]}"
    payload = {
        "messages": [{"role": "user", "content": payload_content}],
        "metadata": {
            "user_id": settings.PIOTECH_AI_USER_ID,
            "project_id": settings.PIOTECH_AI_PROJECT_ID,
            "chat_id": chat_id,
        },
        "reasoning_mode": "instant",
    }

    collected_text: List[str] = []
    final_answer_text: Optional[str] = None
    scenario_report: Optional[Dict[str, Any]] = None
    upstream_error: Optional[str] = None

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
                if response.status_code >= 400:
                    # The service answered but refused the request (e.g. 422 when the
                    # intent breaks its contract). Show its own explanation; this is
                    # not an availability problem.
                    body = response.read().decode("utf-8", errors="replace")
                    logger.error(
                        "[TOOL: SQL_BRIDGE] PioTech AI rejected the request: HTTP %d %s",
                        response.status_code,
                        body[:500],
                    )
                    return {
                        "sql": "",
                        "report": None,
                        "rejection": build_http_rejection(response.status_code, body),
                    }
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
                        elif event_type == "scenario_report":
                            scenario_report = normalize_scenario_report(event)
                        elif event_type == "error":
                            upstream_error = str(
                                event.get("message") or event.get("text") or ""
                            ).strip()
                    except json.JSONDecodeError:
                        continue
    except httpx.HTTPError as exc:
        logger.error("[TOOL: SQL_BRIDGE] HTTP error calling PioTech AI: %s", exc)
        return {
            "sql": "",
            "report": None,
            "rejection": build_unavailable_rejection(settings.PIOTECH_AI_URL, str(exc)),
        }

    # -----------------------------------------------------------
    # Step 3: Extract SQL from the SSE stream response
    # -----------------------------------------------------------
    sql = ""
    if final_answer_text and final_answer_text.strip():
        sql = extract_sql(final_answer_text)

    if not sql:
        full_stream_text = "".join(collected_text).strip()
        sql = extract_sql(full_stream_text)

    if not sql:
        rejection = build_rejection(upstream_error, scenario_report)
        logger.error(
            "[TOOL: SQL_BRIDGE] PioTech AI returned no SQL: %s", summarize_rejection(rejection)
        )
        return {"sql": "", "report": scenario_report, "rejection": rejection}
    return {"sql": sql, "report": scenario_report, "rejection": None}


def _rejection_result(
    rejection: Dict[str, Any],
    scenario_report: Optional[Dict[str, Any]],
    corrections: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Serialize a PioTech rejection as the shadow-test tool's result.

    Args:
        rejection (Dict[str, Any]): Output of build_rejection / build_unavailable_rejection.
        scenario_report (Optional[Dict[str, Any]]): The normalized PioTech report, kept for
            traceability, or None if the service sent none.
        corrections (Optional[List[Dict[str, Any]]]): Automatic corrections already tried
            before this final refusal, if any.

    Returns:
        str: JSON with 'error' (one-line summary), 'rejection' (structured) and
            'scenario_report' (raw, may be null).
    """
    reason = next((plain_text(r.get("reason", "")) for r in rejection.get("reasons") or [] if r.get("reason")), "")
    user_message = "SQL could not be generated for this rule." + (f" {reason.split('.')[0].strip()}." if reason else "")
    return json.dumps(
        {
            "error": plain_text(f"PioTech AI did not generate SQL: {summarize_rejection(rejection)}"),
            "user_message": user_message,
            "rejection": rejection,
            "scenario_report": scenario_report,
            "auto_corrections_tried": corrections or [],
        },
        ensure_ascii=False,
        indent=2,
    )


@tool
def execute_oracle_dwh_shadow_test(intent_json: str) -> str:
    """Generate production-grade Oracle DWH SQL via PioTech AI text-to-SQL service and execute
    a live shadow test against Oracle BI_DWH to compute real alert volume metrics.

    ONLY call this tool AFTER the user has explicitly confirmed, accepted, or approved the
    implementation plan generated by generate_scenario_execution_plan.

    The result may carry 'corrected_intent': the scenario exactly as the SQL was generated. Use
    it, not the original, for every later step including persistence, and do not comment on how
    it differs. Only if 'auto_correction_warnings' is present, show those items verbatim.

    If PioTech AI still does not generate SQL, the result carries a 'rejection' object with the
    service's own explanation: 'blocked_by' (the condition(s) that stopped it), 'reasons',
    'suggestions', 'implementable_without' and 'warnings'. You MUST present those to the user
    as given (see the system prompt) and MUST NOT invent another cause or retry this tool with
    the same intent.

    Args:
        intent_json (str): Serialized AMLIntent payload dictionary JSON string.

    Returns:
        str: JSON containing 'raw_sql', the measured counts ('matched_transaction_count',
             'alerted_customer_count', 'header_alert_count' = distinct customers,
             'transaction_detail_count', 'alert_density_ratio' = transactions per customer,
             'metrics_summary'), and 'validation_success'. When no SQL was generated:
             JSON with 'error' (one-line summary) and 'rejection' (structured explanation).
    """
    logger.info("[TOOL: SQL_BRIDGE] Requesting SQL generation from PioTech AI...")

    # -----------------------------------------------------------
    # Step 1: Validate intent JSON
    # -----------------------------------------------------------
    try:
        intent_dict = (
            json.loads(intent_json) if isinstance(intent_json, str) else intent_json
        )
        if isinstance(intent_dict, dict):
            # Unwrap if wrapped under enriched_intent, AMLIntent, or intent
            if "enriched_intent" in intent_dict and isinstance(
                intent_dict["enriched_intent"], dict
            ):
                intent_dict = intent_dict["enriched_intent"]
            elif "AMLIntent" in intent_dict and isinstance(
                intent_dict["AMLIntent"], dict
            ):
                intent_dict = intent_dict["AMLIntent"]
            elif "intent" in intent_dict and isinstance(intent_dict["intent"], dict):
                intent_dict = intent_dict["intent"]
    except (json.JSONDecodeError, TypeError) as exc:
        logger.error("[TOOL: SQL_BRIDGE] Invalid intent JSON: %s", exc)
        return json.dumps({"error": f"Invalid intent JSON: {exc}"})

    # -----------------------------------------------------------
    # Step 2-3: Generate SQL via PioTech AI, auto-correcting refusals that a change to the
    # intent can clear: NOT_IMPLEMENTABLE (unsupported condition), UNRESOLVED (ambiguous or
    # contradictory phrase, open question) and REQUEST_REJECTED (invalid value such as a 0 window)
    # -----------------------------------------------------------
    correctors = {
        "NOT_IMPLEMENTABLE": auto_correct_intent,
        "UNRESOLVED": resolve_intent,
        "REQUEST_REJECTED": resolve_intent,
    }
    # Never send an incomplete rule for SQL generation, and never show its raw expression to the user.
    problems = validate_conditions(intent_dict)
    if problems:
        message = invalid_rule_message(problems, str(intent_dict.get("detection_logic") or ""))
        logger.warning("[TOOL: SQL_BRIDGE] Rule not sent for SQL generation: %s", problems)
        return json.dumps({"error": message, "user_message": message, "validation_errors": problems,
                           "validation_success": False}, ensure_ascii=False, indent=2)
    sent_intent, fixed_period = apply_fixed_period(intent_dict, *_period_texts(intent_dict))
    # Attribute filters are applied by the AML Builder (below), never by PioTech.
    attribute_filters = validate_filters(intent_dict.get("attribute_filters"))
    corrections: List[Dict[str, Any]] = []
    attempts = 0
    while True:
        # Period wording is removed from the text PioTech reads, so its years are never taken
        # for amounts; the period itself travels in anchor_date and time_window. Attribute
        # phrases are removed too, so PioTech neither refuses nor re-applies them.
        to_piotech = clean_for_sql_service(sent_intent) if fixed_period else sent_intent
        outcome = _request_piotech_sql(for_sql_service(strip_attribute_phrases(to_piotech, attribute_filters)))
        if outcome["sql"]:
            break
        rejection = outcome["rejection"]
        corrector = correctors.get(rejection["kind"])
        if (
            not settings.PIOTECH_AUTO_CORRECT
            or corrector is None
            or attempts >= settings.PIOTECH_AUTO_CORRECT_MAX_ATTEMPTS
        ):
            return _rejection_result(rejection, outcome["report"], corrections)
        corrected, applied = corrector(sent_intent, rejection)
        if not applied:
            logger.warning(
                "[TOOL: SQL_BRIDGE] Auto-correction found nothing to change for: %s",
                summarize_rejection(rejection),
            )
            return _rejection_result(rejection, outcome["report"], corrections)
        attempts += 1
        corrections += applied
        sent_intent = corrected
        logger.warning(
            "[TOOL: SQL_BRIDGE] Auto-correction attempt %d: %s",
            attempts,
            "; ".join(f"{c['action']} \"{c['original']}\"" for c in applied),
        )

    sql = outcome["sql"]
    correction_fields: Dict[str, Any] = {}
    if corrections:
        if settings.PIOTECH_SHOW_AUTO_CORRECTIONS:
            correction_fields = {
                "corrected_intent": sent_intent,
                "auto_corrections": corrections,
                "auto_correction_warnings": [c["warning"] for c in corrections],
            }
        else:
            # Hidden by configuration: the model receives only the scenario as generated, with
            # nothing describing what changed. The audit trail is the server log.
            correction_fields = {
                "corrected_intent": {k: v for k, v in sent_intent.items() if k != "auto_corrections"}
            }
            logger.warning(
                "[TOOL: SQL_BRIDGE] AUDIT auto-corrections (hidden from user): %s",
                json.dumps(
                    [{k: c[k] for k in ("original", "action", "replacement", "reason")} for c in corrections],
                    ensure_ascii=False,
                ),
            )

    logger.info(
        "[TOOL: SQL_BRIDGE] SQL extracted (%d chars). Executing shadow test...",
        len(sql),
    )

    # -----------------------------------------------------------
    # Step 3b: Anchor the SQL to a business date (no SYSDATE, Sun-Thu business-day windows)
    # -----------------------------------------------------------
    try:
        sql = anchor_sql(sql, sent_intent)
    except ValueError as exc:
        logger.error("[TOOL: SQL_BRIDGE] Could not anchor SQL to a business date: %s", exc)
        return json.dumps(
            {"raw_sql": sql, "validation_success": False, "error": f"Date anchoring failed: {exc}", **correction_fields},
            ensure_ascii=False,
            indent=2,
        )
    # Pin only when the window is the period itself; a shorter stated window ("within 7 days")
    # stays a window ending on the period's last day.
    pinned = False
    window_slides = False
    if fixed_period and period_of(sent_intent) == fixed_period:
        sql, pinned = pin_period(sql, fixed_period[0], fixed_period[1])
        if not pinned:
            logger.warning(
                "[TOOL: SQL_BRIDGE] Fixed period %s to %s not pinned: the SQL has no single window start",
                fixed_period[0],
                fixed_period[1],
            )
    elif fixed_period:
        # A short window inside the period ("3 deposits in one week, in 2019"): any window of the period counts.
        sql, window_slides = slide_window_over_period(
            sql, fixed_period[0], fixed_period[1], sent_intent.get("time_window") or {}
        )
        pinned = window_slides
        if not window_slides:
            logger.warning(
                "[TOOL: SQL_BRIDGE] Window not slid over %s to %s (rule form not supported): last window only",
                fixed_period[0],
                fixed_period[1],
            )
    sql, filters_applied = apply_attribute_filters(sql, attribute_filters)
    if filters_applied:
        logger.info("[TOOL: SQL_BRIDGE] Attribute filters applied: %s", filters_applied)
    anchor, anchor_source = _shadow_anchor(sent_intent)
    binds = bind_values(sql, anchor)
    date_fields: Dict[str, Any] = {
        "evaluation_date": anchor.isoformat(),
        "evaluation_date_source": anchor_source,
        "window_dates": {name: value.isoformat() for name, value in binds.items()},
    }
    if fixed_period:
        date_fields["fixed_period"] = {
            "start": fixed_period[0].isoformat(),
            "end": fixed_period[1].isoformat(),
            "pinned_in_sql": pinned,
            "window_slides_over_period": window_slides,
        }
    if filters_applied:
        # Returned with the dates so both the success and the failure result carry them.
        date_fields["attribute_filters_applied"] = filters_applied
    logger.info("[TOOL: SQL_BRIDGE] Business date %s (%s); window binds %s", anchor, anchor_source, date_fields["window_dates"])

    # -----------------------------------------------------------
    # Step 4: Execute shadow test — measured transaction and customer counts
    # -----------------------------------------------------------
    try:
        metrics = _shadow_metrics(sql, binds)
        logger.info("[TOOL: SQL_BRIDGE] Shadow test complete: %s", metrics["metrics_summary"])
        # Registered scenarios that look like this rule, with how similar and what differs (read-only).
        similar = find_similar_scenarios(sql)
        if similar:
            logger.info("[TOOL: SQL_BRIDGE] Similar registered scenarios: %s",
                        [(s["scenario_id"], s["similarity_pct"]) for s in similar])
        return json.dumps(
            {
                "raw_sql": sql,
                **metrics,
                "similar_registered_scenarios": similar,
                "validation_success": True,
                **date_fields,
                **correction_fields,
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
                "matched_transaction_count": None,
                "alerted_customer_count": None,
                "alert_density_ratio": None,
                "validation_success": False,
                "error": str(exc),
                **date_fields,
                **correction_fields,
            },
            ensure_ascii=False,
            indent=2,
        )


def _latest_shadow_corrections(state: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Return the automatic corrections of the most recent successful shadow test, if any.

    The latest shadow test that produced SQL decides: if it needed no correction (for example
    after the user rephrased the scenario), earlier corrections no longer apply.

    Args:
        state (Optional[Dict[str, Any]]): The agent graph state injected by LangGraph.

    Returns:
        Optional[Dict[str, Any]]: 'warnings' (List[str]), 'corrected_intent' (Dict) and
            'raw_sql' (str) of that shadow test, or None if it had no corrections.
    """
    for message in reversed((state or {}).get("messages") or []):
        if getattr(message, "type", "") != "tool" or getattr(message, "name", "") != "execute_oracle_dwh_shadow_test":
            continue
        try:
            payload = json.loads(getattr(message, "content", "") or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(payload, dict) or not payload.get("raw_sql"):
            continue
        if not isinstance(payload.get("corrected_intent"), dict):
            return None
        return {
            "warnings": list(payload.get("auto_correction_warnings") or []),
            "corrected_intent": payload.get("corrected_intent"),
            "raw_sql": payload.get("raw_sql"),
        }
    return None


@tool
def prepare_scenario_metadata_for_persistence(
    metadata_json: str,
    state: Annotated[Optional[Dict[str, Any]], InjectedState] = None,
) -> str:
    """Build the live PIO_AML_SCENARIO governance field catalog for the side panel / chat fallback.

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
        state (Optional[Dict[str, Any]]): Agent graph state, injected by LangGraph (not an LLM
            argument); used to find automatic corrections made by the latest shadow test.

    Returns:
        str: JSON containing 'metadata' (current values), 'fields' (full catalog with live
             lookup options), 'missing_mandatory_fields', and 'ready_for_persistence'. When the
             scenario was auto-corrected, also 'auto_correction_warnings' and 'notice_to_user':
             you MUST show these warnings verbatim at the top of your reply.
    """
    logger.info("[TOOL: METADATA] Building PIO_AML_SCENARIO field catalog...")

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
        corrections = _latest_shadow_corrections(state)
        if corrections and corrections["warnings"]:   # empty when hidden by PIOTECH_SHOW_AUTO_CORRECTIONS
            catalog["auto_correction_warnings"] = corrections["warnings"]
            catalog["notice_to_user"] = (
                "Before reviewing governance, the user must see that this scenario was automatically "
                "corrected. Start your reply with '⚠ Automatic corrections' and list every item of "
                "auto_correction_warnings verbatim."
            )
        return json.dumps(catalog, ensure_ascii=False, indent=2, default=str)
    except Exception as exc:
        logger.error("[TOOL: METADATA] Catalog build failed: %s", exc)
        return json.dumps({"error": str(exc)})


@tool
def persist_and_validate_scenario_in_dwh(
    intent_json: str,
    raw_sql: str,
    metadata_json: str,
    state: Annotated[Optional[Dict[str, Any]], InjectedState] = None,
) -> str:
    """Persist the confirmed, shadow-tested AML scenario atomically into both
    PIO_AML_PRODUCTION_SCENARIOS (for the automated daily ETL runner) and
    PIO_AML_SCENARIO (business metadata for downstream compliance UI/workflow modules).

    CRITICAL: ONLY call this tool when the user in a SUBSEQUENT turn explicitly instructs
    you to persist/save the scenario after reviewing the governance metadata in the side panel.
    NEVER call this tool autonomously immediately after prepare_scenario_metadata_for_persistence!

    Performs an idempotent INSERT-or-UPDATE on both tables in a single Oracle transaction:
    if the scenario_id already exists it updates the record and reactivates it (IS_ACTIVE=1),
    incrementing PIO_AML_SCENARIO.VERSION_NUM. Only call after shadow test results have been
    reviewed AND prepare_scenario_metadata_for_persistence reports ready_for_persistence=True
    (or the user explicitly confirms proceeding despite missing fields).

    Every mandatory PIO_AML_SCENARIO field is re-validated here against its live lookup table
    or allowed-value set before anything is written — if validation fails, NOTHING is written
    to either table and the problems are returned for you to relay to the user.

    If the shadow test returned 'corrected_intent' (automatic corrections were applied), pass
    THAT as intent_json, never the original intent, so the saved description matches the SQL.

    Args:
        intent_json (str): Serialized AMLIntent payload dictionary JSON string — the shadow
            test's 'corrected_intent' when it returned one.
        raw_sql (str): Production-grade Oracle SQL statement from the shadow test.
        metadata_json (str): Serialized PIO_AML_SCENARIO metadata JSON — the merged output of
            prepare_scenario_metadata_for_persistence plus any officer picks.
        state (Optional[Dict[str, Any]]): Agent graph state, injected by LangGraph (not an LLM
            argument); if the latest shadow test was auto-corrected and the intent passed here
            is not the corrected one, the corrected intent is used instead.

    Returns:
        str: JSON containing 'scenario_id', 'scenario_name', 'write_success',
             'write_verification', and 'created_at' on success, or 'validation_errors'
             (list of str) and 'write_success': false if the metadata failed validation.
    """
    logger.info(
        "[TOOL: PERSISTER] Writing confirmed scenario to PIO_AML_PRODUCTION_SCENARIOS + PIO_AML_SCENARIO..."
    )

    try:
        intent_dict = json.loads(intent_json)
    except json.JSONDecodeError as exc:
        logger.error("[TOOL: PERSISTER] Invalid intent JSON: %s", exc)
        return json.dumps({"error": f"Invalid intent JSON: {exc}"})

    # A rule whose SQL was generated from an auto-corrected intent must be saved with that
    # corrected intent, so its description matches what the SQL checks.
    corrections = _latest_shadow_corrections(state)
    if (
        corrections
        and isinstance(corrections.get("corrected_intent"), dict)
        and isinstance(intent_dict, dict)
        and not intent_dict.get("auto_corrections")
    ):
        logger.warning(
            "[TOOL: PERSISTER] Using the auto-corrected intent from the latest shadow test instead of the original."
        )
        intent_dict = corrections["corrected_intent"]

    try:
        metadata_dict = json.loads(metadata_json) if metadata_json else {}
    except json.JSONDecodeError as exc:
        logger.error("[TOOL: PERSISTER] Invalid metadata JSON: %s", exc)
        return json.dumps({"error": f"Invalid metadata JSON: {exc}"})

    is_valid, problems = validate_scenario_metadata(metadata_dict)
    if not is_valid:
        logger.warning("[TOOL: PERSISTER] Metadata validation failed: %s", problems)
        return json.dumps(
            {
                "write_success": False,
                "validation_errors": problems,
            },
            ensure_ascii=False,
            indent=2,
        )

    scenario_name: str = intent_dict.get("scenario_name", "AML Detection Scenario")
    scenario_type: str = intent_dict.get("scenario_type", "CUSTOMER")
    detection_logic: str = intent_dict.get("detection_logic", scenario_name)

    # Compute time_window_days from the intent time_window block
    tw: Dict[str, Any] = intent_dict.get("time_window") or {}
    tw_value: int = int(tw.get("value", 0) or 0)
    tw_unit: str = str(tw.get("unit", "DAYS")).upper()
    unit_to_days: Dict[str, int] = {"DAYS": 1, "WEEKS": 7, "MONTHS": 30, "YEARS": 365}
    time_window_days: Optional[int] = (
        tw_value * unit_to_days.get(tw_unit, 1) if tw_value else None
    )

    # Generate a human-readable unique scenario ID — shared as SCENARIO_CODE on PIO_AML_SCENARIO
    scenario_id = f"PRD_{uuid.uuid4().hex[:8].upper()}"
    created_at = datetime.utcnow().isoformat()

    try:
        success = save_production_scenario(
            scenario_id=scenario_id,
            scenario_name=scenario_name,
            scenario_type=scenario_type,
            detection_logic=detection_logic,
            raw_sql=raw_sql,
            scenario_metadata=metadata_dict,
            time_window_days=time_window_days,
            created_by=settings.AML_CREATED_BY,
        )
    except DuplicateScenarioError as exc:
        return json.dumps(
            {
                "status": "ALREADY_DEPLOYED",
                "already_deployed": True,
                "scenario_id": exc.existing.get("scenario_id"),
                "scenario_name": exc.existing.get("scenario_name"),
                "write_success": False,
                "user_message": exc.user_message,
                "agent_instruction": "Reply with user_message verbatim. Do not call this tool again for this scenario.",
            },
            ensure_ascii=False,
            indent=2,
        )

    if success:
        logger.info(
            "[TOOL: PERSISTER] Scenario persisted successfully. SCENARIO_ID=%s, NAME='%s'",
            scenario_id,
            scenario_name,
        )
    else:
        logger.error(
            "[TOOL: PERSISTER] Failed to persist scenario. SCENARIO_ID=%s",
            scenario_id,
        )

    return json.dumps(
        {
            "scenario_id": scenario_id,
            "scenario_name": scenario_name,
            "scenario_type": scenario_type,
            "time_window_days": time_window_days,
            "raw_sql_preview": raw_sql[:200] if raw_sql else "",
            "write_success": success,
            "write_verification": {
                "production_scenarios_table": "PIO_AML_PRODUCTION_SCENARIOS",
                "scenario_metadata_table": "PIO_AML_SCENARIO",
                "rows_written": 2 if success else 0,
                "is_active": 1,
            },
            "created_at": created_at,
        },
        ensure_ascii=False,
        indent=2,
    )


@tool
def query_production_scenario_registry(
    query_type: Literal["semantic_search", "get_statistics", "get_alert_metrics", "get_scenario_detail"],
    search_text: Optional[str] = None,
    scenario_id: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    customer_number: Optional[str] = None,
    limit: int = 5,
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
    logger.info("[TOOL: REGISTRY] query_type=%s", query_type)

    try:
        if query_type == "semantic_search":
            if not search_text:
                return json.dumps({"error": "search_text is required for query_type='semantic_search'."})
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
                return json.dumps({"error": "scenario_id is required for query_type='get_scenario_detail'."})
            detail = get_scenario_full_details(scenario_id)
            result = (
                detail
                if detail is not None
                else {"error": f"No scenario found with scenario_id='{scenario_id}'."}
            )

        else:
            return json.dumps({"error": f"Unknown query_type: '{query_type}'."})

        return json.dumps(result, ensure_ascii=False, indent=2, default=str)
    except ValueError as exc:
        # Bad date format, etc. — a client-correctable input error, not a system failure.
        logger.warning("[TOOL: REGISTRY] Invalid input: %s", exc)
        return json.dumps({"error": str(exc)})
    except Exception as exc:
        logger.error("[TOOL: REGISTRY] query_type=%s failed: %s", query_type, exc, exc_info=True)
        return json.dumps({"error": str(exc)})
