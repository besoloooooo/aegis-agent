# Portions adapted from Hermes (hermes-agent), © 2025 Nous Research.
# Licensed under the MIT License. See THIRD_PARTY_NOTICES.md.
#
#   * ``IterationBudget`` (below) is PORTED from ``agent/iteration_budget.py``
#     (© 2025 Nous Research, MIT) — a thread-safe consume/refund counter.
#   * The Agent Loop in ``AgentRuntime.run_turn`` is a REWRITE that follows the
#     structure of ``agent/conversation_loop.py:run_conversation`` (guard →
#     build context → call model → detect tool calls → execute → append tool
#     results → loop), decoupled from Hermes' steering/plugins/persistence
#     concerns and reduced to the Stage-1 minimal surface.
"""Agent runtime: orchestration and the minimal Agent Loop.

``AgentRuntime`` aggregates the injected dependencies (model provider, tool
registry + executor, session repository, context builder) and drives one user
turn through the model↔tool loop.  It depends only on the abstractions
(``ModelProvider``, ``SessionRepository``, ``ToolRegistry``/``ToolExecutor``,
``ContextBuilder``) — never on Typer, a concrete provider, or a concrete store.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

from aegis_agent.context.builder import ContextBuilder
from aegis_agent.context.compress import compress_context
from aegis_agent.context.prompt_sections import (
    EnvironmentContributor,
    ModelIdentityContributor,
    TaskCompletionContributor,
    TimestampContributor,
    ToolUseEnforcementContributor,
)
from aegis_agent.context.system_prompt import DEFAULT_IDENTITY, SystemPromptBuilder
from aegis_agent.context.tool_budget import ContentReplacementState, create_state
from aegis_agent.events import ModelEvent, ModelEventKind, collect_response
from aegis_agent.exceptions import ModelProviderError, OperationCancelled
from aegis_agent.memory.manager import MemoryEvent, MemoryManager
from aegis_agent.memory.prompt import (
    MemoryBehaviorContributor,
    RelevantMemoriesContributor,
    default_memory_index_contributor,
    default_user_profile_contributor,
)
from aegis_agent.models.base import Message, ModelProvider, Role, ToolCall, ToolResult
from aegis_agent.sessions.memory_store import InMemorySessionRepository
from aegis_agent.sessions.repository import SessionRepository
from aegis_agent.skills.loader import SkillLoader
from aegis_agent.skills.manage_tool import SkillManageTool
from aegis_agent.skills.prompt import SkillsIndexContributor
from aegis_agent.skills.router import DefaultSkillRouter, SkillRouter
from aegis_agent.skills.tools import SkillsListTool, SkillViewTool
from aegis_agent.tools.builtin import build_default_registry
from aegis_agent.tools.executor import ToolExecutor
from aegis_agent.tools.registry import ToolContext, ToolRegistry

DEFAULT_MAX_ITERATIONS = 10


class IterationBudget:
    """Thread-safe iteration counter for an agent turn.

    PORTED from Hermes ``agent/iteration_budget.py`` (MIT, © 2025 Nous
    Research).  ``consume()`` returns False once the cap is reached;
    ``refund()`` gives one back.
    """

    def __init__(self, max_total: int):
        self.max_total = max_total
        self._used = 0
        self._lock = threading.Lock()

    def consume(self) -> bool:
        """Try to consume one iteration.  Returns True if allowed."""
        with self._lock:
            if self._used >= self.max_total:
                return False
            self._used += 1
            return True

    def refund(self) -> None:
        """Give back one iteration."""
        with self._lock:
            if self._used > 0:
                self._used -= 1

    @property
    def used(self) -> int:
        with self._lock:
            return self._used

    @property
    def remaining(self) -> int:
        with self._lock:
            return max(0, self.max_total - self._used)


class StopReason(str, Enum):
    """Why a turn's agent loop stopped."""

    FINAL_ANSWER = "final_answer"        # model returned text with no tool calls
    MAX_ITERATIONS = "max_iterations"    # iteration budget exhausted
    INTERRUPTED = "interrupted"          # interrupt event was set
    ERROR = "error"                      # model/provider error


