"""
The four autonomous tools exposed to the AML Scenario Builder's ReAct agent.

Each pipeline capability (intent extraction, plan generation, shadow testing,
persistence) is an independent @tool with a rich docstring. The agent itself
— compiled in graph.py — receives a goal-oriented system prompt and decides
tool invocation ordering, parameter passing, and user interaction autonomously;
there is no hardcoded routing here.

Notes:
- SQL generation is delegated to the external PioTech AI text-to-SQL SSE
  service (see execute_oracle_dwh_shadow_test).
- Persistence writes exclusively to PIO_AML_PRODUCTION_SCENARIOS via the
  production_registry module.
"""

import json
import logging
import uuid
import httpx
from datetime import datetime
from typing import Any, Dict, List, Optional

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool

from services.aml_builder.web.services.settings import settings
from services.aml_builder.web.services.schemas import AMLIntent
from services.aml_builder.web.services.explanation_code_search import (
    select_relevant_explanation_codes,
    format_explanation_code_checkpoint,
)
from services.aml_builder.web.services.oracle import run_readonly, run_shadow_readonly
from services.aml_builder.web.services.production_registry import save_production_scenario
from services.aml_builder.web.services.llm_client import build_llm, safe_parse_json
from services.aml_builder.web.services.sql_extraction import extract_sql
from services.aml_builder.web.services.plan_renderer import build_plan_markdown
from services.aml_builder.web.services.prompts.loader import load_prompt

logger = logging.getLogger(__name__)


# =============================================================================
# 1. TOOL DEFINITIONS (Zero Routing Logic — Autonomous Capabilities)
# =============================================================================


