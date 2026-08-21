"""Subagent definitions — the declarative description of a spawnable agent.

An :class:`AgentDefinition` is pure data: it names an agent, gives it a system
prompt, restricts which tools it may use, and bounds how long it may run.  It
carries *no* execution logic — spawning is done by
:class:`~aegis_agent.agents.runner.SubagentRunner`, which turns a definition
into a configured :class:`~aegis_agent.runtime.AgentRuntime` (the same engine
the Main Agent uses).  Keeping definitions declarative is what makes custom
agents easy to add later: a future ``.aegis/agents/*.md`` loader only has to
produce more :class:`AgentDefinition` objects.

Two built-ins ship in this first version:

* ``explore`` — read-only investigation (search / read / web), no mutation.
* ``general-purpose`` — the full builtin tool set (minus the Agent tool).

Neither may spawn further subagents (``allow_agent_tool=False``): the first
version is strictly one level deep, which is also the recursion guard.
"""

from __future__ import annotations

from dataclasses import dataclass

#: The name the Agent tool registers under (kept as a module constant so the
#: runner can exclude it from a subagent's tool set without a string literal).
AGENT_TOOL_NAME = "Agent"

#: Read-only builtin tools an ``explore`` subagent is allowed to use.  Anything
#: that mutates the filesystem or spawns processes (``write_file``, ``patch``,
#: ``terminal``, ``process``) is deliberately excluded.
READ_ONLY_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "read_file",
        "list_directory",
        "search_files",
        "session_search",
        "web_search",
        "web_extract",
        "skills_list",
        "skill_view",
    }
)


@dataclass(frozen=True)
class AgentDefinition:
    """Declarative description of a spawnable subagent.

    Attributes
    ----------
    name:
        The ``subagent_type`` value that selects this agent.
    description:
        One-line summary, surfaced in the Agent tool's schema so the Main
        Agent knows when to pick this type.
    system_prompt:
        The subagent's identity / instructions.  A fresh-context subagent
        receives *no* inherited conversation, so this plus the task prompt is
        all it starts with; a fork subagent additionally inherits history.
    tool_names:
        Whitelist of tool names the subagent may use.  ``None`` means "every
        tool the parent has, except the Agent tool" (subject to
        ``allow_agent_tool``).  An explicit set is intersected with what the
        parent actually has registered.
    max_iterations:
        Per-turn model/tool iteration cap for the subagent.
    allow_agent_tool:
        Whether this subagent may itself call the Agent tool.  Defaults to
        ``False`` — the recursion guard (no nested spawning).
    fork:
        When True the subagent inherits the parent's conversation history
        (fork context).  Used by the implicit-fork path (omitting
        ``subagent_type``); explicit typed agents stay fresh.
    """

    name: str
    description: str
    system_prompt: str
    tool_names: frozenset[str] | None = None
    max_iterations: int = 10
    allow_agent_tool: bool = False
    fork: bool = False


_EXPLORE = AgentDefinition(
    name="explore",
    description=(
        "Read-only investigation agent: search, read and analyse code or the "
        "web, then report findings. Cannot modify files or run commands. Use "
        "for 'find/understand/analyse X' tasks where you only need conclusions."
    ),
    system_prompt=(
        "You are the Explore subagent — a focused, read-only investigator.\n"
        "Your job is to search, read and analyse, then report a clear, concise "
        "conclusion to whoever dispatched you. You have only read-only tools "
        "(file reading, directory listing, code/content search, web search and "
        "extraction); you cannot write files or run commands, so do not promise "
        "to. You start with NO prior conversation — the task prompt is the only "
        "context you have, so work from it directly. When you have gathered "
        "enough, stop calling tools and return a final written summary of what "
        "you found. That summary is the entire value you return; make it "
        "self-contained."
    ),
    tool_names=READ_ONLY_TOOL_NAMES,
)

_GENERAL_PURPOSE = AgentDefinition(
    name="general-purpose",
    description=(
        "General-purpose agent with the full tool set (read, write, search, "
        "terminal, web). Use for multi-step tasks that may need to edit files "
        "or run commands, where you want the work done and only the result "
        "reported back."
    ),
    system_prompt=(
        "You are a general-purpose subagent. You have the full tool set and "
        "should complete the task you were given end to end, then report the "
        "result. You start with NO prior conversation — the task prompt is your "
        "only context, so work from it directly and do not assume access to any "
        "earlier discussion. When the task is done, stop calling tools and "
        "return a final written summary of the outcome; that summary is the "
        "entire value you return, so make it self-contained."
    ),
    tool_names=None,  # all parent tools except the Agent tool
)


#: The implicit-fork agent.  Selected when the Agent tool is called WITHOUT a
#: ``subagent_type``: the child inherits the parent's full conversation
#: (fork=True) and the full tool pool.  Kept out of the selectable registry —
#: you cannot ask for it by name.
FORK_SUBAGENT_TYPE = "fork"

_FORK = AgentDefinition(
    name=FORK_SUBAGENT_TYPE,
    description=(
        "Implicit fork — inherits the full conversation context. Triggered by "
        "omitting subagent_type; not selectable by name."
    ),
    system_prompt=(
        "You are a forked worker — a copy of the parent agent given one focused "
        "task. You have inherited the conversation above and the full tool set. "
        "Work the task directly with your tools; do NOT spawn further sub-agents, "
        "do not converse or ask questions, and do not editorialize. When the task "
        "is done, return a concise final report of what you found or did."
    ),
    tool_names=None,
    max_iterations=25,
    allow_agent_tool=False,
    fork=True,
)


def fork_agent_definition() -> AgentDefinition:
    """Return the implicit-fork agent definition (used by the Agent tool)."""
    return _FORK


#: The built-in subagent registry, keyed by ``subagent_type``.
BUILTIN_AGENTS: dict[str, AgentDefinition] = {
    _EXPLORE.name: _EXPLORE,
    _GENERAL_PURPOSE.name: _GENERAL_PURPOSE,
}


def builtin_agents() -> dict[str, AgentDefinition]:
    """Return a fresh copy of the built-in subagent definitions."""
    return dict(BUILTIN_AGENTS)


__all__ = [
    "AGENT_TOOL_NAME",
    "BUILTIN_AGENTS",
    "FORK_SUBAGENT_TYPE",
    "READ_ONLY_TOOL_NAMES",
    "AgentDefinition",
    "builtin_agents",
    "fork_agent_definition",
]
