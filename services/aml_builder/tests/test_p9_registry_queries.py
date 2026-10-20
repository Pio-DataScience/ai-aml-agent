"""Offline checks for P9 read-only version registry queries."""

from __future__ import annotations

from services.aml_builder.web.services import scenario_registry_analytics as analytics
from services.aml_builder.web.services import alert_engine


def test_list_versions_returns_all_statuses_newest_first_and_uses_select(monkeypatch):
    calls = []
    rows = [
        ("PRD_ABCDEF12_V2", "PRD_ABCDEF12", 2, "PRD_ABCDEF12_V1", "RETIRED", "Rule", "u2", "t2", "reason", '{"change_scope":"metadata"}'),
        ("PRD_ABCDEF12_V1", "PRD_ABCDEF12", 1, None, "SUPERSEDED", "Rule", "u1", "t1", None, "{}"),
    ]

    def select(sql, params=None):
        calls.append((sql, params))
        return ["SCENARIO_ID", "SCENARIO_KEY", "VERSION_NO", "PARENT_SCENARIO_ID", "STATUS", "SCENARIO_NAME", "CREATED_BY", "CREATED_AT", "CHANGE_REASON", "RECEIPT_JSON"], rows

    monkeypatch.setattr(analytics, "run_readonly", select)
    result = analytics.list_scenario_versions("PRD_ABCDEF12_V2")
    assert [item["version_no"] for item in result["versions"]] == [2, 1]
    assert result["versions"][0]["status"] == "RETIRED"
    assert result["versions"][0]["change_scope"] == "metadata"
    assert calls and calls[0][0].lstrip().upper().startswith("SELECT")


def test_compare_versions_is_a_deterministic_stored_json_diff(monkeypatch):
    def select(_sql, _params=None):
        columns = ["SCENARIO_ID", "SCENARIO_KEY", "VERSION_NO", "SIGNATURE_JSON", "PARAMS_JSON", "FILTERS_JSON", "METADATA_JSON"]
        rows = [
            ("PRD_ABCDEF12_V1", "PRD_ABCDEF12", 1, '{"threshold":10}', '{"metric":"amount"}', "[]", '{"risk":"H"}'),
            ("PRD_ABCDEF12_V2", "PRD_ABCDEF12", 2, '{"threshold":10}', '{"metric":"amount"}', "[]", '{"risk":"M"}'),
        ]
        return columns, rows

    monkeypatch.setattr(analytics, "run_readonly", select)
    first = analytics.compare_scenario_versions("PRD_ABCDEF12_V1", "PRD_ABCDEF12_V2")
    second = analytics.compare_scenario_versions("PRD_ABCDEF12_V1", "PRD_ABCDEF12_V2")
    assert first == second
    assert first["changes"] == [{"path": "metadata_json.risk", "old": "H", "new": "M"}]


def test_targeted_alert_run_still_requires_both_active_flags(monkeypatch):
    captured = {}

    def select(sql, params=None):
        captured["sql"] = sql.upper()
        captured["params"] = params
        return [], []

    monkeypatch.setattr(alert_engine, "run_readonly", select)
    assert alert_engine.get_active_scenarios("PRD_ABCDEF12_V2") == []
    assert "P.IS_ACTIVE = 1" in captured["sql"]
    assert "NVL(S.ACTIVE_FLAG, '1') = '1'" in captured["sql"]
    assert captured["params"]["sid"] == "PRD_ABCDEF12_V2"
