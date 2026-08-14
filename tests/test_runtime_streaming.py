"""Agent Loop tests for provider-facing behaviour: errors, timeout, cancel,
multi-tool-call, and the OpenAI-compatible provider driven end-to-end via a
fake client.  The loop must stay provider-agnostic — these use custom
providers implementing only the ModelProvider Protocol.
"""

from __future__ import annotations

import threading

from aegis_agent.events import ModelEvent
from aegis_agent.exceptions import ModelProviderError, ModelTimeoutError
from aegis_agent.models.base import Role, ToolCall
from aegis_agent.models.openai_compat import OpenAICompatibleProvider
from aegis_agent.runtime import AgentRuntime, StopReason
from aegis_agent.sessions.memory_store import InMemorySessionRepository
from tests.fakes import FakeOpenAIClient, make_chunk, make_tool_call_delta


class _RaisingProvider:
    """Provider whose stream raises a given exception when consumed."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    @property
    def name(self) -> str:
        return "raising"

    def stream(self, messages, tools=None):
        raise self._exc
        yield  # pragma: no cover - makes this a generator


def _runtime(provider, **kw):
    return AgentRuntime.with_defaults(provider=provider, repository=InMemorySessionRepository(), **kw)


def test_model_error_stops_turn_gracefully():
    runtime = _runtime(_RaisingProvider(ModelProviderError("boom")))
    result = runtime.run_turn("s1", "hi")
    assert result.stop_reason is StopReason.ERROR
    assert "boom" in result.final_text
    # user message persisted, but no assistant message from the failed call
    roles = [m.role for m in result.messages]
    assert roles == [Role.USER]


def test_model_timeout_stops_turn_gracefully():
    runtime = _runtime(_RaisingProvider(ModelTimeoutError("timed out")))
    result = runtime.run_turn("s1", "hi")
    assert result.stop_reason is StopReason.ERROR
    assert "timed out" in result.final_text


def test_cancel_before_turn():
    # An already-set interrupt stops the loop before any model call.
    class NeverCalled:
        name = "never"

        def stream(self, messages, tools=None):  # pragma: no cover - must not run
            raise AssertionError("provider must not be called when pre-cancelled")
            yield

    runtime = _runtime(NeverCalled())
    ev = threading.Event()
    ev.set()
    result = runtime.run_turn("s1", "hi", interrupt=ev)
    assert result.stop_reason is StopReason.INTERRUPTED
    assert result.iterations == 0


def test_cancel_midstream_discards_partial_response():
    ev = threading.Event()

    class CancellingProvider:
        name = "cancelling"

        def stream(self, messages, tools=None):
            yield ModelEvent.text_delta("partial answer that should be discarded")
            ev.set()  # fire the interrupt mid-stream
            yield ModelEvent.text_delta(" more text")
            yield ModelEvent.done("stop")

    runtime = _runtime(CancellingProvider())
    result = runtime.run_turn("s1", "hi", interrupt=ev)
    assert result.stop_reason is StopReason.INTERRUPTED
    # no assistant message persisted from the cancelled stream
    assert [m.role for m in result.messages] == [Role.USER]


def test_multi_tool_call_all_executed():
    """Model returns two tool calls in one turn; both run and are back-filled."""

    class MultiToolProvider:
        name = "multi"

        def __init__(self):
            self.calls = 0

        def stream(self, messages, tools=None):
            self.calls += 1
            if self.calls == 1:
                yield ModelEvent.tool(ToolCall(id="c1", name="list_directory", arguments='{"path":"."}'))
                yield ModelEvent.tool(ToolCall(id="c2", name="run_shell", arguments='{"command":"echo hi"}'))
                yield ModelEvent.done("tool_calls")
            else:
                yield ModelEvent.text_delta("both done")
                yield ModelEvent.done("stop")

    runtime = _runtime(MultiToolProvider())
    result = runtime.run_turn("s1", "do two things")
    assert result.stop_reason is StopReason.FINAL_ANSWER
    assert result.tool_calls_made == 2
    tool_msgs = [m for m in result.messages if m.role is Role.TOOL]
    assert {m.tool_call_id for m in tool_msgs} == {"c1", "c2"}


def test_openai_provider_drives_full_loop_via_fake_client():
    """End-to-end: OpenAI-compatible provider (fake client) → tool → final."""
    turn1 = [
        make_chunk(tool_calls=[make_tool_call_delta(0, id="c1", name="list_directory", arguments='{"path":"."}')]),
        make_chunk(finish_reason="tool_calls"),
    ]
    turn2 = [make_chunk(content="listing complete"), make_chunk(finish_reason="stop")]
    client = FakeOpenAIClient(results=[turn1, turn2])
    provider = OpenAICompatibleProvider(api_key="k", model="m", stream=True, client=client)

    runtime = _runtime(provider)
    result = runtime.run_turn("s1", "list .")
    assert result.stop_reason is StopReason.FINAL_ANSWER
    assert result.final_text == "listing complete"
    assert result.tool_calls_made == 1
    # two model calls were made through the fake client
    assert len(client.calls) == 2


def test_loop_does_not_import_concrete_provider():
    """Guard: runtime.py must not depend on a concrete provider module."""
    import aegis_agent.runtime as runtime_mod

    src = runtime_mod.__file__
    with open(src, encoding="utf-8") as fh:
        text = fh.read()
    assert "openai_compat" not in text
    assert "OpenAICompatibleProvider" not in text


def test_tool_cancel_propagates_and_discards_result():
    """A tool that raises OperationCancelled stops the turn without persisting a
    tool result (mirrors the mid-stream cancel discard)."""
    from aegis_agent.exceptions import OperationCancelled
    from aegis_agent.models.base import ToolDefinition
    from aegis_agent.tools.executor import ToolExecutor
    from aegis_agent.tools.registry import ToolRegistry

    class CancellingTool:
        definition = ToolDefinition(name="cancel_me", description="raises cancel", parameters={})

        def run(self, arguments, context=None):
            raise OperationCancelled("tool cancelled by interrupt")

    class Provider:
        name = "p"

        def stream(self, messages, tools=None):
            yield ModelEvent.tool(ToolCall(id="c1", name="cancel_me", arguments="{}"))
            yield ModelEvent.done("tool_calls")

    registry = ToolRegistry()
    registry.register(CancellingTool())
    runtime = AgentRuntime(
        provider=Provider(),
        registry=registry,
        executor=ToolExecutor(registry),
        repository=InMemorySessionRepository(),
    )
    result = runtime.run_turn("s1", "go")

    assert result.stop_reason is StopReason.INTERRUPTED
    # user + assistant(tool-call request) persisted, but no TOOL result message.
    assert [m.role for m in result.messages] == [Role.USER, Role.ASSISTANT]


def test_executor_cancelled_before_run_raises():
    """A pre-set cancel aborts the executor before the tool handler runs."""
    import pytest

    from aegis_agent.exceptions import OperationCancelled
    from aegis_agent.models.base import ToolDefinition
    from aegis_agent.tools.executor import ToolExecutor
    from aegis_agent.tools.registry import ToolRegistry

    class NeverTool:
        definition = ToolDefinition(name="never", description="d", parameters={})

        def run(self, arguments, context=None):
            raise AssertionError("tool must not run when pre-cancelled")

    registry = ToolRegistry()
    registry.register(NeverTool())
    executor = ToolExecutor(registry)
    with pytest.raises(OperationCancelled):
        executor.execute_one(ToolCall(id="c1", name="never", arguments="{}"), is_cancelled=lambda: True)


def test_executor_threads_is_cancelled_into_context():
    """The executor injects is_cancelled into the tool context (no pre-cancel)."""
    from aegis_agent.models.base import ToolDefinition, ToolResult
    from aegis_agent.tools.executor import ToolExecutor
    from aegis_agent.tools.registry import ToolContext, ToolRegistry

    seen = {}

    class RecordingTool:
        definition = ToolDefinition(name="rec", description="d", parameters={})

        def run(self, arguments, context=None):
            seen["is_cancelled"] = context.is_cancelled if context else None
            return ToolResult(tool_call_id="", name="rec", content="{}")

    registry = ToolRegistry()
    registry.register(RecordingTool())
    executor = ToolExecutor(registry, ToolContext(cwd="."))
    cancelled = lambda: False
    executor.execute_one(ToolCall(id="c1", name="rec", arguments="{}"), is_cancelled=cancelled)
    assert seen["is_cancelled"] is cancelled
