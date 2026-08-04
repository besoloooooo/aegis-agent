"""Builtin tools for Aegis Agent."""

from __future__ import annotations

from aegis_agent.tools.builtin.list_directory import ListDirectoryTool
from aegis_agent.tools.builtin.read_file import ReadFileTool
from aegis_agent.tools.builtin.run_shell import RunShellTool
from aegis_agent.tools.registry import ToolRegistry


def build_default_registry() -> ToolRegistry:
    """Return a registry pre-populated with all builtin tools."""
    registry = ToolRegistry()
    registry.register(ReadFileTool())
    registry.register(ListDirectoryTool())
    registry.register(RunShellTool())
    return registry


__all__ = [
    "ListDirectoryTool",
    "ReadFileTool",
    "RunShellTool",
    "build_default_registry",
]
