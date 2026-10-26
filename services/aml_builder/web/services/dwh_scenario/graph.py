"""Dependency-injected entry point for the bounded local generator graph."""

from __future__ import annotations

from typing import Any, Callable, TypedDict

from services.aml_builder.web.adapters.dwh_oracle_repo import DwhOracleRepository
from services.aml_builder.web.services.dwh_scenario.generator import (
    LocalScenarioSqlGenerator,
)
from services.aml_builder.web.services.dwh_scenario.retrieval import (
    ChromaRetriever,
    GraphReranker,
)
from services.aml_builder.web.services.settings import Settings, settings


class GenerationState(TypedDict, total=False):
    """Typed state crossing the compiled local generator graph."""

    request: Any
    result: Any


def build_local_generator(
    *,
    llm_factory: Callable[..., Any],
    repository: DwhOracleRepository,
    experience_retriever: ChromaRetriever,
    table_retriever: ChromaRetriever,
    column_retriever: ChromaRetriever,
    reranker: GraphReranker | None = None,
    config: Settings = settings,
) -> LocalScenarioSqlGenerator:
    """Construct the local workflow from explicit adapters without side effects."""
    return LocalScenarioSqlGenerator(
        llm_factory=llm_factory,
        repository=repository,
        experience_retriever=experience_retriever,
        table_retriever=table_retriever,
        column_retriever=column_retriever,
        reranker=reranker,
        config=config,
    )


def build_local_graph(generator: LocalScenarioSqlGenerator) -> Any:
    """Compile an async LangGraph entry point on demand for one local generator.

    The generator owns bounded tool, critique, Oracle and deadline loops. Keeping
    graph construction here explicit means imports and HTTP-only startup do not
    compile or initialize the optional local path.
    """
    from langgraph.graph import END, START, StateGraph

    async def generate_node(state: GenerationState) -> GenerationState:
        """Run one typed scenario request through the bounded generator."""
        return {"result": await generator.generate(state["request"])}

    graph = StateGraph(GenerationState)
    graph.add_node("generate", generate_node)
    graph.add_edge(START, "generate")
    graph.add_edge("generate", END)
    return graph.compile()
