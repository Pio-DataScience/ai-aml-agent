"""
Utility loader for loading system prompts from the prompts directory.
"""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent


def load_prompt(filename: str) -> str:
    """
    Loads a system prompt markdown file from the web/services/prompts directory.

    Args:
        filename (str): Name of the prompt file (e.g., 'intent_extraction.md').
            Must be located in the prompts directory.

    Returns:
        str: Raw text content of the prompt file, or an empty string if not found.

    Raises:
        None: Suppresses FileNotFoundError and logs a warning for robust runtime fallback.
    """
    prompt_path = PROMPTS_DIR / filename
    try:
        return prompt_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.warning("[PROMPT] Prompt file not found: %s — returning empty string.", filename)
        return ""
    except Exception as exc:
        logger.error("[PROMPT] Failed to read prompt file %s: %s", filename, exc)
        return ""
