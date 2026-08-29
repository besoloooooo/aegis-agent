"""Stage-0 observability tests: hierarchy, failures and fail-open behavior."""

from __future__ import annotations

import contextvars
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from typing import Any

from aegis_agent.agents.definitions import AGENT_TOOL_NAME
from aegis_agent.models.fake import FakeModelProvider, FakeReply
from aegis_agent.observability import NoopObservability, create_observability
from aegis_agent.observability.sanitize import REDACTED, sanitize
from aegis_agent.observability.tracer import LangfuseObservability
from aegis_agent.runtime import AgentRuntime, StopReason
from aegis_agent.sessions.memory_store import InMemorySessionRepository


@dataclass
class _Record:
    name: str
    kind: str
    parent: _Record | None
    input: Any
    metadata: dict[str, Any]
    updates: list[dict[str, Any]] = field(default_factory=list)


class _RecordingObservation:
    def __init__(self, record: _Record) -> None:
        self.record = record

    def update(self, **kwargs: Any) -> None:
        self.record.updates.append(kwargs)


class RecordingObservability:
    """Small deterministic backend that records the active parent context."""

    enabled = True

    def __init__(self) -> None:
        self.records: list[_Record] = []
        self._stack: contextvars.ContextVar[tuple[_Record, ...]] = contextvars.ContextVar(
            "observability_test_stack", default=()
        )

    @contextmanager
    def _record(
        self,
        *,
        name: str,
        kind: str,
        input: Any,
        metadata: dict[str, Any] | None = None,
    ):
        stack = self._stack.get()
        record = _Record(
            name=name,
            kind=kind,
            parent=stack[-1] if stack else None,
            input=input,
            metadata=metadata or {},
        )
        self.records.append(record)
        token = self._stack.set((*stack, record))
        try:
            yield _RecordingObservation(record)
        finally:
            self._stack.reset(token)

    def agent_run(
        self,
        *,
        task: str,
        session_id: str,
        agent_name: str,
        version: str,
        is_subagent: bool,
    ) -> AbstractContextManager:
        name = f"Subagent Run: {agent_name}" if is_subagent else "Aegis Run"
        return self._record(
            name=name,
            kind="agent",
            input={"task": task},
            metadata={"session_id": session_id, "version": version},
        )

    def model_call(
        self, *, provider: str, model: str | None, messages: Any
    ) -> AbstractContextManager:
        return self._record(
            name="Model Call",
            kind="generation",
            input=messages,
            metadata={"provider": provider, "model": model},
        )

    def tool_call(self, *, tool_name: str, arguments: Any) -> AbstractContextManager:
        return self._record(
            name=f"Tool Call: {tool_name}",
            kind="tool",
            input=arguments,
        )

    def final_result(self) -> AbstractContextManager:
        return self._record(
            name="Final Result",
            kind="span",
            input=None,
        )

    def flush(self) -> None:
        return None

    def shutdown(self) -> None:
        return None


def _runtime(provider, observability, *, subagents: bool = False) -> AgentRuntime:
    return AgentRuntime.with_defaults(
        provider=provider,
        repository=InMemorySessionRepository(),
        observability=observability,
        enable_subagents=subagents,
        enable_skills=False,
        enable_mcp=False,
        enable_memory=False,
    )


def test_normal_run_records_agent_model_tool_model_final_hierarchy():
    tracing = RecordingObservability()
    provider = FakeModelProvider(
        script=[
            FakeReply.tool("list_directory", {"path": "."}, call_id="t1"),
            FakeReply(text="final answer"),
        ]
    )

    result = _runtime(provider, tracing).run_turn("session-1", "list files")

    assert result.stop_reason is StopReason.FINAL_ANSWER
    assert [(r.kind, r.name) for r in tracing.records] == [
        ("agent", "Aegis Run"),
        ("generation", "Model Call"),
        ("tool", "Tool Call: list_directory"),
        ("generation", "Model Call"),
        ("span", "Final Result"),
    ]
    root = tracing.records[0]
    assert all(record.parent is root for record in tracing.records[1:])
    assert root.metadata["session_id"] == "session-1"
    assert root.updates[-1]["output"] == "final answer"
    assert root.updates[-1]["success"] is True


def test_tool_failure_is_recorded_and_runtime_recovers():
    tracing = RecordingObservability()
    provider = FakeModelProvider(
        script=[
            FakeReply.tool("missing_tool", {"password": "do-not-upload"}, call_id="bad"),
            FakeReply(text="recovered"),
        ]
    )

    result = _runtime(provider, tracing).run_turn("session-2", "try missing tool")

    assert result.final_text == "recovered"
    tool = next(record for record in tracing.records if record.kind == "tool")
    assert tool.updates[-1]["success"] is False
    assert "Unknown tool" in tool.updates[-1]["error"]
    # The original Aegis contract remains intact: the tool error becomes a
    # message and the next model call can produce a successful final answer.
    assert result.stop_reason is StopReason.FINAL_ANSWER


