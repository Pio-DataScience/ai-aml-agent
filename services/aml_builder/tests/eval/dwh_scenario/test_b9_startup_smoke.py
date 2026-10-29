"""Windows-safe Service B import/startup smoke with provider calls disabled."""

from __future__ import annotations

import importlib
import json
import sqlite3
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[5]
SERVICE_B_ROOT = ROOT.parent / "PioTech-AI" / "services" / "dwh_scenario_agent"
SERVICE_B_ENV = ROOT.parent / "PioTech-AI" / "Docs" / "B9_service_b_development.env.example"
SCOPE_PATH = ROOT / "Docs" / "B9_generation_only_scope.json"


def test_service_b_app_imports_with_mock_credentials_and_disposable_paths(
    monkeypatch, tmp_path: Path
) -> None:
    """Import/configure the actual FastAPI app without Oracle or provider calls."""
    for raw in SERVICE_B_ENV.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            name, value = line.split("=", 1)
            monkeypatch.setenv(name, value)
    monkeypatch.setenv("OPENAI_API_KEY", "mock-provider-must-not-be-called")
    monkeypatch.setenv("ORACLE_DSN", "b9-disabled.invalid:1521/b9")
    monkeypatch.setenv("ORACLE_USER", "disabled")
    monkeypatch.setenv("ORACLE_PASSWORD", "disabled")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    runtime_root = tmp_path / "missing-parent" / "b9-smoke"
    for name in ("STATIC_DIR", "ARTIFACTS_DIR", "LOGS_DIR"):
        path = runtime_root / name.casefold()
        path.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv(name, str(path))

    monkeypatch.syspath_prepend(str(SERVICE_B_ROOT))
    torch_stub = types.ModuleType("torch")
    torch_stub.cuda = types.SimpleNamespace(is_available=lambda: False)
    torch_stub.__spec__ = importlib.machinery.ModuleSpec("torch", loader=None)
    monkeypatch.setitem(sys.modules, "torch", torch_stub)
    monkeypatch.setattr(
        sqlite3,
        "connect",
        lambda *_args, **_kwargs: sqlite3.Connection(":memory:"),
    )
    oracle_was_loaded = "web.services.oracle" in sys.modules

    b_settings_module = importlib.import_module("web.services.settings")
    b_settings = importlib.reload(b_settings_module).settings
    # Another Service B route test may have imported the shared ``web`` module
    # under earlier disposable paths. Reload so startup logging uses this
    # test's isolated runtime directories.
    b_main = importlib.reload(importlib.import_module("web.api.main"))

    assert b_settings.DWH_SCENARIO_GENERATION_ONLY_ENABLED is True
    assert b_settings.DWH_SCENARIO_GENERATION_ONLY_ENVIRONMENT == "development"
    assert b_settings.DWH_SCENARIO_GENERATION_ONLY_SCOPE_SHA256 == (
        json.loads(SCOPE_PATH.read_text(encoding="utf-8"))["scope_sha256"]
    )
    assert not oracle_was_loaded and "web.services.oracle" not in sys.modules
    assert any(
        getattr(route, "path", None) == "/chat/stream"
        and "POST" in getattr(route, "methods", set())
        for route in b_main.app.routes
    )
    assert (Path(b_settings.LOGS_DIR) / "app.log").is_file()
