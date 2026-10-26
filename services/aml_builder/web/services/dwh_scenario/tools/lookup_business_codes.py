"""Business-code metadata lookup tool."""

from typing import Any


def lookup_business_codes(repository: Any, column: str, **kwargs: Any) -> Any:
    """Read configured business-code metadata through the repository."""
    return repository.business_code_metadata(column, **kwargs)
