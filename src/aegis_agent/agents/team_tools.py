"""Team tools: ``team_create`` and ``send_message``.

``team_create`` lets the Main Agent (the lead) create a team and spawn named
teammates into it.  ``send_message`` routes a message between agents over the
team's transport — lead → teammate, or teammate → teammate — enforcing the
team boundary (a sender can only address members of its own team).

Both are ordinary :class:`~aegis_agent.tools.registry.Tool` implementations so
they register into the normal registry and are callable by the model.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from aegis_agent.agents.team import TeamManager
from aegis_agent.models.base import ToolDefinition, ToolResult
from aegis_agent.tools.registry import ToolContext

TEAM_CREATE = ToolDefinition(
    name="team_create",
    description=(
        "Create a team of persistent teammates that work together over "
        "multiple turns, then spawn the initial members. Each teammate is a "
        "long-lived agent with its own continuous context: it works, goes idle, "
        "and wakes when you (or another teammate) send it a message with "
        "send_message. Use for multi-part work that benefits from parallel, "
        "specialised, ongoing agents (research + code + review). For a quick "
        "one-off answer, prefer the Agent tool instead."
    ),
    parameters={
        "type": "object",
        "properties": {
            "description": {
                "type": "string",
                "description": "What this team is for.",
            },
            "members": {
                "type": "array",
                "description": "Initial teammates to spawn (stable names).",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Stable teammate name (e.g. 'researcher', 'coder').",
                        },
                        "agent_type": {
                            "type": "string",
                            "description": "Agent type (default 'general-purpose').",
                        },
                        "task": {
                            "type": "string",
                            "description": "Optional initial task to send on spawn.",
                        },
                    },
                    "required": ["name"],
                },
            },
        },
        "required": [],
    },
)

SEND_MESSAGE = ToolDefinition(
    name="send_message",
    description=(
        "Send a message to another agent in your team. Use a teammate's name "
        "(e.g. 'coder'), 'team-lead' for the lead, or '*' to broadcast to all "
        "other members. An idle teammate wakes on arrival and continues with "
        "its existing context. Messages stay within your team — you cannot "
        "address agents in other teams."
    ),
    parameters={
        "type": "object",
        "properties": {
            "recipient": {
                "type": "string",
                "description": "Teammate name, 'team-lead', or '*' for broadcast.",
            },
            "message": {
                "type": "string",
                "description": "The message content.",
            },
        },
        "required": ["recipient", "message"],
    },
)


class TeamCreateTool:
    """Create a team and spawn its initial teammates (called by the lead)."""

    definition = TEAM_CREATE

    def __init__(self, manager: TeamManager, team_id: str | None = None) -> None:
        self._manager = manager
        # The team this tool is bound to.  The lead's instance starts unbound
        # (team_id=None) and creates+remembers a team on first use; a teammate's
        # instance is pre-bound to its own team.
        self._team_id = team_id

    @property
    def team_id(self) -> str | None:
        return self._team_id

    def run(self, arguments: Mapping[str, Any], context: ToolContext | None = None) -> ToolResult:
        description = arguments.get("description") or ""
        members = arguments.get("members") or []
        try:
            if self._team_id is None:
                team = self._manager.create_team(description=str(description))
                self._team_id = team.team_id
            else:
                team = self._manager.get_team(self._team_id)
                if team is None:
                    return _error(f"team {self._team_id} no longer exists")

            spawned = []
            errors = []
            for m in members:
                if not isinstance(m, dict) or not m.get("name"):
                    errors.append("each member needs a 'name'")
                    continue
                try:
                    self._manager.spawn_teammate(
                        team.team_id,
                        str(m["name"]),
                        str(m.get("agent_type") or "general-purpose"),
                        initial_task=m.get("task"),
                    )
                    spawned.append(str(m["name"]))
                except ValueError as exc:
                    errors.append(str(exc))

            payload = {
                "team_id": team.team_id,
                "lead": team.lead,
                "spawned": spawned,
                "members": [mem.name for mem in self._manager.list_members(team.team_id)],
            }
            if errors:
                payload["errors"] = errors
            return ToolResult(
                tool_call_id="",
                name=self.definition.name,
                content=json.dumps(payload, ensure_ascii=False),
                is_error=bool(errors) and not spawned,
            )
        except ValueError as exc:
            return _error(str(exc))


class SendMessageTool:
    """Send a message to another agent in the sender's team.

    ``team_id=None`` means "the lead's active team": the lead creates its team
    dynamically, so its tool resolves the team id at call time via the manager.
    A teammate's tool is always pre-bound to its own team.
    """

    definition = SEND_MESSAGE

    def __init__(self, manager: TeamManager, team_id: str | None, sender: str) -> None:
        self._manager = manager
        self._team_id = team_id
        self._sender = sender

    def run(self, arguments: Mapping[str, Any], context: ToolContext | None = None) -> ToolResult:
        recipient = arguments.get("recipient")
        message = arguments.get("message")
        if not recipient or not isinstance(recipient, str):
            return _error("send_message: missing required field 'recipient'.")
        if not message or not isinstance(message, str):
            return _error("send_message: missing required field 'message'.")

        team_id = self._team_id if self._team_id is not None else self._manager.lead_team_id()
        if team_id is None:
            return _error(
                "send_message: no team exists yet — create one with team_create first."
            )
        try:
            self._manager.send_message(team_id, self._sender, recipient, message)
        except ValueError as exc:
            return _error(str(exc))
        target = "all teammates" if recipient == "*" else recipient
        return ToolResult(
            tool_call_id="",
            name=self.definition.name,
            content=json.dumps(
                {"success": True, "message": f"Message sent to {target}"}, ensure_ascii=False
            ),
        )


def _error(message: str) -> ToolResult:
    return ToolResult(
        tool_call_id="",
        name="team",
        content=json.dumps({"error": message}, ensure_ascii=False),
        is_error=True,
    )


__all__ = ["SEND_MESSAGE", "TEAM_CREATE", "SendMessageTool", "TeamCreateTool"]
