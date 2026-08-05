"""Lightweight Skill loading, routing and prompt injection.

A *skill* is a directory containing a ``SKILL.md`` file (YAML frontmatter + a
markdown body) plus optional supporting files (``references/``, ``templates/``,
``scripts/``).  Skills are discovered from a user directory (default
``~/.aegis/skills``), advertised to the model as a compact *index* in the system
prompt, and disclosed in full on demand — either when the model calls the
``skill_view`` tool (progressive disclosure) or when the user types a
``/skill-name`` slash command.

This subsystem is an *adapted* port of Hermes' skills implementation, reduced to
the lightweight surface Aegis needs.  See ``docs/source-map.md`` for the
provenance of each piece.
"""

from __future__ import annotations

from aegis_agent.skills.frontmatter import parse_frontmatter
from aegis_agent.skills.loader import (
    DEFAULT_SKILLS_ENV_VAR,
    MAX_DESCRIPTION_LENGTH,
    MAX_NAME_LENGTH,
    SkillLoader,
    default_skills_dirs,
)
from aegis_agent.skills.models import Skill, SkillMeta
from aegis_agent.skills.prompt import SkillsIndexContributor
from aegis_agent.skills.router import DefaultSkillRouter, SkillRouter
from aegis_agent.skills.tools import SkillsListTool, SkillViewTool

__all__ = [
    "DEFAULT_SKILLS_ENV_VAR",
    "MAX_DESCRIPTION_LENGTH",
    "MAX_NAME_LENGTH",
    "DefaultSkillRouter",
    "Skill",
    "SkillLoader",
    "SkillMeta",
    "SkillRouter",
    "SkillViewTool",
    "SkillsIndexContributor",
    "SkillsListTool",
    "default_skills_dirs",
    "parse_frontmatter",
]
