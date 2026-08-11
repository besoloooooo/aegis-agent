# Portions adapted from Hermes (hermes-agent), © 2025 Nous Research.
# Licensed under the MIT License. See THIRD_PARTY_NOTICES.md.
#
# ADAPT of the generic filesystem helpers in Hermes ``tools/file_operations.py``
# (``_detect_line_ending``, ``_normalize_line_endings``, ``_strip_bom`` /
# ``_has_bom``, ``_atomic_write``, ``_unified_diff``, ``_is_write_denied``) and
# ``tools/path_security.py`` (``has_traversal_component``, ``validate_within_dir``).
# The Hermes originals run every operation through a pluggable shell/terminal
# backend (docker/ssh/modal ``execute()``); this port replaces that with direct
# Python I/O (``pathlib`` / ``os.replace``).  The multi-backend abstraction,
# LSP/lint tiers, secret redaction, cross-profile mirrors and file-state
# tracking are all dropped.
"""Small, self-contained filesystem helpers for the file-editing tools.

These give ``write_file`` / ``patch`` the properties an editor expects — atomic
writes, BOM and line-ending preservation, unified diffs — plus the generic
safety guards (sensitive-path refusal, ``..`` traversal detection) worth keeping
from Hermes, without any of Hermes' backend coupling.
"""

from __future__ import annotations

import difflib
import os
import tempfile
from pathlib import Path

_UTF8_BOM = "﻿"

# Sensitive path prefixes that writes are refused for.  A deliberately generic
# subset of Hermes' denylist: OS/boot config, device nodes, and per-user
# credential stores.  Compared against the resolved absolute path.
_WRITE_DENIED_PREFIXES: tuple[str, ...] = (
    "/etc/",
    "/boot/",
    "/proc/",
    "/sys/",
    "/dev/",
)

# Credential-ish path *segments* refused anywhere in the resolved path.
_WRITE_DENIED_SEGMENTS: tuple[str, ...] = (
    "/.ssh/",
    "/.aws/credentials",
    "/.gnupg/",
)


def has_bom(text: str) -> bool:
    """Return True if ``text`` begins with a UTF-8 BOM."""
    return text.startswith(_UTF8_BOM)


def strip_bom(text: str) -> str:
    """Return ``text`` without a leading UTF-8 BOM (if present)."""
    return text[len(_UTF8_BOM):] if has_bom(text) else text


def detect_line_ending(text: str) -> str:
    """Return the dominant line ending of ``text`` — ``"\\r\\n"`` or ``"\\n"``.

    Mirrors Hermes: if the first newline is preceded by a carriage return the
    file is treated as CRLF, otherwise LF.  Empty / newline-free text is LF.
    """
    idx = text.find("\n")
    if idx > 0 and text[idx - 1] == "\r":
        return "\r\n"
    return "\n"


def normalize_line_endings(text: str, ending: str) -> str:
    """Rewrite all line endings in ``text`` to ``ending`` (``"\\n"`` or ``"\\r\\n"``)."""
    # Normalize to LF first, then expand if CRLF is wanted.
    lf = text.replace("\r\n", "\n").replace("\r", "\n")
    if ending == "\r\n":
        return lf.replace("\n", "\r\n")
    return lf


def unified_diff(before: str, after: str, path: str) -> str:
    """Return a unified diff between ``before`` and ``after`` for ``path``."""
    diff = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
    )
    return "".join(diff)


def read_text_raw(path: Path, encoding: str = "utf-8") -> str:
    """Read ``path`` as text with **no** newline translation.

    ``Path.read_text`` uses universal-newline mode, which converts ``\\r\\n`` to
    ``\\n`` on read — destroying the line-ending information we need to preserve
    on write.  Reading bytes and decoding keeps ``\\r\\n`` intact.
    """
    return path.read_bytes().decode(encoding, errors="replace")


def atomic_write(path: Path, content: str, encoding: str = "utf-8") -> int:
    """Write ``content`` to ``path`` atomically; return bytes written.

    Bytes are written directly (no text-mode newline translation), so a CRLF
    file stays CRLF.  Writes to a temp file in the same directory then
    ``os.replace``s it over the target, so a crash mid-write never leaves a
    partially written file.  Parent directories are assumed to exist (the
    caller creates them and reports ``dirs_created``).
    """
    data = content.encode(encoding)
    directory = path.parent
    fd, tmp_name = tempfile.mkstemp(prefix=".aegis-tmp-", dir=str(directory))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        # Best-effort cleanup of the temp file on any failure.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return len(data)


def is_write_denied(path: Path) -> str | None:
    """Return a refusal reason if writing ``path`` is forbidden, else None.

    Checks the resolved absolute path against a generic sensitive-path list.
    """
    try:
        resolved = str(path.resolve())
    except OSError:
        resolved = str(path)
    # Use forward slashes for a uniform comparison across platforms.
    normalized = resolved.replace("\\", "/")
    for prefix in _WRITE_DENIED_PREFIXES:
        if normalized.startswith(prefix):
            return f"Write denied: {path} is under a protected system path ({prefix})."
    for segment in _WRITE_DENIED_SEGMENTS:
        if segment in normalized:
            return f"Write denied: {path} looks like a credential file ({segment.strip('/')})."
    return None


def has_traversal_component(path_str: str) -> bool:
    """Return True if ``path_str`` contains a ``..`` traversal component."""
    return ".." in Path(path_str).parts


def resolve_path(path_arg: str, cwd: str | None) -> Path:
    """Resolve ``path_arg`` against ``cwd`` (or the process cwd), expanding ``~``."""
    expanded = Path(path_arg).expanduser()
    if expanded.is_absolute():
        return expanded
    base = Path(cwd) if cwd else Path.cwd()
    return base / expanded


__all__ = [
    "atomic_write",
    "detect_line_ending",
    "has_bom",
    "has_traversal_component",
    "is_write_denied",
    "normalize_line_endings",
    "read_text_raw",
    "resolve_path",
    "strip_bom",
    "unified_diff",
]
