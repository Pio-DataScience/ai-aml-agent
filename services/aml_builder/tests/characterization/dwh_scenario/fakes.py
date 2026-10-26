"""Deterministic fake adapters used by DWH scenario tests.

Args:
    None.

Returns:
    None.

Raises:
    None.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class FakeOracleError(RuntimeError):
    """Represent an injected Oracle failure."""


@dataclass
class FakeOracle:
    """Script deterministic Oracle query results, failures and delays.

    Args:
        responses: Mapping from SQL to `(columns, rows)` tuples or exceptions.
        timeout_sql: SQL statements that should raise a timeout.

    Returns:
        None.

    Raises:
        FakeOracleError: For scripted query failures.
        TimeoutError: For statements in `timeout_sql`.
    """

    responses: dict[str, tuple[list[str], list[tuple[Any, ...]]] | Exception] = field(
        default_factory=dict
    )
    timeout_sql: set[str] = field(default_factory=set)
    statements: list[str] = field(default_factory=list)

    def execute(self, sql: str) -> tuple[list[str], list[tuple[Any, ...]]]:
        """Execute one scripted statement and count it.

        Args:
            sql: SQL text passed to the fake adapter.

        Returns:
            Tuple containing column names and returned rows.

        Raises:
            FakeOracleError: If the configured response is an exception.
            TimeoutError: If the statement is configured to time out.
        """
        self.statements.append(sql)
        if sql in self.timeout_sql:
            raise TimeoutError("scripted Oracle timeout")
        response = self.responses.get(sql, ([], []))
        if isinstance(response, Exception):
            raise FakeOracleError(str(response)) from response
        return response


@dataclass
class FakeLLM:
    """Return scripted responses and tool calls in order.

    Args:
        responses: Responses to return from successive `invoke` calls.

    Returns:
        None.

    Raises:
        RuntimeError: When the script is exhausted.
    """

    responses: list[Any] = field(default_factory=list)
    calls: list[Any] = field(default_factory=list)

    def invoke(self, messages: Any) -> Any:
        """Consume and return the next scripted model response.

        Args:
            messages: Model input captured for characterization.

        Returns:
            The next configured response.

        Raises:
            RuntimeError: If there are no remaining responses.
        """
        self.calls.append(messages)
        if not self.responses:
            raise RuntimeError("scripted LLM responses exhausted")
        return self.responses.pop(0)


@dataclass
class FakeChroma:
    """Provide disposable in-memory collections for retrieval fixtures.

    Args:
        collections: Initial collection names and their ordered documents.

    Returns:
        None.

    Raises:
        None.
    """

    collections: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def query(self, name: str, limit: int) -> list[dict[str, Any]]:
        """Read at most `limit` documents from a collection.

        Args:
            name: Collection name to query.
            limit: Maximum number of documents to return.

        Returns:
            A copy of the selected document list; missing collections are empty.

        Raises:
            None.
        """
        return list(self.collections.get(name, []))[:limit]


@dataclass
class FakeNeo4j:
    """Represent healthy, unavailable, or slow graph retrieval.

    Args:
        status: One of `healthy`, `down`, or `slow`.
        results: Results returned by a healthy graph.

    Returns:
        None.

    Raises:
        ConnectionError: When status is `down`.
        TimeoutError: When status is `slow`.
    """

    status: str = "healthy"
    results: list[dict[str, Any]] = field(default_factory=list)

    def query(self) -> list[dict[str, Any]]:
        """Return configured graph results or the configured failure.

        Args:
            None.

        Returns:
            Graph result documents.

        Raises:
            ConnectionError: If the graph is unavailable.
            TimeoutError: If the graph exceeds its budget.
        """
        if self.status == "down":
            raise ConnectionError("scripted Neo4j outage")
        if self.status == "slow":
            raise TimeoutError("scripted Neo4j timeout")
        return list(self.results)
