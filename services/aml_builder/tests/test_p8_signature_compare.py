"""P8 canonical signatures and deterministic relation comparison."""

from __future__ import annotations

import pytest

from services.aml_builder.web.domain.compare import compare_signatures
from services.aml_builder.web.domain.signature import build_signature
from services.aml_builder.web.services.conflict_check import ConflictChecker, has_comparable_core


def intent(**overrides):
    value = {
        "scenario_name": "Scenario A",
        "scenario_type": "CUSTOMER",
        "aggregation": {"grain": "per customer per month", "function": "SUM"},
        "thresholds": [{"field": "transaction amount", "operator": ">", "value_from": 5000}],
        "time_window": {"value": 1, "unit": "MONTHS", "is_rolling": True},
        "transaction_type": "cash deposit",
        "resolved_filters": [],
    }
    value.update(overrides)
    return value


def signature(source):
    return build_signature(source)[0]


@pytest.mark.parametrize(
    ("new_threshold", "existing_threshold", "expected"),
    [
        ({"operator": ">", "value_from": 10000}, {"operator": ">", "value_from": 5000}, "COVERED"),
        ({"operator": ">", "value_from": 5000}, {"operator": ">", "value_from": 10000}, "COVERS"),
        ({"operator": ">", "value_from": 5000}, {"operator": ">=", "value_from": 5000}, "COVERED"),
        ({"operator": ">=", "value_from": 5000}, {"operator": ">", "value_from": 5000}, "COVERS"),
        ({"operator": "BETWEEN", "value_from": 6, "value_to": 9}, {"operator": "BETWEEN", "value_from": 5, "value_to": 10}, "COVERED"),
        ({"operator": "BETWEEN", "value_from": 5, "value_to": 10}, {"operator": "BETWEEN", "value_from": 6, "value_to": 9}, "COVERS"),
    ],
)
def test_threshold_interval_containment(new_threshold, existing_threshold, expected):
    new = signature(intent(thresholds=[{"field": "amount", **new_threshold}]))
    existing = signature(intent(thresholds=[{"field": "amount", **existing_threshold}]))
    assert compare_signatures(new, existing).relation == expected


def test_between_inclusive_endpoints_do_not_collapse_to_open_bounds():
    closed = signature(intent(thresholds=[{"field": "amount", "operator": "BETWEEN", "value_from": 5, "value_to": 10}]))
    touching = signature(intent(thresholds=[{"field": "amount", "operator": "BETWEEN", "value_from": 10, "value_to": 20}]))
    contained = signature(intent(thresholds=[{"field": "amount", "operator": "BETWEEN", "value_from": 5, "value_to": 9}]))
    assert compare_signatures(closed, touching).relation == "OVERLAP"
    assert compare_signatures(closed, contained).relation == "COVERS"


def test_signature_ignores_scenario_name_and_normalizes_units_and_sets():
    first = intent(
        scenario_name="First name",
        transaction_types=["Cash", "Wire"],
        time_window={"value": 4, "unit": "WEEKS", "is_rolling": True},
    )
    second = intent(
        scenario_name="Different name",
        transaction_types=["wire", "cash"],
        time_window={"value": 28, "unit": "DAYS", "is_rolling": True},
    )
    sig_a, hash_a = build_signature(first)
    sig_b, hash_b = build_signature(second)
    assert sig_a == sig_b
    assert hash_a == hash_b
    assert compare_signatures(sig_a, sig_b).relation == "IDENTICAL"


def test_partially_overlapping_filter_sets_are_overlap():
    new = signature(intent(resolved_filters=[{"dimension": "channel", "values": ["cash", "wire"], "match": "any_of", "polarity": "positive"}]))
    existing = signature(intent(resolved_filters=[{"dimension": "channel", "values": ["wire", "card"], "match": "any_of", "polarity": "positive"}]))
    assert compare_signatures(new, existing).relation == "OVERLAP"


def test_missing_filter_means_all_values_and_scope_all_contains_specific():
    all_scope = signature(intent(transaction_type=None, transaction_types=None))
    specific = signature(intent(transaction_types=["cash", "wire"], transaction_type=None))
    assert compare_signatures(specific, all_scope).relation == "COVERED"
    assert compare_signatures(all_scope, specific).relation == "COVERS"
    assert signature(intent(transaction_type="ALL"))["transaction_scope"]["mode"] == "all"


def test_negated_filters_are_compared_as_excluded_sets():
    new = signature(intent(resolved_filters=[{"dimension": "channel", "values": ["cash"], "match": "any_of", "polarity": "negative"}]))
    existing = signature(intent(resolved_filters=[{"dimension": "channel", "values": ["cash", "wire"], "match": "any_of", "polarity": "negative"}]))
    assert compare_signatures(new, existing).relation == "COVERS"


