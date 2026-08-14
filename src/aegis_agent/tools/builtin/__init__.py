"""Builtin tools for Aegis Agent."""

from __future__ import annotations

from aegis_agent.tools.builtin.list_directory import ListDirectoryTool
from aegis_agent.tools.builtin.patch import PatchTool
from aegis_agent.tools.builtin.process import ProcessTool
from aegis_agent.tools.builtin.read_file import ReadFileTool
from aegis_agent.tools.builtin.search_files import SearchFilesTool
from aegis_agent.tools.builtin.session_search import SessionSearchTool
from aegis_agent.tools.builtin.terminal import TerminalTool
from aegis_agent.tools.builtin.web_extract import WebExtractTool
from aegis_agent.tools.builtin.web_search import WebSearchTool
from aegis_agent.tools.builtin.write_file import WriteFileTool
from aegis_agent.tools.process_registry import ProcessRegistry
from aegis_agent.tools.registry import ToolRegistry


def build_default_registry(
    process_registry: ProcessRegistry | None = None,
    session_repository=None,
) -> ToolRegistry:
    """Return a registry pre-populated with all builtin tools.

    ``terminal`` and ``process`` share a single :class:`ProcessRegistry`: the
    ``terminal`` tool launches background processes into it and the ``process``
    tool manages them.  Pass an existing registry to share state across
    registries; otherwise one is created.

    ``session_repository`` (when it supports FTS5 full-text search — i.e. is a
    :class:`~aegis_agent.sessions.sqlite_store.SQLiteSessionRepository`) enables
    the ``session_search`` tool.  In-memory / non-FTS stores simply omit it.
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
    if session_repository is not None and hasattr(session_repository, "search_messages"):
        registry.register(SessionSearchTool(session_repository))
    return registry


__all__ = [
    "ListDirectoryTool",
    "PatchTool",
    "ProcessTool",
    "ReadFileTool",
    "SearchFilesTool",
    "SessionSearchTool",
    "TerminalTool",
    "WebExtractTool",
    "WebSearchTool",
    "WriteFileTool",
    "build_default_registry",
]
