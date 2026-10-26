"""Policy-guarded candidate SQL execution tool."""

from typing import Any


def execute_sql(repository: Any, sql: str, **kwargs: Any) -> Any:
    """Delegate candidate validation and bounded execution to A's repository."""
    return repository.execute_candidate(sql, **kwargs)