def test_window_mismatch_and_relative_baselines_are_related():
    rolling = signature(intent())
    calendar = signature(intent(time_window={"value": 30, "unit": "DAYS", "is_rolling": False}))
    baseline = signature(intent(baseline_window={"duration": 180, "unit": "DAYS", "offset_days": 30}))
    assert compare_signatures(rolling, calendar).relation == "RELATED"
    assert compare_signatures(rolling, baseline).relation == "RELATED"


def test_scenario_type_is_case_normalized_and_different_types_are_not_compared():
    upper = signature(intent(scenario_type="CUSTOMER"))
    lower = signature(intent(scenario_type="customer"))
    other = signature(intent(scenario_type="ACCOUNT"))
    assert compare_signatures(upper, lower).relation == "IDENTICAL"
    assert compare_signatures(upper, other).relation is None


def test_configured_dimension_weights_drive_similarity_and_diff_shape():
    new = signature(intent(resolved_filters=[{"dimension": "channel", "values": ["cash"], "match": "any_of", "polarity": "positive"}]))
    existing = signature(intent(resolved_filters=[{"dimension": "channel", "values": ["wire"], "match": "any_of", "polarity": "positive"}]))
    result = compare_signatures(new, existing, dimension_weights={"scenario_type": 3, "filters": 1})
    assert result.relation == "RELATED"
    assert 0 <= result.similarity_pct <= 100
    assert all(set(item) == {"dimension", "new", "existing", "same"} for item in result.diff)


def test_early_hint_requires_the_comparable_core_to_be_resolved():
    complete = intent()
    incomplete = intent(time_window=None)
    assert has_comparable_core(complete)
    assert not has_comparable_core(incomplete)


def test_legacy_similarity_fallback_is_informational_and_provisional():
    repo = type("FakeRepo", (), {
        "list_active_signatures": lambda self, _type: [{
            "scenario_id": "legacy", "scenario_key": "old", "scenario_name": "Legacy",
            "version_no": 1, "thread_id": "legacy-thread", "signature_json": None,
        }]
    })()
    policy = {
        "relations": {}, "dimension_weights": {}, "similar_min_score": 0.85,
        "max_matches_shown": 5, "conflict_check": {},
    }
    checker = ConflictChecker(
        repo,
        policy,
        semantic_search=lambda _query, _limit: {"results": [{
            "scenario_id": "legacy", "scenario_name": "Legacy", "is_active": True,
            "similarity_score": 0.91,
        }]},
    )
    report = checker.check_intent(intent(), provisional=True)
    assert report["provisional"] is True
    assert report["matches"][0]["relation"] == "SIMILAR"
    assert report["matches"][0]["action"] == "info"
    assert report["blocking"] is False


def test_conflict_checker_filters_parent_key_and_same_thread_and_hashes_report():
    parent = {
        "scenario_id": "parent",
        "scenario_key": "same-key",
        "scenario_name": "Parent",
        "version_no": 1,
        "thread_id": "other-thread",
        "signature_json": build_signature(intent())[0],
    }
    warning = {
        "scenario_id": "warning",
        "scenario_key": "other-key",
        "scenario_name": "Similar",
        "version_no": 2,
        "thread_id": "different-thread",
        "signature_json": build_signature(intent(thresholds=[{"field": "transaction amount", "operator": ">", "value_from": 10000}]))[0],
    }
    same_thread = {**warning, "scenario_id": "self", "thread_id": "this-thread"}
    repo = type("FakeRepo", (), {"list_active_signatures": lambda self, _type: [parent, warning, same_thread]})()
    policy = {
        "relations": {"IDENTICAL": "block", "COVERED": "warn_confirm", "COVERS": "warn_confirm", "OVERLAP": "warn_confirm", "RELATED": "info", "SIMILAR": "info"},
        "dimension_weights": {"scenario_type": 3, "grain": 3, "metric": 3},
        "max_matches_shown": 5,
        "similar_min_score": 0.85,
        "conflict_check": {},
    }
    report = ConflictChecker(repo, policy).check_intent(
        intent(), thread_id="this-thread", scenario_key="same-key", revision_id="rev-1"
    )
    assert report["status"] == "blocked"
    assert report["revision_id"] == "rev-1"
    assert [item["scenario_id"] for item in report["matches"]] == ["parent", "warning"]
    assert report["conflict_hash"]


def test_conflict_checker_returns_unavailable_on_registry_read_failure():
    repo = type("FailingRepo", (), {"list_active_signatures": lambda self, _type: (_ for _ in ()).throw(RuntimeError("offline"))})()
    report = ConflictChecker(repo, {}).check_intent(intent())
    assert report["status"] == "unavailable"
    assert report["warning"]
