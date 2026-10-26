"""Column-level schema retrieval tool."""

from typing import Any


def search_columns(
    retriever: Any, query: str, institution_id: str, limit: int
) -> list[dict[str, Any]]:
    """Retrieve institution-scoped column metadata."""
    return retriever.search(query, institution_id, limit)
