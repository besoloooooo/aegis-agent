# Portions adapted from Hermes (hermes-agent), © 2025 Nous Research.
# Licensed under the MIT License. See THIRD_PARTY_NOTICES.md.
#
# Behavioural source (adapted and simplified):
#   * ``tools/skills_tool.py:skills_list`` / ``skill_view`` (© 2025 Nous
#     Research, MIT) — the two progressive-disclosure tools: ``skills_list``
#     returns the tier-1 index; ``skill_view`` returns a skill's full SKILL.md
#     body, or a specific supporting file *within the skill directory* (guarded
#     against path traversal).  Aegis keeps the two-tool shape and the traversal
#     guard but drops the prompt-injection scanner, credential/setup checks,
#     collision reporting across dirs, plugin-namespace handling and usage
#     telemetry.
"""The ``skills_list`` and ``skill_view`` tools (progressive disclosure).

These implement Aegis's :class:`~aegis_agent.tools.registry.Tool` Protocol so
they register into the ordinary :class:`~aegis_agent.tools.registry.ToolRegistry`
alongside the builtin tools.  ``skills_list`` returns the metadata index;
``skill_view`` returns a skill's full instructions (or one supporting file).
Every failure is returned as an ``{"error": ...}`` result, never raised, so the
executor contract (a tool never crashes the loop) holds.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from aegis_agent.models.base import ToolDefinition, ToolResult
from aegis_agent.skills.loader import SkillLoader
from aegis_agent.tools.registry import ToolContext

SKILLS_LIST = ToolDefinition(
    name="skills_list",
    description=(
        "List the available skills (name, description, category). Skills are "
        "reusable instruction sets for specific tasks. Call this to browse what "
        "is available, then use skill_view to load a skill's full instructions."
    ),
    parameters={
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "description": "Optional category to filter by (case-insensitive).",
            },
        },
        "required": [],
    },
)

SKILL_VIEW = ToolDefinition(
    name="skill_view",
    description=(
        "Load a skill's full instructions by name. Optionally pass file_path to "
        "read a specific supporting file within the skill's directory (e.g. "
        "'references/guide.md'). Call this before acting on a skill."
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "The skill name to view."},
            "file_path": {
                "type": "string",
                "description": "Optional path to a supporting file, relative to the skill directory.",
            },
        },
        "required": ["name"],
    },
)


class SkillsListTool:
    """Return the tier-1 skills index as JSON."""

    definition = SKILLS_LIST

    def __init__(self, loader: SkillLoader) -> None:
        self._loader = loader

    def run(self, arguments: Mapping[str, Any], context: ToolContext | None = None) -> ToolResult:
        category = arguments.get("category")
        metas = self._loader.metas()
        if isinstance(category, str) and category.strip():
            wanted = category.strip().lower()
            metas = [m for m in metas if (m.category or "general").lower() == wanted]
        payload = {
            "skills": [
                {"name": m.name, "description": m.description, "category": m.category or "general"}
                for m in metas
            ],
            "count": len(metas),
        }
        return ToolResult(
            tool_call_id="",
            name=self.definition.name,
            content=json.dumps(payload, ensure_ascii=False),
        )


class SkillViewTool:
    """Return a skill's full body, or a specific supporting file."""

    definition = SKILL_VIEW

    def __init__(self, loader: SkillLoader) -> None:
        self._loader = loader

    def run(self, arguments: Mapping[str, Any], context: ToolContext | None = None) -> ToolResult:
        name = arguments.get("name")
        if not name or not isinstance(name, str):
            return _error("skill_view: missing required field 'name'.")

        skill = self._loader.get(name)
        if skill is None:
            available = ", ".join(m.name for m in self._loader.metas()) or "(none)"
            return _error(f"Unknown skill: {name!r}. Available skills: {available}")

        file_path = arguments.get("file_path")
        if file_path and isinstance(file_path, str) and file_path.strip():
            return self._read_supporting_file(skill.directory, file_path.strip(), skill.name)

        payload = {
            "name": skill.name,
            "description": skill.description,
            "category": skill.category or "general",
            "directory": str(skill.directory),
            "content": skill.body,
        }
        return ToolResult(
            tool_call_id="",
            name=self.definition.name,
            content=json.dumps(payload, ensure_ascii=False),
        )

    def _read_supporting_file(self, skill_dir: Path, file_path: str, skill_name: str) -> ToolResult:
        """Read a file within ``skill_dir``, rejecting any path escape."""
        candidate = Path(file_path)
        if candidate.is_absolute():
            return _error(f"skill_view: file_path must be relative, got {file_path!r}.")

        base = skill_dir.resolve()
        target = (base / candidate).resolve()
        # Traversal guard: the resolved target must stay within the skill dir.
        if base != target and base not in target.parents:
            return _error(f"skill_view: file_path escapes the skill directory: {file_path!r}.")
        if not target.is_file():
            return _error(f"skill_view: file not found in skill {skill_name!r}: {file_path!r}.")

        try:
            content = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return _error(f"skill_view: could not read {file_path!r}: {exc}")

        payload = {
            "name": skill_name,
            "file_path": file_path,
            "content": content,
        }
        return ToolResult(
            tool_call_id="",
            name="skill_view",
            content=json.dumps(payload, ensure_ascii=False),
        )


def _error(message: str) -> ToolResult:
    return ToolResult(
        tool_call_id="",
        name="skill_view",
        content=json.dumps({"error": message}, ensure_ascii=False),
        is_error=True,
    )


__all__ = ["SKILLS_LIST", "SKILL_VIEW", "SkillViewTool", "SkillsListTool"]
