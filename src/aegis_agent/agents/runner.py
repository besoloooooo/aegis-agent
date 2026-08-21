"""Spawn a subagent by *reusing* the shared :class:`AgentRuntime`.

The single rule of this module: a subagent is not a new kind of loop — it is
the same :class:`~aegis_agent.runtime.AgentRuntime`, re-instantiated with a
different :class:`~aegis_agent.runtime.AgentConfig`, a filtered tool registry,
a system prompt and its OWN session repository.  The Main Agent and a subagent
therefore run byte-for-byte the same model↔tool loop; only their configuration
differs.  (This mirrors Claude Code's ``runAgent`` — "run the main ``query()``
loop again with custom config" — adapted to Aegis's dependency-injected
runtime.)

Isolation guarantees:

* **Independent transcript** — every message the subagent produces (its model
  turns, tool calls and tool results) lands in its private repository.  None of
  it is written back to the parent's session; the parent only ever receives the
  final :class:`SubagentResult` (plus, on request, the transcript itself).
* **Fresh vs fork context** — with no parent history the subagent starts empty
  ("fresh"); when the caller supplies ``parent_messages`` the history is seeded
  into the private repo first ("fork"), so the child inherits the conversation
  but never touches the parent's store.
* **One-shot lifecycle** — ``CREATED → RUNNING → COMPLETED/FAILED``.  The
  runtime and repository are local to a single run and discarded afterwards.

Concurrency / recursion limits are *not* enforced here — they live in
:class:`~aegis_agent.agents.manager.SubagentManager`, which owns the task
lifecycle.  This class only builds and runs one subagent runtime.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from enum import Enum

from aegis_agent.agents.definitions import AGENT_TOOL_NAME, AgentDefinition
from aegis_agent.context.builder import ContextBuilder
from aegis_agent.context.prompt_sections import (
    EnvironmentContributor,
    ModelIdentityContributor,
    TaskCompletionContributor,
    TimestampContributor,
    ToolUseEnforcementContributor,
)
from aegis_agent.context.system_prompt import SystemPromptBuilder
from aegis_agent.models.base import Message, ModelProvider
from aegis_agent.runtime import AgentConfig, AgentRuntime, StopReason
from aegis_agent.sessions.memory_store import InMemorySessionRepository
from aegis_agent.tools.executor import ToolExecutor
from aegis_agent.tools.registry import ToolContext, ToolRegistry


class SubagentStatus(str, Enum):
    """Terminal lifecycle state of a one-shot subagent run."""

    COMPLETED = "completed"  # produced a final answer (or hit its iteration cap)
    FAILED = "failed"        # a provider/loop error
    KILLED = "killed"        # interrupted / cancelled before a result


@dataclass
class SubagentResult:
    """What a subagent hands back to the Main Agent.

    ``output`` is the subagent's final text (the only thing a foreground caller
    sees).  ``status`` distinguishes completion / failure / kill.  ``error`` is
    set on failure with a short reason.  ``transcript`` is the subagent's full
    private message log — useful for debugging and for background tasks whose
    result the parent consumes later.  ``iterations`` / ``tool_calls`` are
    lightweight telemetry.
    """

    agent_type: str
    status: SubagentStatus
    output: str = ""
    error: str | None = None
    iterations: int = 0
    tool_calls: int = 0
    transcript: list[Message] = field(default_factory=list)


class SubagentRunner:
    """Turn an :class:`AgentDefinition` + task prompt into a completed run.

    Built once (by the Agent tool / manager) with the collaborators a subagent
    should *share* with the parent: the model provider, the parent's tool
    registry (the pool a subagent's tools are filtered from) and the parent's
    tool-context defaults (cwd / dangerous-shell switch).  Each :meth:`run`
    call then builds a throwaway runtime for one subagent and returns its
    result.

    Parameters
    ----------
    allowed_agent_types:
        The subagent types this runner may spawn when a definition opts into
        nesting (``allow_agent_tool=True``).  ``None`` disables nesting entirely
        (default) — the Agent tool is then stripped from every child.  A
        non-empty set lets a nested subagent see an Agent tool restricted to
        those types.  Depth is enforced separately by the manager.
    """

    def __init__(
        self,
        provider: ModelProvider,
        parent_registry: ToolRegistry,
        *,
        cwd: str | None = None,
        allow_dangerous_shell: bool = False,
        allowed_agent_types: frozenset[str] | None = None,
    ) -> None:
        self._provider = provider
        self._parent_registry = parent_registry
        self._cwd = cwd
        self._allow_dangerous_shell = allow_dangerous_shell
        self._allowed_agent_types = allowed_agent_types
        # Optional extra tools merged into every sub-registry this runner
        # builds (e.g. the team ``send_message`` tool a teammate needs).  Set by
        # the Team layer via :meth:`add_extra_tool`; empty for plain subagents.
        self._extra_tools: list = []

    def add_extra_tool(self, tool) -> None:
        """Register an additional tool into every sub-registry built hereafter."""
        self._extra_tools.append(tool)

    def run(
        self,
        definition: AgentDefinition,
        prompt: str,
        *,
        parent_context: ToolContext | None = None,
        parent_messages: list[Message] | None = None,
        cancel_event: threading.Event | None = None,
        repository: InMemorySessionRepository | None = None,
        session_id: str | None = None,
        provider: ModelProvider | None = None,
        extra_tools: list | None = None,
    ) -> SubagentResult:
        """Run one subagent to completion and return its result + transcript.

        ``parent_context`` (the tool context the Agent tool was invoked with)
        supplies the live ``is_cancelled`` callback so a Ctrl+C mid-parent-turn
        also stops the child, plus the cwd when the runner wasn't given one.

        ``cancel_event`` is an explicit cancellation signal (set by
        :meth:`SubagentManager.kill` or :meth:`TeamManager.stop_teammate`).  It
        is OR-ed with the parent's ``is_cancelled`` so killing a task actually
        aborts its in-flight model call / tool execution — not just its status.

        ``repository`` / ``session_id`` let a *persistent* agent (a teammate)
        pass in its long-lived store and session so successive turns keep one
        continuous transcript; when omitted a fresh private repo + session is
        created (one-shot subagent behaviour).

        ``provider`` overrides the runner's default model provider for this
        run, so different agents can use different models without the runtime
        hard-coding a single shared provider.

        ``extra_tools`` are merged into this run's sub-registry on top of the
        filtered parent pool and the runner-level extras — used to give one
        specific agent (e.g. a teammate) a tool bound to its own identity, like
        a ``send_message`` addressed from its name.

        ``parent_messages`` implements **fork context**: when given, the
        parent's message history is seeded into the subagent's repository
        before the turn, so the child inherits the conversation.  When omitted
        the child starts fresh (no inherited history).  Ignored when the
        repository already has history (a persistent teammate mid-conversation).
        """
        sub_registry = self._build_sub_registry(definition, extra_tools=extra_tools)
        tool_cwd = self._cwd
        if tool_cwd is None and parent_context is not None:
            tool_cwd = parent_context.cwd
        parent_cancel = parent_context.is_cancelled if parent_context is not None else None

        # Combine the parent's cancel callback with the task's own cancel event
        # so either stops the child.  ``None`` when neither source exists.
        def _is_cancelled() -> bool:
            if cancel_event is not None and cancel_event.is_set():
                return True
            return parent_cancel is not None and parent_cancel()

        is_cancelled = _is_cancelled if (cancel_event is not None or parent_cancel is not None) else None

        sub_context = ToolContext(
            cwd=tool_cwd if tool_cwd is not None else ToolContext().cwd,
            allow_dangerous_shell=self._allow_dangerous_shell,
            is_cancelled=is_cancelled,
        )
        executor = ToolExecutor(sub_registry, sub_context)
        context_builder = self._build_context_builder(definition, sub_registry, sub_context.cwd)

        repo = repository if repository is not None else InMemorySessionRepository()
        runtime = AgentRuntime(
            provider=provider if provider is not None else self._provider,
            registry=sub_registry,
            executor=executor,
            repository=repo,
            context_builder=context_builder,
            config=AgentConfig(
                agent_name=definition.name,
                max_iterations=definition.max_iterations,
            ),
        )

        # Reuse the caller's session (persistent teammate) or create a private
        # one (one-shot subagent).
        sid = session_id or f"subagent-{definition.name}-{uuid.uuid4().hex[:8]}"
        if repo.get_session(sid) is None:
            repo.create_session(sid)
        if parent_messages and repo.message_count(sid) == 0:
            # Fork context: seed the parent's history into the private repo.
            # Each message is re-appended (a fresh client_msg_id is minted by
            # the runtime's persist path only for NEW messages; seeded ones are
            # appended verbatim via the repository so their order is kept).
            for message in parent_messages:
                repo.append_message(sid, self._forked_copy(message))

        turn = runtime.run_turn(sid, prompt, is_cancelled=is_cancelled)
        transcript = repo.list_messages(sid)

        if turn.stop_reason is StopReason.ERROR:
            return SubagentResult(
                agent_type=definition.name,
                status=SubagentStatus.FAILED,
                error=turn.final_text or "subagent failed",
                iterations=turn.iterations,
                tool_calls=turn.tool_calls_made,
                transcript=transcript,
            )
        if turn.stop_reason is StopReason.INTERRUPTED:
            return SubagentResult(
                agent_type=definition.name,
                status=SubagentStatus.KILLED,
                error="subagent interrupted before producing a result",
                iterations=turn.iterations,
                tool_calls=turn.tool_calls_made,
                transcript=transcript,
            )
        # FINAL_ANSWER or MAX_ITERATIONS: a (possibly capped) textual result.
        return SubagentResult(
            agent_type=definition.name,
            status=SubagentStatus.COMPLETED,
            output=turn.final_text,
            iterations=turn.iterations,
            tool_calls=turn.tool_calls_made,
            transcript=transcript,
        )

    @staticmethod
    def _forked_copy(message: Message) -> Message:
        """Copy a parent message for seeding into the child's private repo.

        ``client_msg_id`` is cleared so the child's idempotency keys stay in
        its own namespace (two agents never share a key); ``seq`` is cleared so
        the child repo re-assigns its own ordering.  Content, role and tool
        linkage are preserved verbatim.
        """
        return Message(
            role=message.role,
            content=message.content,
            tool_calls=list(message.tool_calls),
            tool_call_id=message.tool_call_id,
            name=message.name,
            reasoning_content=message.reasoning_content,
            client_msg_id=None,
            seq=None,
        )

    def _build_sub_registry(self, definition: AgentDefinition, extra_tools: list | None = None) -> ToolRegistry:
        """Filter the parent's tool pool down to what this subagent may use.

        The whitelist (``definition.tool_names``) is intersected with the tools
        the parent actually has, so a definition can name tools that simply
        won't appear if the parent lacks them.  The Agent tool itself is
        excluded unless the definition opts in (``allow_agent_tool``) — the
        recursion guard — and when nesting IS allowed it is rebuilt restricted
        to ``allowed_agent_types``.
        """
        sub = ToolRegistry()
        agent_tool_source = None
        for tool in self._parent_registry:
            name = tool.definition.name
            if name == AGENT_TOOL_NAME:
                agent_tool_source = tool
                continue  # handled after the loop
            if definition.tool_names is not None and name not in definition.tool_names:
                continue
            sub.register(tool)

        if definition.allow_agent_tool and agent_tool_source is not None:
            # Re-expose a nesting-capable Agent tool restricted to the allowed
            # child types.  We rebuild it rather than reusing the parent's so
            # the child cannot escalate beyond its granted subagent types.
            from aegis_agent.agents.agent_tool import AgentTool
            from aegis_agent.agents.definitions import builtin_agents

            agents = {
                name: defn
                for name, defn in builtin_agents().items()
                if self._allowed_agent_types is None or name in self._allowed_agent_types
            }
            sub.register(AgentTool(self, agents))

        # Merge any runner-level extra tools (e.g. a teammate's send_message).
        for tool in self._extra_tools:
            sub.register(tool)
        # Merge per-run extra tools (e.g. a teammate's own-bound send_message).
        for tool in extra_tools or ():
            sub.register(tool)
        return sub

    def _build_context_builder(
        self, definition: AgentDefinition, registry: ToolRegistry, cwd: str
    ) -> ContextBuilder:
        """Assemble the subagent's system prompt.

        Deliberately minimal: the definition's identity, the same behaviour
        tier the Main Agent uses (task-completion + tool-use enforcement, which
        self-suppress when the registry is empty), plus the model-identity and
        environment/timestamp lines.  No skills index, MCP note, memory or user
        profile — a subagent gets self-contained context only.
        """
        builder = SystemPromptBuilder(identity=definition.system_prompt)
        builder.add(TaskCompletionContributor(registry))
        builder.add(ToolUseEnforcementContributor(registry))
        builder.add(ModelIdentityContributor(self._provider))
        builder.add(EnvironmentContributor(cwd=cwd))
        builder.add(TimestampContributor())
        return ContextBuilder(builder)


__all__ = ["SubagentResult", "SubagentRunner", "SubagentStatus"]
