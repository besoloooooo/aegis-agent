# Behavioural reference (adapted and heavily simplified):
#   * Claude Code Auto Memory / ``src/memdir/paths.ts`` — resolves a per-scope
#     memory directory and a ``MEMORY.md`` index inside it.  See
#     ``Claude-Code/docs/08-memory.md`` in the workspace.  Aegis implements only
#     the *personal* (user-level) scope for this milestone: a single home dir
#     holding ``USER.md`` plus a ``memory/`` subdirectory whose ``MEMORY.md`` is
#     the index and whose other ``*.md`` files are the memory bodies.  Project-
#     and team-scoped directories, the settings-driven override chain and the
#     git-root canonicalisation are intentionally dropped.
"""Filesystem layout for personal long-term memory.

The personal memory home defaults to ``$AEGIS_HOME`` or ``~/.aegis`` and lays
out as::

    <home>/
    ├── USER.md            # stable user profile / long-term preferences
    └── memory/
        ├── MEMORY.md      # the auto-memory *index* (one line per memory)
        └── *.md           # individual memory bodies (frontmatter + text)

``USER.md`` and the auto-memory index are deliberately separate concerns (a
stable profile vs. a growing memory index), so they get distinct resolvers and
are injected as distinct prompt sections.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Environment variable overriding the personal memory home (``~/.aegis``).
AEGIS_HOME_ENV_VAR = "AEGIS_HOME"

#: Environment variable overriding the memory *directory* directly (takes
#: precedence over the home-derived ``<home>/memory``).  Mirrors the
#: ``AEGIS_SKILLS_DIR`` escape hatch used by the skills loader.
AEGIS_MEMORY_DIR_ENV_VAR = "AEGIS_MEMORY_DIR"

_USER_PROFILE_FILENAME = "USER.md"
_MEMORY_SUBDIR = "memory"
_MEMORY_INDEX_FILENAME = "MEMORY.md"


def aegis_home(home: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the personal memory home directory.

    Explicit ``home`` wins (used by tests), then ``$AEGIS_HOME``, then the
    ``~/.aegis`` default.  The path is expanded but not required to exist.
    """
    if home is not None:
        return Path(home).expanduser()
    override = os.environ.get(AEGIS_HOME_ENV_VAR)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".aegis"


def user_profile_path(home: str | os.PathLike[str] | None = None) -> Path:
    """Return the path to ``USER.md`` under the memory home."""
    return aegis_home(home) / _USER_PROFILE_FILENAME


def memory_dir(home: str | os.PathLike[str] | None = None) -> Path:
    """Return the auto-memory directory (``<home>/memory`` by default).

    ``$AEGIS_MEMORY_DIR`` overrides the location directly, independent of the
    home; otherwise the directory is derived from :func:`aegis_home`.
    """
    override = os.environ.get(AEGIS_MEMORY_DIR_ENV_VAR)
    if override:
        return Path(override).expanduser()
    return aegis_home(home) / _MEMORY_SUBDIR


def memory_index_path(home: str | os.PathLike[str] | None = None) -> Path:
    """Return the path to the ``MEMORY.md`` index inside the memory directory."""
    return memory_dir(home) / _MEMORY_INDEX_FILENAME


__all__ = [
    "AEGIS_HOME_ENV_VAR",
    "AEGIS_MEMORY_DIR_ENV_VAR",
    "aegis_home",
    "memory_dir",
    "memory_index_path",
    "user_profile_path",
]
