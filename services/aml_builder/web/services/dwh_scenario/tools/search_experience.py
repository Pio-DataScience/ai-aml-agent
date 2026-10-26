"""Optional experience-pair retrieval tool."""

from typing import Any


def search_experience(
    retriever: Any, query: str, institution_id: str, limit: int
) -> list[dict[str, Any]]:
    """Search experience examples; an empty collection is a valid empty result."""
    return retriever.search(query, institution_id, limit)
