"""Tool Protocol, execution context and the :class:`ToolRegistry`.

The registry is an *explicitly constructed* object (dependency-injected into
the executor/runtime), not a module-level singleton — unlike Hermes, where
``tools/registry.py`` holds a global ``registry = ToolRegistry()`` populated by
import side-effects and AST discovery.  Here tools are registered in code, so
there is no hidden global state and tests can build isolated registries.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from aegis_agent.models.base import ToolDefinition, ToolResult


@dataclass(frozen=True)
class ToolContext:
    """Ambient, read-only context handed to tools at execution time.

    ``cwd`` is the working directory used to resolve relative paths.
    ``allow_dangerous_shell`` is an *operator-only* switch: when False (the
    default) the ``terminal`` tool refuses commands that match the dangerous
    pattern list.  It is intentionally NOT a tool argument, so the model can
    never enable it — only the embedding application / CLI can (mirroring
    Hermes' internal ``force`` flag).  Kept frozen so tools cannot mutate
    shared runtime state.

    ``is_cancelled`` is an optional cooperative-cancel callback: when set,
    long-running tools (terminal, web, MCP, process wait) poll it and raise
    :class:`~aegis_agent.exceptions.OperationCancelled` to abort early instead
    of blocking to their timeout.  It is ambient state injected by the
    executor/runtime, never a tool argument the model can set.
    """

    cwd: str = field(default_factory=os.getcwd)
    allow_dangerous_shell: bool = False
    is_cancelled: Callable[[], bool] | None = None
    session_id: str | None = None


@runtime_checkable
class Tool(Protocol):
    """Structural interface for a tool.

    A tool exposes its :attr:`definition` (name/description/JSON-Schema) and a
    :meth:`run` handler that maps parsed arguments to a :class:`ToolResult`.
    """

    @property
    def definition(self) -> ToolDefinition:
        ...

    def run(self, arguments: Mapping[str, Any], context: ToolContext | None = None) -> ToolResult:
        ...


class ToolRegistry:
    """Name → :class:`Tool` mapping with schema assembly.

    Plain, dependency-injected container.  Registering a tool stores it under
    its declared name; :meth:`definitions` returns the advertised schema list
    (OpenAI order = registration order) for the model.
    """

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> Tool:
        """Register ``tool`` under its definition name; returns it for chaining."""
        name = tool.definition.name
        if not name:
            raise ValueError("tool definition must have a non-empty name")
        self._tools[name] = tool
        return tool

    def get(self, name: str) -> Tool | None:
        """Look up a tool by name, or ``None`` if unregistered."""
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def definitions(self) -> list[ToolDefinition]:
        """Return the tool definitions to advertise to the model."""
        return [tool.definition for tool in self._tools.values()]

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def __iter__(self) -> Iterator[Tool]:
        return iter(self._tools.values())


__all__ = ["Tool", "ToolContext", "ToolRegistry"]
