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

from services.aml_builder.web.api.routes import chat, sessions, engine, deploy
from services.aml_builder.web.services.graph import get_tool_driven_graph, close_checkpointer
from services.aml_builder.web.services.logging_config import setup_logging
from services.aml_builder.web.services.oracle import close_pool, close_shadow_pool, init_pool, init_shadow_pool
from services.aml_builder.web.services.session_store import init_session_db
from services.aml_builder.web.services.settings import settings

logger = logging.getLogger(__name__)


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
    setup_logging()
    logger.info("[STARTUP] AML Builder service starting. port=%d", settings.SERVICE_PORT)

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
    close_pool()
    close_shadow_pool()
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
    allow_origins=["*"],  # Restrict in production to known frontend origins
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chat.router)
app.include_router(sessions.router)
app.include_router(engine.router)
app.include_router(deploy.router)


# =============================================================================
# HEALTH
# =============================================================================


@app.get("/health", tags=["System"])
async def health_check() -> dict:
    """Health check endpoint for Docker and load balancer probes.

    Returns:
        dict: Service status and version.
    """
    return {
        "status": "healthy",
        "service": settings.SERVICE_NAME,
        "version": "1.0.0",
    }
