"""Standard join metadata lookup tool."""

from typing import Any


def lookup_joins(repository: Any, table_a: str, table_b: str, **kwargs: Any) -> Any:
    """Read standard join metadata through the read-only repository."""
    return repository.standard_joins(table_a, table_b, **kwargs)
