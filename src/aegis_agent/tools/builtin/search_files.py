"""``search_files`` builtin tool — search contents (regex) or file names (glob).

REWRITE (behaviour-equivalent to the minimal surface of Hermes' ``search_tool``
→ ``ShellFileOperations.search``).  Two targets:
  * ``target="content"`` — regex search inside files (grep-like);
  * ``target="files"``   — find files whose name matches a glob.

Ripgrep (``rg``) is used when it is on PATH (fast, respects .gitignore); a pure
``os.walk`` + ``re`` + ``fnmatch`` fallback gives equivalent behaviour when it is
not.  Both prune hidden and VCS directories.  Payload:
``{total_count, matches | files | counts, truncated}`` or ``{"error": ...}``.

Dropped Hermes concerns: consecutive-search loop breaking, secret redaction,
sandbox backend routing.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from aegis_agent.models.base import ToolResult
from aegis_agent.tools import schemas
from aegis_agent.tools.registry import ToolContext

_MATCH_CHAR_CAP = 500        # per-line content truncation
_DEFAULT_LIMIT = 50
_WALK_FILE_CAP = 20_000      # safety cap on files scanned in the Python fallback
_EXCLUDED_DIRS = frozenset({".git", ".hg", ".svn", "__pycache__", "node_modules", ".venv", "venv"})


class SearchFilesTool:
    """Search file contents by regex or file names by glob."""

    definition = schemas.SEARCH_FILES

    def run(self, arguments: Mapping[str, Any], context: ToolContext | None = None) -> ToolResult:
        pattern = arguments.get("pattern")
        if not pattern or not isinstance(pattern, str):
            return _error("search_files: missing required field 'pattern'.")

        target = arguments.get("target", "content")
        if target not in ("content", "files"):
            target = "content"

        output_mode = arguments.get("output_mode", "content")
        if output_mode not in ("content", "files_only", "count"):
            output_mode = "content"

        limit = _as_int(arguments.get("limit", _DEFAULT_LIMIT), default=_DEFAULT_LIMIT)
        limit = max(1, limit)
        offset = max(0, _as_int(arguments.get("offset", 0), default=0))
        context_lines = max(0, _as_int(arguments.get("context", 0), default=0))
        file_glob = arguments.get("file_glob") if isinstance(arguments.get("file_glob"), str) else None

        cwd = context.cwd if context is not None else None
        base = Path(cwd) if cwd else Path.cwd()
        path_arg = arguments.get("path") or "."
        if not isinstance(path_arg, str):
            return _error("search_files: 'path' must be a string.")
        search_path = Path(path_arg)
        root = search_path if search_path.is_absolute() else base / search_path
        if not root.exists():
            return _error(f"search_files: path not found: {path_arg}")

        try:
            if target == "files":
                items = self._search_files(root, pattern)
            else:
                items = self._search_content(root, pattern, file_glob, context_lines)
        except re.error as exc:
            return _error(f"search_files: invalid regex {pattern!r}: {exc}")
        except OSError as exc:
            return _error(f"search_files: {exc}")

        return self._format(items, target, output_mode, limit, offset)

    # -- search implementations -------------------------------------------

    def _search_files(self, root: Path, glob: str) -> list[str]:
        """Return file paths under ``root`` matching ``glob`` (newest first)."""
        rg = shutil.which("rg")
        if rg is not None:
            out = _run_rg([rg, "--files", "--sortr=modified", "-g", glob, str(root)])
            if out is not None:
                return [line for line in out.splitlines() if line]

        # Pure-Python fallback. A bare pattern matches at any depth.
        needle = glob if any(c in glob for c in "*?[") else f"*{glob}*"
        results: list[tuple[float, str]] = []
        for path in _walk_files(root):
            if fnmatch.fnmatch(path.name, needle):
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    mtime = 0.0
                results.append((mtime, str(path)))
        results.sort(key=lambda x: x[0], reverse=True)
        return [p for _, p in results]

    def _search_content(
        self, root: Path, pattern: str, file_glob: str | None, context_lines: int
    ) -> list[dict[str, Any]]:
        """Return content matches: list of {path, line, content}."""
        rg = shutil.which("rg")
        if rg is not None:
            cmd = [rg, "--line-number", "--no-heading", "--with-filename", "--color=never"]
            if context_lines > 0:
                cmd += ["-C", str(context_lines)]
            if file_glob:
                cmd += ["--glob", file_glob]
            cmd += [pattern, str(root)]
            out = _run_rg(cmd)
            if out is not None:
                return _parse_rg_content(out)

        # Pure-Python fallback.
        regex = re.compile(pattern)
        matches: list[dict[str, Any]] = []
        for path in _walk_files(root):
            if file_glob and not fnmatch.fnmatch(path.name, file_glob):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if "\x00" in text[:1024]:  # skip binary-looking files
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    matches.append({
                        "path": str(path),
                        "line": lineno,
                        "content": line[:_MATCH_CHAR_CAP],
                    })
        return matches

    # -- formatting --------------------------------------------------------

    def _format(
        self,
        items: list[Any],
        target: str,
        output_mode: str,
        limit: int,
        offset: int,
    ) -> ToolResult:
        total = len(items)
        window = items[offset:offset + limit]
        truncated = offset + limit < total

        payload: dict[str, Any] = {"total_count": total}
        if target == "files" or output_mode == "files_only":
            if target == "files":
                files = window
            else:
                seen: list[str] = []
                for m in window:
                    if m["path"] not in seen:
                        seen.append(m["path"])
                files = seen
            payload["files"] = files
        elif output_mode == "count":
            counts: dict[str, int] = {}
            for m in items:
                counts[m["path"]] = counts.get(m["path"], 0) + 1
            payload["counts"] = counts
        else:
            payload["matches"] = window

        if truncated:
            payload["truncated"] = True

        return ToolResult(
            tool_call_id="",
            name=self.definition.name,
            content=json.dumps(payload, ensure_ascii=False),
        )


def _walk_files(root: Path):
    """Yield files under ``root``, pruning hidden/VCS dirs, capped for safety."""
    if root.is_file():
        yield root
        return
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in _EXCLUDED_DIRS]
        for name in filenames:
            if name.startswith("."):
                continue
            yield Path(dirpath) / name
            count += 1
            if count >= _WALK_FILE_CAP:
                return


def _run_rg(cmd: list[str]) -> str | None:
    """Run ripgrep; return stdout, or None if it errored fatally.

    rg exit code 1 means "no matches" (not an error) — return "" then.
    """
    try:
        completed = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode in (0, 1):
        return completed.stdout
    return None  # rg returned a real error (e.g. bad regex) → fall back


# rg content lines look like ``path:line:content`` (Windows drive-letter aware).
_RG_LINE = re.compile(r"^([A-Za-z]:)?(.*?):(\d+):(.*)$")


def _parse_rg_content(out: str) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for line in out.splitlines():
        m = _RG_LINE.match(line)
        if not m:
            continue
        drive, path, lineno, content = m.groups()
        full_path = (drive or "") + path
        matches.append({
            "path": full_path,
            "line": int(lineno),
            "content": content[:_MATCH_CHAR_CAP],
        })
    return matches


def _as_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _error(message: str) -> ToolResult:
    return ToolResult(
        tool_call_id="",
        name="search_files",
        content=json.dumps({"total_count": 0, "error": message}, ensure_ascii=False),
        is_error=True,
    )


__all__ = ["SearchFilesTool"]
