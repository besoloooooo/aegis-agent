"""Builtin tools for Aegis Agent."""

from __future__ import annotations

from aegis_agent.tools.builtin.list_directory import ListDirectoryTool
from aegis_agent.tools.builtin.patch import PatchTool
from aegis_agent.tools.builtin.process import ProcessTool
from aegis_agent.tools.builtin.read_file import ReadFileTool
from aegis_agent.tools.builtin.search_files import SearchFilesTool
from aegis_agent.tools.builtin.terminal import TerminalTool
from aegis_agent.tools.builtin.web_extract import WebExtractTool
from aegis_agent.tools.builtin.web_search import WebSearchTool
from aegis_agent.tools.builtin.write_file import WriteFileTool
from aegis_agent.tools.process_registry import ProcessRegistry
from aegis_agent.tools.registry import ToolRegistry


def build_default_registry(process_registry: ProcessRegistry | None = None) -> ToolRegistry:
    """Return a registry pre-populated with all builtin tools.

    ``terminal`` and ``process`` share a single :class:`ProcessRegistry`: the
    ``terminal`` tool launches background processes into it and the ``process``
    tool manages them.  Pass an existing registry to share state across
    registries; otherwise one is created.
    """
    if process_registry is None:
        process_registry = ProcessRegistry()
    registry = ToolRegistry()
    registry.register(ReadFileTool())
    registry.register(ListDirectoryTool())
    registry.register(WriteFileTool())
    registry.register(PatchTool())
    registry.register(SearchFilesTool())
    registry.register(TerminalTool(process_registry))
    registry.register(ProcessTool(process_registry))
    registry.register(WebSearchTool())
    registry.register(WebExtractTool())
    return registry


__all__ = [
    "ListDirectoryTool",
    "PatchTool",
    "ProcessTool",
    "ReadFileTool",
    "SearchFilesTool",
    "TerminalTool",
    "WebExtractTool",
    "WebSearchTool",
    "WriteFileTool",
    "build_default_registry",
]
