"""Skill data structures.

Two records: :class:`Skill` — a fully-parsed skill (frontmatter + body + the
on-disk locations of its supporting files) — and :class:`SkillMeta`, the
name/description/category triple that makes up the tier-1 index advertised to
the model.  Keeping the index a separate lightweight type mirrors Hermes'
progressive-disclosure split (``skills_list`` returns metadata only; the full
body is fetched on demand).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SkillMeta:
    """The token-cheap index entry for a skill (tier 1)."""

    name: str
    description: str
    category: str = ""


@dataclass
class Skill:
    """A parsed skill loaded from a ``SKILL.md`` directory.

    ``directory`` is the skill's root; ``skill_md_path`` is the ``SKILL.md``
    file inside it.  ``frontmatter`` is the raw parsed YAML mapping and ``body``
    is the markdown that follows it.  ``category`` is derived from the parent
    folder name at load time.
    """

    name: str
    description: str
    directory: Path
    skill_md_path: Path
    category: str = ""
    frontmatter: dict[str, Any] = field(default_factory=dict)
    body: str = ""

    @property
    def meta(self) -> SkillMeta:
        return SkillMeta(name=self.name, description=self.description, category=self.category)


__all__ = ["Skill", "SkillMeta"]
