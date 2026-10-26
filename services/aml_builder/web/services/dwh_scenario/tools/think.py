"""Bounded internal reasoning marker; never calls a model itself."""


def think(note: str, limit: int = 2000) -> str:
    """Return a size-limited planning note for an injected orchestrator."""
    return note[:limit]