@dataclass
class TurnResult:
    """The outcome of one :meth:`AgentRuntime.run_turn`."""

    final_text: str
    messages: list[Message] = field(default_factory=list)  # full session history (seq order)
    iterations: int = 0
    stop_reason: StopReason = StopReason.FINAL_ANSWER
    tool_calls_made: int = 0


class TurnEventKind(str, Enum):
    """The kinds of observable events emitted during one agent turn.

    These are the runtime-level events a live UI subscribes to.  They wrap the
    model-facing :class:`~aegis_agent.events.ModelEvent` stream (text deltas,
    tool-call requests) and add tool-execution lifecycle events (a tool result
    landing) plus a terminal ``TURN_END``.  The runtime emits them in order;
    a UI that renders them as they arrive reproduces the streamed experience
    without the runtime knowing anything about rendering.
    """

    TEXT_DELTA = "text_delta"        # incremental assistant text
    TOOL_CALL = "tool_call"          # a complete tool-call request from the model
    TOOL_RESULT = "tool_result"      # a tool finished (success or error)
    TURN_END = "turn_end"            # the whole turn finished (stop reason set)
    ERROR = "error"                  # a runtime/loop error occurred


@dataclass
class TurnEvent:
    """One observable turn event.  Exactly one payload field is set per kind.

    ``TEXT_DELTA``/``TOOL_CALL`` mirror the wrapped :class:`ModelEvent` so a UI
    can treat them uniformly; ``TOOL_RESULT`` carries the executed
    :class:`~aegis_agent.models.base.ToolResult`; ``TURN_END`` carries the
    :class:`StopReason` name; ``ERROR`` carries a message.
    """

    kind: TurnEventKind
    text: str = ""
    tool_call: ToolCall | None = None
    tool_result: ToolResult | None = None
    stop_reason: str | None = None
    error: str | None = None

    @classmethod
    def from_model_event(cls, event: ModelEvent) -> TurnEvent | None:
        """Map a model event to a turn event, or ``None`` to skip it.

        ``TEXT_DELTA`` and ``TOOL_CALL`` are forwarded; ``ERROR`` becomes an
        ``ERROR`` turn event.  The provider's terminal ``DONE`` event carries
        only the finish reason, which the runtime surfaces itself as the final
        ``TURN_END`` (with the resolved :class:`StopReason`) — so ``DONE`` maps
        to ``None`` to avoid a spurious premature ``TURN_END``.
        """
        if event.kind is ModelEventKind.TEXT_DELTA:
            return cls(kind=TurnEventKind.TEXT_DELTA, text=event.text)
        if event.kind is ModelEventKind.TOOL_CALL:
            return cls(kind=TurnEventKind.TOOL_CALL, tool_call=event.tool_call)
        if event.kind is ModelEventKind.ERROR:
            return cls(kind=TurnEventKind.ERROR, error=event.error)
        return None  # DONE (and any future non-UI kind)

    @classmethod
    def from_tool_result(cls, result: ToolResult) -> TurnEvent:
        return cls(kind=TurnEventKind.TOOL_RESULT, tool_result=result)

    @classmethod
    def turn_end(cls, stop_reason: StopReason) -> TurnEvent:
        return cls(kind=TurnEventKind.TURN_END, stop_reason=stop_reason.value)

    @classmethod
    def failed(cls, error: str) -> TurnEvent:
        return cls(kind=TurnEventKind.ERROR, error=error)


