# Portions adapted from Hermes (hermes-agent), © 2025 Nous Research.
# Licensed under the MIT License. See THIRD_PARTY_NOTICES.md.
#
# ADAPT of Hermes' ``patch`` tool, replace mode only
# (``ShellFileOperations.patch_replace``): read → fuzzy find/replace →
# write back → re-read verify.  The V4A multi-file diff mode
# (``patch_parser.py`` / ``patch_v4a``) is intentionally NOT ported.  Dropped
# Hermes concerns: cross-profile, file-state/staleness, consecutive-failure
# escalation, lint/LSP, secret redaction, backend routing.
"""``patch`` builtin tool — precise fuzzy find/replace in a file.

``{path, old_string, new_string, replace_all=false}`` →
``{success, path, diff, replaced, strategy}`` or ``{"error": ...}``.

Matching is delegated to :func:`aegis_agent.tools.fuzzy_match.fuzzy_find_and_replace`
(9-strategy chain tolerant of whitespace/indentation drift).  A non-unique match
without ``replace_all`` is an error; a no-match returns a "did you mean?" hint.
After writing, the file is re-read and compared to confirm the edit persisted.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from aegis_agent.models.base import ToolResult
from aegis_agent.tools import fsutil, schemas
from aegis_agent.tools.fuzzy_match import (
    format_no_match_hint,
    fuzzy_find_and_replace,
)
from aegis_agent.tools.registry import ToolContext


class PatchTool:
    """Find and replace a snippet of text in a file (fuzzy, replace mode)."""

    definition = schemas.PATCH

    def run(self, arguments: Mapping[str, Any], context: ToolContext | None = None) -> ToolResult:
        path_arg = arguments.get("path")
        if not path_arg or not isinstance(path_arg, str):
            return _error("patch: missing required field 'path'.")

        old_string = arguments.get("old_string")
        if old_string is None or not isinstance(old_string, str):
            return _error("patch: missing required field 'old_string'.")

        new_string = arguments.get("new_string")
        if new_string is None or not isinstance(new_string, str):
            return _error("patch: missing required field 'new_string'.")

        replace_all = bool(arguments.get("replace_all", False))

        cwd = context.cwd if context is not None else None
        path = fsutil.resolve_path(path_arg, cwd)

        denied = fsutil.is_write_denied(path)
        if denied is not None:
            return _error(denied)

        if not path.exists():
            return _error(f"patch: file not found: {path_arg}")
        if path.is_dir():
            return _error(f"patch: path is a directory, not a file: {path_arg}")

        try:
            raw = fsutil.read_text_raw(path)
        except OSError as exc:
            return _error(f"patch: could not read {path_arg}: {exc}")

        had_bom = fsutil.has_bom(raw)
        ending = fsutil.detect_line_ending(raw)
        content = fsutil.strip_bom(raw)

        new_content, count, strategy, match_error = fuzzy_find_and_replace(
            content, old_string, new_string, replace_all=replace_all
        )

        if count == 0:
            message = match_error or "Could not find a match for old_string in the file"
            message += format_no_match_hint(match_error, count, old_string, content)
            return _error(f"patch: {message}")

        # Re-apply the file's BOM / line-ending style before writing.
        to_write = fsutil.normalize_line_endings(new_content, ending)
        if had_bom and not fsutil.has_bom(to_write):
            to_write = "﻿" + to_write

        try:
            fsutil.atomic_write(path, to_write)
        except OSError as exc:
            return _error(f"patch: failed to write {path_arg}: {exc}")

        # Post-write verification: re-read and confirm the change persisted.
        try:
            verify = fsutil.strip_bom(fsutil.read_text_raw(path))
        except OSError as exc:
            return _error(f"patch: wrote {path_arg} but could not re-read to verify: {exc}")
        if fsutil.normalize_line_endings(verify, "\n") != fsutil.normalize_line_endings(new_content, "\n"):
            return _error(
                f"patch: post-write verification failed for {path_arg} — the file "
                "on disk does not match the expected content."
            )

        payload = {
            "success": True,
            "path": str(path),
            "replaced": count,
            "strategy": strategy,
            "diff": fsutil.unified_diff(content, new_content, path_arg),
        }
        return ToolResult(
            tool_call_id="",
            name=self.definition.name,
            content=json.dumps(payload, ensure_ascii=False),
        )


def _error(message: str) -> ToolResult:
    return ToolResult(
        tool_call_id="",
        name="patch",
        content=json.dumps({"success": False, "error": message}, ensure_ascii=False),
        is_error=True,
    )


__all__ = ["PatchTool"]
