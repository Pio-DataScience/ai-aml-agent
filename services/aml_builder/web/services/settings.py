"""
Settings module for the AML Builder service.

All configuration is sourced from environment variables or a .env file.
Never hardcode secrets or connection strings — per SOLID Dependency Inversion.
"""

from functools import lru_cache
import os
from pathlib import Path
from typing import Optional

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Central configuration for the AML Builder agent service.

    All values are read from environment variables. A .env file in the
    project root is automatically loaded when present.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ─── Service ──────────────────────────────────────────────────────────────
    SERVICE_NAME: str = Field(
        default="aml-builder",
        description="Identifier used in logs and LangSmith traces.",
    )
    SERVICE_PORT: int = Field(
        default=8005,
        description="Port this FastAPI service listens on.",
    )
    SERVICE_AUTH_TOKEN: Optional[str] = Field(
        default=None,
        description="Shared secret required on BFF-to-Service-A requests.",
    )
    CORS_ALLOWED_ORIGINS: str = Field(
        default="http://localhost:3000",
        description="Comma-separated browser origins permitted by CORS.",
    )
    DEBUG: bool = Field(
        default=False,
        description="Enables verbose debug logging when True.",
    )

    # ─── LLM ──────────────────────────────────────────────────────────────────
    LLM_PROVIDER: str = Field(
        default="openai",
        description="LLM backend: 'openai' or 'lmstudio'.",
    )
    LLM_BASE_URL: Optional[str] = Field(
        default=None,
        description="Base URL for local LLM (LM Studio). Ignored when provider=openai.",
    )
    LLM_MODEL: str = Field(
        default="gpt-4o",
        description="Primary model name for the orchestrator and intent analyst.",
    )
    LLM_MODEL_FAST: str = Field(
        default="gpt-4o-mini",
        description="Lightweight model for quick classification tasks.",
    )
    OPENAI_API_KEY: Optional[str] = Field(
        default=None,
        description="OpenAI API key. Required when LLM_PROVIDER=openai.",
    )
    LLM_TEMPERATURE: float = Field(
        default=0.0,
        description="Generation temperature. 0.0 for deterministic agent decisions.",
    )
    LLM_MAX_TOKENS: Optional[int] = Field(
        default=None,
        description="Optional max output tokens; unset preserves provider defaults.",
    )
    LLM_REASONING_EFFORT: Optional[str] = Field(
        default=None,
        description="Optional model-specific reasoning effort. Leave unset or use 'none' to omit it; accepted values vary by model.",
    )

    # ─── DWH SQL provider policy ───────────────────────────────────────────
    DWH_SQL_GENERATOR_PROVIDER: Optional[str] = None
    DWH_SQL_GENERATOR_MODEL: Optional[str] = None
    DWH_SQL_CRITIC_PROVIDER: Optional[str] = None
    DWH_SQL_CRITIC_MODEL: Optional[str] = None
    DWH_SQL_SUMMARIZER_PROVIDER: Optional[str] = None
    DWH_SQL_SUMMARIZER_MODEL: Optional[str] = None
    DWH_SQL_EMBEDDINGS_PROVIDER: str = "openai"
    DWH_SQL_EMBEDDINGS_MODEL: str = "text-embedding-3-small"
    DWH_SQL_EMBEDDINGS_DIMENSION: int = Field(default=1536, ge=1)
    DWH_SQL_EMBEDDINGS_FINGERPRINT: Optional[str] = None
    DWH_SQL_EMBEDDINGS_DEVICE: str = "cpu"
    DWH_SQL_CONNECT_TIMEOUT_SECONDS: float = Field(default=10.0, gt=0)
    DWH_SQL_READ_TIMEOUT_SECONDS: float = Field(default=120.0, gt=0)
    DWH_SQL_DEADLINE_SECONDS: float = Field(default=180.0, gt=0)
    DWH_SQL_RETRY_BUDGET: int = Field(default=2, ge=0)
    DWH_SQL_MAX_ATTEMPTS: int = Field(default=3, ge=1)
    DWH_SQL_OUTPUT_TOKEN_LIMIT: int = Field(default=4096, ge=1)
    DWH_SQL_CONTEXT_BUDGET: int = Field(default=32768, ge=1)
    DWH_SQL_MAX_CANDIDATE_ROWS: int = Field(default=100, ge=1)
    DWH_SQL_ALLOWED_SCHEMAS: tuple[str, ...] = ()
    DWH_SQL_OPENAI_API_KEY: Optional[str] = None
    DWH_SQL_COHERE_API_KEY: Optional[str] = None
    DWH_SQL_SAMBANOVA_API_KEY: Optional[str] = None
    DWH_SQL_OPENROUTER_API_KEY: Optional[str] = None
    DWH_SQL_BASE_URL: Optional[str] = None
    DWH_SQL_TOOL_CALLING: bool = True

    # ─── Oracle DB (Primary) ──────────────────────────────────────────────────
    ORACLE_DSN: str = Field(
        ...,
        description="Oracle Data Source Name (e.g., 'host:port/service_name').",
    )
    ORACLE_USER: str = Field(
        ...,
        description="Oracle database username.",
    )
    ORACLE_PASSWORD: str = Field(
        ...,
        description="Oracle database password.",
    )
    ORACLE_POOL_MIN: int = Field(
        default=2,
        description="Minimum connections in the Oracle connection pool.",
    )
    ORACLE_POOL_MAX: int = Field(
        default=10,
        description="Maximum connections in the Oracle connection pool.",
    )

    # ─── Shadow Oracle DB (Test Data / Shadow Testing) ────────────────────────
    SHADOW_ORACLE_DSN: Optional[str] = Field(
        default=None,
        description="Secondary Oracle DSN for shadow testing (5M+ test records). Falls back to ORACLE_DSN if unset.",
    )
    SHADOW_ORACLE_USER: Optional[str] = Field(
        default=None,
        description="Secondary Oracle username for shadow testing. Falls back to ORACLE_USER if unset.",
    )
    SHADOW_ORACLE_PASSWORD: Optional[str] = Field(
        default=None,
        description="Secondary Oracle password for shadow testing. Falls back to ORACLE_PASSWORD if unset.",
    )
    SHADOW_ORACLE_POOL_MIN: int = Field(
        default=2,
        description="Minimum connections in shadow Oracle connection pool.",
    )
    SHADOW_ORACLE_POOL_MAX: int = Field(
        default=10,
        description="Maximum connections in shadow Oracle connection pool.",
    )
    IO_WORKERS: int = Field(
        default=4,
        ge=1,
        description="Maximum workers for blocking adapter and state-store work.",
    )
    RETRIEVAL_CONCURRENCY: int = Field(
        default=3,
        ge=1,
        description="Maximum concurrent per-type explanation-code searches.",
    )

    # ─── AML Domain Defaults ──────────────────────────────────────────────────
    AML_COUNTRY_CODE: str = Field(
        default="400",
        description="Default country code for all AML scenario inserts.",
    )
    AML_INST_CODE: str = Field(
        default="1",
        description="Default institution code for all AML scenario inserts.",
    )
    AML_CREATED_BY: str = Field(
        default="999",
        description="Value written to CREATED_BY / UPDATED_BY on all agent-generated rows.",
    )

    # ─── PioTech AI (SQL Bridge) ───────────────────────────────────────────────
    PIOTECH_AI_URL: str = Field(
        default="http://localhost:8006/chat/stream",
        description="PioTech AI DWH agent streaming endpoint (text-to-SQL).",
    )
    PIOTECH_AI_TIMEOUT_SECONDS: int = Field(
        default=120,
        description="HTTP timeout when calling PioTech AI agent.",
    )
    PIOTECH_AI_USER_ID: str = Field(
        default="aml_builder_agent",
        description="user_id sent in PioTech AI requests for traceability.",
    )
    PIOTECH_AI_PROJECT_ID: str = Field(
        default="0",
        description="project_id sent in PioTech AI requests.",
    )

    # ─── LangGraph / State ────────────────────────────────────────────────────
    STATE_DIR: Path = Field(
        default=Path("artifacts/v2"),
        description="Directory for new-generation local state and catalogs.",
    )
    CHECKPOINT_DB_PATH: Optional[Path] = Field(
        default=None,
        description="Path to the SQLite file used by LangGraph's checkpointer.",
    )
    APP_STATE_DB_PATH: Optional[Path] = Field(
        default=None,
        description="Path to the new workflow/application state SQLite database.",
    )
    CATALOG_DIR: Path = Field(
        default=Path("config/aml_catalogs"),
        description="Directory containing versioned AML YAML catalogs.",
    )
    AGENT_RECURSION_LIMIT: int = Field(
        default=25,
        ge=1,
        description="Authoritative LangGraph recursion limit for chat requests.",
    )
    MAX_AGENT_ITERATIONS: int = Field(
        default=25,
        description="Reserved legacy setting; AGENT_RECURSION_LIMIT is authoritative.",
    )
    MAX_VALIDATION_RETRIES: int = Field(
        default=3,
        description="Max self-correction cycles in the Validator node.",
    )

    # ─── LangSmith (Observability) ─────────────────────────────────────────────
    LANGCHAIN_TRACING_V2: bool = Field(
        default=False,
        description="Enable LangSmith tracing when True.",
    )
    LANGCHAIN_API_KEY: Optional[str] = Field(
        default=None,
        description="LangSmith API key.",
    )
    LANGCHAIN_PROJECT: str = Field(
        default="ai-aml-agent",
        description="LangSmith project name for trace grouping.",
    )

    @model_validator(mode="after")
    def set_state_database_defaults(self) -> "Settings":
        """Place state databases under STATE_DIR unless explicitly overridden.

        Returns:
            Settings: The validated settings instance.
        """
        if self.CHECKPOINT_DB_PATH is None:
            self.CHECKPOINT_DB_PATH = self.STATE_DIR / "checkpoints.sqlite"
        if self.APP_STATE_DB_PATH is None:
            self.APP_STATE_DB_PATH = self.STATE_DIR / "app_state.sqlite"
        return self

    @field_validator("LLM_PROVIDER")
    @classmethod
    def validate_provider(cls, value: str) -> str:
        """Ensure LLM provider is one of the supported backends.

        Args:
            value (str): The configured provider string.

        Returns:
            str: The validated provider string (lowercased).

        Raises:
            ValueError: If provider is not 'openai' or 'lmstudio'.
        """
        allowed = {"openai", "lmstudio"}
        normalized = value.lower().strip()
        if normalized not in allowed:
            raise ValueError(f"LLM_PROVIDER must be one of {allowed}, got '{value}'.")
        return normalized


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the singleton settings instance (cached after first call).

    Returns:
        Settings: The fully validated settings object.
    """
    return Settings()


# Module-level convenience alias
settings: Settings = get_settings()


def configure_langsmith_environment(config: Settings = settings) -> None:
    """Expose BaseSettings LangSmith values to tracing libraries if unset.

    Args:
        config (Settings): Application settings loaded from environment or .env.

    Returns:
        None: Values are written to the process environment only when absent.
    """
    values = {
        "LANGCHAIN_TRACING_V2": str(config.LANGCHAIN_TRACING_V2).lower(),
        "LANGCHAIN_API_KEY": config.LANGCHAIN_API_KEY,
        "LANGCHAIN_PROJECT": config.LANGCHAIN_PROJECT,
        "LANGSMITH_TRACING": str(config.LANGCHAIN_TRACING_V2).lower(),
        "LANGSMITH_API_KEY": config.LANGCHAIN_API_KEY,
        "LANGSMITH_PROJECT": config.LANGCHAIN_PROJECT,
    }
    for key, value in values.items():
        if value is not None and key not in os.environ:
            os.environ[key] = value
