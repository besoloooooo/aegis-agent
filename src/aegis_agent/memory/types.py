# Behavioural reference (adapted and simplified):
#   * Claude Code Auto Memory / ``src/memdir/memoryTypes.ts`` — defines the four
#     memory *kinds* (user / feedback / project / reference) and the behaviour
#     text that tells the model what to store under each.  See
#     ``Claude-Code/docs/08-memory.md``.  Aegis reproduces the four-kind
#     taxonomy verbatim (so a later project-scoped stage can reuse it) but keeps
#     only a compact behaviour description per kind rather than the full
#     eval-tuned prose.
"""The four kinds of long-term memory.

Even though this milestone only stores *personal* (user-level) memory, the kind
taxonomy is kept identical to Claude Code's so project- and team-scoped stages
can reuse it unchanged:

* ``user`` — who the user is (role, expertise, stable preferences).
* ``feedback`` — corrections/approvals the user has given on how to work.
* ``project`` — background on in-progress work not derivable from the code.
* ``reference`` — pointers to external systems (URLs, dashboards, tickets).
"""

from __future__ import annotations

from enum import Enum


class MemoryType(str, Enum):
    """A memory's kind.  ``str`` mix-in so the value round-trips as frontmatter."""

    USER = "user"
    FEEDBACK = "feedback"
    PROJECT = "project"
    REFERENCE = "reference"

    @classmethod
    def parse(cls, raw: object) -> MemoryType | None:
        """Coerce a frontmatter value to a :class:`MemoryType`, or ``None``.

        Matching is case-insensitive and whitespace-tolerant; an unknown or
        missing value returns ``None`` so a malformed memory degrades to
        "untyped" rather than breaking the scan.
        """
        if isinstance(raw, cls):
            return raw
        if not isinstance(raw, str):
            return None
        try:
            return cls(raw.strip().lower())
        except ValueError:
            return None


__all__ = ["MemoryType"]
