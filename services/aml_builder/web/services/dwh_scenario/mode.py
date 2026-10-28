"""Mode selection and isolated shadow evaluation for DWH SQL generators."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

from services.aml_builder.web.domain.dwh_scenario.contracts import (
    ScenarioSqlGenerator,
    ScenarioSqlRequest,
    ScenarioSqlResult,
)
from services.aml_builder.web.services.dwh_scenario.http_adapter import (
    HttpScenarioSqlGenerator,
)
from services.aml_builder.web.services.settings import Settings, settings

logger = logging.getLogger(__name__)


def build_local_generator(
    config: Settings = settings, *, repository: Any | None = None
) -> ScenarioSqlGenerator:
    """Build the optional local generator lazily from configured adapters.

    Args:
        config: Active DWH and application settings.
        repository: Optional injected repository. B8 acceptance uses this to
            keep local candidate execution on its dedicated read-only pool.

    Returns:
        Fully injected local generator; optional dependencies are loaded on demand.

    Raises:
        RuntimeError: If local dependencies or configuration are unavailable.
    """
    from services.aml_builder.web.adapters.dwh_oracle_repo import DwhOracleRepository
    from services.aml_builder.web.services.dwh_scenario.embeddings import (
        EmbeddingProviderManager,
        embedding_identity,
    )
    from services.aml_builder.web.services.dwh_scenario.graph import (
        build_local_generator as build,
    )
    from services.aml_builder.web.services.dwh_scenario.retrieval import (
        ChromaRetriever,
        IndexManifest,
        Neo4jReranker,
    )
    from services.aml_builder.web.services.dwh_scenario.toolchain import (
        validate_schema_collection_versions,
    )
    from services.aml_builder.web.services.llm_client import build_llm

    if config.DWH_SQL_CHROMA_PERSIST_DIR is None:
        raise RuntimeError(
            "DWH_SQL_CHROMA_PERSIST_DIR is required for local generation"
        )
    try:
        import chromadb
    except ImportError as exc:
        raise RuntimeError(
            "optional chromadb package is required for local generation"
        ) from exc
    client = chromadb.PersistentClient(path=str(config.DWH_SQL_CHROMA_PERSIST_DIR))

    def collection_provider(name: str) -> Any:
        try:
            return client.get_collection(name=name)
        except Exception as exc:
            if type(exc).__name__ == "ChromaNotFoundError":
                raise KeyError(name) from exc
            raise

    identity = embedding_identity(config)
    embedding_provider = EmbeddingProviderManager(config)
    try:
        table_collection = collection_provider(config.DWH_SQL_SCHEMA_TABLES_COLLECTION)
        column_collection = collection_provider(
            config.DWH_SQL_SCHEMA_COLUMNS_COLLECTION
        )
        validate_schema_collection_versions((table_collection, column_collection))
        for collection in (table_collection, column_collection):
            manifest = IndexManifest.from_metadata(collection.metadata or {})
            manifest.validate(identity)
            if str(manifest.institution_id or "") != str(config.AML_INST_CODE):
                raise ValueError(
                    "Chroma schema index institution scope is incompatible"
                )
            if int(collection.count()) < 1:
                raise ValueError("Required Chroma schema collection is empty")
    except Exception as exc:
        raise RuntimeError(
            "Configured Chroma schema indexes are missing, empty, or incompatible "
            "by version, embedding identity, or institution scope"
        ) from exc
    reranker = Neo4jReranker(config) if config.DWH_SQL_NEO4J_ENABLED else None
    if config.DWH_SQL_NEO4J_REQUIRED and reranker is None:
        raise RuntimeError("Neo4j is required by configuration but is disabled")
    if reranker is not None and config.DWH_SQL_NEO4J_REQUIRED:
        status = reranker.readiness()
        if status.get("status") != "ok":
            raise RuntimeError("Required Neo4j graph is not ready")
    return build(
        llm_factory=lambda *, role: build_llm(role=role),
        repository=(repository if repository is not None else DwhOracleRepository()),
        experience_retriever=ChromaRetriever(
            config.DWH_SQL_EXPERIENCE_COLLECTION,
            collection_provider,
            identity,
            query_embedder=embedding_provider.embed_query,
            optional_empty=True,
            threshold=config.DWH_SQL_RETRIEVAL_THRESHOLD,
        ),
        table_retriever=ChromaRetriever(
            config.DWH_SQL_SCHEMA_TABLES_COLLECTION,
            collection_provider,
            identity,
            query_embedder=embedding_provider.embed_query,
            threshold=config.DWH_SQL_RETRIEVAL_THRESHOLD,
        ),
        column_retriever=ChromaRetriever(
            config.DWH_SQL_SCHEMA_COLUMNS_COLLECTION,
            collection_provider,
            identity,
            query_embedder=embedding_provider.embed_query,
            threshold=config.DWH_SQL_RETRIEVAL_THRESHOLD,
        ),
        reranker=reranker,
        config=config,
    )


async def generate_by_mode(
    request: ScenarioSqlRequest,
    *,
    http_generator: ScenarioSqlGenerator | None = None,
    local_factory: Callable[[], ScenarioSqlGenerator] | None = None,
    config: Settings = settings,
) -> ScenarioSqlResult:
    """Generate through the selected mode, preserving HTTP authority in shadow.

    Args:
        request: Confirmed, typed scenario request.
        http_generator: Optional injected HTTP implementation.
        local_factory: Optional lazy factory for the in-process implementation.
        config: Active application configuration.

    Returns:
        The selected result; shadow mode always returns the HTTP result.

    Raises:
        ValueError: If shadow mode is selected in production.
        RuntimeError: If local mode is selected but cannot be initialized.
    """
    http = http_generator or HttpScenarioSqlGenerator()
    mode = config.DWH_SQL_MODE
    if mode == "http":
        return await http.generate(request)
    if mode == "shadow" and config.DWH_SQL_ENVIRONMENT.casefold() == "production":
        raise ValueError("DWH_SQL_MODE=shadow is forbidden in production")
    if local_factory is None:
        local_factory = lambda: build_local_generator(config)
    if mode == "inprocess":
        return await local_factory().generate(request)

    # Local SQL, approval, evidence and errors remain isolated in this task.
    # Only a redacted structural summary is logged; timeout/failure cannot alter HTTP.
    http_task = asyncio.create_task(_run_http(http, request))
    local_task = asyncio.create_task(_run_local(local_factory, request))
    http_result, local_summary = await asyncio.gather(
        http_task,
        asyncio.wait_for(local_task, timeout=config.DWH_SQL_SHADOW_TIMEOUT_SECONDS),
        return_exceptions=True,
    )
    if isinstance(http_result, BaseException):
        raise http_result
    http_result, http_elapsed_ms = http_result
    if isinstance(local_summary, BaseException):
        local_summary = {"error": type(local_summary).__name__}
    try:
        comparison = compare_results(request, http_result, local_summary)
        comparison["latency_ms"]["http"] = http_elapsed_ms
        logger.info("DWH SQL shadow comparison: %s", comparison)
    except Exception as exc:  # Comparison is observational and must not affect HTTP.
        logger.warning("DWH SQL shadow comparison unavailable: %s", type(exc).__name__)
    return http_result


async def _run_local(
    factory: Callable[[], ScenarioSqlGenerator], request: ScenarioSqlRequest
) -> dict[str, Any]:
    """Run local generation and return only metrics plus structural observations."""
    started = time.monotonic()
    generator = await asyncio.to_thread(factory)
    result = await generator.generate(request)
    return {
        "result": result,
        "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
    }


async def _run_http(
    generator: ScenarioSqlGenerator, request: ScenarioSqlRequest
) -> tuple[ScenarioSqlResult, float]:
    """Time HTTP generation without changing its result or error contract."""
    started = time.monotonic()
    result = await generator.generate(request)
    return result, round((time.monotonic() - started) * 1000, 3)


def compare_results(
    request: ScenarioSqlRequest,
    http_result: ScenarioSqlResult,
    local_summary: dict[str, Any],
) -> dict[str, Any]:
    """Compare structural SQL properties without returning SQL or customer data.

    Args:
        request: Confirmed request shared by both generators.
        http_result: Active HTTP result.
        local_summary: Local result and elapsed time, kept inside shadow execution.

    Returns:
        JSON-safe discrepancy and metrics summary with no SQL text.
    """
    from sqlglot import exp, parse
    from services.aml_builder.web.domain.dwh_scenario.sql_policy import (
        SqlPolicyError,
        validate_oracle_read,
    )

    local_result = local_summary.get("result")
    if not isinstance(local_result, ScenarioSqlResult):
        local_result = ScenarioSqlResult(
            error=str(local_summary.get("error", "local failed"))
        )

    def facts(result: ScenarioSqlResult) -> dict[str, Any]:
        sql = result.sql or ""
        try:
            parsed = parse(sql, read="oracle")
            root = parsed[0] if len(parsed) == 1 and parsed[0] is not None else None
        except Exception:
            root = None
        valid = root is not None
        select_only = valid and isinstance(
            root, (exp.Select, exp.Union, exp.Intersect, exp.Except)
        )
        projection = bool(
            root
            and any(
                isinstance(col, exp.Column) and col.name.casefold() == "cus_num"
                for col in root.find_all(exp.Column)
            )
        )
        filters = [item.model_dump(mode="python") for item in request.confirmed_filters]
        try:
            validate_oracle_read(
                sql,
                allowed_schemas=(
                    tuple({table.db for table in root.find_all(exp.Table) if table.db})
                    if root
                    else ()
                ),
                allowed_objects=(
                    tuple(
                        {table.name for table in root.find_all(exp.Table)}
                        | {
                            f"{table.db}.{table.name}"
                            for table in root.find_all(exp.Table)
                            if table.db
                        }
                    )
                    if root
                    else ()
                ),
                required_projection="CUS_NUM",
                confirmed_filters=filters,
            )
            safe = True
        except (SqlPolicyError, ValueError):
            safe = False
        normalized = root.sql(dialect="oracle", normalize=True) if root else None
        return {
            "sql_parses": valid,
            "select_only": bool(select_only),
            "required_projection": projection,
            "sql_safe": safe,
            "confirmed_filter_count": len(filters) if safe else 0,
            "confirmed_filter_total": len(filters),
            "error": result.error is not None,
            "execution_evidence": result.execution.row_count is not None
            or result.execution.error is not None,
            "semantic_fingerprint": normalized,
            "metrics": result.metrics,
        }

    left, right = facts(http_result), facts(local_result)
    checks = (
        "sql_parses",
        "select_only",
        "sql_safe",
        "required_projection",
        "confirmed_filter_count",
        "confirmed_filter_total",
        "error",
        "execution_evidence",
    )
    discrepancies = [key for key in checks if left[key] != right[key]]
    if (
        left["semantic_fingerprint"]
        and right["semantic_fingerprint"]
        and left["semantic_fingerprint"] != right["semantic_fingerprint"]
    ):
        discrepancies.append("semantic_difference")
    # Semantic SQL differences are recorded as observations, not failed comparisons.
    return {
        "discrepancies": discrepancies,
        "http": {
            key: value for key, value in left.items() if key != "semantic_fingerprint"
        },
        "local": {
            key: value for key, value in right.items() if key != "semantic_fingerprint"
        },
        "latency_ms": {
            "http": http_result.metrics.get("latency_ms"),
            "local": local_summary.get(
                "elapsed_ms", local_result.metrics.get("latency_ms")
            ),
        },
    }