class AgentRuntime:
    """Aggregate dependencies and run user turns through the Agent Loop."""

    def __init__(
        self,
        provider: ModelProvider,
        registry: ToolRegistry,
        executor: ToolExecutor,
        repository: SessionRepository,
        context_builder: ContextBuilder | None = None,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        skill_router: SkillRouter | None = None,
        startup_info: dict[str, int] | None = None,
        context_token_budget: int | None = None,
        compress_storage_dir: str | None = None,
        summary_provider: ModelProvider | None = None,
        memory_manager: MemoryManager | None = None,
    ) -> None:
        self._provider = provider
        self._registry = registry
        self._executor = executor
        self._repository = repository
        self._context = context_builder or ContextBuilder()
        self._max_iterations = max_iterations
        self._skill_router = skill_router
        self._startup_info = startup_info or {}
        # Personal long-term memory (Stage 2/3): recall before the turn, extract
        # after the final reply.  ``None`` disables both channels (Stage-1
        # behaviour).  The manager only shapes the derived context and writes to
        # the memory dir — it never mutates session history.
        self._memory_manager = memory_manager
        # Context compression (Stage 11).  ``context_token_budget=None`` disables
        # compression entirely; when set, the derived context is compressed
        # before every model call.  ``_budget_states`` holds one
        # ContentReplacementState per session so phase-A replacement decisions
        # are frozen across turns and the on-wire prompt prefix stays
        # byte-stable (prompt-cache friendly).
        self._context_token_budget = context_token_budget
        self._compress_storage_dir = compress_storage_dir
        self._summary_provider = summary_provider
        self._budget_states: dict[str, ContentReplacementState] = {}

    @classmethod
    def with_defaults(
        cls,
        provider: ModelProvider | None = None,
        repository: SessionRepository | None = None,
        *,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        system_prompt: str | None = None,
        cwd: str | None = None,
        allow_dangerous_shell: bool = False,
        enable_skills: bool = True,
        skills_dir: str | None = None,
        enable_mcp: bool = True,
        mcp_config_path: str | None = None,
        enable_memory: bool = True,
        memory_home: str | None = None,
        enable_memory_recall: bool = False,
        enable_memory_extract: bool = False,
        memory_side_provider: ModelProvider | None = None,
        memory_event_sink: Callable[[MemoryEvent], None] | None = None,
        context_token_budget: int | None = None,
        compress_storage_dir: str | None = None,
        summary_provider: ModelProvider | None = None,
    ) -> AgentRuntime:
        """Build a runtime wired with builtin tools and sensible defaults.

        Used by the CLI and by tests that want a ready-made runtime.  Defaults
        to the in-memory repository and (via the caller) the fake provider.
        ``allow_dangerous_shell`` is an operator-only switch passed to the tool
        context (the model cannot enable it).

        When ``enable_skills`` is True (the default) skills are discovered from
        ``skills_dir`` (or the default user dir), the ``skills_list`` /
        ``skill_view`` tools are registered, and the skills index is injected
        into the system prompt via a :class:`SystemPromptBuilder`.  When no
        skills are found the tools are still registered but the index renders
        nothing, so behaviour matches the pre-skills default.

        When ``enable_mcp`` is True (the default), MCP servers are discovered
        from ``mcp_config_path`` (or ``~/.aegis/config.yaml``) and their tools
        are registered into the tool registry.  If the ``mcp`` SDK is not
        installed this is a no-op.

        When ``enable_memory`` is True (the default), personal long-term memory
        is wired into the system prompt: the memory behaviour rules, the
        ``USER.md`` profile and the ``MEMORY.md`` index (from ``memory_home`` or
        ``$AEGIS_HOME``/``~/.aegis``).  Missing files render nothing, so an
        empty install behaves exactly as before.  Only the *index* is injected —
        memory bodies are read on demand by the model; there is no automatic
        recall or extraction in this milestone.

        When ``context_token_budget`` is set, the derived context is compressed
        (``context.compress_context``) before every model call — oversized tool
        results are offloaded to ``compress_storage_dir`` (default
        ``~/.aegis/tool-result-cache``), old rounds are summarised with
        ``summary_provider`` (default: the main provider).  The source history
        is never modified; compression only affects the derived view.
        """
        if provider is None:
            from aegis_agent.models.fake import FakeModelProvider

            provider = FakeModelProvider()
        registry = build_default_registry()
        tool_cwd = cwd if cwd else None
        context = (
            ToolContext(cwd=tool_cwd, allow_dangerous_shell=allow_dangerous_shell)
            if tool_cwd
            else ToolContext(allow_dangerous_shell=allow_dangerous_shell)
        )
        executor = ToolExecutor(registry, context)
        repo = repository or InMemorySessionRepository()

        identity = system_prompt if system_prompt is not None else DEFAULT_IDENTITY
        prompt_builder = SystemPromptBuilder(identity=identity)
        # Behaviour tier: task-completion + tool-use enforcement.  Both render
        # live against the registry, so they include themselves only once tools
        # exist (they read the *final* registry state at build time, even though
        # skills/MCP tools are registered further below).
        prompt_builder.add(TaskCompletionContributor(registry))
        prompt_builder.add(ToolUseEnforcementContributor(registry))
        skill_router: SkillRouter | None = None
        builtin_count = len(registry.names())
        skills_count = 0
        mcp_server_count = 0
        mcp_tool_count = 0
        if enable_skills:
            loader = SkillLoader([skills_dir] if skills_dir else None)
            loader.discover()
            skills_count = len(loader.metas())
            registry.register(SkillsListTool(loader))
            registry.register(SkillViewTool(loader))
            registry.register(SkillManageTool(loader))
            prompt_builder.add(SkillsIndexContributor(loader))
            skill_router = DefaultSkillRouter(loader)

        # ---- MCP ----------------------------------------------------------
        mcp_guidance = None
        if enable_mcp:
            try:
                from aegis_agent.mcp import is_available as _mcp_available

                if _mcp_available():
                    from aegis_agent.mcp.client import (
                        connect_servers_parallel,
                        get_server_tool_timeout,
                        get_server_tools,
                    )
                    from aegis_agent.mcp.config import load_mcp_config
                    from aegis_agent.mcp.guidance import MCPToolsGuidance
                    from aegis_agent.mcp.tools import build_wrappers

                    servers = load_mcp_config(mcp_config_path)
                    connected = 0
                    tool_total = 0
                    results = connect_servers_parallel(servers)
                    for name, ok in results.items():
                        if ok:
                            tools_list = get_server_tools(name)
                            if tools_list:
                                timeout = get_server_tool_timeout(name)
                                wrappers = build_wrappers(name, tools_list, timeout)
                                for w in wrappers:
                                    registry.register(w)
                                connected += 1
                                tool_total += len(tools_list)
                    if connected > 0:
                        mcp_guidance = MCPToolsGuidance()
                        mcp_guidance.set_servers(connected)
                        prompt_builder.add(mcp_guidance)
                    mcp_server_count = connected
                    mcp_tool_count = tool_total
            except Exception:
                # MCP is optional — a config parse error or connection failure
                # should not prevent Aegis from starting.
                import logging
                logging.getLogger(__name__).warning("MCP discovery failed", exc_info=True)

        # ---- Personal long-term memory -----------------------------------
        # Stage-1 sections (behaviour rules, USER.md profile, MEMORY.md index) +
        # the Stage-2/3 relevant-memories slot.  Each renders on every build so
        # file edits show up next turn; missing files render nothing (memory
        # never blocks startup).  When recall/extract are enabled a
        # MemoryManager is built to drive the side-query channels.
        memory_present = False
        memory_manager: MemoryManager | None = None
        if enable_memory:
            prompt_builder.add(MemoryBehaviorContributor())
            profile_contrib = default_user_profile_contributor(memory_home)
            index_contrib = default_memory_index_contributor(memory_home)
            prompt_builder.add(profile_contrib)
            prompt_builder.add(index_contrib)
            relevant_contrib = RelevantMemoriesContributor()
            prompt_builder.add(relevant_contrib)
            memory_present = bool(profile_contrib.render() or index_contrib.render())

            if enable_memory_recall or enable_memory_extract:
                # Default the side-query model to the main provider unless an
                # explicit (usually cheaper) one is supplied.
                side = memory_side_provider or provider
                memory_manager = MemoryManager(
                    relevant_contrib,
                    recall_provider=side if enable_memory_recall else None,
                    extract_provider=side if enable_memory_extract else None,
                    home=memory_home,
                    on_event=memory_event_sink,
                )

        # Model / environment / timestamp tiers (appended last so they render
        # after the skills index and MCP note, matching Hermes' section order).
        prompt_builder.add(ModelIdentityContributor(provider))
        prompt_builder.add(EnvironmentContributor(cwd=context.cwd))
        prompt_builder.add(TimestampContributor())

        builder = ContextBuilder(prompt_builder)
        startup_info = {
            "builtin_tools": builtin_count,
            "skills": skills_count,
            "mcp_servers": mcp_server_count,
            "mcp_tools": mcp_tool_count,
            "memory": 1 if memory_present else 0,
            "memory_recall": 1 if (memory_manager and enable_memory_recall) else 0,
            "memory_extract": 1 if (memory_manager and enable_memory_extract) else 0,
        }
        return cls(
            provider=provider,
            registry=registry,
            executor=executor,
            repository=repo,
            context_builder=builder,
            max_iterations=max_iterations,
            skill_router=skill_router,
            startup_info=startup_info,
            context_token_budget=context_token_budget,
            compress_storage_dir=compress_storage_dir,
            summary_provider=summary_provider,
            memory_manager=memory_manager,
        )

    @property
    def repository(self) -> SessionRepository:
        return self._repository

    @property
    def skill_router(self) -> SkillRouter | None:
        return self._skill_router

    @property
    def startup_info(self) -> dict[str, int]:
        """Counts of loaded subsystems: skills, MCP servers/tools, builtin tools."""
        return dict(self._startup_info)

    @property
    def max_iterations(self) -> int:
        return self._max_iterations

    def shutdown(self) -> None:
        """Wait for any in-flight background memory work to finish.

        The CLI calls this on exit so queued memory extraction isn't cut off
        mid-write.  A no-op when no memory manager is wired in.
        """
        if self._memory_manager is not None:
            self._memory_manager.drain()

    def run_turn(
        self,
        session_id: str,
        user_message: str,
        *,
        interrupt: threading.Event | None = None,
        on_event: Callable[[TurnEvent], None] | None = None,
    ) -> TurnResult:
        """Run one user turn: persist input, loop model↔tools, return the result.

        The loop guard checks (in order): interrupt event, then the iteration
        budget.  Each pass builds a fresh derived context from the source
        history, calls the model, persists the assistant message, and either
        finishes (no tool calls) or executes the requested tools, persists
        their results, and loops.

        When ``on_event`` is given it receives one :class:`TurnEvent` per
        streamed model event (text deltas, tool-call requests) plus a
        ``TOOL_RESULT`` event per executed tool and a terminal ``TURN_END``.
        This is the seam the live TUI hooks into; it is otherwise inert.
        """
        if self._repository.get_session(session_id) is None:
            self._repository.create_session(session_id)

        self._persist(session_id, Message(role=Role.USER, content=user_message))

        # Recall (pre-turn): best-effort inject relevant memories for this query.
        # Only shapes the system prompt via the manager's contributor; the
        # source history is untouched.  Never raises.
        if self._memory_manager is not None:
            self._memory_manager.before_turn(session_id, user_message)

        budget = IterationBudget(self._max_iterations)
        iterations = 0
        tool_calls_made = 0
        turn_tool_calls: list[ToolCall] = []
        final_text = ""
        stop_reason = StopReason.FINAL_ANSWER

        def _emit(event: ModelEvent) -> None:
            if on_event is not None:
                te = TurnEvent.from_model_event(event)
                if te is not None:
                    on_event(te)

        while True:
            # Guard 1: cooperative interrupt.
            if interrupt is not None and interrupt.is_set():
                stop_reason = StopReason.INTERRUPTED
                break

            # Guard 2: iteration budget.
            if not budget.consume():
                stop_reason = StopReason.MAX_ITERATIONS
                if not final_text:
                    final_text = "(maximum iterations reached without a final answer)"
                break

            iterations += 1
            # Recall collect point (non-blocking): if the background recall for
            # this query has finished, inject it before building context.  On the
            # first iteration it usually hasn't finished yet (side query takes
            # longer than build); it lands on a later iteration after a tool
            # round, mirroring Claude Code's prefetch/collect timing.
            if self._memory_manager is not None:
                self._memory_manager.collect_recall(session_id)
            source = self._repository.list_messages(session_id)
            api_messages = self._context.build(source)
            if self._context_token_budget is not None:
                # Compress the DERIVED view only; the source history is untouched.
                api_messages = compress_context(
                    api_messages,
                    self._provider,
                    self._context_token_budget,
                    storage_dir=self._compress_storage_dir,
                    budget_state=self._budget_state_for(session_id),
                    summary_provider=self._summary_provider,
                )

            is_cancelled = (lambda: interrupt.is_set()) if interrupt is not None else None
            try:
                response = collect_response(
                    self._provider.stream(api_messages, tools=self._registry.definitions()),
                    is_cancelled=is_cancelled,
                    on_event=_emit,
                )
            except OperationCancelled:
                # Interrupt fired mid-stream: discard the partial response.
                stop_reason = StopReason.INTERRUPTED
                break
            except ModelProviderError as exc:
                final_text = f"(model error: {exc})"
                stop_reason = StopReason.ERROR
                if on_event is not None:
                    on_event(TurnEvent.failed(str(exc)))
                break

            assistant = Message(
                role=Role.ASSISTANT,
                content=response.content,
                tool_calls=response.tool_calls,
                reasoning_content=response.reasoning_content,
            )
            self._persist(session_id, assistant)

            if not response.tool_calls:
                # Normal termination: final text answer, no tools requested.
                final_text = response.content
                stop_reason = StopReason.FINAL_ANSWER
                break

            tool_calls_made += len(response.tool_calls)
            turn_tool_calls.extend(response.tool_calls)
            results = self._executor.execute(response.tool_calls)
            for tool_message, result in zip(self._executor.to_messages(results), results):
                self._persist(session_id, tool_message)
                if on_event is not None:
                    on_event(TurnEvent.from_tool_result(result))
            # loop continues: the tool results are now in history for the next call

        # Extraction (post-turn): only after a normal final reply — mirrors
        # Claude Code firing after the tool-free final answer.  Best-effort;
        # never affects the result the user already has.  Skipped on
        # interrupt/error so a partial turn is not mined for memory.
        final_messages = self._repository.list_messages(session_id)
        if self._memory_manager is not None:
            self._memory_manager.after_turn(
                session_id,
                final_messages,
                tool_calls=turn_tool_calls,
                extract=stop_reason is StopReason.FINAL_ANSWER,
            )

        if on_event is not None:
            on_event(TurnEvent.turn_end(stop_reason))

        return TurnResult(
            final_text=final_text,
            messages=final_messages,
            iterations=iterations,
            stop_reason=stop_reason,
            tool_calls_made=tool_calls_made,
        )

    def _persist(self, session_id: str, message: Message) -> Message:
        """Mint an idempotency key if absent, then append to the session."""
        if message.client_msg_id is None:
            message.client_msg_id = uuid.uuid4().hex
        return self._repository.append_message(session_id, message)

    def _budget_state_for(self, session_id: str) -> ContentReplacementState:
        """Return the per-session tool-budget state, creating it on first use.

        The state lives as long as the runtime and is keyed by session so two
        sessions never share replacement decisions (no cross-session leakage).
        """
        state = self._budget_states.get(session_id)
        if state is None:
            state = create_state()
            self._budget_states[session_id] = state
        return state


__all__ = [
    "DEFAULT_MAX_ITERATIONS",
    "AgentRuntime",
    "IterationBudget",
    "StopReason",
    "TurnEvent",
    "TurnEventKind",
    "TurnResult",
]
