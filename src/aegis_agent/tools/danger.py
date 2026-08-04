# Portions adapted from Hermes (hermes-agent), © 2025 Nous Research.
# Licensed under the MIT License. See THIRD_PARTY_NOTICES.md.
#
# Behavioural source (decoupled and reduced to a generic subset):
#   * ``tools/approval.py`` ``DANGEROUS_PATTERNS`` (line 367) and
#     ``detect_dangerous_command`` (line 543) — a list of (regex, description)
#     pairs scanned with ``re.IGNORECASE | re.DOTALL``; the first match wins
#     and its description is reported.
#
# Only the *generic* destructive patterns are ported.  Hermes-specific entries
# (Hermes config/env paths, gateway lifecycle, docker compose, sudo-askpass
# chaining) are intentionally omitted as out of scope for Aegis.
"""Dangerous-shell-command detection for the ``run_shell`` guardrail.

``detect_dangerous_command`` returns a human-readable description of the first
matched destructive pattern, or ``None`` when the command looks safe.  The
``run_shell`` tool blocks matching commands by default; an operator can allow
them explicitly (never the model — mirroring Hermes' internal ``force`` flag).
"""

from __future__ import annotations

import re

# Flags mirror Hermes' ``_RE_FLAGS``: case-insensitive + DOTALL.
_RE_FLAGS = re.IGNORECASE | re.DOTALL

# Generic destructive patterns, adapted from Hermes ``DANGEROUS_PATTERNS``.
# Each entry is (regex, description).  Order matters: first match is reported.
DANGEROUS_PATTERNS: list[tuple[str, str]] = [
    (r"\brm\s+(-[^\s]*\s+)*/", "delete in root path"),
    (r"\brm\s+-[^\s]*r", "recursive delete"),
    (r"\brm\s+--recursive\b", "recursive delete (long flag)"),
    (r"\bchmod\s+(-[^\s]*\s+)*(777|666|o\+[rwx]*w|a\+[rwx]*w)\b", "world/other-writable permissions"),
    (r"\bchown\s+(-[^\s]*)?R\s+root", "recursive chown to root"),
    (r"\bmkfs\b", "format filesystem"),
    (r"\bdd\s+.*if=", "disk copy"),
    (r">\s*/dev/sd", "write to block device"),
    (r"\bDROP\s+(TABLE|DATABASE)\b", "SQL DROP"),
    # [^\n]* (not .*) so a WHERE on the next line can't satisfy the lookahead.
    (r"\bDELETE\s+FROM\b(?![^\n]*\bWHERE\b)", "SQL DELETE without WHERE"),
    (r"\bTRUNCATE\s+(TABLE)?\s*\w", "SQL TRUNCATE"),
    (r"\bsystemctl\s+(-[^\s]+\s+)*(stop|restart|disable|mask)\b", "stop/restart system service"),
    (r"\bkill\s+-9\s+-1\b", "kill all processes"),
    (r"\bpkill\s+-9\b", "force kill processes"),
    (r"\bkillall\s+(-[^\s]*\s+)*-(9|KILL|SIGKILL)\b", "force kill processes (killall)"),
    (r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:", "fork bomb"),
    (r"\b(curl|wget)\b.*\|\s*(?:[/\w]*/)?(?:ba)?sh(?:\s|$|-c)", "pipe remote content to shell"),
    (r"\bxargs\s+.*\brm\b", "xargs with rm"),
    (r"\bfind\b.*-exec(?:dir)?\s+(/\S*/)?rm\b", "find -exec/-execdir rm"),
    (r"\bfind\b.*-delete\b", "find -delete"),
    (r"\bgit\s+reset\s+--hard\b", "git reset --hard (destroys uncommitted changes)"),
    (r"\bgit\s+push\b.*(--force|-f)\b", "git force push (rewrites remote history)"),
    (r"\bgit\s+clean\s+-[^\s]*f", "git clean with force (deletes untracked files)"),
    (r"\bgit\s+branch\s+-D\b", "git branch force delete"),
    (r"\bshutdown\b|\breboot\b|\bpoweroff\b", "power off / reboot machine"),
]

_COMPILED: list[tuple[re.Pattern[str], str]] = [
    (re.compile(pattern, _RE_FLAGS), description) for pattern, description in DANGEROUS_PATTERNS
]


def detect_dangerous_command(command: str) -> str | None:
    """Return a description of the first matched dangerous pattern, else None.

    Matching is case-insensitive.  Mirrors Hermes' ``detect_dangerous_command``
    but returns just the description (Aegis does not need the pattern key).
    """
    if not command:
        return None
    lowered = command.lower()
    for pattern_re, description in _COMPILED:
        if pattern_re.search(lowered):
            return description
    return None


__all__ = ["DANGEROUS_PATTERNS", "detect_dangerous_command"]
