"""``skill_manage`` tool — install/uninstall/update/list skills.

REWRITE (follows Hermes' ``skill_manage`` tool surface for the hub actions):
``{action, source?, name?, force?}`` → ``{success, ...}`` / ``{success: false, error}``.

Delegates to :mod:`aegis_agent.skills.install` and the existing
:class:`~aegis_agent.skills.loader.SkillLoader`.  Registered inside
:meth:`~aegis_agent.runtime.AgentRuntime.with_defaults` (``enable_skills``
branch) alongside the existing ``skills_list`` / ``skill_view`` tools.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from aegis_agent.models.base import ToolDefinition, ToolResult
from aegis_agent.skills import install
from aegis_agent.skills.loader import SkillLoader
from aegis_agent.tools.registry import ToolContext

SKILL_MANAGE = ToolDefinition(
    name="skill_manage",
    description=(
        "Install, uninstall, update, or list skills. Skills are stored in the "
        "skills directory and automatically discovered. Install from a local "
        "directory containing a SKILL.md, or from a direct URL to a SKILL.md "
        "file. Only skills installed via this tool can be uninstalled/updated "
        "(a lock file tracks their provenance)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["install", "uninstall", "update", "list"],
                "description": "The management action.",
            },
            "source": {
                "type": "string",
                "description": "For install/update: a local directory path or a URL to a SKILL.md file.",
            },
            "name": {
                "type": "string",
                "description": "Skill name for uninstall/update; optional for install (override).",
            },
            "force": {
                "type": "boolean",
                "description": "Reinstall or overwrite an existing skill.",
                "default": False,
            },
        },
        "required": ["action"],
    },
)


class SkillManageTool:
    """Manage skills: install/uninstall/update/list."""

    definition = SKILL_MANAGE

    def __init__(self, loader: SkillLoader) -> None:
        self._loader = loader

    @property
    def _skills_dir(self) -> Path:
        return self._loader.dirs[0]

    def run(self, arguments: Mapping[str, Any], context: ToolContext | None = None) -> ToolResult:
        action = arguments.get("action")
        if action not in ("install", "uninstall", "update", "list"):
            return _error("skill_manage: 'action' must be one of install/uninstall/update/list.")

        if action == "list":
            payload = {"installed": install.list_installed(self._skills_dir)}
            return _ok(self.definition.name, payload)

        name = arguments.get("name")
        if action in ("uninstall", "update") and (not name or not isinstance(name, str)):
            return _error(f"skill_manage: '{action}' requires 'name'.")

        if action == "uninstall":
            result = install.uninstall_skill(name, self._skills_dir, self._loader)
        elif action == "update":
            result = install.update_skill(name, self._skills_dir, self._loader)
        else:  # install
            source = arguments.get("source")
            if not source or not isinstance(source, str):
                return _error("skill_manage: 'install' requires 'source' (local path or URL).")
            force = bool(arguments.get("force", False))
            name_override = name if isinstance(name, str) and name else None
            result = install.install_skill(
                source, self._skills_dir, self._loader, name=name_override, force=force,
            )

        content = json.dumps(result, ensure_ascii=False)
        is_error = not result.get("success", False)
        return ToolResult(tool_call_id="", name=self.definition.name, content=content, is_error=is_error)


def _ok(name: str, payload: dict) -> ToolResult:
    return ToolResult(tool_call_id="", name=name, content=json.dumps(payload, ensure_ascii=False))


def _error(message: str) -> ToolResult:
    return ToolResult(
        tool_call_id="",
        name="skill_manage",
        content=json.dumps({"success": False, "error": message}, ensure_ascii=False),
        is_error=True,
    )


__all__ = ["SKILL_MANAGE", "SkillManageTool"]
