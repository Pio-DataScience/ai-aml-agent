"""Run the exact approved B9 cases through existing guarded generation paths.

This runner is intended for a disposable development process. It starts the
real Service B HTTP SSE route without lifespan, persistence, or Oracle
initialization, then uses Service A's local generator and HTTP adapter.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import sqlite3
import socket
import sys
import time
import types
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from .b9_execution_journal import (
        DurableCallJournal,
        UncertainDeliveryError,
        dispatch_with_journal,
    )
except ImportError:  # Direct script execution places this directory on sys.path.
    from b9_execution_journal import (
        DurableCallJournal,
        UncertainDeliveryError,
        dispatch_with_journal,
    )

ROOT = Path(__file__).resolve().parents[5]
SERVICE_B_ROOT = ROOT.parent / "PioTech-AI" / "services" / "dwh_scenario_agent"
FIXTURE_PATH = Path(__file__).with_name("b9_cases.jsonl")
SCOPE_PATH = ROOT / "Docs" / "B9_generation_only_scope.json"
PAYLOAD_PATH = ROOT / "Docs" / "B9_model_payloads.json"
EXPECTED_SCOPE = "d24ceb8538482a8e8b551a22890f704c78f07c4a135f3878be599e56569766a9"


def _sha256(data: bytes) -> str:
    """Return a lowercase SHA-256 digest."""
    return hashlib.sha256(data).hexdigest()


def _write_results_atomically(path: Path, result: dict[str, Any]) -> None:
    """Durably replace the completed JSON result without exposing partial JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.pending")
    content = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    with temporary.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _prepare_runtime_directories(root: Path) -> dict[str, Path]:
    """Create disposable Service B data, artifact, and log directories."""
    directories = {
        name: root / name for name in ("b9-static", "b9-artifacts", "b9-logs")
    }
    for path in directories.values():
        path.mkdir(parents=True, exist_ok=True)
    return directories


def _canonical_digest(value: dict[str, Any]) -> str:
    """Hash a manifest excluding its self-referential digest."""
    unsigned = dict(value)
    unsigned.pop("scope_sha256", None)
    return _sha256(
        json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        .encode("utf-8")
    )


def _semantic_state(
    sql: str | None,
    expected: tuple[Any, ...] | None,
    bind_values: dict[str, Any] | None = None,
) -> str:
    """Compare SQL against the deterministic fixture signature."""
    if not sql:
        return "no_candidate"
    from services.aml_builder.tests.eval.dwh_scenario.runner import semantic_signature

    try:
        signature = semantic_signature(sql, bind_values)
    except Exception as exc:  # Diagnostic parsing only; validation remains authoritative.
        return f"unparseable:{type(exc).__name__}"
    if signature is None:
        return "unparseable"
    return "matches_fixture" if signature == expected else "differs_from_fixture"


