# Portions adapted from Hermes (hermes-agent), © 2025 Nous Research.
# Licensed under the MIT License. See THIRD_PARTY_NOTICES.md.
#
# Behavioural source (adapted and simplified):
#   * ``agent/skill_commands.py`` (© 2025 Nous Research, MIT) — slash-command
#     resolution (``resolve_skill_command_key``, hyphen-slug normalisation) and
#     the invoked-skill message wrapper (``build_skill_invocation_message`` /
#     ``_build_skill_message``: an activation note + the skill body + the skill
#     directory + a supporting-files listing).  Aegis keeps the resolution and
#     message shape but drops template-var substitution, inline-shell expansion,
#     config resolution, and the platform-keyed command cache.
"""Resolve and activate skills by name.

Two selection paths exist (mirroring Hermes): the model can call the
``skill_view`` tool (handled in ``tools.py``), or the user can type a
``/skill-name`` slash command.  :class:`DefaultSkillRouter` handles the latter:
it normalises the typed token to a slug, resolves it against the loader, and
builds the message that gets fed into the turn — an activation note wrapping the
skill's full body so the model treats it as instructions to follow now.

Routing is deterministic (slug match), not model- or embedding-based; Hermes has
no LLM router either.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from aegis_agent.skills.loader import SkillLoader
from aegis_agent.skills.models import Skill

_SUPPORTING_SUBDIRS = ("references", "templates", "scripts", "assets")


def normalize_skill_key(token: str) -> str:
    """Normalise a user token (``/My_Skill``) to a comparison slug (``my-skill``)."""
    token = token.strip()
    token = token.removeprefix("/")
    return token.strip().lower().replace("_", "-").replace(" ", "-")


@runtime_checkable
class SkillRouter(Protocol):
    """Resolve a skill name/slug and build its activation message."""

    def resolve(self, name: str) -> Skill | None:
        ...

    def invocation_message(self, skill: Skill, instruction: str = "") -> str:
        ...


class DefaultSkillRouter:
    """Slug-based :class:`SkillRouter` backed by a :class:`SkillLoader`."""

    def __init__(self, loader: SkillLoader) -> None:
        self._loader = loader

    def resolve(self, name: str) -> Skill | None:
        """Resolve ``name`` to a skill by exact name first, then by slug."""
        exact = self._loader.get(name)
        if exact is not None:
            return exact
        target = normalize_skill_key(name)
        for skill in self._loader.discover():
            if normalize_skill_key(skill.name) == target:
                return skill
        return None

    def invocation_message(self, skill: Skill, instruction: str = "") -> str:
        """Build the message injected when a skill is explicitly invoked.

        Wraps the skill body with an activation note, the skill directory (so
        tools/the model can reference supporting files by path) and a listing of
        available supporting files.  A trailing ``instruction`` (the rest of the
        slash-command line) is appended as the user's concrete request.
        """
        parts: list[str] = [
            (
                f'[The "{skill.name}" skill was invoked. Follow its instructions below '
                f"for this task.]"
            ),
            "",
            skill.body.strip(),
            "",
            f"[Skill directory: {skill.directory}]",
        ]

        supporting = self._supporting_files(skill)
        if supporting:
            parts.append("[Supporting files: " + ", ".join(supporting) + "]")

        instruction = instruction.strip()
        if instruction:
            parts.extend(["", instruction])

        return "\n".join(parts)

    def _supporting_files(self, skill: Skill) -> list[str]:
        """List relative paths of supporting files under known subdirectories."""
        found: list[str] = []
        for sub in _SUPPORTING_SUBDIRS:
            subdir = skill.directory / sub
            if not subdir.is_dir():
                continue
            for path in sorted(subdir.rglob("*")):
                if path.is_file():
                    found.append(str(path.relative_to(skill.directory)))
        return found


__all__ = ["DefaultSkillRouter", "SkillRouter", "normalize_skill_key"]
