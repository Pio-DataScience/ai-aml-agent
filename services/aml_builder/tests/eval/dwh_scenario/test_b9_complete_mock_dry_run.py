"""Linux end-to-end B9 dry run with both provider surfaces replaced by fakes."""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import importlib
import json
import os
import sqlite3
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

# Service A and Service B share the historical ``web`` import root. In the
# combined offline test process, extend A's regular packages with B's modules
# before either package imports B-only modules such as ``cloud_safe``.
ROOT = Path(__file__).resolve().parents[5]
SERVICE_B_ROOT = ROOT.parent / "PioTech-AI" / "services" / "dwh_scenario_agent"
sys.path.insert(0, str(ROOT / "services" / "aml_builder"))
sys.path.insert(0, str(SERVICE_B_ROOT))
import web

web.__path__.append(str(SERVICE_B_ROOT / "web"))
import web.services
import web.api

web.services.__path__.insert(0, str(SERVICE_B_ROOT / "web" / "services"))
web.api.__path__.insert(0, str(SERVICE_B_ROOT / "web" / "api"))

from services.aml_builder.tests.eval.dwh_scenario.build_b9_payload_manifest import (
    build_payload_artifacts,
    build_service_a_model_messages,
    service_a_schema_context,
)
from services.aml_builder.web.domain.dwh_scenario.contracts import ScenarioSqlRequest
from services.aml_builder.web.domain.dwh_scenario.sql_policy import validate_oracle_read
from services.aml_builder.web.domain.dwh_scenario.validation import (
    derive_candidate_bind_values,
    validate_generated_sql,
)
from services.aml_builder.web.services.dwh_scenario.generator import (
    GenerationOnlyApproval,
    LocalScenarioSqlGenerator,
    _generation_payload_hash,
)
from services.aml_builder.web.services.dwh_scenario.http_adapter import (
    HttpScenarioSqlGenerator,
)
from services.aml_builder.web.services.dwh_scenario.prompt_loader import load_prompt
from services.aml_builder.web.services.settings import settings as a_settings
from services.aml_builder.tests.eval.dwh_scenario.runner import (
    semantic_pair_matches,
)


SERVICE_B_ENV = ROOT.parent / "PioTech-AI" / "Docs" / "B9_service_b_development.env.example"
FIXTURE_PATH = Path(__file__).with_name("b9_cases.jsonl")
SCOPE_PATH = ROOT / "Docs" / "B9_generation_only_scope.json"
PAYLOAD_PATH = ROOT / "Docs" / "B9_model_payloads.json"
EXPECTED_SCOPE = "d24ceb8538482a8e8b551a22890f704c78f07c4a135f3878be599e56569766a9"


def _now() -> str:
    """Return a UTC timestamp for persisted smoke artifacts."""
    return datetime.now(timezone.utc).isoformat()


