# Portions adapted from Hermes (hermes-agent), © 2025 Nous Research.
# Licensed under the MIT License. See THIRD_PARTY_NOTICES.md.
#
# Behavioural source (adapted and simplified):
#   * ``agent/skill_utils.py`` (© 2025 Nous Research, MIT) — the SKILL.md walk
#     (``iter_skill_index_files``), name/description length caps
#     (``MAX_NAME_LENGTH`` / ``MAX_DESCRIPTION_LENGTH``) and platform gating
#     (``skill_matches_platform``, with the macos→darwin / windows→win32
#     mapping).  Aegis discovers from a single user directory only (no bundled
#     skills, no external-dir config, no plugin namespaces) and drops the
#     mtime-cached directory indices — discovery is cheap and explicit.
"""Discover skills from the filesystem.

:class:`SkillLoader` walks one or more directories for ``SKILL.md`` files,
parses their frontmatter, and returns validated :class:`Skill` objects.  The
default search root is a single user directory — ``$AEGIS_SKILLS_DIR`` if set,
otherwise ``~/.aegis/skills`` — matching the "user dir only" scope for this
milestone.  A missing directory yields no skills (never an error), and one
malformed skill is skipped without aborting the rest.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

from aegis_agent.skills.frontmatter import parse_frontmatter
from aegis_agent.skills.models import Skill, SkillMeta

logger = logging.getLogger(__name__)

#: Environment variable that overrides the default skills directory.
DEFAULT_SKILLS_ENV_VAR = "AEGIS_SKILLS_DIR"

#: Frontmatter validation caps (mirrors Hermes).
MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024

_SKILL_INDEX_FILENAME = "SKILL.md"

#: Directory names pruned from the walk (VCS / packaging / skill sub-assets).
_EXCLUDED_DIR_NAMES = frozenset(
    {".git", ".hg", ".svn", "__pycache__", "node_modules", "references", "templates", "scripts", "assets"}
)


def default_skills_dirs() -> list[Path]:
    """Return the default search roots: ``$AEGIS_SKILLS_DIR`` or ``~/.aegis/skills``."""
    override = os.environ.get(DEFAULT_SKILLS_ENV_VAR)
    if override:
        return [Path(override).expanduser()]
    return [Path.home() / ".aegis" / "skills"]


class SkillLoader:
    """Load and cache skills discovered under one or more directories."""

    def __init__(self, dirs: Sequence[str | Path] | None = None) -> None:
        if dirs is None:
            self._dirs = default_skills_dirs()
        else:
            self._dirs = [Path(d).expanduser().resolve() for d in dirs]
        self._skills: dict[str, Skill] | None = None
        self._cached_list: list[Skill] | None = None

    @property
    def dirs(self) -> list[Path]:
        return list(self._dirs)

    def discover(self, *, force: bool = False) -> list[Skill]:
        """Return all valid skills, parsing on first call (cached thereafter).

        Pass ``force=True`` to re-scan (used by an explicit reload).  On name
        collisions the first-seen skill wins and the later one is logged and
        skipped.
        """
        if self._skills is not None and self._cached_list is not None and not force:
            return self._cached_list

        self._skills = None
        self._cached_list = None
        skills: dict[str, Skill] = {}
        for skill_md in self._iter_skill_files():
            skill = self._load_one(skill_md)
            if skill is None:
                continue
            if skill.name in skills:
                logger.warning(
                    "skill name collision: %r already loaded from %s; skipping %s",
                    skill.name,
                    skills[skill.name].skill_md_path,
                    skill_md,
                )
                continue
            skills[skill.name] = skill

        self._skills = skills
        self._cached_list = list(skills.values())
        return self._cached_list

    def get(self, name: str) -> Skill | None:
        """Look up a discovered skill by exact name."""
        if self._skills is None:
            self.discover()
        assert self._skills is not None
        return self._skills.get(name)

    def metas(self) -> list[SkillMeta]:
        """Return the tier-1 index (name/description/category) for all skills."""
        return [skill.meta for skill in self.discover()]

    # -- internals ---------------------------------------------------------

    def _iter_skill_files(self) -> Iterable[Path]:
        """Yield every ``SKILL.md`` under the search roots, in sorted order."""
        seen: set[Path] = set()
        for root in self._dirs:
            if not root.is_dir():
                continue
            for dirpath, dirnames, filenames in os.walk(root):
                # Prune excluded subdirectories in place.
                dirnames[:] = sorted(d for d in dirnames if d not in _EXCLUDED_DIR_NAMES)
                if _SKILL_INDEX_FILENAME in filenames:
                    path = Path(dirpath) / _SKILL_INDEX_FILENAME
                    resolved = path.resolve()
                    if resolved not in seen:
                        seen.add(resolved)
                        yield path

    def _load_one(self, skill_md: Path) -> Skill | None:
        """Parse and validate one ``SKILL.md`` file, or ``None`` if unusable."""
        try:
            content = skill_md.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("could not read skill %s: %s", skill_md, exc)
            return None

        frontmatter, body = parse_frontmatter(content)

        name = str(frontmatter.get("name", "")).strip()
        description = str(frontmatter.get("description", "")).strip()
        if not name or not description:
            logger.warning("skipping skill %s: missing 'name' or 'description'", skill_md)
            return None
        if len(name) > MAX_NAME_LENGTH:
            logger.warning("skipping skill %s: name exceeds %d chars", skill_md, MAX_NAME_LENGTH)
            return None
        if len(description) > MAX_DESCRIPTION_LENGTH:
            description = description[:MAX_DESCRIPTION_LENGTH]

        if not _matches_platform(frontmatter):
            logger.debug("skipping skill %s: platform gate excludes %s", skill_md, sys.platform)
            return None

        directory = skill_md.parent
        # A skill directly under a search root has no category; one nested
        # deeper derives the category from its immediate parent dir name.
        category = ""
        parent = directory.parent
        if parent != directory and parent.resolve() not in self._dirs:
            category = parent.name

        return Skill(
            name=name,
            description=description,
            directory=directory,
            skill_md_path=skill_md,
            category=category,
            frontmatter=frontmatter,
            body=body,
        )


def _matches_platform(frontmatter: dict) -> bool:
    """Return True when the skill's ``platforms`` list includes the current OS.

    Adapted from Hermes ``skill_matches_platform``: an absent/empty ``platforms``
    key means "all platforms"; declared names are normalised (``macos``→
    ``darwin``, ``windows``→``win32``) before matching :data:`sys.platform`.
    """
    platforms = frontmatter.get("platforms")
    if not platforms or not isinstance(platforms, (list, tuple)):
        return True

    current = sys.platform
    for raw in platforms:
        name = str(raw).strip().lower()
        if name in ("macos", "mac", "osx", "darwin"):
            normalized = "darwin"
        elif name in ("windows", "win", "win32"):
            normalized = "win32"
        elif name in ("linux",):
            normalized = "linux"
        else:
            normalized = name
        if current == normalized or (normalized == "linux" and current.startswith("linux")):
            return True
    return False


__all__ = [
    "DEFAULT_SKILLS_ENV_VAR",
    "MAX_DESCRIPTION_LENGTH",
    "MAX_NAME_LENGTH",
    "SkillLoader",
    "default_skills_dirs",
]
