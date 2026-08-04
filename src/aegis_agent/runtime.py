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
from dataclasses import dataclass, field
from enum import Enum

from aegis_agent.context.builder import ContextBuilder
from aegis_agent.events import collect_response
from aegis_agent.exceptions import ModelProviderError, OperationCancelled
from aegis_agent.models.base import Message, ModelProvider, Role
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
    ) -> TurnResult:
        """Run one user turn: persist input, loop model↔tools, return the result.

        The loop guard checks (in order): interrupt event, then the iteration
        budget.  Each pass builds a fresh derived context from the source
        history, calls the model, persists the assistant message, and either
        finishes (no tool calls) or executes the requested tools, persists
        their results, and loops.
        """
        if self._repository.get_session(session_id) is None:
            self._repository.create_session(session_id)

        self._persist(session_id, Message(role=Role.USER, content=user_message))

        budget = IterationBudget(self._max_iterations)
        iterations = 0
        tool_calls_made = 0
        final_text = ""
        stop_reason = StopReason.FINAL_ANSWER

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
                )
            except OperationCancelled:
                # Interrupt fired mid-stream: discard the partial response.
                stop_reason = StopReason.INTERRUPTED
                break
            except ModelProviderError as exc:
                final_text = f"(model error: {exc})"
                stop_reason = StopReason.ERROR
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
            for tool_message in self._executor.to_messages(results):
                self._persist(session_id, tool_message)
            # loop continues: the tool results are now in history for the next call

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
    "TurnResult",
]
