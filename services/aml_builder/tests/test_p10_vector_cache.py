"""Offline cache-key and stale-vector behavior checks."""

from datetime import datetime, timezone

import numpy as np

from services.aml_builder.web.services import explanation_code_search as search
from services.aml_builder.web.services import oracle


def test_vector_cache_key_uses_update_timestamp_when_available(monkeypatch) -> None:
    """A visible update column selects row-count plus maximum timestamp."""
    calls: list[str] = []

    def fake_readonly(sql: str, params=None):
        calls.append(sql)
        if "USER_TAB_COLUMNS" in sql:
            return ["COLUMN_NAME", "DATA_TYPE"], [("UPDATED_AT", "TIMESTAMP")]
        return ["ROW_COUNT", "MAX_UPDATED_AT"], [
            (2, datetime(2026, 1, 2, tzinfo=timezone.utc))
        ]

    monkeypatch.setattr(search, "run_readonly", fake_readonly)
    key = search._get_vector_cache_key()
    assert key == {
        "basis": "row_count+max:UPDATED_AT",
        "row_count": 2,
        "max_updated_at": "2026-01-02T00:00:00+00:00",
    }
    assert len(calls) == 2
    assert all(sql.lstrip().upper().startswith("SELECT") for sql in calls)


def test_vector_cache_key_falls_back_to_checksum(monkeypatch) -> None:
    """Catalogs without a known timestamp use the read-only row checksum."""
    def fake_readonly(sql: str, params=None):
        if "USER_TAB_COLUMNS" in sql:
            return ["COLUMN_NAME"], []
        return ["ROW_COUNT", "ROW_CHECKSUM"], [(7, 12345)]

    monkeypatch.setattr(search, "run_readonly", fake_readonly)
    assert search._get_vector_cache_key() == {
        "basis": "row_count+checksum",
        "row_count": 7,
        "checksum": 12345,
    }


def test_failed_cache_key_lookup_rebuilds_instead_of_returning_memory_cache(
    monkeypatch, tmp_path
) -> None:
    """A fingerprint outage does not serve a known stale in-memory index."""
    monkeypatch.setattr(search, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(search, "_VECTOR_CACHE", {"metadata": [{"code": "stale"}]})
    monkeypatch.setattr(search, "_get_vector_cache_key", lambda: (_ for _ in ()).throw(RuntimeError("offline")))
    monkeypatch.setattr(oracle, "run_dwh_readonly", lambda *_args, **_kwargs: ([], [("fresh", "Fresh", None, "Long")]))

    class FakeEmbeddings:
        def __init__(self, **_kwargs):
            pass

        def embed_documents(self, _texts):
            return [[1.0, 0.0]]

    monkeypatch.setattr(search, "OpenAIEmbeddings", FakeEmbeddings)
    monkeypatch.setattr(search, "settings", type("Settings", (), {"AML_COUNTRY_CODE": "400", "AML_INST_CODE": "1", "OPENAI_API_KEY": "fake"})())

    index = search._load_or_build_vector_index()
    assert index["metadata"][0]["code"] == "fresh"
    assert index["metadata"][0]["code"] != "stale"
    assert np.allclose(index["embeddings"], np.array([[1.0, 0.0]], dtype=np.float32))

