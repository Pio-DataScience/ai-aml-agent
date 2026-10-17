"""Pure extraction of Oracle SQL from text responses."""

import re


def _is_real_sql(candidate: str) -> bool:
    """Check whether candidate text resembles a SELECT query.

    Args:
        candidate: SQL candidate string.

    Returns:
        True when text contains SELECT and FROM with a valid WITH prefix.
    """
    candidate_upper = candidate.upper()
    if "SELECT" not in candidate_upper or "FROM" not in candidate_upper:
        return False
    if candidate_upper.startswith("WITH") and not re.search(
        r"\bWITH\s+[a-zA-Z0-9_\"#]+\s+AS\b", candidate, re.IGNORECASE
    ):
        return False
    return True


def extract_sql(text: str) -> str:
    """Extract a clean SQL query from model or SSE response text.

    Args:
        text: Raw response text, which may contain fenced SQL or prose.

    Returns:
        Extracted SQL, or an empty string if no query is present.
    """
    if not text or not isinstance(text, str):
        return ""
    blocks = re.findall(r"```(?:sql)?\s*([\s\S]+?)```", text, re.IGNORECASE)
    if blocks:
        valid_blocks = []
        for block in blocks:
            cleaned = block.strip().rstrip(";").strip()
            if re.search(
                r"\b(SELECT|WITH)\b", cleaned, re.IGNORECASE
            ) and _is_real_sql(cleaned):
                valid_blocks.append(cleaned)
        if valid_blocks:
            return valid_blocks[-1]
    with_match = re.search(r"\bWITH\b", text, re.IGNORECASE)
    select_match = re.search(r"\bSELECT\b", text, re.IGNORECASE)
    start_index = -1
    if with_match and select_match:
        start_index = min(with_match.start(), select_match.start())
    elif with_match:
        start_index = with_match.start()
    elif select_match:
        start_index = select_match.start()
    if start_index != -1:
        candidate = text[start_index:].strip()
        end_match = re.search(r";(?:\s*\n\s*\n|\s*$|\s*```)", candidate)
        if end_match:
            candidate = candidate[: end_match.start() + 1].strip()
        else:
            trailing = re.search(r";\s*\n\s*[A-Za-z]{3,}\b", candidate)
            if trailing:
                candidate = candidate[: trailing.start() + 1].strip()
        cleaned = candidate.rstrip(";").strip()
        if _is_real_sql(cleaned):
            return cleaned
    return ""
