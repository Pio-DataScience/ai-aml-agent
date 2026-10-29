"""Build and verify the exact model-bound content for the B9 synthetic cases."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[5]
SERVICE_B_ROOT = ROOT.parent / "PioTech-AI" / "services" / "dwh_scenario_agent"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SERVICE_B_ROOT))

from web.services.cloud_safe import (
    generation_only_payload_hash,
    prepare_generation_only_context,
    project_allowlisted,
    project_intent,
    project_schema_records,
    provider_identity,
    safe_schema_message,
    sanitize_model_messages,
    verify_prompt,
)
from web.services.scenario_contract import parse_service_a_intent
from web.services.utils import _load_prompt_from_md
from services.aml_builder.web.domain.dwh_scenario.contracts import ScenarioSqlRequest
from services.aml_builder.web.services.config_loader import load_catalogs
from services.aml_builder.web.services.dwh_scenario.context_assembly import assemble_context
from services.aml_builder.web.services.dwh_scenario.http_adapter import (
    build_service_b_generation_content,
)
from services.aml_builder.web.services.dwh_scenario.generator import (
    _generation_payload_hash,
    _generator_endpoint,
)
from services.aml_builder.web.services.dwh_scenario.prompt_loader import load_prompt
from services.aml_builder.web.services.settings import settings as service_a_settings
from services.aml_builder.web.services.sql_extraction import extract_sql

MANIFEST_PATH = ROOT / "Docs" / "B9_generation_only_scope.json"
FIXTURE_PATH = ROOT / "services" / "aml_builder" / "tests" / "eval" / "dwh_scenario" / "b9_cases.jsonl"
PAYLOAD_PATH = ROOT / "Docs" / "B9_model_payloads.json"
SERVICE_B_SETTINGS_PATH = ROOT.parent / "PioTech-AI" / "Docs" / "B9_service_b_development.env.example"

CASE_IDS = tuple(f"b9-{index:02d}" for index in range(1, 13))
SYNTHETIC_TENANT = "b9-development-synthetic"
SERVICE_A_IDENTITY = "openai|gpt-5.4-mini|https://api.openai.com/v1"
SERVICE_B_IDENTITY = "openai|gpt-5-nano|https://api.openai.com/v1"
INTENT_PATHS = (
    "scenario_type",
    "ready_for_handoff",
    "explanation_codes",
    "thresholds.field",
    "thresholds.operator",
    "thresholds.target_scope",
    "thresholds.value_from",
    "time_window.unit",
    "time_window.value",
    "time_window.is_rolling",
    "baseline_window.unit",
    "baseline_window.duration",
    "baseline_window.exclude_current_window",
    "baseline_window.offset_days",
    "temporal_contract.kind",
    "temporal_contract.current_lower_boundary",
    "temporal_contract.current_upper_boundary",
    "temporal_contract.baseline_lower_boundary",
    "temporal_contract.baseline_upper_boundary",
    "temporal_contract.baseline_relationship",
    "aggregation.metric",
    "aggregation.function",
    "aggregation.grain",
    "resolved_filters.dimension",
    "resolved_filters.level",
    "resolved_filters.status",
    "resolved_filters.values",
    "resolved_filters.target.table",
    "resolved_filters.target.column",
    "resolved_filters.match",
    "resolved_filters.polarity",
    "resolved_filters.polarity_allowed",
)


def _sha256(value: bytes) -> str:
    """Return a lowercase SHA-256 digest."""
    return hashlib.sha256(value).hexdigest()


def _read_cases() -> list[dict[str, Any]]:
    """Load exactly twelve B9 fixture rows with complete typed inputs."""
    rows = [
        json.loads(line)
        for line in FIXTURE_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if len(rows) != 12 or any(not row.get("intent") for row in rows):
        raise ValueError("B9 requires twelve fully typed synthetic case inputs")
    return rows


def _records(schema_scope: dict[str, Any]) -> list[dict[str, Any]]:
    """Create the exact minimal dictionary-record envelope for pinned columns."""
    return [
        {
            "metadata": {
                "tenant_id": SYNTHETIC_TENANT,
                "institution_id": SYNTHETIC_TENANT,
                "source": "oracle_dictionary",
                "object_type": "TABLE",
                "table_name": schema_scope["object"],
                "column_name": column["name"],
                "data_type": column["type"],
            }
        }
        for column in schema_scope["columns"]
    ]


def service_a_schema_context(
    schema_scope: dict[str, Any],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Build the pinned table and column context shared by approval and runtime."""
    table_context = [{"schema": "BI_DWH", "table": "PIO_TRANSACTIONS"}]
    column_context = [
        {
            "schema": "BI_DWH",
            "table": "PIO_TRANSACTIONS",
            "column": column["name"],
            "data_type": column["type"],
        }
        for column in schema_scope["columns"]
    ]
    return table_context, column_context


