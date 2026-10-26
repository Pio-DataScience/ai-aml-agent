"""
Shared LLM client construction and response-parsing helpers.

Centralizes ChatOpenAI/LM Studio instantiation so every module that needs an
LLM (the tool-driven agent, explanation code reranking, etc.) goes through
the same provider-switching logic instead of duplicating it.
"""

import json
import logging
import re
from typing import Any, Dict, Literal

import httpx

from langchain_openai import ChatOpenAI

from services.aml_builder.web.services.settings import settings

logger = logging.getLogger(__name__)


def build_llm(
    fast: bool = False,
    *,
    role: Literal["generator", "critic", "summarizer"] | None = None,
) -> Any:
    """Instantiate a ChatOpenAI-compatible LLM from settings.

    Supports two providers controlled by LLM_PROVIDER env var:
    - 'openai'   : standard OpenAI endpoint (requires OPENAI_API_KEY).
                   Includes smart detection for reasoning models (e.g. gpt-5.6-luna, o1, o3)
                   to configure reasoning_effort and temperature parameters appropriately.
    - 'lmstudio' : local LM Studio / vLLM / Ollama server at LLM_BASE_URL (e.g. http://127.0.0.1:1234/v1).
                   Uses standard ChatOpenAI params without proprietary OpenAI kwargs.

    Args:
        fast (bool): If True, use settings.LLM_MODEL_FAST instead of the primary model —
            for lightweight tasks like explanation code reranking.

    Returns:
        ChatOpenAI: Configured LLM instance.
    """
    model_name = settings.LLM_MODEL_FAST if fast else settings.LLM_MODEL
    provider = settings.LLM_PROVIDER
    output_tokens = settings.LLM_MAX_TOKENS
    retries = 5
    timeout: Any = 120.0
    api_key = settings.OPENAI_API_KEY
    base_url = settings.LLM_BASE_URL
    if role is not None:
        role_provider = getattr(settings, f"DWH_SQL_{role.upper()}_PROVIDER")
        role_model = getattr(settings, f"DWH_SQL_{role.upper()}_MODEL")
        provider = role_provider or provider
        model_name = role_model or model_name
        output_tokens = settings.DWH_SQL_OUTPUT_TOKEN_LIMIT
        retries = min(settings.DWH_SQL_RETRY_BUDGET, settings.DWH_SQL_MAX_ATTEMPTS - 1)
        timeout = settings.DWH_SQL_DEADLINE_SECONDS
        transport_timeout = httpx.Timeout(
            timeout=settings.DWH_SQL_READ_TIMEOUT_SECONDS,
            connect=settings.DWH_SQL_CONNECT_TIMEOUT_SECONDS,
        )
        base_url = settings.DWH_SQL_BASE_URL or base_url
        key_setting = {
            "openai": settings.DWH_SQL_OPENAI_API_KEY or settings.OPENAI_API_KEY,
            "lmstudio": "lm-studio",
            "cohere": settings.DWH_SQL_COHERE_API_KEY,
            "sambanova": settings.DWH_SQL_SAMBANOVA_API_KEY,
            "openrouter": settings.DWH_SQL_OPENROUTER_API_KEY,
        }
        api_key = key_setting.get(provider)
        if provider not in key_setting:
            raise ValueError(f"Unsupported LLM provider for {role}: {provider}")
        if provider != "lmstudio" and not api_key:
            raise ValueError(
                f"API key is required for configured {role} provider '{provider}'"
            )

    kwargs: Dict[str, Any] = {
        "model": model_name,
        "max_retries": retries,
        "timeout": timeout,
    }
    if role is not None and provider != "cohere":
        kwargs["http_client"] = httpx.Client(timeout=transport_timeout)

    if provider in {"lmstudio", "openrouter", "sambanova"}:
        endpoints = {
            "lmstudio": "http://127.0.0.1:1234/v1",
            "openrouter": "https://openrouter.ai/api/v1",
            "sambanova": "https://api.sambanova.ai/v1",
        }
        kwargs["base_url"] = base_url or endpoints[provider]
        kwargs["api_key"] = api_key or "lm-studio"
        kwargs["temperature"] = settings.LLM_TEMPERATURE
        if output_tokens is not None:
            kwargs["max_tokens"] = output_tokens
    elif provider == "cohere":
        try:
            from langchain_cohere import ChatCohere
        except ImportError as exc:
            raise ImportError(
                "Cohere support requires the optional 'cohere' extra"
            ) from exc
        return ChatCohere(
            model=model_name,
            cohere_api_key=api_key,
            max_retries=retries,
            timeout=timeout,
            max_tokens=output_tokens,
            callbacks=_tracing_callbacks(),
        )
    else:
        kwargs["api_key"] = api_key

        # Detect if model is an OpenAI reasoning model
        is_reasoning_model = any(
            tag in model_name.lower()
            for tag in ("o1", "o3", "gpt-5", "reasoner", "thinking", "luna")
        )

        if output_tokens is not None:
            token_parameter = (
                "max_completion_tokens" if is_reasoning_model else "max_tokens"
            )
            kwargs[token_parameter] = output_tokens

        # Reasoning-effort values vary across models. Treat "none" as a
        # request to omit the parameter and leave it unset by default so the
        # selected model can use its own compatible default.
        configured_effort = (settings.LLM_REASONING_EFFORT or "").strip().casefold()
        if configured_effort and configured_effort != "none":
            kwargs["reasoning_effort"] = configured_effort

        # Some strict reasoning models (like o1-preview, o1-mini) reject custom temperature
        is_strict_fixed_temp = any(
            model_name.lower().startswith(prefix)
            for prefix in ("o1-preview", "o1-mini")
        )
        if not is_strict_fixed_temp:
            kwargs["temperature"] = settings.LLM_TEMPERATURE

    kwargs["callbacks"] = _tracing_callbacks()
    return ChatOpenAI(**kwargs)


def _tracing_callbacks() -> list[Any] | None:
    """Return LangSmith callbacks when tracing is enabled, without forcing setup."""
    if not settings.LANGCHAIN_TRACING_V2:
        return None
    from langchain_core.tracers import LangChainTracer

    return [LangChainTracer(project_name=settings.LANGCHAIN_PROJECT)]


def safe_parse_json(text: str) -> dict:
    """Parse JSON string with fallback repairs for common LLM formatting flaws.

    Args:
        text (str): Raw LLM response text, possibly wrapped in markdown fences
            or containing trailing commas.

    Returns:
        dict: The parsed JSON object.

    Raises:
        ValueError: If no valid JSON object could be extracted from the text.
    """
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)

    # 1. Direct parse attempt
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 2. Extract substring between first '{' and last '}'
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        extracted = match.group(0)
        try:
            return json.loads(extracted)
        except json.JSONDecodeError:
            # Repair trailing commas before closing braces/brackets
            repaired = re.sub(r",\s*([\}\]])", r"\1", extracted)
            return json.loads(repaired)

    raise ValueError(f"Could not parse valid JSON from text: {text[:100]}...")
