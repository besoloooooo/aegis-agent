"""Prompt contributor: announce available MCP tools.

:class:`MCPToolsGuidance` implements the :class:`~aegis_agent.context.system_prompt.
PromptContributor` Protocol created in Milestone A.  When one or more MCP
servers are connected, ``render()`` emits a short notice reminding the model
that MCP-provided tools are available.  When no servers are connected it
returns ``None`` (no empty section).
"""

from __future__ import annotations


class MCPToolsGuidance:
    """Inject a brief MCP-tool availability note into the system prompt."""

    def __init__(self) -> None:
        self._server_count = 0

    def set_servers(self, count: int) -> None:
        self._server_count = count

    def render(self) -> str | None:
        if self._server_count <= 0:
            return None
        suffix = "" if self._server_count == 1 else "s"
        return (
            f"MCP tools from {self._server_count} server{suffix} are "
            "available. Use them alongside the built-in tools when they help "
            "answer the user's request."
        )


__all__ = ["MCPToolsGuidance"]
