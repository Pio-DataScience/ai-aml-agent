"""Advanced schema table retrieval tool."""

from typing import Any


def search_schema(
    retriever: Any, query: str, institution_id: str, limit: int
) -> list[dict[str, Any]]:
    """Retrieve institution-scoped schema table metadata."""
    return retriever.search(query, institution_id, limit)
