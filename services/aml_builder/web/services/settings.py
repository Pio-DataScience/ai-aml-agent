"""
Settings module for the AML Builder service.

All configuration is sourced from environment variables or a .env file.
Never hardcode secrets or connection strings — per SOLID Dependency Inversion.
"""

from functools import lru_cache
from typing import Final, Optional

from pydantic import Field, field_validator
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
    SERVICE_NAME: Final[str] = Field(
        default="aml-builder",
        description="Identifier used in logs and LangSmith traces.",
    )
    SERVICE_PORT: int = Field(
        default=8005,
        description="Port this FastAPI service listens on.",
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
    LLM_MAX_TOKENS: int = Field(
        default=4096,
        description="Max tokens per LLM response.",
    )

    # ─── Oracle DB ────────────────────────────────────────────────────────────
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
    AML_DEFAULT_ACTIVE_FLAG: str = Field(
        default="1",
        description="Default active flag for scenarios/rules. '1' = active (Oracle standard).",
    )
    AML_DEFAULT_VIOLATION_LEVEL: str = Field(
        default="1",
        description="Default violation level written to PIO_AML_SCENARIO.VIOLATION_LEVEL.",
    )
    AML_DEFAULT_DEGREE_RISK_FLAG: str = Field(
        default="D",
        description="Default risk degree written to PIO_AML_SCENARIO.DEGREE_RISK_FLAG.",
    )
    AML_RUN_FLAG: str = Field(
        default="1",
        description="RUN_FLAG for PIO_AML_SCENARIO. '1' = scenario is scheduled to run.",
    )
    AML_APPROVAL_FLAG: str = Field(
        default="1",
        description="APPROVAL_FLAG for PIO_AML_SCENARIO. '1' = approved.",
    )
    AML_GROUP_BY_FLAG: str = Field(
        default="1",
        description="GROUP_BY_FLAG for PIO_AML_SCENARIO. '1' = group alerts by customer.",
    )
    AML_CLASS_CODE: str = Field(
        default="1",
        description="CLASS_CODE for PIO_AML_SCENARIO. '1' = Individual customer class.",
    )
    AML_SCE_TYPE_CODE: str = Field(
        default="1",
        description="SCE_TYPE_CODE for PIO_AML_SCENARIO. '1' = standard transaction scenario.",
    )
    AML_CATEGORY_CODE: str = Field(
        default="999",
        description="CATEGORY_CODE for PIO_AML_SCENARIO. '999' = generic/uncategorised.",
    )
    AML_TRANS_WITHOUTTRANS_FLAG: str = Field(
        default="1",
        description="TRANS_WITHOUTTRANS_FLAG. '1' = include accounts with no transactions.",
    )
    AML_RULE_TYPE: str = Field(
        default="3",
        description=(
            "RULE_TYPE for PIO_AML_SCENARIO_RULES. "
            "'3' = standalone rule (no sequential/follow-by relationship). "
            "See PIO_AML_RULE_TYPE lookup table."
        ),
    )

    # ─── PioTech AI (SQL Bridge) ───────────────────────────────────────────────
    PIOTECH_AI_URL: str = Field(
        default="http://localhost:8001/chat/stream",
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
    CHECKPOINT_DB_PATH: str = Field(
        default="./artifacts/checkpoints.sqlite",
        description="Path to the SQLite file used by LangGraph's checkpointer.",
    )
    MAX_AGENT_ITERATIONS: int = Field(
        default=25,
        description="Hard cap on LangGraph node execution cycles per turn.",
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
            raise ValueError(
                f"LLM_PROVIDER must be one of {allowed}, got '{value}'."
            )
        return normalized

    @field_validator("AML_DEFAULT_DEGREE_RISK_FLAG")
    @classmethod
    def validate_degree_risk_flag(cls, value: str) -> str:
        """Ensure the degree risk flag is a valid Oracle AML code.

        The reference scenario uses 'D' (Low/Default risk degree).
        Allowed values from PIO_AML_SCENARIO.DEGREE_RISK_FLAG domain.

        Args:
            value (str): The configured degree risk flag.

        Returns:
            str: Uppercased validated flag.

        Raises:
            ValueError: If value is not one of the known codes.
        """
        normalized = value.upper().strip()
        allowed = {"D", "L", "M", "H"}
        if normalized not in allowed:
            raise ValueError(
                f"AML_DEFAULT_DEGREE_RISK_FLAG must be one of {allowed} — got '{value}'."
            )
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