def build_service_a_model_messages(
    request: ScenarioSqlRequest,
    prompt: str,
    *,
    tables: list[dict[str, str]],
    columns: list[dict[str, str]],
    budget: int,
) -> list[dict[str, str]]:
    """Build Service A's exact disclosed system and user messages."""
    context = assemble_context(
        request,
        experience=[],
        tables=tables,
        columns=columns,
        budget=budget,
    )
    return [
        {"role": "system", "content": prompt},
        {"role": "user", "content": context + "\nREPAIR FEEDBACK: "},
    ]


def _scalar_allowlist(value: Any, path: str = "") -> list[str]:
    """Enumerate only the scalar values in a projected synthetic intent."""
    result: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            result.extend(_scalar_allowlist(item, child_path))
    elif isinstance(value, list):
        for item in value:
            result.extend(_scalar_allowlist(item, path))
    elif value is not None:
        result.append(f"{path}={value}")
    return result


def build_payload_artifacts(
    *, refresh_prompt_scope: bool = False
) -> tuple[dict[str, Any], dict[str, Any], str]:
    """Serialize both services' exact generation messages and dev-only settings."""
    scope = json.loads(MANIFEST_PATH.read_text(encoding="utf-8-sig"))
    cases = _read_cases()
    fixtures_digest = _sha256(FIXTURE_PATH.read_bytes())

    schema_scope = scope["schema_scope"]
    schema_records = _records(schema_scope)
    schema_hash = _sha256(
        json.dumps(schema_records, sort_keys=True, separators=(",", ":")).encode()
    )
    table_context, column_context = service_a_schema_context(schema_scope)
    a_prompt = load_prompt("generator_v1.md")
    b_prompt = _load_prompt_from_md("generation_only_system.md")
    a_prompt_hash = _sha256(a_prompt.encode("utf-8"))
    b_prompt_hash = _sha256(b_prompt.encode("utf-8"))
    if not refresh_prompt_scope and a_prompt_hash != scope["service_a"]["prompt_sha256"]:
        raise ValueError("Service A generator prompt hash differs from scope")
    if not refresh_prompt_scope and b_prompt_hash != scope["service_b"]["prompt_sha256"]:
        raise ValueError("Service B generation-only prompt hash differs from scope")
    if service_a_settings.DWH_SQL_MODE != "http":
        raise ValueError("Service A DWH_SQL_MODE must remain http")
    if service_a_settings.DWH_SQL_BASE_URL or service_a_settings.LLM_BASE_URL:
        raise ValueError("Service A has a custom endpoint outside this reviewed scope")
    resolved_provider = (
        service_a_settings.DWH_SQL_GENERATOR_PROVIDER
        or service_a_settings.LLM_PROVIDER
    )
    resolved_model = (
        service_a_settings.DWH_SQL_GENERATOR_MODEL
        or service_a_settings.LLM_MODEL
    )
    if (
        f"{resolved_provider.casefold()}|{resolved_model.strip()}|"
        f"{_generator_endpoint(resolved_provider, service_a_settings)}"
        != SERVICE_A_IDENTITY
    ):
        raise ValueError("Effective Service A provider identity differs from B9 scope")

    catalog = load_catalogs(service_a_settings.CATALOG_DIR).filters
    b_settings = SimpleNamespace(
        DWH_SCENARIO_GENERATION_ONLY_ENABLED=True,
        DWH_SCENARIO_GENERATION_ONLY_ENVIRONMENT="development",
        DWH_SCENARIO_GENERATION_ONLY_SCOPE_SHA256="0" * 64,
        DWH_SCENARIO_GENERATION_ONLY_SCHEMA_SHA256=schema_hash,
        DWH_SCENARIO_GENERATION_ONLY_PROMPT_SHA256=b_prompt_hash,
        DWH_SCENARIO_GENERATION_ONLY_PAYLOAD_HASHES_JSON="{}",
        CLOUD_SAFE_LLM_MODE=True,
        CLOUD_SAFE_PROVIDER_IDENTITIES=(SERVICE_B_IDENTITY,),
        CLOUD_SAFE_LOCAL_ENDPOINTS=(),
        CLOUD_SAFE_INTENT_FIELDS=INTENT_PATHS,
        CLOUD_SAFE_INTENT_VALUES=(),
        CLOUD_SAFE_SCHEMA_OBJECTS=(schema_scope["object"],),
        CLOUD_SAFE_SCHEMA_COLUMNS=tuple(
            item
            for column in schema_scope["columns"]
            for item in (column["name"], f"{schema_scope['object']}.{column['name']}")
        ),
        CLOUD_SAFE_SCHEMA_TYPES=tuple(sorted({item["type"] for item in schema_scope["columns"]})),
        CLOUD_SAFE_SCHEMA_RELATIONSHIPS=(),
        CLOUD_SAFE_SCHEMA_TENANT_ID=SYNTHETIC_TENANT,
        SCHEMA_INDEX_TENANT_ID=SYNTHETIC_TENANT,
        CLOUD_SAFE_SYSTEM_PROMPT_SHA256="",
        CLOUD_SAFE_CRITIC_PROMPT_SHA256=None,
    )
    projected_schema = project_schema_records(schema_records, b_settings)
    schema_context = safe_schema_message(projected_schema, b_settings)
    b_system = f"{b_prompt}\n\nPINNED APPROVED SCHEMA METADATA:\n{schema_context}"
    b_settings.CLOUD_SAFE_SYSTEM_PROMPT_SHA256 = _sha256(b_system.encode("utf-8"))
    verify_prompt(b_prompt, b_prompt_hash)

    prepared: list[dict[str, Any]] = []
    allowed_values: set[str] = set()
    for case_id, case in zip(CASE_IDS, cases, strict=True):
        request = ScenarioSqlRequest(
            intent=case["intent"], confirmed_filters=case["confirmed_filters"]
        )
        a_messages = build_service_a_model_messages(
            request,
            a_prompt,
            tables=table_context,
            columns=column_context,
            budget=service_a_settings.DWH_SQL_CONTEXT_BUDGET,
        )
        a_identity = SERVICE_A_IDENTITY
        a_hash = _generation_payload_hash(
            "openai", "gpt-5.4-mini", a_messages, "https://api.openai.com/v1"
        )

        b_content = build_service_b_generation_content(request, catalog)
        parsed_intent = parse_service_a_intent(b_content)
        if parsed_intent is None:
            raise ValueError(f"{case_id}: Service B did not parse AML_SCENARIO_GENERATION")
        raw_projection = project_allowlisted(
            parsed_intent, INTENT_PATHS, allow_confirmed_filter_values=True
        )
        if not isinstance(raw_projection, dict) or not raw_projection:
            raise ValueError(f"{case_id}: Service B intent projection is empty")
        allowed_values.update(_scalar_allowlist(raw_projection))
        prepared.append(
            {
                "case_id": case_id,
                "scenario": case["scenario"],
                "intent_text": case["intent_text"],
                "typed_intent": request.intent.model_dump(mode="json", exclude_none=False),
                "confirmed_filters": [
                    item.model_dump(mode="json") for item in request.confirmed_filters
                ],
                "service_b_wire_user_content": b_content,
                "service_a_messages": a_messages,
                "service_a_payload_sha256": a_hash,
                "service_a_provider_identity": a_identity,
                "service_b_projected_intent": raw_projection,
            }
        )

    b_settings.CLOUD_SAFE_INTENT_VALUES = tuple(sorted(allowed_values))
    b_schema = json.loads(schema_context)
    b_object_columns = {
        item["object"]: {column["name"] for column in item["columns"]}
        for item in b_schema["objects"]
    }
    service_b_payload_hashes: dict[str, str] = {}
    for item in prepared:
        model_user = json.dumps(
            {
                "request_type": "AML_SCENARIO_GENERATION",
                "intent": item["service_b_projected_intent"],
            },
            separators=(",", ":"),
            ensure_ascii=True,
        )
        b_messages = [
            {"role": "system", "content": b_system},
            {"role": "user", "content": model_user},
        ]
        from langchain_core.messages import HumanMessage, SystemMessage

        sanitized = sanitize_model_messages(
            [SystemMessage(content=b_system), HumanMessage(content=model_user)],
            b_settings,
            allow_confirmed_filter_values=True,
        )
        actual_messages = [
            {
                "role": "user" if message.type == "human" else message.type,
                "content": message.content,
            }
            for message in sanitized
        ]
        if actual_messages != b_messages:
            raise ValueError(f"{item['case_id']}: disclosure differs from provider messages")
        b_hash = generation_only_payload_hash(SERVICE_B_IDENTITY, b_messages)
        service_b_payload_hashes[item["case_id"]] = b_hash
        item["service_b_model_messages"] = b_messages
        item["service_b_payload_sha256"] = b_hash
        item["service_b_provider_identity"] = SERVICE_B_IDENTITY
        item.pop("service_b_projected_intent")

    payloads = {
        "format": "b9-model-bound-payloads-v1",
        "disclosure": "Each message list is the complete provider-bound conversation; no developer messages or hidden tool outputs are present.",
        "synthetic_only": True,
        "evaluator_sql_sent": False,
        "schema_records_sha256": schema_hash,
        "schema_records": schema_records,
        "service_a_prompt_sha256": a_prompt_hash,
        "service_a_system_prompt": a_prompt,
        "service_b_prompt_sha256": b_prompt_hash,
        "service_b_generation_prompt": b_prompt,
        "service_b_system_prompt_sha256": b_settings.CLOUD_SAFE_SYSTEM_PROMPT_SHA256,
        "service_b_provider_identity": SERVICE_B_IDENTITY,
        "service_a_provider_identity": SERVICE_A_IDENTITY,
        "cases": prepared,
    }

    fields = list(INTENT_PATHS)
    settings_file = _render_service_b_settings(
        scope_hash="PENDING_SCOPE_HASH",
        schema_hash=schema_hash,
        prompt_hash=b_prompt_hash,
        system_hash=b_settings.CLOUD_SAFE_SYSTEM_PROMPT_SHA256,
        payload_hashes=service_b_payload_hashes,
        intent_values=sorted(allowed_values),
        intent_fields=fields,
        schema_scope=schema_scope,
    )
    return payloads, service_b_payload_hashes, settings_file


