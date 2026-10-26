"""Tests for validated AML catalog loading and mtime-based reloads."""

from pathlib import Path

import pytest

from services.aml_builder.web.services.config_loader import CatalogLoader, ConflictCheckPolicy


def _write_catalogs(root: Path) -> None:
    """Write the four minimal valid catalog documents used by loader tests."""
    (root / "params.yaml").write_text(
        "version: 1\nstatus: demo\nparams: {}\n", encoding="utf-8"
    )
    (root / "filters.yaml").write_text(
        "version: 1\nstatus: demo\ndimensions: {}\n", encoding="utf-8"
    )
    (root / "policy.yaml").write_text(
        "version: 1\nstatus: demo\nroles: {}\nusers: {}\n"
        "default_role: viewer\ncross_user_chat_read: false\n"
        "chat_may_deploy: false\nauth_mode: dev\nrelations: {}\n"
        "similar_min_score: 0.85\nmax_matches_shown: 5\n"
        "dimension_weights: {}\nreason_required_for: []\n",
        encoding="utf-8",
    )
    (root / "ui_copy.yaml").write_text(
        'locked_chat_tip: "Locked {scenario_id} v{version}"\n'
        'locked_composer_placeholder: "Locked"\n'
        'conflict_block: "Conflict {scenario_id}"\n'
        'conflict_warn: "{n} conflicts"\n',
        encoding="utf-8",
    )


def test_loads_valid_catalogs(tmp_path: Path) -> None:
    """The loader returns validated models for all four catalog files."""
    _write_catalogs(tmp_path)

    catalogs = CatalogLoader(tmp_path).load()

    assert catalogs.params.params == {}
    assert catalogs.filters.dimensions == {}
    assert catalogs.policy.default_role == "viewer"
    assert catalogs.ui_copy.conflict_warn == "{n} conflicts"


def test_conflict_check_policy_has_validated_defaults_and_rejects_unknown_modes():
    defaults = ConflictCheckPolicy()
    assert defaults.on_unavailable == "block"
    assert defaults.window_mismatch_relation == "RELATED"
    with pytest.raises(ValueError):
        ConflictCheckPolicy(on_unavailable="ignore")
    with pytest.raises(ValueError):
        ConflictCheckPolicy(window_mismatch_relation="IDENTICAL")


def test_invalid_catalog_raises_clear_error(tmp_path: Path) -> None:
    """Invalid YAML data identifies the catalog and validation failure."""
    _write_catalogs(tmp_path)
    (tmp_path / "params.yaml").write_text("version: nope\n", encoding="utf-8")

    with pytest.raises(ValueError, match="params.yaml"):
        CatalogLoader(tmp_path).load()


def test_catalogs_reload_when_file_mtime_changes(tmp_path: Path) -> None:
    """A modified catalog is loaded again instead of returning a stale model."""
    _write_catalogs(tmp_path)
    loader = CatalogLoader(tmp_path)
    initial = loader.load()
    params_path = tmp_path / "params.yaml"
    params_path.write_text(
        "version: 1\nstatus: demo\nparams:\n"
        "  new_field: {label: New, required: false, input: text, options: [], default: null, validation: {}, prompt: Ask, status: demo}\n",
        encoding="utf-8",
    )
    params_path.touch()

    updated = loader.load()

    assert initial.params.params == {}
    assert "new_field" in updated.params.params


def test_parameter_catalog_requires_ui_and_validation_metadata(tmp_path: Path) -> None:
    _write_catalogs(tmp_path)
    (tmp_path / "params.yaml").write_text(
        "version: 1\nstatus: demo\nparams:\n"
        "  window: {required: true, input: duration, default: null}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="params.yaml"):
        CatalogLoader(tmp_path).load()


def test_enabled_filter_with_tbd_value_is_rejected(tmp_path: Path) -> None:
    """TBD is allowed only for entries explicitly disabled for advisor input."""
    _write_catalogs(tmp_path)
    (tmp_path / "filters.yaml").write_text(
        "version: 1\nstatus: demo\ndimensions:\n"
        "  enabled_filter:\n"
        "    label: Enabled\n"
        "    level: account\n"
        "    target: {table: TBD, column: TBD}\n"
        "    resolver: lookup\n"
        "    status: confirmed\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="TBD"):
        CatalogLoader(tmp_path).load()


def test_needs_advisor_input_filter_may_contain_tbd(tmp_path: Path) -> None:
    """Disabled advisor-input entries can preserve the section 6 TBD draft."""
    _write_catalogs(tmp_path)
    (tmp_path / "filters.yaml").write_text(
        "version: 1\nstatus: demo\ndimensions:\n"
        "  pending_filter:\n"
        "    label: Pending\n"
        "    level: account\n"
        "    target: {table: TBD, column: TBD}\n"
        "    resolver: lookup\n"
        "    status: needs_advisor_input\n",
        encoding="utf-8",
    )

    catalogs = CatalogLoader(tmp_path).load()

    assert catalogs.filters.dimensions["pending_filter"].status == "needs_advisor_input"


def test_section_six_catalog_drafts_load_from_repository() -> None:
    """The configured section 6 catalog drafts are valid startup inputs."""
    catalogs = CatalogLoader(Path("config/aml_catalogs")).load()

    assert catalogs.params.status == "demo"
    assert catalogs.filters.dimensions["account_status"].status == "needs_advisor_input"
    assert catalogs.filters.dimensions["explanation_code"].status == "confirmed"
    assert catalogs.policy.revisions.history_cap == 5
    assert catalogs.policy.workflow.enforcement.model_dump() == {
        "approvals": "enforce",
        "card_confirmation": "enforce",
        "conflict_check": "enforce",
    }
    assert catalogs.params.card["one_pending_per_chat"] is True
    assert catalogs.params.params["baseline"].required_if.model_dump() == {
        "param": "relative", "equals": True
    }
