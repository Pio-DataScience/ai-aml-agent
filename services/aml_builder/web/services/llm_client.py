"""
Shared LLM client construction and response-parsing helpers.

Centralizes ChatOpenAI/LM Studio instantiation so every module that needs an
LLM (the tool-driven agent, explanation code reranking, etc.) goes through
the same provider-switching logic instead of duplicating it.
"""

import json
import logging
import re
from typing import Any, Dict

from langchain_openai import ChatOpenAI

from services.aml_builder.web.services.settings import settings

logger = logging.getLogger(__name__)


def build_llm(fast: bool = False) -> ChatOpenAI:
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
    kwargs: Dict[str, Any] = {
        "model": model_name,
        "max_retries": 5,
        "timeout": 120.0,
    }

    if settings.LLM_PROVIDER == "lmstudio":
        kwargs["base_url"] = settings.LLM_BASE_URL or "http://127.0.0.1:1234/v1"
        kwargs["api_key"] = "lm-studio"
        kwargs["temperature"] = settings.LLM_TEMPERATURE
    else:
        kwargs["api_key"] = settings.OPENAI_API_KEY

        # Detect if model is an OpenAI reasoning model
        is_reasoning_model = any(
            tag in model_name.lower()
            for tag in ("o1", "o3", "gpt-5", "reasoner", "thinking", "luna")
        )

        # Configure reasoning_effort
        if settings.LLM_REASONING_EFFORT:
            kwargs["reasoning_effort"] = settings.LLM_REASONING_EFFORT
        elif is_reasoning_model:
            # Default to 'none' for function-calling compatibility in chat completions
            kwargs["reasoning_effort"] = "none"

        # Some strict reasoning models (like o1-preview, o1-mini) reject custom temperature
        is_strict_fixed_temp = any(
            model_name.lower().startswith(prefix) for prefix in ("o1-preview", "o1-mini")
        )
        if not is_strict_fixed_temp:
            kwargs["temperature"] = settings.LLM_TEMPERATURE

    return ChatOpenAI(**kwargs)


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