def test_subagent_model_and_tool_are_nested_under_subagent_run():
    tracing = RecordingObservability()
    provider = FakeModelProvider(
        script=[
            FakeReply.tool(
                AGENT_TOOL_NAME,
                {"prompt": "inspect files", "subagent_type": "explore"},
                call_id="agent-1",
            ),
            FakeReply.tool("list_directory", {"path": "."}, call_id="sub-tool"),
            FakeReply(text="subagent result"),
            FakeReply(text="parent result"),
        ]
    )

    result = _runtime(provider, tracing, subagents=True).run_turn("parent", "delegate")

    assert result.final_text == "parent result"
    subagent = next(record for record in tracing.records if record.name == "Subagent Run: explore")
    agent_tool = next(record for record in tracing.records if record.name == "Tool Call: Agent")
    assert subagent.parent is agent_tool
    subagent_children = [record for record in tracing.records if record.parent is subagent]
    assert [(record.kind, record.name) for record in subagent_children] == [
        ("generation", "Model Call"),
        ("tool", "Tool Call: list_directory"),
        ("generation", "Model Call"),
        ("span", "Final Result"),
    ]


def test_unconfigured_and_failing_langfuse_are_noop(monkeypatch):
    import aegis_agent.observability.tracer as tracer_module

    monkeypatch.setattr(tracer_module, "load_dotenv", lambda: None)
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    assert isinstance(create_observability(), NoopObservability)

    class FailingClient:
        def start_as_current_observation(self, **kwargs):
            raise ConnectionError("Langfuse unavailable")

        def flush(self):
            raise ConnectionError("Langfuse unavailable")

        def shutdown(self):
            raise ConnectionError("Langfuse unavailable")

    tracing = LangfuseObservability(FailingClient())
    runtime = _runtime(FakeModelProvider(script=[FakeReply(text="still works")]), tracing)
    result = runtime.run_turn("session-3", "hello")
    runtime.shutdown()

    assert result.final_text == "still works"
    assert result.stop_reason is StopReason.FINAL_ANSWER


def test_sanitize_redacts_nested_json_and_bounds_large_values():
    value = {
        "Authorization": "Bearer top-secret",
        "LANGFUSE_SECRET_KEY": "sk-lf-do-not-upload",
        "payload": '{"access_token":"abc123","ok":true}',
        "prompt": "api_key=super-secret " + "x" * 30,
    }

    cleaned = sanitize(value, max_string_length=20)

    assert cleaned["Authorization"] == REDACTED
    assert cleaned["LANGFUSE_SECRET_KEY"] == REDACTED
    assert cleaned["payload"]["access_token"] == REDACTED
    assert cleaned["prompt"]["_aegis_truncated"] is True
    assert cleaned["prompt"]["_aegis_original_length"] == len(value["prompt"])
    assert "super-secret" not in cleaned["prompt"]["content"]


def test_langfuse_adapter_uses_v4_observation_types_and_sanitizes():
    calls: list[dict[str, Any]] = []
    updates: list[dict[str, Any]] = []

    class FakeObservation:
        def update(self, **kwargs):
            updates.append(kwargs)

    class FakeManager:
        def __enter__(self):
            return FakeObservation()

        def __exit__(self, *args):
            return False

    class FakeClient:
        def start_as_current_observation(self, **kwargs):
            calls.append(kwargs)
            return FakeManager()

    tracing = LangfuseObservability(FakeClient())
    with tracing.agent_run(
        task="parent",
        session_id="s1",
        agent_name="main",
        version="0.1.0",
        is_subagent=False,
    ), tracing.agent_run(
        task="child",
        session_id="sub-s1",
        agent_name="explore",
        version="0.1.0",
        is_subagent=True,
    ):
        pass
    with tracing.tool_call(
        tool_name="terminal",
        arguments={"command": "echo ok", "api_key": "secret"},
    ) as observation:
        observation.update(output={"cookie": "secret", "result": "ok"}, success=True)

    assert calls[0]["as_type"] == "agent"
    assert calls[1]["metadata"]["parent_agent"] == "main"
    assert calls[2]["as_type"] == "tool"
    assert calls[2]["name"] == "Tool Call: terminal"
    assert calls[2]["input"]["api_key"] == REDACTED
    assert updates[0]["output"]["cookie"] == REDACTED
