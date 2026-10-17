"""Offline tests for exact-ID, SELECT-only legacy archive extraction."""

from typing import Any

from scripts.archive_legacy import collect_legacy_records


def test_archive_reads_only_legacy_ids_and_matching_rows() -> None:
    """Oracle reads are SELECT-only and scope shared rows to discovered IDs."""
    calls: list[tuple[str, dict[str, Any] | None]] = []

    def fake_query(
        sql: str, params: dict[str, Any] | None = None
    ) -> tuple[list[str], list[tuple[Any, ...]]]:
        calls.append((sql, params))
        normalized = sql.strip().upper()
        assert normalized.startswith("SELECT")
        if "PIO_AML_PRODUCTION_SCENARIOS" in normalized:
            return ["scenario_id", "scenario_name"], [("PRD_1234", "Example")]
        if "PIO_AML_SCENARIO" in normalized:
            assert params == {"id0": "PRD_1234"}
            assert "SCENARIO_CODE IN (:ID0)" in normalized
            return ["scenario_code"], [("PRD_1234",)]
        if "PIO_AML_CUSTOMERS_DET" in normalized:
            assert params == {"id0": "PRD_1234"}
            return ["row_count"], [(4,)]
        if "PIO_AML_CUSTOMERS" in normalized:
            assert params == {"id0": "PRD_1234"}
            return ["row_count"], [(2,)]
        raise AssertionError(f"Unexpected query: {sql}")

    records = collect_legacy_records(fake_query)

    assert records["scenario_ids"] == ["PRD_1234"]
    assert len(records["scenario_metadata_rows"]) == 1
    assert records["alert_counts"] == {
        "PIO_AML_CUSTOMERS": 2,
        "PIO_AML_CUSTOMERS_DET": 4,
    }
    assert len(calls) == 4
