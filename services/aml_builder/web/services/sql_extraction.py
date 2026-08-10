"""
SQL text-extraction helpers for parsing Oracle SQL out of raw LLM/SSE text.

Used by the shadow-test tool to pull a clean query out of the PioTech AI
text-to-SQL service's streamed response, which may wrap SQL in markdown
fences or trailing prose.
"""

import re


def _is_real_sql(candidate: str) -> bool:
    """Helper to verify if a candidate string represents a genuine SQL query."""
    candidate_upper = candidate.upper()
    if "SELECT" not in candidate_upper or "FROM" not in candidate_upper:
        return False
    if candidate_upper.startswith("WITH"):
        if not re.search(r"\bWITH\s+[a-zA-Z0-9_\"#]+\s+AS\b", candidate, re.IGNORECASE):
            return False
    return True


def extract_sql(text: str) -> str:
    """Extract a clean SQL query from LLM response text.

    Args:
        text (str): Raw text response from PioTech AI.

    Returns:
        str: The extracted SQL query, or empty string if none found.
    """
    if not text or not isinstance(text, str):
        return ""

    # 1. Find all code blocks delimited by ```sql ... ``` or ``` ... ```
    blocks = re.findall(r"```(?:sql)?\s*([\s\S]+?)```", text, re.IGNORECASE)
    if blocks:
        valid_blocks = []
        for block in blocks:
            block_clean = block.strip().rstrip(";").strip()
            if re.search(r"\b(SELECT|WITH)\b", block_clean, re.IGNORECASE):
                if _is_real_sql(block_clean):
                    valid_blocks.append(block_clean)
        if valid_blocks:
            return valid_blocks[-1]

    # 2. Fallback: if no code blocks found, extract raw WITH or SELECT statement
    with_match = re.search(r"\bWITH\b", text, re.IGNORECASE)
    select_match = re.search(r"\bSELECT\b", text, re.IGNORECASE)

    start_idx = -1
    if with_match and select_match:
        start_idx = min(with_match.start(), select_match.start())
    elif with_match:
        start_idx = with_match.start()
    elif select_match:
        start_idx = select_match.start()

    if start_idx != -1:
        candidate = text[start_idx:].strip()
        end_match = re.search(r";(?:\s*\n\s*\n|\s*$|\s*```)", candidate)
        if end_match:
            candidate = candidate[: end_match.start() + 1].strip()
        else:
            trailing_exp = re.search(r";\s*\n\s*[A-Za-z]{3,}\b", candidate)
            if trailing_exp:
                candidate = candidate[: trailing_exp.start() + 1].strip()

        candidate_clean = candidate.rstrip(";").strip()
        if _is_real_sql(candidate_clean):
            return candidate_clean

    return ""
