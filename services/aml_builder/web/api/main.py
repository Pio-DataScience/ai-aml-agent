"""
FastAPI entry point for the AML Builder agent service.

Exposes:
  POST /chat/stream  — SSE streaming chat with the AML scenario agent
  GET  /health       — Health check (used by Docker / load balancer)

Follows the same API contract as the existing PioTech AI services
(AML reporting, DWH) for frontend compatibility.
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from services.aml_builder.web.api.routes import chat, sessions, engine, deploy, me, workflow
from services.aml_builder.web.services.graph import get_tool_driven_graph, close_checkpointer
from services.aml_builder.web.services.logging_config import setup_logging
from services.aml_builder.web.services.oracle import close_pool, close_shadow_pool, init_pool, init_shadow_pool
from services.aml_builder.web.services.session_store import init_session_db
from services.aml_builder.web.services.io_executor import (
    close_io_executor,
    initialize_io_executor,
)
from services.aml_builder.web.services.settings import (
    configure_langsmith_environment,
    settings,
)
from services.aml_builder.web.services.config_loader import load_catalogs
from services.aml_builder.web.services.dwh_scenario.retrieval import readiness_report
from services.aml_builder.web.services.dwh_scenario.retrieval import build_readiness_checks
from services.aml_builder.web.services import oracle as oracle_service

logger = logging.getLogger(__name__)


def ensure_service_auth_token(service_token: str | None) -> None:
    """Fail startup with clear guidance when the BFF shared secret is missing.

    Args:
        service_token: Configured Service A/BFF shared token.

    Raises:
        RuntimeError: When no non-empty token is configured.
    """
    if not service_token:
        raise RuntimeError(
            "SERVICE_AUTH_TOKEN is required. Configure the same server-side token in the UI and Service A."
        )


def _oracle_readiness() -> dict[str, str]:
    """Check the initialized Oracle pool without exposing connection details."""
    pool = oracle_service._pool
    if pool is None:
        return {"status": "down"}
    connection = None
    try:
        connection = pool.acquire()
        connection.ping()
        return {"status": "ok"}
    except Exception as exc:
        return {"status": "down", "error": type(exc).__name__}
    finally:
        if connection is not None:
            pool.release(connection)


# =============================================================================
# LIFESPAN — Startup / Shutdown
# =============================================================================


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifecycle: initialize and clean up shared resources.

    Args:
        app (FastAPI): The FastAPI application instance.

    Yields:
        None: Control returns to FastAPI during the app's lifetime.
    """
    # Startup
    ensure_service_auth_token(settings.SERVICE_AUTH_TOKEN)
    configure_langsmith_environment(settings)
    load_catalogs(settings.CATALOG_DIR)
    setup_logging()
    initialize_io_executor()
    logger.info("[STARTUP] AML Builder service starting. port=%d", settings.SERVICE_PORT)

    app.state.readiness_checks, graph_reranker = build_readiness_checks(
        settings,
        _oracle_readiness,
    )
    app.state.readiness_checks["neo4j"] = graph_reranker.readiness
    app.state.graph_reranker = graph_reranker
    app.state.startup_readiness = readiness_report(
        app.state.readiness_checks["oracle"],
        app.state.readiness_checks["chroma"],
        app.state.readiness_checks["neo4j"],
    )

    # Init sessions metadata table
    init_session_db()

    # Initialize Oracle pools (Primary & Shadow test DB)
    try:
        init_pool()
        init_shadow_pool()
    except Exception as exc:
        logger.error("[STARTUP] Oracle pool init failed: %s", exc)
        # Non-fatal — service can still run and return meaningful errors

    # Pre-warm the LangGraph
    try:
        await get_tool_driven_graph()
        logger.info("[STARTUP] Tool-driven ReAct graph compiled and warmed.")
    except Exception as exc:
        logger.error("[STARTUP] LangGraph compilation failed: %s", exc)

    yield

    # Shutdown
    logger.info("[SHUTDOWN] Closing SQLite checkpointer connection.")
    try:
        await close_checkpointer()
    except Exception as exc:
        logger.error("[SHUTDOWN] Error closing checkpointer: %s", exc)

    logger.info("[SHUTDOWN] Closing Oracle pools.")
    app.state.graph_reranker.close()
    close_pool()
    close_shadow_pool()
    close_io_executor()
    logger.info("[SHUTDOWN] AML Builder service stopped.")


# =============================================================================
# APP
# =============================================================================


app = FastAPI(
    title="PioTech AI — AML Scenario Builder",
    description=(
        "Autonomous multi-agent system that converts compliance manager intent "
        "into live, validated AML detection scenarios in the PioTech Oracle QB engine."
    ),
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        origin.strip()
        for origin in settings.CORS_ALLOWED_ORIGINS.split(",")
        if origin.strip()
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chat.router)
app.include_router(sessions.router)
app.include_router(engine.router)
app.include_router(deploy.router)
app.include_router(me.router)
app.include_router(workflow.router)


# =============================================================================
# HEALTH
# =============================================================================


@app.get("/health", tags=["System"])
async def health_check() -> dict:
    """Health check endpoint for Docker and load balancer probes.

    Returns:
        dict: Service status and version.
    """
    checks = getattr(app.state, "readiness_checks", {})
    report = readiness_report(
        checks.get("oracle", {"status": "unknown"}),
        checks.get("chroma", {"schema_tables": {"status": "unconfigured"}, "schema_columns": {"status": "unconfigured"}, "experience_templates": {"status": "empty"}}),
        checks.get("neo4j", {"status": "disabled"}),
    )
    return {
        **report,
        "service": settings.SERVICE_NAME,
        "version": "1.0.0",
    }