def _digest(text: str) -> str:
    """Return a SHA-256 hash of text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.mark.skipif(sys.platform == "win32", reason="This complete route dry run is exercised in Linux")
def test_complete_twelve_case_mock_dry_run(monkeypatch, tmp_path: Path) -> None:
    """Run every approved intent through A's local generator and B's real SSE route.

    Service A's LLM factory and Service B's graph are deterministic fakes. The
    live provider clients are never constructed and the Oracle lifespan is off.
    """
    fixtures = [
        json.loads(line)
        for line in FIXTURE_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    scope = json.loads(SCOPE_PATH.read_text(encoding="utf-8"))
    disclosed = json.loads(PAYLOAD_PATH.read_text(encoding="utf-8"))
    assert len(fixtures) == 12
    assert scope["scope_sha256"] == EXPECTED_SCOPE
    assert disclosed["synthetic_only"] is True and disclosed["evaluator_sql_sent"] is False

    for raw in SERVICE_B_ENV.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            name, value = line.split("=", 1)
            monkeypatch.setenv(name, value)
    monkeypatch.setenv("OPENAI_API_KEY", "mock-only-no-provider-call")
    monkeypatch.setenv("ORACLE_DSN", "b9-disabled.invalid:1521/b9")
    monkeypatch.setenv("ORACLE_USER", "disabled")
    monkeypatch.setenv("ORACLE_PASSWORD", "disabled")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    runtime_root = tmp_path / "b9-mock-runtime"
    for name in ("static", "artifacts", "logs"):
        (runtime_root / name).mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("STATIC_DIR", str(runtime_root / "static"))
    monkeypatch.setenv("ARTIFACTS_DIR", str(runtime_root / "artifacts"))
    monkeypatch.setenv("LOGS_DIR", str(runtime_root / "logs"))
    monkeypatch.setenv("LANGGRAPH_API_URL", "b9-no-checkpointer-mock-run")
    monkeypatch.syspath_prepend(str(SERVICE_B_ROOT))

    a_prompt = load_prompt("generator_v1.md")
    for name, value in {
        "DWH_SQL_MODE": "http",
        "DWH_SQL_ENVIRONMENT": "development",
        "DWH_SQL_GENERATION_ONLY_ENABLED": True,
        "DWH_SQL_GENERATION_ONLY_SCOPE_SHA256": EXPECTED_SCOPE,
        "DWH_SQL_GENERATION_ONLY_PROMPT_SHA256": scope["service_a"]["prompt_sha256"],
        "DWH_SQL_GENERATOR_PROVIDER": "openai",
        "DWH_SQL_GENERATOR_MODEL": "gpt-5.4-mini",
        "DWH_SQL_BASE_URL": None,
        "LLM_PROVIDER": "openai",
        "LLM_MODEL": "gpt-5.4-mini",
        "LLM_BASE_URL": None,
        "DWH_SQL_RETRY_BUDGET": 0,
        "DWH_SQL_MAX_ATTEMPTS": 1,
        "DWH_SQL_ALLOWED_SCHEMAS": ("BI_DWH",),
        "DWH_SQL_ALLOWED_OBJECTS": ("BI_DWH.PIO_TRANSACTIONS",),
        "DWH_SQL_OPENAI_API_KEY": "mock-only-no-provider-call",
        "OPENAI_API_KEY": "mock-only-no-provider-call",
        "LANGCHAIN_TRACING_V2": False,
        "PIOTECH_AI_TIMEOUT_SECONDS": 20,
    }.items():
        monkeypatch.setattr(a_settings, name, value)
    rebuilt, _, _ = build_payload_artifacts()
    assert rebuilt == disclosed

    expected_sql_b = {
        f"b9-{index:02d}": fixture["sql_b"]
        for index, fixture in enumerate(fixtures, start=1)
    }
    fake_b_calls: list[str] = []
    fake_a_calls: list[str] = []
    active: dict[str, str] = {"case_id": ""}

    class FakeGraph:
        """Return the selected synthetic SQL as a mocked provider generation."""

        def stream(self, state: dict[str, Any], *, config: dict[str, Any]):
            case_id = str(state.get("generation_only_case_id", ""))
            fake_b_calls.append(case_id)
            yield {
                "sql_generation": {
                    "verdict": "GENERATION_ONLY",
                    "generation_only_candidate_sql": expected_sql_b[case_id],
                    "generation_only_normalizations": [],
                    "generation_only_validation_findings": [],
                }
            }

    fake_agent = types.ModuleType("web.services.agent")
    fake_agent.MAX_ITERATIONS = 1
    fake_agent.app = FakeGraph()
    fake_agent._register_thread_sse = lambda *_args: None
    fake_agent._unregister_thread_sse = lambda: None
    fake_agent.register_sse_session = lambda *_args: None
    fake_agent.unregister_sse_session = lambda: None
    fake_agent.sse_queue_var = contextvars.ContextVar("b9_dry_queue")
    fake_agent.sse_loop_var = contextvars.ContextVar("b9_dry_loop")
    monkeypatch.setitem(sys.modules, "web.services.agent", fake_agent)

    torch_stub = types.ModuleType("torch")
    torch_stub.cuda = types.SimpleNamespace(is_available=lambda: False)
    torch_stub.__spec__ = importlib.machinery.ModuleSpec("torch", loader=None)
    monkeypatch.setitem(sys.modules, "torch", torch_stub)
    monkeypatch.setattr(
        sqlite3,
        "connect",
        lambda *_args, **_kwargs: sqlite3.Connection(":memory:"),
    )

    b_settings_module = importlib.import_module("web.services.settings")
    b_settings = importlib.reload(b_settings_module).settings
    monkeypatch.setattr(b_settings, "OPENAI_API_KEY", "mock-only-no-provider-call")
    from web.services.cloud_safe import assert_tracing_disabled

    assert_tracing_disabled()
    from web.api import main as b_main

    monkeypatch.setattr(b_main, "upsert_chat_session", lambda *_args, **_kwargs: None)
    assert "web.services.oracle" not in sys.modules

    class FakeAChatModel:
        """Return only the deterministic synthetic candidate selected per case."""

        def invoke(self, _messages: list[dict[str, str]]) -> SimpleNamespace:
            case_id = active["case_id"]
            fake_a_calls.append(case_id)
            sql = fixtures[int(case_id[-2:]) - 1]["sql_a"]
            return SimpleNamespace(
                content=sql,
                usage_metadata={"input_tokens": 1, "output_tokens": 1},
                response_metadata={"x-request-id": f"mock-a-{case_id}"},
            )

    def fake_a_factory(**_kwargs: Any) -> FakeAChatModel:
        return FakeAChatModel()

    local_generator = LocalScenarioSqlGenerator(
        llm_factory=fake_a_factory,
        repository=None,
        experience_retriever=None,
        table_retriever=None,
        column_retriever=None,
        config=a_settings,
    )
    table_context, column_context = service_a_schema_context(scope["schema_scope"])
    catalog = importlib.import_module(
        "services.aml_builder.web.services.config_loader"
    ).load_catalogs(a_settings.CATALOG_DIR).filters
    b_generator = HttpScenarioSqlGenerator(filter_catalog=catalog)
    schema_records = disclosed["schema_records"]

    async def run_all() -> list[dict[str, Any]]:
        """Serve the real local route and run the complete mocked A/B matrix."""
        with __import__("socket").socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = int(listener.getsockname()[1])
        a_settings.PIOTECH_AI_URL = f"http://127.0.0.1:{port}/chat/stream"
        server = __import__("uvicorn").Server(
            __import__("uvicorn").Config(
                b_main.app,
                host="127.0.0.1",
                port=port,
                lifespan="off",
                timeout_graceful_shutdown=4,
                log_level="error",
                access_log=False,
            )
        )
        task = asyncio.create_task(server.serve())
        rows: list[dict[str, Any]] = []
        try:
            async with asyncio.timeout(15):
                while not server.started:
                    await asyncio.sleep(0.01)
            for index, fixture in enumerate(fixtures, start=1):
                case_id = f"b9-{index:02d}"
                active["case_id"] = case_id
                request = ScenarioSqlRequest(
                    intent=fixture["intent"],
                    confirmed_filters=fixture["confirmed_filters"],
                )
                messages = build_service_a_model_messages(
                    request,
                    a_prompt,
                    tables=table_context,
                    columns=column_context,
                    budget=a_settings.DWH_SQL_CONTEXT_BUDGET,
                )
                disclosed_case = disclosed["cases"][index - 1]
                assert messages == disclosed_case["service_a_messages"]
                a_hash = _generation_payload_hash(
                    "openai", "gpt-5.4-mini", messages, "https://api.openai.com/v1"
                )
                assert a_hash == disclosed_case["service_a_payload_sha256"]
                a_approval = GenerationOnlyApproval(
                    payload_sha256=a_hash,
                    scope_sha256=EXPECTED_SCOPE,
                    prompt_sha256=scope["service_a"]["prompt_sha256"],
                    provider_identity=scope["service_a"]["provider_identity"],
                    approved_by="offline deterministic mock dry run",
                    provider_data_approval_ref=EXPECTED_SCOPE,
                    context_snapshot={
                        "experience": [], "tables": table_context, "columns": column_context,
                    },
                )
                a_result = await local_generator.generate_only_for_approved_evaluation(
                    request, a_approval
                )
                a_candidate = a_result.generation_only_candidate_sql or a_result.sql
                assert a_candidate == fixture["sql_a"]
                assert a_result.approved is False
                assert a_result.metrics["oracle_statements"] == 0
                assert a_result.metrics["critique_attempts"] == 0

                b_result = await b_generator.generate_only_for_approved_evaluation(
                    request,
                    scope_sha256=EXPECTED_SCOPE,
                    schema_records=schema_records,
                    case_id=case_id,
                    service_b_payload_sha256=disclosed_case[
                        "service_b_payload_sha256"
                    ],
                )
                candidate_event = next(
                    event for event in b_result.generation_only_sse_events
                    if event.get("type") == "generation_only_candidate"
                )
                b_candidate = candidate_event["text"]
                assert b_candidate == fixture["sql_b"]
                assert candidate_event["approved"] is False

                validators: dict[str, Any] = {}
                for service, sql in (("service_a", a_candidate), ("service_b", b_candidate)):
                    binds: dict[str, Any] = {}
                    policy_findings: list[str] = []
                    contract_findings: list[str] = []
                    try:
                        binds = derive_candidate_bind_values(request, sql)
                    except Exception as exc:
                        policy_findings.append(
                            f"bind derivation: {type(exc).__name__}: {exc}"
                        )
                    if not policy_findings:
                        try:
                            validate_oracle_read(
                                sql,
                                params=binds,
                                allowed_schemas=("BI_DWH",),
                                allowed_objects=("BI_DWH.PIO_TRANSACTIONS",),
                                required_projection="CUS_NUM",
                                confirmed_filters=[
                                    item.model_dump(mode="json")
                                    for item in request.confirmed_filters
                                ],
                            )
                        except Exception as exc:
                            policy_findings.append(
                                f"Oracle-read policy: {type(exc).__name__}: {exc}"
                            )
                    try:
                        contract_findings = validate_generated_sql(
                            request, sql, params=binds
                        )
                    except Exception as exc:
                        contract_findings = [
                            f"contract validation: {type(exc).__name__}: {exc}"
                        ]
                    validators[service] = {
                        "binds": binds,
                        "oracle_policy_findings": policy_findings,
                        "contract_findings": contract_findings,
                    }
                a_binds = validators["service_a"]["binds"]
                b_binds = validators["service_b"]["binds"]
                fixture_binds = derive_candidate_bind_values(request, fixture["sql_a"])
                assert semantic_pair_matches(
                    a_candidate, a_binds, b_candidate, b_binds
                )
                assert semantic_pair_matches(
                    a_candidate, a_binds, fixture["sql_a"], fixture_binds
                )
                rows.append({
                    "case_id": case_id,
                    "scenario": fixture["scenario"],
                    "service_a": {
                        "provider": "mock:Service A",
                        "payload_sha256": a_hash,
                        "candidate_sql": a_candidate,
                        "approved": False,
                        "validation_findings": a_result.validation_findings,
                        "independent_oracle_policy_findings": validators[
                            "service_a"
                        ]["oracle_policy_findings"],
                        "independent_contract_findings": validators[
                            "service_a"
                        ]["contract_findings"],
                        "binds": validators["service_a"]["binds"],
                    },
                    "service_b": {
                        "provider": "mock:Service B graph",
                        "payload_sha256": disclosed_case[
                            "service_b_payload_sha256"
                        ],
                        "candidate_sql": b_candidate,
                        "approved": False,
                        "provider_validation_findings": candidate_event.get(
                            "validation_findings", []
                        ),
                        "independent_oracle_policy_findings": validators[
                            "service_b"
                        ]["oracle_policy_findings"],
                        "independent_contract_findings": validators[
                            "service_b"
                        ]["contract_findings"],
                        "sse_events": b_result.generation_only_sse_events,
                        "binds": validators["service_b"]["binds"],
                    },
                    "semantic_assessment": {
                        "service_a_matches_fixture": True,
                        "service_b_matches_fixture": True,
                        "service_a_service_b_match": True,
                    },
                })
            return rows
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, timeout=8)

    results = asyncio.run(run_all())
    assert len(fake_a_calls) == len(fake_b_calls) == 12
    assert fake_a_calls == fake_b_calls == [f"b9-{index:02d}" for index in range(1, 13)]
    assert "web.services.oracle" not in sys.modules
    assert len(results) == 12

    output_path = Path(os.environ.get(
        "B9_DRY_RUN_ARTIFACT", str(ROOT / "Docs" / "B9_mock_dry_run_linux.json")
    ))
    journal_path = Path(os.environ.get(
        "B9_DRY_RUN_JOURNAL", str(ROOT / "Docs" / "B9_mock_dry_run_linux_journal.jsonl")
    ))
    assert not output_path.exists() and not journal_path.exists()
    mock_journal = [
        {
            "event": "dry_run_started",
            "timestamp": _now(),
            "scope_sha256": EXPECTED_SCOPE,
            "mode": "linux_mocked_providers",
            "real_provider_dispatches": 0,
            "max_live_dispatches": 0,
        }
    ]
    for case in results:
        for service in ("service_a", "service_b"):
            item = case[service]
            mock_journal.append({
                "event": "mock_response_recorded",
                "timestamp": _now(),
                "case_id": case["case_id"],
                "service": service,
                "payload_sha256": item["payload_sha256"],
                "candidate_sql_sha256": _digest(item["candidate_sql"]),
                "provider_dispatch": False,
            })
    mock_journal.append({
        "event": "dry_run_completed",
        "timestamp": _now(),
        "cases": 12,
        "mock_responses": 24,
        "real_provider_dispatches": 0,
    })
    artifact = {
        "scope_sha256": EXPECTED_SCOPE,
        "status": "completed_offline_mock_dry_run",
        "synthetic_only": True,
        "linux": True,
        "mock_providers": True,
        "mock_responses": 24,
        "real_provider_dispatches": 0,
        "oracle_execution": False,
        "customer_data_access": False,
        "critics": False,
        "deployment": False,
        "retries": False,
        "cases": results,
        "artifact_checks": {
            "case_count": len(results),
            "all_sql_candidates_present": all(
                case["service_a"]["candidate_sql"] and case["service_b"]["candidate_sql"]
                for case in results
            ),
            "all_candidates_pass_strict_contract_validation": all(
                not case["service_a"]["independent_oracle_policy_findings"]
                and not case["service_a"]["independent_contract_findings"]
                and not case["service_b"]["independent_oracle_policy_findings"]
                and not case["service_b"]["independent_contract_findings"]
                for case in results
            ),
            "all_semantic_pairs_match": all(
                case["semantic_assessment"]["service_a_service_b_match"]
                for case in results
            ),
            "scope_payload_fixture_hashes_verified": True,
            "service_b_sse_events_captured": all(
                bool(case["service_b"]["sse_events"]) for case in results
            ),
        },
    }
    output_path.write_text(json.dumps(artifact, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    journal_path.write_text(
        "".join(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n" for record in mock_journal),
        encoding="utf-8",
    )
    saved = json.loads(output_path.read_text(encoding="utf-8"))
    assert saved["status"] == "completed_offline_mock_dry_run"
    assert saved["synthetic_only"] and saved["mock_providers"]
    assert saved["scope_sha256"] == EXPECTED_SCOPE
    assert saved["artifact_checks"]["case_count"] == 12
    assert saved["artifact_checks"]["all_sql_candidates_present"]
    assert saved["artifact_checks"]["all_semantic_pairs_match"]
    assert saved["artifact_checks"]["scope_payload_fixture_hashes_verified"]
    assert saved["artifact_checks"]["service_b_sse_events_captured"]
    assert saved["real_provider_dispatches"] == 0
    assert saved["oracle_execution"] is False
    assert saved["customer_data_access"] is False
    assert saved["critics"] is False and saved["deployment"] is False
    assert saved["retries"] is False
    assert not saved["artifact_checks"]["all_candidates_pass_strict_contract_validation"]
    strict_failure_cases = {
        case["case_id"]
        for case in saved["cases"]
        if any(
            case[service][finding]
            for service in ("service_a", "service_b")
            for finding in (
                "independent_oracle_policy_findings",
                "independent_contract_findings",
            )
        )
    }
    assert strict_failure_cases == {"b9-01"}
    journal_lines = journal_path.read_text(encoding="utf-8").splitlines()
    assert len(journal_lines) == 26
    journal_records = [json.loads(line) for line in journal_lines]
    assert journal_records[0]["event"] == "dry_run_started"
    assert journal_records[-1]["event"] == "dry_run_completed"
    mock_responses = journal_records[1:-1]
    assert len(mock_responses) == 24
    assert all(record["provider_dispatch"] is False for record in mock_responses)
