"""``list_directory`` builtin tool — list a directory's entries.

REWRITE: Aegis-specific (Hermes has no standalone list tool; it folds listing
into ``search_files target=files``).  ``{path="."}`` →
``{path, entries: [{name, type, size}], count}`` or ``{"error": ...}``.
``size`` is null for directories and other non-regular files.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from aegis_agent.models.base import ToolResult
from aegis_agent.tools import schemas
from aegis_agent.tools.registry import ToolContext


class ListDirectoryTool:
    """List the entries of a directory, sorted by name."""

    definition = schemas.LIST_DIRECTORY

    def run(self, arguments: Mapping[str, Any], context: ToolContext | None = None) -> ToolResult:
        path_arg = arguments.get("path") or "."
        if not isinstance(path_arg, str):
            return _error("list_directory: 'path' must be a string.")

        path = _resolve(path_arg, context)
        if not path.exists():
            return _error(f"Directory not found: {path_arg}")
        if not path.is_dir():
            return _error(f"Path is not a directory: {path_arg}")

        entries: list[dict[str, Any]] = []
        try:
            children = sorted(path.iterdir(), key=lambda p: p.name)
            for child in children:
                entries.append(
                    {
                        "name": child.name,
                        "type": _entry_type(child),
                        "size": child.stat().st_size if child.is_file() else None,
                    }
                )
        except OSError as exc:
            return _error(f"Could not list '{path_arg}': {exc}")

        payload = {"path": path_arg, "entries": entries, "count": len(entries)}
        return ToolResult(tool_call_id="", name=self.definition.name, content=json.dumps(payload, ensure_ascii=False))


def _entry_type(path: Path) -> str:
    if path.is_dir():
        return "dir"
    if path.is_file():
        return "file"
    return "other"


def _resolve(path_arg: str, context: ToolContext | None) -> Path:
    expanded = Path(path_arg).expanduser()
    if expanded.is_absolute():
        return expanded
    base = Path(context.cwd) if context is not None else Path.cwd()
    return base / expanded


def _error(message: str) -> ToolResult:
    return ToolResult(tool_call_id="", name="list_directory", content=json.dumps({"error": message}, ensure_ascii=False), is_error=True)


__all__ = ["ListDirectoryTool"]
