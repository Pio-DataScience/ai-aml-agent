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
    - 'lmstudio' : local LM Studio server at LLM_BASE_URL (e.g. http://127.0.0.1:1234/v1).
                   Uses a dummy api_key value since LM Studio does not enforce authentication.

    Args:
        fast (bool): If True, use settings.LLM_MODEL_FAST instead of the primary model —
            for lightweight tasks like explanation code reranking.

    Returns:
        ChatOpenAI: Configured LLM instance.
    """
    kwargs: Dict[str, Any] = {
        "model": settings.LLM_MODEL_FAST if fast else settings.LLM_MODEL,
        "temperature": settings.LLM_TEMPERATURE,
        "max_retries": 5,
        "timeout": 120.0,
    }
    if settings.LLM_PROVIDER == "lmstudio":
        kwargs["base_url"] = settings.LLM_BASE_URL or "http://127.0.0.1:1234/v1"
        kwargs["api_key"] = (
            "lm-studio"  # LM Studio ignores the key but langchain requires it
        )
    else:
        kwargs["api_key"] = settings.OPENAI_API_KEY
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