def _preflight(
    case_id: str,
    manifest: dict[str, Any],
    payload_manifest: dict[str, Any],
    rebuilt_payloads: dict[str, Any],
    a_settings: Any,
    b_settings: Any,
    a_prompt: str,
    b_prompt: str,
) -> dict[str, Any]:
    """Recheck exact scope and model-bound content immediately before a call."""
    scope = json.loads(SCOPE_PATH.read_text(encoding="utf-8"))
    stored_payloads = json.loads(PAYLOAD_PATH.read_text(encoding="utf-8"))
    if scope != manifest or stored_payloads != payload_manifest:
        raise RuntimeError("B9 scope or payload disclosure changed during the run")
    if _canonical_digest(scope) != EXPECTED_SCOPE or scope.get("scope_sha256") != EXPECTED_SCOPE:
        raise RuntimeError("B9 approved scope digest mismatch")
    if _sha256(FIXTURE_PATH.read_bytes()) != scope["fixtures_sha256"]:
        raise RuntimeError("B9 fixture digest mismatch")
    payload_bytes = PAYLOAD_PATH.read_bytes()
    if _sha256(payload_bytes) != scope["payload_manifest_sha256"]:
        raise RuntimeError("B9 payload disclosure digest mismatch")
    if (
        _sha256(a_prompt.encode("utf-8")) != scope["service_a"]["prompt_sha256"]
        or _sha256(b_prompt.encode("utf-8")) != scope["service_b"]["prompt_sha256"]
    ):
        raise RuntimeError("B9 generator prompt digest mismatch")
    if a_settings.DWH_SQL_MODE != "http":
        raise RuntimeError("Service A DWH_SQL_MODE is not http")
    if not a_settings.DWH_SQL_GENERATION_ONLY_ENABLED or a_settings.DWH_SQL_ENVIRONMENT != "development":
        raise RuntimeError("Service A development generation guard is not enabled")
    if a_settings.LANGCHAIN_TRACING_V2:
        raise RuntimeError("Service A tracing is enabled")
    if not b_settings.DWH_SCENARIO_GENERATION_ONLY_ENABLED:
        raise RuntimeError("Service B generation-only guard is not enabled")
    if b_settings.DWH_SCENARIO_GENERATION_ONLY_ENVIRONMENT != "development":
        raise RuntimeError("Service B is not in the development environment")
    from web.services.cloud_safe import assert_tracing_disabled

    assert_tracing_disabled()
    case = next(item for item in payload_manifest["cases"] if item["case_id"] == case_id)
    rebuilt = next(item for item in rebuilt_payloads["cases"] if item["case_id"] == case_id)
    if case != rebuilt:
        raise RuntimeError(f"{case_id}: exact model-bound payload differs from disclosure")
    if (
        case["service_a_payload_sha256"] != scope["service_a"]["payload_hashes"][case_id]
        or case["service_b_payload_sha256"] != scope["service_b"]["payload_hashes"][case_id]
    ):
        raise RuntimeError(f"{case_id}: payload hash differs from approved scope")
    return case