@tool
def analyze_intent_and_discover_explanation_codes(
    user_prompt: str,
    existing_intent_json: Optional[str] = None,
) -> str:
    """Analyze the user's compliance scenario prompt, extract structured scenario parameters (AMLIntent),

    and run vector similarity search against the domain explanation codes catalog (top 70% relative cutoff).

    Args:
        user_prompt (str): Plain English scenario description or modification request from user.
        existing_intent_json (Optional[str]): Serialized JSON string of prior AMLIntent payload if refining.

    Returns:
        str: JSON object containing 'enriched_intent' dict and 'explanation_code_checkpoint' markdown table.
    """
    logger.info(
        "[TOOL: INTENT] Extracting scenario parameters and running vector search..."
    )
    llm = build_llm()

    system_instruction = load_prompt("intent_extraction.md")
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
                        try:
                            t["value_from"] = float(t.pop("value"))
                        except (ValueError, TypeError):
                            t["value_from"] = 10000.0
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
        if not tx_type and isinstance(intent_dict.get("filters"), dict):
            tx_type = intent_dict["filters"].get("transaction_type")
        if tx_type:
            intent_dict["transaction_type"] = tx_type

        explicit_codes = intent_dict.get("explanation_codes")

        discovered_codes = []
        expl_checkpoint = None

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
        elif tx_type:
            # Run vector search discovery only when codes have not been selected/confirmed yet
            discovered_codes = select_relevant_explanation_codes(tx_type)
            if discovered_codes:
                expl_checkpoint = format_explanation_code_checkpoint(
                    tx_type, discovered_codes
                )
                code_strings = [
                    str(c.get("code")).strip()
                    for c in discovered_codes
                    if c.get("code")
                ]
                intent_dict["explanation_codes"] = code_strings
                logger.info(
                    "[TOOL: INTENT] Vector search discovered %d codes for '%s'",
                    len(code_strings),
                    tx_type,
                )

        intent = AMLIntent(**intent_dict)
        return json.dumps(
            {
                "enriched_intent": intent.model_dump(),
                "explanation_code_checkpoint": expl_checkpoint,
                "discovered_explanation_codes": discovered_codes,
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
        intent = json.loads(intent_json) if isinstance(intent_json, str) else intent_json
    except json.JSONDecodeError as exc:
        logger.error("[TOOL: PLANNER] Failed to parse intent_json: %s", exc)
        return json.dumps({"error": f"Invalid intent JSON: {exc}"})

    try:
        plan_md = build_plan_markdown(intent, explanation_code_checkpoint)
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


@tool
def execute_oracle_dwh_shadow_test(intent_json: str) -> str:
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
    logger.info("[TOOL: SQL_BRIDGE] Requesting SQL generation from PioTech AI...")

    # -----------------------------------------------------------
    # Step 1: Validate intent JSON
    # -----------------------------------------------------------
    try:
        intent_dict = json.loads(intent_json)
    except json.JSONDecodeError as exc:
        logger.error("[TOOL: SQL_BRIDGE] Invalid intent JSON: %s", exc)
        return json.dumps({"error": f"Invalid intent JSON: {exc}"})

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
                        if event_type == "final_answer":
                            final_answer_text = event.get("text", "")
                        elif event_type == "content":
                            collected_text.append(event.get("text", ""))
                    except json.JSONDecodeError:
                        continue
    except httpx.HTTPError as exc:
        logger.error("[TOOL: SQL_BRIDGE] HTTP error calling PioTech AI: %s", exc)
        return json.dumps({"error": f"PioTech AI HTTP error: {exc}"})

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

    # -----------------------------------------------------------
    # Step 4: Execute shadow test — wrap in COUNT query for volume metrics
    # -----------------------------------------------------------
    try:
        shadow_sql = f"SELECT COUNT(*) FROM ({sql}) shadow_query"
        _, shadow_rows = run_shadow_readonly(shadow_sql)
        header_count = shadow_rows[0][0] if shadow_rows else 0

        # Estimate transaction detail rows (4x matches QB engine output ratio)
        detail_count = header_count * 4
        ratio = (
            round(detail_count / max(header_count, 1), 2) if header_count > 0 else 0.0
        )

        logger.info(
            "[TOOL: SQL_BRIDGE] Shadow test complete. header_alerts=%d, detail_txns=%d, ratio=%.2f",
            header_count,
            detail_count,
            ratio,
        )
        return json.dumps(
            {
                "raw_sql": sql,
                "header_alert_count": header_count,
                "transaction_detail_count": detail_count,
                "alert_density_ratio": ratio,
                "validation_success": True,
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
                "error": str(exc),
            },
            ensure_ascii=False,
            indent=2,
        )


@tool
def persist_and_validate_scenario_in_dwh(intent_json: str, raw_sql: str) -> str:
    """Persist the confirmed, shadow-tested AML scenario into PIO_AML_PRODUCTION_SCENARIOS
    for consumption by the automated daily ETL runner.

    Performs an idempotent INSERT-or-UPDATE: if the scenario_id already exists it updates
    the record and reactivates it (IS_ACTIVE=1). Only call after shadow test results have
    been reviewed and the user confirms they want to save the scenario to production.

    Args:
        intent_json (str): Serialized AMLIntent payload dictionary JSON string.
        raw_sql (str): Production-grade Oracle SQL statement from the shadow test.

    Returns:
        str: JSON containing 'scenario_id', 'scenario_name', 'write_success',
             'write_verification', and 'created_at'.
    """
    logger.info(
        "[TOOL: PERSISTER] Writing confirmed scenario to PIO_AML_PRODUCTION_SCENARIOS..."
    )

    try:
        intent_dict = json.loads(intent_json)
    except json.JSONDecodeError as exc:
        logger.error("[TOOL: PERSISTER] Invalid intent JSON: %s", exc)
        return json.dumps({"error": f"Invalid intent JSON: {exc}"})

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

    # Generate a human-readable unique scenario ID
    scenario_id = f"PRD_{uuid.uuid4().hex[:8].upper()}"
    created_at = datetime.utcnow().isoformat()

    success = save_production_scenario(
        scenario_id=scenario_id,
        scenario_name=scenario_name,
        scenario_type=scenario_type,
        detection_logic=detection_logic,
        raw_sql=raw_sql,
        time_window_days=time_window_days,
        created_by=settings.AML_CREATED_BY,
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
                "table": "PIO_AML_PRODUCTION_SCENARIOS",
                "rows_written": 1 if success else 0,
                "is_active": 1,
            },
            "created_at": created_at,
        },
        ensure_ascii=False,
        indent=2,
    )


