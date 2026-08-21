"""The ``Agent`` tool — the Main Agent's entry point for spawning a subagent.

An ordinary :class:`~aegis_agent.tools.registry.Tool` invoked by the model with
a tool call.  It delegates to :class:`~aegis_agent.agents.manager.SubagentManager`
(which reuses the shared :class:`~aegis_agent.runtime.AgentRuntime` via the
:class:`~aegis_agent.agents.runner.SubagentRunner`) and returns either the
subagent's final output (foreground) or a "started in background" handle
(background).  The subagent's intermediate model turns, tool calls and tool
results stay in the subagent's private transcript — they never enter the Main
Agent's session.

Parameters (Claude Code parity, minimal):

* ``prompt`` — the complete, self-contained task (required).
* ``subagent_type`` — which agent to use.  Optional: omitting it triggers a
  **fork**, a child that inherits the full conversation context.
* ``run_in_background`` — when True the subagent runs on a thread and this tool
  returns immediately; completion is delivered back as a notification on a
  later turn (no polling).

A subagent's tool set excludes this tool unless its definition opts in
(``allow_agent_tool``) and the depth cap allows it — the recursion guard.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from aegis_agent.agents.definitions import (
    AGENT_TOOL_NAME,
    AgentDefinition,
    fork_agent_definition,
)
from aegis_agent.agents.manager import (
    SubagentManager,
    SubagentTask,
)
from aegis_agent.agents.runner import SubagentResult, SubagentStatus
from aegis_agent.models.base import Message, ToolDefinition, ToolResult
from aegis_agent.tools.registry import ToolContext


def _build_definition(agents: Mapping[str, AgentDefinition], allow_fork: bool) -> ToolDefinition:
    """Build the tool schema, listing the available subagent types inline."""
    types = sorted(agents)
    lines = "\n".join(f"  - {name}: {agents[name].description}" for name in types)
    fork_note = (
        " Omit `subagent_type` to fork a child that inherits this whole "
        "conversation's context."
        if allow_fork
        else ""
    )
    description = (
        "Dispatch a task to a specialised subagent and get back its result. "
        "The subagent runs independently with its own transcript; its "
        "intermediate work is not shown here — only its final report is "
        "returned. For a typed agent, put everything it needs in `prompt` (it "
        "does not see this conversation). Set `run_in_background` to true to "
        "start it without blocking; you'll be notified when it finishes."
        + fork_note
        + "\nAvailable subagent types:\n"
        + lines
    )
    properties: dict[str, Any] = {
        "prompt": {
            "type": "string",
            "description": (
                "The complete, self-contained task for the subagent, including "
                "all background it needs."
            ),
        },
        "run_in_background": {
            "type": "boolean",
            "description": (
                "Run the subagent on a background thread and return immediately "
                "with a task id. You are notified on a later turn when it "
                "finishes; do not poll."
            ),
            "default": False,
        },
    }
    required = ["prompt"]
    if types:
        properties["subagent_type"] = {
            "type": "string",
            "enum": types,
            "description": "Which subagent to dispatch to." + (" Omit to fork." if allow_fork else ""),
        }
        if not allow_fork:
            required.append("subagent_type")
    return ToolDefinition(
        name=AGENT_TOOL_NAME,
        description=description,
        parameters={"type": "object", "properties": properties, "required": required},
    )


class AgentTool:
    """Spawn a subagent (fresh, fork, foreground or background) via the manager."""

    def __init__(
        self,
        manager: SubagentManager,
        *,
        allow_fork: bool = True,
        history_provider: Callable[[str], list[Message]] | None = None,
    ) -> None:
        """Create the tool.

        ``history_provider`` maps a parent session id to its current message
        list; it backs the fork path (omitted ``subagent_type``).  When absent,
        fork requests are rejected with a clear error.
        """
        self._manager = manager
        self._allow_fork = allow_fork
        self._history_provider = history_provider
        self._definition = _build_definition(manager.agents, allow_fork)

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    @property
    def manager(self) -> SubagentManager:
        return self._manager

    def run(self, arguments: Mapping[str, Any], context: ToolContext | None = None) -> ToolResult:
        prompt = arguments.get("prompt")
        if not prompt or not isinstance(prompt, str):
            return self._error("Agent: missing required field 'prompt'.")
        background = bool(arguments.get("run_in_background", False))

        definition, fork_messages, err = self._resolve_definition(arguments, context)
        if err is not None:
            return err
        assert definition is not None

        spawned = self._manager.spawn(
            definition,
            prompt,
            background=background,
            parent_context=context,
            parent_messages=fork_messages,
        )

        if isinstance(spawned, SubagentTask):
            # Background: return a handle immediately; the model is told a
            # notification will follow.  Mirrors Claude's async-from-start path.
            payload = {
                "task_id": spawned.task_id,
                "subagent_type": definition.name,
                "status": "running",
                "background": True,
                "note": (
                    "Subagent started in the background. You will receive a "
                    "completion notification on a later turn; do not poll."
                ),
            }
            return ToolResult(
                tool_call_id="", name=AGENT_TOOL_NAME, content=json.dumps(payload, ensure_ascii=False)
            )

        return self._result_to_tool_result(definition.name, spawned)

    # -- internals ---------------------------------------------------------

    def _resolve_definition(
        self, arguments: Mapping[str, Any], context: ToolContext | None
    ) -> tuple[AgentDefinition | None, list[Message] | None, ToolResult | None]:
        """Pick the agent definition; resolve fork (omitted type) if allowed."""
        agent_type = arguments.get("subagent_type")
        agents = self._manager.agents

        if agent_type is None or agent_type == "":
            # Implicit fork: inherit the parent's conversation.
            if not self._allow_fork:
                return None, None, self._error(
                    "Agent: 'subagent_type' is required (fork is disabled). "
                    f"Available: {', '.join(sorted(agents)) or '(none)'}."
                )
            if self._history_provider is None or context is None or context.session_id is None:
                return None, None, self._error(
                    "Agent: fork requested (no subagent_type) but no parent "
                    "history is available in this context."
                )
            return fork_agent_definition(), self._history_provider(context.session_id), None

        if not isinstance(agent_type, str):
            return None, None, self._error("Agent: 'subagent_type' must be a string.")
        definition = agents.get(agent_type)
        if definition is None:
            available = ", ".join(sorted(agents)) or "(none)"
            return None, None, self._error(
                f"Agent: unknown subagent_type '{agent_type}'. Available: {available}."
            )
        return definition, None, None

    @staticmethod
    def _result_to_tool_result(agent_type: str, result: SubagentResult) -> ToolResult:
        if result.status is SubagentStatus.COMPLETED:
            payload = {
                "subagent_type": agent_type,
                "status": result.status.value,
                "result": result.output,
            }
            return ToolResult(
                tool_call_id="", name=AGENT_TOOL_NAME, content=json.dumps(payload, ensure_ascii=False)
            )
        # FAILED / KILLED: surface a clear error the Main Agent can react to —
        # as a normal (is_error) tool result, never a raised exception.
        reason = result.error or "unknown error"
        return ToolResult(
            tool_call_id="",
            name=AGENT_TOOL_NAME,
            content=json.dumps(
                {"error": f"Subagent '{agent_type}' {result.status.value}: {reason}"},
                ensure_ascii=False,
            ),
            is_error=True,
        )

    @staticmethod
    def _error(message: str) -> ToolResult:
        return ToolResult(
            tool_call_id="",
            name=AGENT_TOOL_NAME,
            content=json.dumps({"error": message}, ensure_ascii=False),
            is_error=True,
        )


__all__ = ["AgentTool"]
