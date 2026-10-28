"""Offline regression tests for owner-run DWH maintenance primitives."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.aml_builder.web.services.dwh_scenario.toolchain import (
    build_experience_collection,
    build_schema_index,
    build_join_projection,
    harvest_dictionary,
    ingest_pairs,
    promote_versioned_collection,
    scoped_enrichment_sql,
    schema_records_from_oracle_metadata,
)


class _Collection:
    """Small Chroma fake with rename and count behavior."""

    def __init__(self, name, metadata, store):
        self.name, self.metadata, self.store = name, metadata, store
        self.items = []
        self.metadatas = []
        self.documents = []

    def add(self, **kwargs):
        self.items.extend(kwargs["ids"])
        self.metadatas.extend(kwargs.get("metadatas", []))
        self.documents.extend(kwargs.get("documents", []))

    def count(self):
        return len(self.items)

    def modify(self, *, name):
        self.store.pop(self.name)
        self.name = name
        self.store[name] = self

    def get(self, *, include):
        return {
            "ids": list(self.items),
            "documents": list(self.documents) if "documents" in include else None,
            "metadatas": list(self.metadatas) if "metadatas" in include else None,
        }


class _Chroma:
    """In-memory collection registry."""

    def __init__(self):
        self.collections = {}

    def create_collection(self, *, name, metadata):
        collection = _Collection(name, metadata, self.collections)
        self.collections[name] = collection
        return collection

    def get_collection(self, name):
        return self.collections[name]

    def delete_collection(self, name):
        del self.collections[name]


class _Embeddings:
    """Deterministic fake embedder that rejects unexpected empty calls."""

    def embed_documents(self, texts):
        assert texts
        return [[float(len(text)), 1.0] for text in texts]


class _Identity:
    provider = "fake"
    model = "fake-model"
    dimension = 2
    fingerprint = "fake-fingerprint"


def test_harvest_stream_is_bounded_and_manifested(tmp_path: Path) -> None:
    """Harvest writes deterministic JSONL and rejects one row over its cap."""
    path = tmp_path / "dictionary.jsonl"
    manifest = harvest_dictionary(iter([{"a": 1}, {"a": 2}]), path, max_rows=2)
    assert manifest["row_count"] == 2
    assert json.loads(path.read_text(encoding="utf-8").splitlines()[0]) == {"a": 1}
    assert path.with_suffix(".jsonl.manifest.json").exists()
    with pytest.raises(ValueError, match="row limit"):
        harvest_dictionary(iter([{}, {}, {}]), tmp_path / "too-many.jsonl", max_rows=2)


def test_harvest_cli_dry_run_uses_configured_scope_without_connecting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The owner-run command validates scope before any connection is opened."""
    from scripts.dwh_scenario import cli

    monkeypatch.setattr(cli.settings, "DWH_SQL_ALLOWED_SCHEMAS", ("CLIENT",))
    monkeypatch.setattr(cli.settings, "DWH_SQL_ALLOWED_OBJECTS", ("CLIENT.TX",))
    assert (
        cli.main(
            ["harvest-dictionary", "--output", str(tmp_path / "m.jsonl"), "--dry-run"]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["connected"] is False


def test_harvest_cli_uses_dwh_reader_pool_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Live metadata harvesting routes through the DWH pool, never registry pool."""
    from scripts.dwh_scenario import cli
    from services.aml_builder.web.adapters import dwh_oracle_repo
    from services.aml_builder.web.services import oracle

    monkeypatch.setattr(cli.settings, "DWH_SQL_ALLOWED_SCHEMAS", ("CLIENT",))
    monkeypatch.setattr(cli.settings, "DWH_SQL_ALLOWED_OBJECTS", ("CLIENT.TX",))
    calls: list[str] = []
    monkeypatch.setattr(oracle, "init_shadow_pool", lambda: calls.append("dwh-open"))
    monkeypatch.setattr(
        oracle, "close_shadow_pool", lambda: calls.append("dwh-close")
    )
    monkeypatch.setattr(
        oracle,
        "init_pool",
        lambda: pytest.fail("registry pool must not serve DWH discovery"),
    )
    monkeypatch.setattr(
        oracle,
        "close_pool",
        lambda: pytest.fail("registry pool must not be closed by DWH discovery"),
    )
    monkeypatch.setattr(
        dwh_oracle_repo.DwhOracleRepository,
        "read_schema_metadata",
        lambda self, *args, **kwargs: (
            [
                "OWNER",
                "TABLE_NAME",
                "COLUMN_NAME",
                "DATA_TYPE",
                "DATA_LENGTH",
                "DATA_PRECISION",
                "DATA_SCALE",
                "NULLABLE",
                "TABLE_COMMENT",
                "COLUMN_COMMENT",
                "TARGET_OWNER",
                "TARGET_NAME",
                "OBJECT_TYPE",
            ],
            [
                (
                    "CLIENT",
                    "TX",
                    "ID",
                    "NUMBER",
                    22,
                    None,
                    None,
                    "N",
                    None,
                    None,
                    "CLIENT",
                    "TX",
                    "TABLE",
                )
            ],
        ),
    )
    output = tmp_path / "metadata.jsonl"
    assert cli.main(["harvest-dictionary", "--output", str(output)]) == 0
    assert calls == ["dwh-open", "dwh-close"]
    assert json.loads(capsys.readouterr().out)["row_count"] == 2


def test_oracle_dictionary_records_use_only_actual_metadata_and_client_scope() -> None:
    headers = [
        "OWNER",
        "TABLE_NAME",
        "COLUMN_NAME",
        "DATA_TYPE",
        "DATA_LENGTH",
        "DATA_PRECISION",
        "DATA_SCALE",
        "NULLABLE",
        "TABLE_COMMENT",
        "COLUMN_COMMENT",
        "TARGET_OWNER",
        "TARGET_NAME",
        "OBJECT_TYPE",
    ]
    tables, columns, found = schema_records_from_oracle_metadata(
        headers,
        [
            (
                "CLIENT",
                "TX",
                "AMOUNT",
                "NUMBER",
                22,
                12,
                2,
                "N",
                None,
                "Booked amount",
                "CLIENT",
                "TX",
                "TABLE",
            )
        ],
        allowed_schemas=("CLIENT",),
        allowed_objects=("CLIENT.TX",),
        internal_objects=(),
    )
    assert found == {"CLIENT.TX"}
    assert "Booked amount" in columns[0]["text"]
    assert "NUMBER" in columns[0]["text"] and "precision=12" in columns[0]["text"]
    assert tables[0]["metadata"]["object_category"] == "business"
    assert columns[0]["metadata"]["source"] == "oracle_dictionary"
    with pytest.raises(ValueError, match="exceeded configured object allowlist"):
        schema_records_from_oracle_metadata(
            headers,
            [
                (
                    "OTHER",
                    "SECRET",
                    "X",
                    "VARCHAR2",
                    2,
                    None,
                    None,
                    "Y",
                    None,
                    None,
                    "OTHER",
                    "SECRET",
                    "TABLE",
                )
            ],
            allowed_schemas=("CLIENT",),
            allowed_objects=("CLIENT.TX",),
        )


def test_schema_metadata_query_is_bound_bounded_and_policy_checked() -> None:
    from contextlib import contextmanager
    from services.aml_builder.web.adapters.dwh_oracle_repo import (
        DwhOracleRepository,
        OracleRequestBudget,
    )

    class Cursor:
        call_timeout = 0
        description = [
            (name,)
            for name in (
                "OWNER",
                "TABLE_NAME",
                "COLUMN_NAME",
                "DATA_TYPE",
                "DATA_LENGTH",
                "DATA_PRECISION",
                "DATA_SCALE",
                "NULLABLE",
                "TABLE_COMMENT",
                "COLUMN_COMMENT",
                "TARGET_OWNER",
                "TARGET_NAME",
                "OBJECT_TYPE",
            )
        ]
        sql = ""
        params = None

        def execute(self, sql, params):
            self.sql, self.params = sql, params

        def fetchmany(self, count):
            self.fetch_count = count
            return []

    cursor = Cursor()

    class Connection:
        def cursor(self):
            return cursor

    @contextmanager
    def connection_provider():
        yield Connection()

    repo = DwhOracleRepository(
        connection_provider=connection_provider,
        allowed_schemas=("BI_DWH",),
        read_timeout_seconds=10,
    )
    headers, rows = repo.read_schema_metadata(
        ("BI_DWH",),
        ("BI_DWH.TX", "BI_DWH.REF"),
        budget=OracleRequestBudget(max_statements=1, deadline_seconds=20),
        max_rows=50,
    )
    assert not rows and headers[0] == "OWNER"
    assert "ALL_OBJECTS" in cursor.sql and "ALL_SYNONYMS" in cursor.sql
    assert "ALL_TAB_COLUMNS" in cursor.sql and "ALL_COL_COMMENTS" in cursor.sql
    assert "FETCH FIRST 50 ROWS ONLY" in cursor.sql
    assert cursor.params == {"schema_0": "BI_DWH", "object_0": "TX", "object_1": "REF"}
    assert cursor.fetch_count == 51 and cursor.call_timeout <= 10000
    repo.read_schema_object_resolution(
        ("BI_DWH",),
        ("BI_DWH.TX",),
        budget=OracleRequestBudget(max_statements=1, deadline_seconds=20),
        max_rows=25,
    )
    assert "ALL_OBJECTS" in cursor.sql and "ALL_SYNONYMS" in cursor.sql
    assert cursor.params == {"schema_0": "BI_DWH", "object_0": "TX"}
    assert "FETCH FIRST 25 ROWS ONLY" in cursor.sql and cursor.fetch_count == 26


def test_experience_index_accepts_empty_input(tmp_path: Path) -> None:
    """An empty experience source creates/promotes a valid zero-count collection."""
    client = _Chroma()
    previous = build_experience_collection(
        client, [], _Embeddings(), _Identity(), institution_id="tenant-a"
    )
    assert previous is None
    assert client.get_collection("experience_templates").count() == 0


def test_schema_index_is_configurable_versioned_and_tenant_scoped() -> None:
    from services.aml_builder.web.services.settings import Settings

    client = _Chroma()
    config = Settings(
        ORACLE_DSN="unused",
        ORACLE_USER="unused",
        ORACLE_PASSWORD="unused",
        DWH_SQL_ALLOWED_SCHEMAS=("BI_DWH",),
        DWH_SQL_ALLOWED_OBJECTS=("BI_DWH.PIO_TRANSACTIONS",),
        DWH_SQL_SCHEMA_TABLES_COLLECTION="client_tables",
        DWH_SQL_SCHEMA_COLUMNS_COLLECTION="client_columns",
        _env_file=None,
    )
    build_schema_index(
        client,
        [
            {
                "id": "t1",
                "text": "table text",
                "metadata": {"table_name": "BI_DWH.PIO_TRANSACTIONS"},
            }
        ],
        [
            {
                "id": "c1",
                "text": "column text",
                "metadata": {
                    "institution_id": "tenant-a",
                    "table_name": "BI_DWH.PIO_TRANSACTIONS",
                },
            }
        ],
        _Embeddings(),
        _Identity(),
        institution_id="tenant-a",
        config=config,
    )
    tables = client.get_collection("client_tables")
    columns = client.get_collection("client_columns")
    assert tables.metadatas == [
        {"table_name": "BI_DWH.PIO_TRANSACTIONS", "institution_id": "tenant-a"}
    ]
    assert columns.metadatas == [
        {"institution_id": "tenant-a", "table_name": "BI_DWH.PIO_TRANSACTIONS"}
    ]
    assert (
        tables.metadata["schema_index_version"]
        == columns.metadata["schema_index_version"]
    )


def test_schema_staging_requires_exact_source_signature_before_promotion() -> None:
    """A staged record mutation fails before the existing active index moves."""
    from services.aml_builder.web.services.settings import Settings

    class MutatingCollection(_Collection):
        def add(self, **kwargs):
            super().add(**kwargs)
            if "_build_" in self.name:
                self.documents[0] = "mutated"

    class MutatingChroma(_Chroma):
        def create_collection(self, *, name, metadata):
            collection = MutatingCollection(name, metadata, self.collections)
            self.collections[name] = collection
            return collection

    config = Settings(
        ORACLE_DSN="unused",
        ORACLE_USER="unused",
        ORACLE_PASSWORD="unused",
        DWH_SQL_ALLOWED_SCHEMAS=("CLIENT",),
        DWH_SQL_ALLOWED_OBJECTS=("CLIENT.TX",),
        DWH_SQL_SCHEMA_TABLES_COLLECTION="client_tables",
        DWH_SQL_SCHEMA_COLUMNS_COLLECTION="client_columns",
        _env_file=None,
    )
    client = MutatingChroma()
    active = client.create_collection(name="client_tables", metadata={"sentinel": 1})
    active.add(ids=["old"], documents=["old document"], metadatas=[{}])
    with pytest.raises(ValueError, match="source signature"):
        build_schema_index(
            client,
            [{"id": "t1", "text": "table", "metadata": {"table_name": "CLIENT.TX"}}],
            [{"id": "c1", "text": "column", "metadata": {"table_name": "CLIENT.TX"}}],
            _Embeddings(),
            _Identity(),
            institution_id="tenant-a",
            config=config,
        )
    assert client.get_collection("client_tables").items == ["old"]
    assert not any("_build_" in name for name in client.collections)


def test_schema_index_rejects_cross_tenant_source_before_writing() -> None:
    from services.aml_builder.web.services.settings import Settings

    client = _Chroma()
    config = Settings(
        ORACLE_DSN="unused",
        ORACLE_USER="unused",
        ORACLE_PASSWORD="unused",
        DWH_SQL_ALLOWED_SCHEMAS=("BI_DWH",),
        DWH_SQL_ALLOWED_OBJECTS=("BI_DWH.PIO_TRANSACTIONS",),
        _env_file=None,
    )
    with pytest.raises(ValueError, match="does not match index build"):
        build_schema_index(
            client,
            [
                {
                    "id": "t1",
                    "text": "table",
                    "metadata": {
                        "institution_id": "tenant-b",
                        "table_name": "BI_DWH.PIO_TRANSACTIONS",
                    },
                }
            ],
            [],
            _Embeddings(),
            _Identity(),
            institution_id="tenant-a",
            config=config,
        )
    assert client.collections == {}


def test_schema_index_rejects_partial_configured_allowlist_before_staging() -> None:
    from services.aml_builder.web.services.settings import Settings

    client = _Chroma()
    config = Settings(
        ORACLE_DSN="unused",
        ORACLE_USER="unused",
        ORACLE_PASSWORD="unused",
        DWH_SQL_ALLOWED_SCHEMAS=("BI_DWH",),
        DWH_SQL_ALLOWED_OBJECTS=("BI_DWH.TX", "BI_DWH.REFERENCE"),
        _env_file=None,
    )
    with pytest.raises(ValueError, match="incomplete for configured allowlist"):
        build_schema_index(
            client,
            [{"id": "t", "text": "table", "metadata": {"table_name": "BI_DWH.TX"}}],
            [],
            _Embeddings(),
            _Identity(),
            institution_id="tenant-a",
            config=config,
        )
    assert client.collections == {}


def test_failed_promotion_keeps_live_collection_and_reports_failure() -> None:
    """A bad staging count never renames or deletes the active collection."""
    client = _Chroma()
    active = client.create_collection(name="schema_tables", metadata={})
    active.add(ids=["old"])
    client.create_collection(name="stage", metadata={})
    with pytest.raises(ValueError, match="count"):
        promote_versioned_collection(
            client,
            temporary_name="stage",
            live_name="schema_tables",
            expected_count=1,
            expected_manifest={"embedding_provider": "fake"},
        )
    assert client.get_collection("schema_tables").count() == 1


def test_ingest_pairs_normalizes_and_deduplicates(tmp_path: Path) -> None:
    """Future officer format converts to canonical, deduplicated JSONL."""
    source, output = tmp_path / "pairs.jsonl", tmp_path / "canonical.jsonl"
    source.write_text(
        '{"scenario_description":"Check A","sql":"SELECT 1"}\n'
        '{"question":"Check A","sql":"SELECT 1"}\n',
        encoding="utf-8",
    )
    assert ingest_pairs(source, output) == 1
    row = json.loads(output.read_text(encoding="utf-8"))
    assert row["question"] == "Check A" and row["sql"] == "SELECT 1"


def test_join_projection_is_bounded_and_manifested(tmp_path: Path) -> None:
    """Join projection export validates the row cap and writes provenance."""
    output = tmp_path / "joins.jsonl"
    manifest = build_join_projection(
        [{"fact_table": "TX", "ref_table": "CUSTOMER", "formula": "TX.C=C.C"}],
        output,
        max_rows=1,
    )
    assert manifest["row_count"] == 1
    assert output.with_suffix(".jsonl.manifest.json").exists()
    original = output.read_text(encoding="utf-8")
    with pytest.raises(FileExistsError, match="cannot be overwritten"):
        build_join_projection(
            [{"fact_table": "X", "ref_table": "Y"}], output, max_rows=1
        )
    assert output.read_text(encoding="utf-8") == original
    with pytest.raises(ValueError, match="row limit"):
        build_join_projection(
            [
                {"fact_table": "A", "ref_table": "B"},
                {"fact_table": "C", "ref_table": "D"},
            ],
            tmp_path / "overflow.jsonl",
            max_rows=1,
        )


def test_enrichment_literals_escape_and_scope_duplicate_column_names() -> None:
    """Same column in multiple tables receives independent safely quoted updates."""
    backup, updates, reverse = scoped_enrichment_sql(
        [
            {
                "table_name": "T1",
                "column_name": "STATUS",
                "project_code": "AML",
                "old": "owner's",
                "new": "current",
            },
            {
                "table_name": "T2",
                "column_name": "STATUS",
                "project_code": "AML",
                "old": "old",
                "new": "new",
            },
        ],
        description_column="OBJECT_NAME",
        project_column="PROJECT_CODE",
    )
    assert updates.count("TABLE_NAME = '") == 2
    assert "TABLE_NAME = 'T1' AND COLUMN_NAME = 'STATUS'" in updates
    assert "TABLE_NAME = 'T2' AND COLUMN_NAME = 'STATUS'" in updates
    assert "owner''s" in reverse
    assert "PROJECT_CODE = 'AML'" in backup
