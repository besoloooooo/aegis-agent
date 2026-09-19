"""Shared shell selection for foreground and managed background commands."""

from __future__ import annotations

import platform
import shutil


def build_shell_argv(command: str) -> list[str]:
    """Return a platform-compatible shell argv, enabling pipefail when possible.

    Windows keeps its existing ``cmd /c`` behavior.  On POSIX, ordinary
    commands retain ``/bin/sh``; commands containing a real shell pipeline use
    Bash with ``pipefail`` when Bash is installed.  The fallback remains
    ``/bin/sh`` so Bash is not a new hard dependency outside environments such
    as Harbor that already provide it.
    """
    if platform.system() == "Windows":
        return ["cmd", "/c", command]
    if _has_pipeline(command):
        bash = shutil.which("bash")
        if bash:
            return [bash, "-o", "pipefail", "-c", command]
    return ["/bin/sh", "-c", command]


def _has_pipeline(command: str) -> bool:
    """Recognize an unquoted, unescaped ``|`` operator but not ``||``."""
    quote: str | None = None
    escaped = False
    for index, char in enumerate(command):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote != "'":
            escaped = True
            continue
        if quote is not None:
            if char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
            continue
        if char == "|":
            before = command[index - 1] if index else ""
            after = command[index + 1] if index + 1 < len(command) else ""
            if before != "|" and after != "|":
                return True
    return False


__all__ = ["build_shell_argv"]
