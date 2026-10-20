"""Adapt synchronous LangChain tools to the dedicated bounded I/O executor."""

from __future__ import annotations

from functools import wraps
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool

from services.aml_builder.web.services.io_executor import run_in_io_pool


def run_sync_tool_in_io_pool(tool: BaseTool) -> BaseTool:
    """Return an async tool wrapper that executes a synchronous tool in the I/O pool.

    Tools that already expose an async coroutine are returned unchanged.

    Args:
        tool: LangChain tool to adapt.

    Returns:
        The original async tool or a schema-preserving async wrapper.
    """
    sync_function = getattr(tool, "func", None)
    if sync_function is None or getattr(tool, "coroutine", None) is not None:
        return tool

    @wraps(sync_function)
    async def invoke_tool(**kwargs: Any) -> Any:
        return await run_in_io_pool(sync_function, **kwargs)

    if not isinstance(tool, StructuredTool):
        return tool
    return StructuredTool(
        name=tool.name,
        description=tool.description,
        args_schema=tool.args_schema,
        return_direct=tool.return_direct,
        verbose=tool.verbose,
        callbacks=tool.callbacks,
        tags=tool.tags,
        metadata=tool.metadata,
        handle_tool_error=tool.handle_tool_error,
        handle_validation_error=tool.handle_validation_error,
        response_format=tool.response_format,
        func=None,
        coroutine=invoke_tool,
    )

