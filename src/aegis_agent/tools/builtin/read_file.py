"""``read_file`` builtin tool — read a text file with line numbers + pagination.

REWRITE (behaviour-equivalent to Hermes' ``tools/file_tools.py:read_file_tool``
minimal surface): ``{path, offset=1, limit=500}`` →
``{content, total_lines, truncated}`` or ``{"error": ...}``.  Content lines are
formatted ``LINE_NUM|CONTENT`` with real 1-indexed file line numbers.  The
Hermes-only concerns (read dedup tracking, secret redaction, device-path
guards, cross-agent file-state) are out of scope for the Stage-1 skeleton.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from aegis_agent.models.base import ToolResult
from aegis_agent.tools import schemas
from aegis_agent.tools.registry import ToolContext

_MAX_LIMIT = 2000
_MAX_CHARS = 100_000


class ReadFileTool:
    """Read a UTF-8 text file with pagination."""

    definition = schemas.READ_FILE

    def run(self, arguments: Mapping[str, Any], context: ToolContext | None = None) -> ToolResult:
        path_arg = arguments.get("path")
        if not path_arg or not isinstance(path_arg, str):
            return _error("read_file: missing required field 'path'.")

        offset = _as_int(arguments.get("offset", 1), default=1)
        limit = _as_int(arguments.get("limit", 500), default=500)
        offset = max(1, offset)
        limit = max(1, min(limit, _MAX_LIMIT))

        path = _resolve(path_arg, context)
        if not path.exists():
            return _error(f"File not found: {path_arg}")
        if path.is_dir():
            return _error(f"Path is a directory, not a file: {path_arg}")

        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return _error(f"Could not read '{path_arg}': {exc}")

        lines = text.splitlines()
        total_lines = len(lines)
        start = offset - 1
        end = start + limit
        selected = lines[start:end]
        truncated = end < total_lines

        content = "\n".join(f"{start + i + 1}|{line}" for i, line in enumerate(selected))
        if len(content) > _MAX_CHARS:
            return _error(
                f"Read produced {len(content):,} characters which exceeds the "
                f"limit ({_MAX_CHARS:,}). Use offset/limit to read a smaller range. "
                f"The file has {total_lines} lines total."
            )

        payload = {
            "path": path_arg,
            "content": content,
            "total_lines": total_lines,
            "truncated": truncated,
        }
        return ToolResult(tool_call_id="", name=self.definition.name, content=json.dumps(payload, ensure_ascii=False))


def _resolve(path_arg: str, context: ToolContext | None) -> Path:
    expanded = Path(path_arg).expanduser()
    if expanded.is_absolute():
        return expanded
    base = Path(context.cwd) if context is not None else Path.cwd()
    return base / expanded


def _as_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _error(message: str) -> ToolResult:
    return ToolResult(tool_call_id="", name="read_file", content=json.dumps({"error": message}, ensure_ascii=False), is_error=True)


__all__ = ["ReadFileTool"]