def _render_service_b_settings(
    *,
    scope_hash: str,
    schema_hash: str,
    prompt_hash: str,
    system_hash: str,
    payload_hashes: dict[str, str],
    intent_values: list[str],
    intent_fields: list[str],
    schema_scope: dict[str, Any],
) -> str:
    """Render a process-scoped development configuration with no credentials."""
    objects = [schema_scope["object"]]
    columns = [
        item
        for column in schema_scope["columns"]
        for item in (column["name"], f"{schema_scope['object']}.{column['name']}")
    ]
    types = sorted({column["type"] for column in schema_scope["columns"]})
    identities = [SERVICE_B_IDENTITY]
    assignments = {
        "LANGCHAIN_TRACING_V2": "false",
        "LANGSMITH_TRACING": "false",
        "CLOUD_SAFE_LLM_MODE": "true",
        "CLOUD_SAFE_PROVIDER_IDENTITIES": json.dumps(identities),
        "CLOUD_SAFE_INTENT_FIELDS": json.dumps(intent_fields),
        "CLOUD_SAFE_INTENT_VALUES": json.dumps(intent_values, ensure_ascii=False),
        "CLOUD_SAFE_SCHEMA_OBJECTS": json.dumps(objects),
        "CLOUD_SAFE_SCHEMA_COLUMNS": json.dumps(columns),
        "CLOUD_SAFE_SCHEMA_TYPES": json.dumps(types),
        "CLOUD_SAFE_SCHEMA_RELATIONSHIPS": "[]",
        "CLOUD_SAFE_SCHEMA_TENANT_ID": SYNTHETIC_TENANT,
        "SCHEMA_INDEX_TENANT_ID": SYNTHETIC_TENANT,
        "CLOUD_SAFE_SYSTEM_PROMPT_SHA256": system_hash,
        "LLM_PROVIDER_FAST": "openai",
        "LLM_MODEL_FAST": "gpt-5-nano",
        "DWH_SCENARIO_GENERATION_ONLY_ENABLED": "true",
        "DWH_SCENARIO_GENERATION_ONLY_ENVIRONMENT": "development",
        "DWH_SCENARIO_GENERATION_ONLY_SCOPE_SHA256": scope_hash,
        "DWH_SCENARIO_GENERATION_ONLY_SCHEMA_SHA256": schema_hash,
        "DWH_SCENARIO_GENERATION_ONLY_PROMPT_SHA256": prompt_hash,
        "DWH_SCENARIO_GENERATION_ONLY_PAYLOAD_HASHES_JSON": json.dumps(payload_hashes, sort_keys=True),
    }
    return (
        "# Development process only. Do not copy into production configuration.\n"
        "# Supply OPENAI_API_KEY through the existing secret manager/environment.\n"
        + "\n".join(f"{key}={value}" for key, value in assignments.items())
        + "\n"
    )


