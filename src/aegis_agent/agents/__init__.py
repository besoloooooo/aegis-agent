"""Subagents: spawn an independent agent that reuses the Agent Runtime.

* :mod:`~aegis_agent.agents.definitions` — declarative :class:`AgentDefinition`
  objects (identity, tool whitelist, iteration cap, fork/nesting flags) and the
  built-ins (``explore``, ``general-purpose``, plus the implicit ``fork``).
* :mod:`~aegis_agent.agents.runner` — :class:`SubagentRunner`: turns a
  definition + prompt into a *configured* :class:`AgentRuntime` run with an
  isolated transcript, returning the final result + transcript.
* :mod:`~aegis_agent.agents.manager` — :class:`SubagentManager`: task lifecycle
  (running→completed/failed/killed), background threads, concurrency/depth
  limits, and the completion-notification queue.
* :mod:`~aegis_agent.agents.agent_tool` — :class:`AgentTool`, the Main Agent's
  tool for dispatching a task (fresh / fork, foreground / background).

No new agent loop is defined here: a subagent is the same
:class:`~aegis_agent.runtime.AgentRuntime`, re-instantiated with a different
:class:`~aegis_agent.runtime.AgentConfig`.
"""

from __future__ import annotations

from aegis_agent.agents.agent_tool import AgentTool
from aegis_agent.agents.definitions import (
    AGENT_TOOL_NAME,
    BUILTIN_AGENTS,
    FORK_SUBAGENT_TYPE,
    READ_ONLY_TOOL_NAMES,
    AgentDefinition,
    builtin_agents,
    fork_agent_definition,
)
from aegis_agent.agents.manager import (
    DEFAULT_MAX_CONCURRENT,
    DEFAULT_MAX_DEPTH,
    SubagentManager,
    SubagentTask,
    TaskNotification,
    TaskStatus,
)
from aegis_agent.agents.messaging import (
    AgentMessage,
    AgentTransport,
    InProcessTransport,
    MessageType,
)
from aegis_agent.agents.runner import SubagentResult, SubagentRunner, SubagentStatus
from aegis_agent.agents.team import LEAD_NAME, Team, TeamManager, TeamMember
from aegis_agent.agents.team_tools import SendMessageTool, TeamCreateTool
from aegis_agent.agents.teammate import PersistentTeammate, TeammateStatus

__all__ = [
    "AGENT_TOOL_NAME",
    "BUILTIN_AGENTS",
    "DEFAULT_MAX_CONCURRENT",
    "DEFAULT_MAX_DEPTH",
    "FORK_SUBAGENT_TYPE",
    "LEAD_NAME",
    "READ_ONLY_TOOL_NAMES",
    "AgentDefinition",
    "AgentMessage",
    "AgentTool",
    "AgentTransport",
    "InProcessTransport",
    "MessageType",
    "PersistentTeammate",
    "SendMessageTool",
    "SubagentManager",
    "SubagentResult",
    "SubagentRunner",
    "SubagentStatus",
    "SubagentTask",
    "TaskNotification",
    "TaskStatus",
    "Team",
    "TeamCreateTool",
    "TeamManager",
    "TeamMember",
    "TeammateStatus",
    "builtin_agents",
    "fork_agent_definition",
]
