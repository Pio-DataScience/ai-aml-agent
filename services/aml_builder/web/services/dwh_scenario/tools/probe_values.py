"""Bounded dictionary column-value probe tool."""

from typing import Any


def probe_values(
    repository: Any, owner: str, table: str, column: str, **kwargs: Any
) -> Any:
    """Probe a column through the read-only Oracle repository."""
    return repository.probe_column_values(owner, table, column, **kwargs)
