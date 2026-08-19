# Behavioural reference (adapted and simplified):
#   * Claude Code Auto Memory / ``src/memdir/paths.ts`` — resolves a per-scope
#     memory directory and a ``MEMORY.md`` index inside it, keying the *project*
#     scope on the canonical git root (``findCanonicalGitRoot``) so every
#     worktree / subdirectory of one repo shares a single memory dir.  See
#     ``Claude-Code/docs/08-memory.md`` in the workspace.
#
# Aegis implements two scopes:
#   * *personal* — a single home dir (``$AEGIS_HOME`` / ``~/.aegis``) holding
#     ``USER.md`` + a ``memory/`` subdir (index + bodies).
#   * *project*  — a per-project dir ``<home>/projects/<project-id>`` holding
#     only a ``memory/`` subdir.  It has NO ``USER.md`` of its own: the user
#     profile is global and read in both scopes.
# The whole read/write pipeline already threads a ``home`` argument and derives
# ``<home>/memory`` from it, so project scope is just "point the same pipeline at
# the project home".  Team-scoped dirs and the settings-driven override chain are
# intentionally dropped.
"""Filesystem layout for long-term memory (personal + project scopes).

The personal memory home defaults to ``$AEGIS_HOME`` or ``~/.aegis`` and lays
out as::

    <home>/
    ├── USER.md                     # stable user profile (GLOBAL — both scopes)
    ├── memory/                     # personal scope
    │   ├── MEMORY.md               # the auto-memory *index* (one line each)
    │   └── *.md                    # individual memory bodies
    └── projects/
        └── <project-id>/
            └── memory/             # project scope (isolated per project)
                ├── MEMORY.md
                └── *.md

``USER.md`` and the auto-memory index are deliberately separate concerns (a
stable profile vs. a growing memory index), so they get distinct resolvers and
are injected as distinct prompt sections.  ``USER.md`` always resolves against
the *global* home — a project never gets its own profile.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from enum import Enum
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
_PROJECTS_SUBDIR = "projects"


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


# ── project scope ────────────────────────────────────────────────────────────


def _find_git_root(start: Path) -> Path | None:
    """Return the nearest ancestor of ``start`` containing ``.git``, or ``None``.

    ``.git`` counts whether it is a directory (a normal clone) or a file (a git
    worktree / submodule gitlink) — so every worktree of one repository resolves
    to the same root, mirroring Claude Code's ``findCanonicalGitRoot``.  Purely
    filesystem-based (no ``git`` subprocess) so it stays cheap and offline.
    """
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _sanitize_slug(text: str) -> str:
    """Reduce ``text`` to a safe, bare path segment (no separators / dots)."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-._")
    return slug or "project"


def project_id(project_path: str | os.PathLike[str]) -> str:
    """Return a stable, filesystem-safe id for ``project_path``.

    The id keys on the *canonical git root* of the path (falling back to the
    resolved path itself when it is not inside a git repository), so any
    subdirectory or git worktree of one repository maps to the same id.  The
    result is ``<root-basename>-<hash>`` where ``hash`` is a short digest of the
    full resolved root path — the basename keeps ids human-recognisable while the
    hash prevents collisions between like-named directories.  Deterministic: the
    same path always yields the same id (no time / randomness).
    """
    resolved = Path(project_path).expanduser().resolve()
    root = _find_git_root(resolved) or resolved
    digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:8]
    base = _sanitize_slug(root.name or "root")
    return f"{base}-{digest}"


def projects_dir(home: str | os.PathLike[str] | None = None) -> Path:
    """Return the parent directory holding all per-project memory homes."""
    return aegis_home(home) / _PROJECTS_SUBDIR


def project_home(
    project_path: str | os.PathLike[str],
    home: str | os.PathLike[str] | None = None,
) -> Path:
    """Return the per-project memory home ``<home>/projects/<project-id>``.

    This is handed to the ordinary read/write pipeline as its ``home``, so
    ``memory_dir(project_home(...))`` is ``<...>/projects/<id>/memory`` and the
    project index / bodies live beside it — no pipeline changes required.  The
    directory is not created here (the store creates it on first write).
    """
    return projects_dir(home) / project_id(project_path)


class MemoryScopeKind(str, Enum):
    """Which memory scope is active for a run."""

    PERSONAL = "personal"
    PROJECT = "project"


@dataclass(frozen=True)
class MemoryScope:
    """Resolved memory scope: the single object the runtime/CLI pass around.

    ``memory_home`` is the ``home`` fed to the read/write pipeline (personal home
    or a project home).  ``profile_path`` is ALWAYS the global ``USER.md`` — a
    project never gets its own profile.  ``project_id`` and ``project_root`` are
    set only for the project scope (the root is the directory the user pointed
    ``--project`` at — used for the system prompt / tool cwd, not for storage).
    """

    kind: MemoryScopeKind
    memory_home: Path
    profile_path: Path
    project_id: str | None = None
    project_root: Path | None = None

    @property
    def is_project(self) -> bool:
        return self.kind is MemoryScopeKind.PROJECT

    @classmethod
    def personal(cls, home: str | os.PathLike[str] | None = None) -> MemoryScope:
        """Resolve the personal scope (memory + profile under the global home)."""
        return cls(
            kind=MemoryScopeKind.PERSONAL,
            memory_home=aegis_home(home),
            profile_path=user_profile_path(home),
        )

    @classmethod
    def project(
        cls,
        project_path: str | os.PathLike[str],
        home: str | os.PathLike[str] | None = None,
    ) -> MemoryScope:
        """Resolve the project scope: project memory home + GLOBAL ``USER.md``."""
        return cls(
            kind=MemoryScopeKind.PROJECT,
            memory_home=project_home(project_path, home),
            profile_path=user_profile_path(home),  # global — never project-local
            project_id=project_id(project_path),
            project_root=Path(project_path).expanduser().resolve(),
        )


def resolve_scope(
    project_path: str | os.PathLike[str] | None,
    home: str | os.PathLike[str] | None = None,
) -> MemoryScope:
    """Resolve to the project scope when ``project_path`` is given, else personal."""
    if project_path is None:
        return MemoryScope.personal(home)
    return MemoryScope.project(project_path, home)


__all__ = [
    "AEGIS_HOME_ENV_VAR",
    "AEGIS_MEMORY_DIR_ENV_VAR",
    "MemoryScope",
    "MemoryScopeKind",
    "aegis_home",
    "memory_dir",
    "memory_index_path",
    "project_home",
    "project_id",
    "projects_dir",
    "resolve_scope",
    "user_profile_path",
]
