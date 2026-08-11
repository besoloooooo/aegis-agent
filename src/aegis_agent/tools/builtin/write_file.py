"""``write_file`` builtin tool — atomically write a file, creating parents.

REWRITE (behaviour-equivalent to the minimal surface of Hermes'
``write_file_tool`` → ``ShellFileOperations.write_file``): ``{path, content}`` →
``{path, bytes_written, created, dirs_created}`` or ``{"error": ...}``.

Behaviour kept from Hermes: auto-create parent directories, full overwrite
(no append), atomic temp+rename write, UTF-8, preserve an existing file's BOM
and line-ending style, and refuse writes to sensitive system/credential paths.
Dropped Hermes concerns: cross-profile mirrors, file-state/staleness tracking,
post-write lint/LSP, secret redaction.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from aegis_agent.models.base import ToolResult
from aegis_agent.tools import fsutil, schemas
from aegis_agent.tools.registry import ToolContext


class WriteFileTool:
    """Write content to a file (create or overwrite) atomically."""

    definition = schemas.WRITE_FILE

    def run(self, arguments: Mapping[str, Any], context: ToolContext | None = None) -> ToolResult:
        path_arg = arguments.get("path")
        if not path_arg or not isinstance(path_arg, str):
            return _error("write_file: missing required field 'path'.")

        content = arguments.get("content")
        if content is None or not isinstance(content, str):
            return _error("write_file: missing required field 'content' (must be a string).")

        cwd = context.cwd if context is not None else None
        path = fsutil.resolve_path(path_arg, cwd)

        denied = fsutil.is_write_denied(path)
        if denied is not None:
            return _error(denied)

        existed = path.exists()
        if existed and path.is_dir():
            return _error(f"write_file: path is a directory, not a file: {path_arg}")

        # Preserve an existing file's BOM and line-ending style.
        to_write = content
        if existed:
            try:
                existing = fsutil.read_text_raw(path)
            except OSError:
                existing = ""
            if fsutil.has_bom(existing) and not fsutil.has_bom(to_write):
                to_write = "﻿" + to_write
            if fsutil.detect_line_ending(existing) == "\r\n":
                to_write = fsutil.normalize_line_endings(to_write, "\r\n")

        dirs_created = False
        parent = path.parent
        if not parent.exists():
            try:
                parent.mkdir(parents=True, exist_ok=True)
                dirs_created = True
            except OSError as exc:
                return _error(f"write_file: could not create parent directories for {path_arg}: {exc}")

        try:
            bytes_written = fsutil.atomic_write(path, to_write)
        except OSError as exc:
            return _error(f"write_file: failed to write {path_arg}: {exc}")

        payload = {
            "path": str(path),
            "bytes_written": bytes_written,
            "created": not existed,
            "dirs_created": dirs_created,
        }
        return ToolResult(
            tool_call_id="",
            name=self.definition.name,
            content=json.dumps(payload, ensure_ascii=False),
        )


def _error(message: str) -> ToolResult:
    return ToolResult(
        tool_call_id="",
        name="write_file",
        content=json.dumps({"error": message}, ensure_ascii=False),
        is_error=True,
    )


__all__ = ["WriteFileTool"]
