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
from aegis_agent.events import ModelEvent, ModelEventKind, collect_response
from aegis_agent.exceptions import ModelProviderError, OperationCancelled
from aegis_agent.models.base import Message, ModelProvider, Role, ToolCall, ToolResult
from aegis_agent.sessions.memory_store import InMemorySessionRepository
from aegis_agent.sessions.repository import SessionRepository
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
    ) -> None:
        self._provider = provider
        self._registry = registry
        self._executor = executor
        self._repository = repository
        self._context = context_builder or ContextBuilder()
        self._max_iterations = max_iterations

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
    ) -> AgentRuntime:
        """Build a runtime wired with builtin tools and sensible defaults.

        Used by the CLI and by tests that want a ready-made runtime.  Defaults
        to the in-memory repository and (via the caller) the fake provider.
        ``allow_dangerous_shell`` is an operator-only switch passed to the tool
        context (the model cannot enable it).
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
        builder = ContextBuilder(system_prompt)
        return cls(
            provider=provider,
            registry=registry,
            executor=executor,
            repository=repo,
            context_builder=builder,
            max_iterations=max_iterations,
        )

    @property
    def repository(self) -> SessionRepository:
        return self._repository

    @property
    def max_iterations(self) -> int:
        return self._max_iterations

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

        budget = IterationBudget(self._max_iterations)
        iterations = 0
        tool_calls_made = 0
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
            source = self._repository.list_messages(session_id)
            api_messages = self._context.build(source)

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

            assistant = Message(role=Role.ASSISTANT, content=response.content, tool_calls=response.tool_calls)
            self._persist(session_id, assistant)

            if not response.tool_calls:
                # Normal termination: final text answer, no tools requested.
                final_text = response.content
                stop_reason = StopReason.FINAL_ANSWER
                break

            tool_calls_made += len(response.tool_calls)
            results = self._executor.execute(response.tool_calls)
            for tool_message, result in zip(self._executor.to_messages(results), results):
                self._persist(session_id, tool_message)
                if on_event is not None:
                    on_event(TurnEvent.from_tool_result(result))
            # loop continues: the tool results are now in history for the next call

        if on_event is not None:
            on_event(TurnEvent.turn_end(stop_reason))

        return TurnResult(
            final_text=final_text,
            messages=self._repository.list_messages(session_id),
            iterations=iterations,
            stop_reason=stop_reason,
            tool_calls_made=tool_calls_made,
        )

    def _persist(self, session_id: str, message: Message) -> Message:
        """Mint an idempotency key if absent, then append to the session."""
        if message.client_msg_id is None:
            message.client_msg_id = uuid.uuid4().hex
        return self._repository.append_message(session_id, message)


__all__ = [
    "DEFAULT_MAX_ITERATIONS",
    "AgentRuntime",
    "IterationBudget",
    "StopReason",
    "TurnEvent",
    "TurnEventKind",
    "TurnResult",
]
