"""Aegis ``Tool`` Protocol wrappers for MCP tools.

Each MCP tool discovered from a server is wrapped in :class:`MCPToolWrapper`,
which implements Aegis's :class:`~aegis_agent.tools.registry.Tool` Protocol so
it can be registered into the ordinary :class:`~aegis_agent.tools.registry.
ToolRegistry` alongside the builtin and skills tools.  The ``run()`` method
marshals the call to the MCP background event loop and returns a
:class:`~aegis_agent.models.base.ToolResult`.  Errors are always returned as
results, never raised — the executor contract holds.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from aegis_agent.mcp import client as _client
from aegis_agent.mcp.schema_adapter import convert_mcp_tool
from aegis_agent.models.base import ToolDefinition, ToolResult
from aegis_agent.tools.registry import ToolContext


class MCPToolWrapper:
    """Aegis :class:`~aegis_agent.tools.registry.Tool` backed by an MCP server tool.

    ``definition`` is built from ``convert_mcp_tool`` at construction time.
    ``run()`` calls the MCP server via :func:`~aegis_agent.mcp.client.call_tool`.
    """

    def __init__(self, server_name: str, mcp_tool: Any, tool_timeout: float = 120) -> None:
        schema = convert_mcp_tool(server_name, mcp_tool)
        self._definition = ToolDefinition(
            name=schema["name"],
            description=schema["description"],
            parameters=schema["parameters"],
        )
        self._server_name = server_name
        self._tool_name = mcp_tool.name
        self._timeout = tool_timeout

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    def run(self, arguments: Mapping[str, Any], context: ToolContext | None = None) -> ToolResult:
        if arguments is None:
            arguments = {}
        raw = _client.call_tool(
            self._server_name,
            self._tool_name,
            dict(arguments),
            timeout=self._timeout,
        )
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {"error": "MCP call returned non-JSON response"}
        is_error = "error" in parsed
        return ToolResult(
            tool_call_id="",
            name=self._definition.name,
            content=json.dumps(parsed, ensure_ascii=False),
            is_error=is_error,
        )


def build_wrappers(server_name: str, mcp_tools: list, tool_timeout: float = 120) -> list[MCPToolWrapper]:
    """Create an :class:`MCPToolWrapper` for every MCP tool in *mcp_tools*."""
    return [MCPToolWrapper(server_name, t, tool_timeout) for t in mcp_tools]


__all__ = ["MCPToolWrapper", "build_wrappers"]