async def _run(journal: DurableCallJournal) -> dict[str, Any]:
    """Execute 12 sequential guarded generation calls per service."""
    if os.getenv("B9_APPROVED_SCOPE_SHA256") != EXPECTED_SCOPE:
        raise RuntimeError("The approved B9 scope hash was not supplied to the run")
    if not os.getenv("B9_SERVICE_A_OPENAI_API_KEY") or not os.getenv("B9_SERVICE_B_OPENAI_API_KEY"):
        raise RuntimeError("Both approved provider credentials must be available")

    service_b_env_path = Path(os.environ["B9_SERVICE_B_ENV_PATH"])
    for raw_line in service_b_env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#") and "=" in line:
            name, value = line.split("=", 1)
            os.environ[name] = value
    os.environ["OPENAI_API_KEY"] = os.environ["B9_SERVICE_B_OPENAI_API_KEY"]
    # The generation-only route uses the approved OpenAI fast model. Selecting
    # it as the default also keeps the dormant summarizer client on that same
    # provider during graph import; no summarizer invocation occurs.
    os.environ["LLM_PROVIDER"] = "openai"
    runtime_directories = _prepare_runtime_directories(
        Path(os.getenv("B9_TEMP_ROOT", "/tmp"))
    )
    os.environ.update({
        "ORACLE_DSN": "b9-disabled.invalid:1521/b9",
        "ORACLE_USER": "disabled",
        "ORACLE_PASSWORD": "disabled",
        "LLM_BASE_URL": "",
        "LLM_BASE_URL_FAST": "",
        "STATIC_DIR": str(runtime_directories["b9-static"]),
        "ARTIFACTS_DIR": str(runtime_directories["b9-artifacts"]),
        "LOGS_DIR": str(runtime_directories["b9-logs"]),
    })

    service_a_module_root = (ROOT / "services" / "aml_builder").resolve()
    # The two repositories both use a top-level ``web`` package. The runner
    # imports Service A through ``services.aml_builder`` and Service B through
    # ``web``; keeping A's module root on sys.path would shadow B's namespace
    # package on Linux and route the request to Service A's API by mistake.
    sys.path[:] = [
        entry for entry in sys.path
        if Path(entry or os.curdir).resolve() != service_a_module_root
    ]
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(SERVICE_B_ROOT))
    os.environ["LANGGRAPH_API_URL"] = "b9-no-checkpointer-generation-only"

    from services.aml_builder.tests.eval.dwh_scenario.build_b9_payload_manifest import (
        build_payload_artifacts,
        build_service_a_model_messages,
        service_a_schema_context,
    )
    from services.aml_builder.web.domain.dwh_scenario.contracts import ScenarioSqlRequest
    from services.aml_builder.web.services.dwh_scenario.generator import (
        GenerationOnlyApproval,
        LocalScenarioSqlGenerator,
        _generation_payload_hash,
        _generator_endpoint,
    )
    from services.aml_builder.web.services.dwh_scenario.http_adapter import (
        HttpScenarioSqlGenerator,
        build_service_b_generation_content,
    )
    from services.aml_builder.web.services.dwh_scenario.prompt_loader import load_prompt
    from services.aml_builder.web.services.llm_client import build_llm
    from services.aml_builder.web.services.settings import settings as a_settings
    from services.aml_builder.web.services.config_loader import load_catalogs

    import uvicorn
    from web.services.cloud_safe import provider_identity as b_provider_identity
    from web.services.settings import settings as b_settings
    from web.services.utils import _load_prompt_from_md

    # Re-read and verify the full current disclosure before opening the route.
    manifest = json.loads(SCOPE_PATH.read_text(encoding="utf-8"))
    payload_manifest = json.loads(PAYLOAD_PATH.read_text(encoding="utf-8"))
    rebuilt_payloads, _, _ = build_payload_artifacts()
    if _canonical_digest(manifest) != EXPECTED_SCOPE or manifest.get("scope_sha256") != EXPECTED_SCOPE:
        raise RuntimeError("Approved scope hash does not match the live artifacts")
    if rebuilt_payloads != payload_manifest:
        raise RuntimeError("Rebuilt model payloads differ from B9_model_payloads.json")

    a_settings.OPENAI_API_KEY = os.environ["B9_SERVICE_A_OPENAI_API_KEY"]
    b_settings.OPENAI_API_KEY = os.environ["B9_SERVICE_B_OPENAI_API_KEY"]
    a_prompt = load_prompt("generator_v1.md")
    b_prompt = _load_prompt_from_md("generation_only_system.md")
    a_provider = a_settings.DWH_SQL_GENERATOR_PROVIDER or a_settings.LLM_PROVIDER
    a_model = a_settings.DWH_SQL_GENERATOR_MODEL or a_settings.LLM_MODEL
    a_identity = (
        f"{a_provider.casefold()}|{a_model.strip()}|"
        f"{_generator_endpoint(a_provider, a_settings)}"
    )
    b_provider = b_settings.LLM_PROVIDER_FAST or b_settings.LLM_PROVIDER
    b_model = b_settings.LLM_MODEL_FAST or b_settings.LLM_MODEL
    b_endpoint = b_settings.LLM_BASE_URL_FAST or b_settings.LLM_BASE_URL
    b_identity = b_provider_identity(b_provider, b_model, b_endpoint or "https://api.openai.com/v1")
    if (
        a_identity != manifest["service_a"]["provider_identity"]
        or b_identity != manifest["service_b"]["provider_identity"]
    ):
        raise RuntimeError("Effective provider identity differs from the approved manifest")
    if a_settings.DWH_SQL_RETRY_BUDGET != 0 or a_settings.DWH_SQL_MAX_ATTEMPTS != 1:
        raise RuntimeError("Service A provider retries are not disabled for the 24-call run")

    # Keep even route-level session database initialization in-memory.
    sqlite3.connect = lambda *_args, **_kwargs: sqlite3.Connection(  # type: ignore[assignment]
        ":memory:"
    )
    # Service B imports torch at module load for optional startup device
    # selection. The guarded generation-only route never uses torch; keep the
    # Linux disposable runner independent of the multi-gigabyte ML runtime.
    torch_stub = types.ModuleType("torch")
    torch_stub.cuda = types.SimpleNamespace(is_available=lambda: False)
    torch_stub.__spec__ = __import__("importlib.machinery").machinery.ModuleSpec(
        "torch", loader=None
    )
    sys.modules.setdefault("torch", torch_stub)
    from web.api import main as b_main

    # Disable local session persistence. The graph is compiled without a
    # checkpointer via LANGGRAPH_API_URL, and lifespan is disabled below.
    b_main.upsert_chat_session = lambda *_args, **_kwargs: None
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = int(listener.getsockname()[1])
    a_settings.PIOTECH_AI_URL = f"http://127.0.0.1:{port}/chat/stream"
    a_settings.PIOTECH_AI_TIMEOUT_SECONDS = 180
    b_app_server = uvicorn.Server(uvicorn.Config(
        b_main.app,
        host="127.0.0.1",
        port=port,
        lifespan="off",
        timeout_graceful_shutdown=5,
        log_level="error",
        access_log=False,
    ))
    server_task = asyncio.create_task(b_app_server.serve())
    try:
        async with asyncio.timeout(30):
            while not b_app_server.started:
                await asyncio.sleep(0.02)

        from services.aml_builder.web.services.dwh_scenario.generator import _tracing_is_enabled

        local_generator = LocalScenarioSqlGenerator(
            llm_factory=lambda **kwargs: build_llm(**kwargs),
            repository=None,
            experience_retriever=None,
            table_retriever=None,
            column_retriever=None,
            config=a_settings,
        )
        catalog = load_catalogs(a_settings.CATALOG_DIR).filters
        http_generator = HttpScenarioSqlGenerator(filter_catalog=catalog)
        fixture_rows = [
            json.loads(line)
            for line in FIXTURE_PATH.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        schema_scope = manifest["schema_scope"]
        schema_records = payload_manifest["schema_records"]
        a_tables, a_columns = service_a_schema_context(schema_scope)
        results: list[dict[str, Any]] = []

        def verify_before_call(case_id: str) -> dict[str, Any]:
            if _tracing_is_enabled(a_settings):
                raise RuntimeError("Service A tracing became enabled")
            from web.services.cloud_safe import assert_tracing_disabled

            assert_tracing_disabled()
            if a_settings.DWH_SQL_MODE != "http":
                raise RuntimeError("DWH_SQL_MODE changed during the run")
            return _preflight(
                case_id, manifest, payload_manifest, rebuilt_payloads,
                a_settings, b_settings, a_prompt, b_prompt,
            )

        for index, fixture in enumerate(fixture_rows, start=1):
            case_id = f"b9-{index:02d}"
            journal.case_started(case_id=case_id)
            request = ScenarioSqlRequest(
                intent=fixture["intent"],
                confirmed_filters=fixture["confirmed_filters"],
            )
            payload_case = verify_before_call(case_id)
            a_messages = build_service_a_model_messages(
                request,
                a_prompt,
                tables=a_tables,
                columns=a_columns,
                budget=a_settings.DWH_SQL_CONTEXT_BUDGET,
            )
            actual_a_hash = _generation_payload_hash(
                a_provider, a_model, a_messages, _generator_endpoint(a_provider, a_settings)
            )
            if actual_a_hash != payload_case["service_a_payload_sha256"]:
                raise RuntimeError(f"{case_id}: Service A pre-call payload hash mismatch")
            a_approval = GenerationOnlyApproval(
                payload_sha256=payload_case["service_a_payload_sha256"],
                scope_sha256=EXPECTED_SCOPE,
                prompt_sha256=manifest["service_a"]["prompt_sha256"],
                provider_identity=a_identity,
                approved_by="user-authorized exact B9 scope",
                provider_data_approval_ref=EXPECTED_SCOPE,
                context_snapshot={
                    "experience": [], "tables": a_tables, "columns": a_columns,
                },
            )
            try:
                a_result = await dispatch_with_journal(
                    journal,
                    case_id=case_id,
                    service="Service A",
                    payload_sha256=payload_case["service_a_payload_sha256"],
                    call=lambda: local_generator.generate_only_for_approved_evaluation(
                        request, a_approval
                    ),
                )
                a_candidate = a_result.generation_only_candidate_sql or a_result.sql
                a_record = {
                    "provider_identity": a_identity,
                    "payload_sha256": actual_a_hash,
                    "candidate_sql": a_candidate,
                    "candidate_accepted_by_service_a": bool(a_result.sql)
                    and not a_result.validation_findings,
                    "validation_findings": a_result.validation_findings,
                    "error": a_result.error,
                    "metrics": a_result.metrics,
                    "approved": a_result.approved,
                }
            except UncertainDeliveryError:
                raise
            except Exception as exc:
                a_candidate = None
                a_record = {
                    "provider_identity": a_identity,
                    "payload_sha256": actual_a_hash,
                    "candidate_sql": None,
                    "candidate_accepted_by_service_a": False,
                    "validation_findings": [],
                    "error": f"{type(exc).__name__}: {exc}",
                    "approved": False,
                }

            payload_case = verify_before_call(case_id)
            actual_b_wire = build_service_b_generation_content(request, catalog)
            if actual_b_wire != payload_case["service_b_wire_user_content"]:
                raise RuntimeError(f"{case_id}: Service B wire intent differs from disclosure")
            runtime_model = b_settings.LLM_MODEL_FAST or b_settings.LLM_MODEL
            runtime_provider = b_settings.LLM_PROVIDER_FAST or b_settings.LLM_PROVIDER
            runtime_endpoint = b_settings.LLM_BASE_URL_FAST or b_settings.LLM_BASE_URL
            actual_b_identity = b_provider_identity(
                runtime_provider,
                runtime_model,
                runtime_endpoint or "https://api.openai.com/v1",
            )
            if actual_b_identity != manifest["service_b"]["provider_identity"]:
                raise RuntimeError(f"{case_id}: Service B runtime provider changed")
            try:
                b_result = await dispatch_with_journal(
                    journal,
                    case_id=case_id,
                    service="Service B",
                    payload_sha256=payload_case["service_b_payload_sha256"],
                    call=lambda: http_generator.generate_only_for_approved_evaluation(
                        request,
                        scope_sha256=EXPECTED_SCOPE,
                        schema_records=schema_records,
                        case_id=case_id,
                        service_b_payload_sha256=payload_case["service_b_payload_sha256"],
                    ),
                )
                candidate_event = next(
                    (
                        event for event in b_result.generation_only_sse_events
                        if event.get("type") == "generation_only_candidate"
                    ),
                    {},
                )
                b_candidate = candidate_event.get("text") or b_result.generation_only_candidate_sql or b_result.sql
                b_record = {
                    "provider_identity": actual_b_identity,
                    "payload_sha256": payload_case["service_b_payload_sha256"],
                    "candidate_sql": b_candidate,
                    "candidate_sql_extracted_by_adapter": b_result.generation_only_candidate_sql or b_result.sql,
                    "candidate_accepted_by_service_b": bool(b_candidate)
                    and not b_result.generation_only_provider_validation_findings,
                    "service_b_validation_findings": b_result.generation_only_provider_validation_findings,
                    "service_a_parity_validation_findings": b_result.validation_findings,
                    "normalizations": b_result.generation_only_normalizations,
                    "sse_events": b_result.generation_only_sse_events,
                    "error": b_result.error,
                    "approved": b_result.approved,
                }
            except UncertainDeliveryError:
                raise
            except Exception as exc:
                b_candidate = None
                b_record = {
                    "provider_identity": actual_b_identity,
                    "payload_sha256": payload_case["service_b_payload_sha256"],
                    "candidate_sql": None,
                    "candidate_accepted_by_service_b": False,
                    "service_b_validation_findings": [],
                    "service_a_parity_validation_findings": [],
                    "sse_events": [],
                    "error": f"{type(exc).__name__}: {exc}",
                    "approved": False,
                }

            expected_signature = None
            from services.aml_builder.tests.eval.dwh_scenario.runner import (
                semantic_pair_matches,
                semantic_signature,
            )
            from services.aml_builder.web.domain.dwh_scenario.validation import (
                derive_candidate_bind_values,
            )

            try:
                expected_signature = semantic_signature(
                    fixture["sql_a"], fixture["params"]
                )
            except Exception:
                expected_signature = None
            a_bind_values: dict[str, Any] | None = None
            b_bind_values: dict[str, Any] | None = None
            try:
                if a_candidate:
                    a_bind_values = derive_candidate_bind_values(request, a_candidate)
                if b_candidate:
                    b_bind_values = derive_candidate_bind_values(request, b_candidate)
            except Exception:
                a_bind_values = None
                b_bind_values = None
            a_semantics = _semantic_state(
                a_candidate, expected_signature, a_bind_values
            )
            b_semantics = _semantic_state(
                b_candidate, expected_signature, b_bind_values
            )
            pair_match = (
                a_bind_values is not None
                and b_bind_values is not None
                and semantic_pair_matches(
                    a_candidate, a_bind_values, b_candidate, b_bind_values
                )
            )
            case_result = {
                "case_id": case_id,
                "scenario": fixture["scenario"],
                "service_a": a_record,
                "service_b": b_record,
                "semantic_assessment": {
                    "service_a_vs_fixture": a_semantics,
                    "service_b_vs_fixture": b_semantics,
                    "service_a_vs_service_b_match": pair_match,
                    "fixture_signature_is_deterministic_reference_only": True,
                },
            }
            results.append(case_result)
            journal.case_outcome(
                case_id=case_id,
                outcome=(
                    "completed"
                    if a_record.get("candidate_sql") is not None
                    and b_record.get("candidate_sql") is not None
                    else "completed_with_generation_failure"
                ),
                details={
                    "service_a_payload_sha256": a_record["payload_sha256"],
                    "service_b_payload_sha256": b_record["payload_sha256"],
                    "service_a_candidate_present": a_record.get("candidate_sql") is not None,
                    "service_b_candidate_present": b_record.get("candidate_sql") is not None,
                    "service_a_error": a_record.get("error"),
                    "service_b_error": b_record.get("error"),
                    "semantic_assessment": case_result["semantic_assessment"],
                },
            )
            print(
                f"{case_id}: A={'candidate' if a_candidate else 'none'}, "
                f"B={'candidate' if b_candidate else 'none'}, "
                f"fixture_semantics={a_semantics}/{b_semantics}, pair_match={pair_match}",
                flush=True,
            )

        return {
            "scope_sha256": EXPECTED_SCOPE,
            "run_completed_at_utc": datetime.now(timezone.utc).isoformat(),
            "synthetic_only": True,
            "oracle_execution": False,
            "customer_row_retrieval": False,
            "critics": False,
            "deployment": False,
            "persistence": False,
            "tracing_disabled": True,
            "retries_enabled": False,
            "dwh_sql_mode": a_settings.DWH_SQL_MODE,
            "service_b_transport": "real local HTTP /chat/stream SSE route",
            "results": results,
        }
    finally:
        b_app_server.should_exit = True
        await asyncio.wait_for(server_task, timeout=10)


def main() -> None:
    """Run all cases and write a scope-bound results artifact."""
    started = time.monotonic()
    run_started = datetime.now(timezone.utc).isoformat()
    path = Path(os.environ["B9_RESULTS_PATH"])
    journal = DurableCallJournal(
        Path(os.environ["B9_JOURNAL_PATH"]),
        scope_sha256=EXPECTED_SCOPE,
        max_dispatches=24,
    )
    previous_handlers: dict[int, Any] = {}

    def mark_interrupted(signum: int, _frame: Any) -> None:
        """Convert supported termination signals into a journaled interruption."""
        raise KeyboardInterrupt(f"signal {signum}")

    for signal_name in ("SIGINT", "SIGTERM"):
        signum = getattr(signal, signal_name, None)
        if signum is not None:
            previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, mark_interrupted)
    try:
        result = asyncio.run(_run(journal))
        result["run_started_at_utc"] = run_started
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        a_valid = sum(item["service_a"]["candidate_accepted_by_service_a"] for item in result["results"])
        b_valid = sum(item["service_b"]["candidate_accepted_by_service_b"] for item in result["results"])
        semantics_a = sum(item["semantic_assessment"]["service_a_vs_fixture"] == "matches_fixture" for item in result["results"])
        semantics_b = sum(item["semantic_assessment"]["service_b_vs_fixture"] == "matches_fixture" for item in result["results"])
        parity = sum(item["semantic_assessment"]["service_a_vs_service_b_match"] for item in result["results"])
        result["summary"] = {
            "cases": len(result["results"]),
            "service_a_validator_pass": a_valid,
            "service_b_validator_pass": b_valid,
            "service_a_matches_fixture_semantics": semantics_a,
            "service_b_matches_fixture_semantics": semantics_b,
            "service_a_service_b_semantic_matches": parity,
        }
        _write_results_atomically(path, result)
        journal.finish_run("completed", details={"results_path": str(path)})
        print(json.dumps(result["summary"], sort_keys=True), flush=True)
        print(f"results_path={path}", flush=True)
    except KeyboardInterrupt as exc:
        journal.finish_run("interrupted", details={"reason": str(exc) or "KeyboardInterrupt"})
        raise
    except BaseException as exc:
        journal.finish_run(
            "failed",
            details={"error_type": type(exc).__name__, "error": str(exc)},
        )
        raise
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    main()