def write_artifacts() -> tuple[str, str]:
    """Write payload disclosures, then bind their digest into the scope manifest."""
    payloads, b_hashes, _ = build_payload_artifacts(refresh_prompt_scope=True)
    payload_bytes = (json.dumps(payloads, indent=2, ensure_ascii=False) + "\n").encode()
    PAYLOAD_PATH.write_bytes(payload_bytes)
    payload_digest = _sha256(payload_bytes)
    scope = json.loads(MANIFEST_PATH.read_text(encoding="utf-8-sig"))
    previous_scope_hash = scope.get("scope_sha256")
    scope.pop("scope_sha256", None)
    cases = _read_cases()
    scope["status"] = "pending_new_authorization"
    if previous_scope_hash:
        scope["supersedes_scope_sha256"] = previous_scope_hash
    scope["fixtures_sha256"] = _sha256(FIXTURE_PATH.read_bytes())
    scope["synthetic_intents"] = [case["intent_text"] for case in cases]
    scope["payload_manifest_path"] = str(PAYLOAD_PATH.relative_to(ROOT)).replace("\\", "/")
    scope["payload_manifest_sha256"] = payload_digest
    scope["schema_records_sha256"] = payloads["schema_records_sha256"]
    scope["service_a"]["provider_identity"] = SERVICE_A_IDENTITY
    scope["service_a"]["prompt_sha256"] = payloads["service_a_prompt_sha256"]
    scope["service_a"]["payload_hashes"] = {
        item["case_id"]: item["service_a_payload_sha256"] for item in payloads["cases"]
    }
    scope["service_b"]["provider_identity"] = SERVICE_B_IDENTITY
    scope["service_b"]["prompt_sha256"] = payloads["service_b_prompt_sha256"]
    scope["service_b"]["payload_hashes"] = b_hashes
    scope["verification"]["model_payloads_disclosed"] = True
    scope["verification"]["service_b_guard_enabled_only_in_development"] = True
    scope["scope_sha256"] = _sha256(
        json.dumps(scope, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    )
    MANIFEST_PATH.write_text(
        json.dumps(scope, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    _, _, runtime_settings = build_payload_artifacts(refresh_prompt_scope=True)
    runtime_settings = runtime_settings.replace(
        "DWH_SCENARIO_GENERATION_ONLY_SCOPE_SHA256=PENDING_SCOPE_HASH",
        f"DWH_SCENARIO_GENERATION_ONLY_SCOPE_SHA256={scope['scope_sha256']}",
    )
    SERVICE_B_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SERVICE_B_SETTINGS_PATH.write_text(runtime_settings, encoding="utf-8")
    return scope["scope_sha256"], payload_digest


if __name__ == "__main__":
    scope_digest, payload_digest = write_artifacts()
    print(f"scope_sha256={scope_digest}")
    print(f"payload_manifest_sha256={payload_digest}")
