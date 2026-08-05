# Portions adapted from Hermes (hermes-agent), © 2025 Nous Research.
# Licensed under the MIT License. See THIRD_PARTY_NOTICES.md.
#
# Behavioural source (adapted and simplified):
#   * ``agent/prompt_builder.py:build_skills_system_prompt`` (© 2025 Nous
#     Research, MIT) — emits a compact ``<available_skills>`` index (name +
#     description, grouped by category) into the stable system-prompt section,
#     instructing the model to fetch full instructions on demand.  Aegis keeps
#     the index + progressive-disclosure guidance but drops the two-layer
#     on-disk prompt-snapshot cache and the conditional fallback/requires
#     visibility rules.
"""Inject the skills index into the system prompt.

:class:`SkillsIndexContributor` is a :class:`~aegis_agent.context.system_prompt.
PromptContributor`: on every prompt build it renders a compact listing of the
available skills (name + description, grouped by category) plus a one-line
instruction telling the model to call ``skill_view`` for the full instructions.
Skill *bodies* are never placed in the prompt — only this index — which is the
progressive-disclosure design.  When no skills are loaded it renders nothing.
"""

from __future__ import annotations

from aegis_agent.skills.loader import SkillLoader

_HEADER = (
    "## Skills\n"
    "The following skills are available. Each is a set of instructions for a "
    "specific kind of task. When a user's request matches a skill, call the "
    "`skill_view` tool with the skill's name to load its full instructions "
    "before proceeding. Use `skills_list` to browse them."
)


class SkillsIndexContributor:
    """Render the tier-1 skills index for the system prompt."""

    def __init__(self, loader: SkillLoader) -> None:
        self._loader = loader

    def render(self) -> str | None:
        metas = self._loader.metas()
        if not metas:
            return None

        # Group by category, preserving first-seen category order.
        grouped: dict[str, list[str]] = {}
        for meta in metas:
            category = meta.category or "general"
            grouped.setdefault(category, []).append(f"- {meta.name}: {meta.description}")

        lines: list[str] = [_HEADER, "", "<available_skills>"]
        for category, entries in grouped.items():
            lines.append(f"### {category}")
            lines.extend(entries)
        lines.append("</available_skills>")
        return "\n".join(lines)


__all__ = ["SkillsIndexContributor"]
