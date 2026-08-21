"""Tests for the second-version Subagent capabilities.

Covers the milestone acceptance points:

* fresh vs fork context (fork inherits the parent's history);
* foreground vs background execution;
* background completion notification without polling;
* independent subagent transcripts;
* task lifecycle state (running → completed/failed/killed);
* concurrency cap and nesting-depth guard;
* tool / permission isolation (already covered in test_subagent.py for the
  read-only whitelist; here for fork inheriting the full pool).
"""

from __future__ import annotations

import json
import threading
import time

from aegis_agent.agents.agent_tool import AgentTool
from aegis_agent.agents.definitions import (
    AGENT_TOOL_NAME,
    builtin_agents,
    fork_agent_definition,
)
from aegis_agent.agents.manager import (
    DEFAULT_MAX_DEPTH,
    SubagentManager,
    TaskStatus,
)
from aegis_agent.agents.runner import SubagentRunner, SubagentStatus
from aegis_agent.models.base import Message, Role, ToolCall
from aegis_agent.models.fake import FakeModelProvider, FakeReply
from aegis_agent.runtime import AgentRuntime, StopReason
from aegis_agent.sessions.memory_store import InMemorySessionRepository
from aegis_agent.tools.builtin import build_default_registry


def _manager(provider, *, max_concurrent=8, max_depth=DEFAULT_MAX_DEPTH, parent=None):
    parent = parent if parent is not None else build_default_registry()
    runner = SubagentRunner(provider, parent)
    return SubagentManager(
        runner, builtin_agents(), max_concurrent=max_concurrent, max_depth=max_depth
    )


# ---------------------------------------------------------------------------
# Fork vs fresh context
# ---------------------------------------------------------------------------


def test_fresh_subagent_starts_empty():
    """A typed (fresh) subagent sees only its own turn — no inherited history."""
    provider = FakeModelProvider(script=[FakeReply(text="fresh result")])
    manager = _manager(provider)

    result = manager.spawn(builtin_agents()["explore"], "task", parent_messages=None)
    assert result.status is SubagentStatus.COMPLETED
    # transcript = the dispatched prompt + the final answer only; no seeded history
    user_texts = [m.content for m in result.transcript if m.role is Role.USER]
    assert user_texts == ["task"]
    # exactly one user turn and one assistant answer — nothing inherited
    assert len(result.transcript) == 2


def test_fork_subagent_inherits_parent_history():
    """Fork (omitted subagent_type) seeds the parent's messages into the child."""
    provider = FakeModelProvider(script=[FakeReply(text="fork answer")])
    manager = _manager(provider)
    parent_msgs = [
        Message(role=Role.USER, content="the codebase uses uv"),
        Message(role=Role.ASSISTANT, content="noted"),
    ]
    result = manager.spawn(
        fork_agent_definition(), "now list the files", parent_messages=parent_msgs
    )
    assert result.status is SubagentStatus.COMPLETED
    contents = [m.content for m in result.transcript]
    # Seeded history is present, then the new task prompt.
    assert "the codebase uses uv" in contents
    assert "noted" in contents
    assert "now list the files" in contents
    # Ordering: seeded history precedes the new prompt.
    assert contents.index("the codebase uses uv") < contents.index("now list the files")


def test_fork_via_agent_tool_when_type_omitted():
    """The Agent tool with no subagent_type resolves to the fork agent."""
    repo = InMemorySessionRepository()
    repo.create_session("main")
    repo.append_message("main", Message(role=Role.USER, content="we use uv"))
    provider = FakeModelProvider(script=[FakeReply(text="fork done")])

    tool = AgentTool(
        _manager(provider),
        allow_fork=True,
        history_provider=repo.list_messages,
    )
    from aegis_agent.tools.registry import ToolContext

    ctx = ToolContext(session_id="main")
    result = tool.run({"prompt": "continue the work"}, context=ctx)
    assert not result.is_error
    payload = json.loads(result.content)
    assert payload["subagent_type"] == "fork"
    assert payload["result"] == "fork done"


def test_fork_requires_history_provider():
    """Fork without a history provider is a clear error, not a crash."""
    provider = FakeModelProvider()
    tool = AgentTool(_manager(provider), allow_fork=True, history_provider=None)
    result = tool.run({"prompt": "x"}, context=None)
    assert result.is_error


def test_fork_disabled_requires_type():
    provider = FakeModelProvider()
    tool = AgentTool(_manager(provider), allow_fork=False)
    result = tool.run({"prompt": "x"})
    assert result.is_error
    assert "subagent_type" in json.loads(result.content)["error"]


# ---------------------------------------------------------------------------
# Background execution + notification
# ---------------------------------------------------------------------------


def test_background_spawn_returns_task_and_notifies():
    provider = FakeModelProvider(script=[FakeReply(text="bg result")])
    manager = _manager(provider)

    spawned = manager.spawn(builtin_agents()["explore"], "bg task", background=True)
    # A task handle is returned immediately, status RUNNING (or already done).
    assert spawned.task_id
    # Wait for the daemon thread to finish, then a notification is queued.
    spawned.thread.join(timeout=10)
    assert spawned.status is TaskStatus.COMPLETED

    notes = manager.drain_notifications()
    assert len(notes) == 1
    assert notes[0].task_id == spawned.task_id
    assert notes[0].status is TaskStatus.COMPLETED
    assert "bg result" in notes[0].output
    # Draining is destructive — no repeat notification (no polling needed).
    assert manager.drain_notifications() == []


def test_background_via_agent_tool_returns_handle():
    provider = FakeModelProvider(script=[FakeReply(text="later")])
    manager = _manager(provider)
    tool = AgentTool(manager)
    result = tool.run(
        {"prompt": "do it", "subagent_type": "explore", "run_in_background": True}
    )
    assert not result.is_error
    payload = json.loads(result.content)
    assert payload["background"] is True
    assert payload["status"] == "running"
    assert payload["task_id"].startswith("task-")
    # Let the background thread finish, then a notification appears.
    for _ in range(100):
        if manager.has_pending_notifications():
            break
        time.sleep(0.02)
    notes = manager.drain_notifications()
    assert len(notes) == 1
    assert "later" in notes[0].output


def test_background_failure_notifies_with_error():
    class Boom:
        name = "boom"

        def stream(self, messages, tools=None):
            from aegis_agent.exceptions import ModelProviderError

            raise ModelProviderError("down")

    manager = _manager(Boom())
    spawned = manager.spawn(builtin_agents()["explore"], "fail", background=True)
    spawned.thread.join(timeout=10)
    notes = manager.drain_notifications()
    assert notes[0].status is TaskStatus.FAILED
    assert notes[0].error


def test_runtime_drains_notifications():
    """The runtime exposes the drain seam the CLI uses between turns."""
    provider = FakeModelProvider(script=[FakeReply(text="x")])
    runtime = AgentRuntime.with_defaults(provider=provider, repository=InMemorySessionRepository())
    manager = runtime.subagent_manager
    assert manager is not None
    spawned = manager.spawn(builtin_agents()["explore"], "t", background=True)
    spawned.thread.join(timeout=10)
    notes = runtime.drain_subagent_notifications()
    assert len(notes) == 1
    assert runtime.drain_subagent_notifications() == []


# ---------------------------------------------------------------------------
# Task lifecycle + limits
# ---------------------------------------------------------------------------


def test_task_state_transitions_to_completed():
    provider = FakeModelProvider(script=[FakeReply(text="done")])
    manager = _manager(provider)
    manager.spawn(builtin_agents()["explore"], "t", background=False)
    tasks = manager.tasks()
    assert len(tasks) == 1
    assert tasks[0].status is TaskStatus.COMPLETED
    assert tasks[0].result is not None


def test_concurrency_cap_rejects_overflow():
    """With the cap saturated, an extra spawn returns a FAILED result."""
    release = threading.Event()

    class SlowProvider:
        name = "slow"

        def stream(self, messages, tools=None):
            from aegis_agent.events import ModelEvent

            release.wait(timeout=5)
            yield ModelEvent.text_delta("done")
            yield ModelEvent.done("stop")

    manager = _manager(SlowProvider(), max_concurrent=1)
    first = manager.spawn(builtin_agents()["explore"], "a", background=True)
    assert first.status is TaskStatus.RUNNING
    # Second concurrent spawn exceeds the cap → immediate FAILED result.
    overflow = manager.spawn(builtin_agents()["explore"], "b", background=True)
    assert isinstance(overflow, SubagentStatus) is False  # it's a SubagentResult
    assert overflow.status is SubagentStatus.FAILED
    assert "concurrency" in (overflow.error or "").lower()
    release.set()
    first.thread.join(timeout=10)


def test_depth_guard_blocks_nested_spawn():
    """At the depth cap a spawn is refused with a FAILED result."""
    provider = FakeModelProvider(script=[FakeReply(text="nested")])
    manager = _manager(provider, max_depth=1)
    # Simulate spawning from inside a depth-1 subagent (parent_depth=1).
    result = manager.spawn(
        builtin_agents()["general-purpose"], "nested task", parent_depth=1
    )
    assert result.status is SubagentStatus.FAILED
    assert "depth" in (result.error or "").lower()


def test_kill_marks_task_killed():
    release = threading.Event()

    class SlowProvider:
        """Yields one event, then blocks until killed; the second event lets
        collect_response observe the cancel and raise OperationCancelled."""

        name = "slow"

        def stream(self, messages, tools=None):
            from aegis_agent.events import ModelEvent

            yield ModelEvent.text_delta("partial")
            release.wait(timeout=5)  # killed while blocked here
            yield ModelEvent.text_delta("late")  # never folded: cancel seen first
            yield ModelEvent.done("stop")

    manager = _manager(SlowProvider())
    task = manager.spawn(builtin_agents()["explore"], "t", background=True)
    assert manager.kill(task.task_id) is True
    release.set()  # unblock the provider so collect_response can re-check cancel
    task.thread.join(timeout=10)
    assert task.status is TaskStatus.KILLED
    # Killing again / unknown id is a no-op returning False.
    assert manager.kill(task.task_id) is False
    assert manager.kill("nonexistent") is False


# ---------------------------------------------------------------------------
# Isolation + reuse (regression guards)
# ---------------------------------------------------------------------------


def test_background_subagent_transcript_isolated_from_main():
    """A background subagent's tool rounds never enter the parent session.

    Uses a thread-routing provider so the Main turn and the background subagent
    each consume their own deterministic script (a single shared FIFO queue
    would interleave nondeterministically across the two threads).
    """
    from aegis_agent.events import ModelEvent

    main_thread = threading.get_ident()

    class ThreadRoutingProvider:
        name = "routing"

        def stream(self, messages, tools=None):
            if threading.get_ident() == main_thread:
                # Main turn: request a background Agent call, then finish.
                if not any(m.role is Role.TOOL for m in messages):
                    yield ModelEvent.tool(
                        ToolCall(
                            id="a1",
                            name=AGENT_TOOL_NAME,
                            arguments=json.dumps(
                                {
                                    "prompt": "list",
                                    "subagent_type": "explore",
                                    "run_in_background": True,
                                }
                            ),
                        )
                    )
                    yield ModelEvent.done("tool_calls")
                else:
                    yield ModelEvent.text_delta("Main returns now.")
                    yield ModelEvent.done("stop")
            else:
                # Background subagent thread: one tool round then a final answer.
                if not any(m.role is Role.TOOL for m in messages):
                    yield ModelEvent.tool(
                        ToolCall(id="s1", name="list_directory", arguments='{"path": "."}')
                    )
                    yield ModelEvent.done("tool_calls")
                else:
                    yield ModelEvent.text_delta("bg files listed")
                    yield ModelEvent.done("stop")

    repo = InMemorySessionRepository()
    runtime = AgentRuntime.with_defaults(provider=ThreadRoutingProvider(), repository=repo)
    result = runtime.run_turn("main", "go")
    assert result.stop_reason is StopReason.FINAL_ANSWER

    # The background subagent completes; only the Agent tool handle (not its
    # internal tool messages) is in the main transcript.
    manager = runtime.subagent_manager
    for _ in range(200):
        if manager.has_pending_notifications():
            break
        time.sleep(0.02)
    main_tool_names = [m.name for m in repo.list_messages("main") if m.role is Role.TOOL]
    assert main_tool_names == [AGENT_TOOL_NAME]
    # The notification carries the subagent's final output.
    notes = runtime.drain_subagent_notifications()
    assert notes and "bg files listed" in notes[0].output


def test_subagent_manager_present_in_with_defaults():
    runtime = AgentRuntime.with_defaults(
        provider=FakeModelProvider(), repository=InMemorySessionRepository()
    )
    assert runtime.subagent_manager is not None


def test_subagent_manager_absent_when_disabled():
    runtime = AgentRuntime.with_defaults(
        provider=FakeModelProvider(),
        repository=InMemorySessionRepository(),
        enable_subagents=False,
    )
    assert runtime.subagent_manager is None
    assert runtime.drain_subagent_notifications() == []


# ---------------------------------------------------------------------------
# /agents slash command
# ---------------------------------------------------------------------------


def test_agents_slash_command_lists_tasks():
    from aegis_agent.slash_commands import SlashHandler

    provider = FakeModelProvider(script=[FakeReply(text="done")])
    runtime = AgentRuntime.with_defaults(provider=provider, repository=InMemorySessionRepository())
    manager = runtime.subagent_manager
    manager.spawn(builtin_agents()["explore"], "investigate the thing")

    out: list[str] = []
    handler = SlashHandler(
        runtime=runtime, repository=runtime.repository, emit=out.append, session_id="s1"
    )
    handler.handle("/agents")
    text = "\n".join(out)
    assert "explore" in text
    assert "completed" in text
    assert "investigate the thing" in text


def test_agents_slash_command_empty_and_disabled():
    from aegis_agent.slash_commands import SlashHandler

    runtime = AgentRuntime.with_defaults(
        provider=FakeModelProvider(), repository=InMemorySessionRepository()
    )
    out: list[str] = []
    handler = SlashHandler(
        runtime=runtime, repository=runtime.repository, emit=out.append, session_id="s1"
    )
    handler.handle("/agents")
    assert "no subagent tasks" in "\n".join(out)

    disabled = AgentRuntime.with_defaults(
        provider=FakeModelProvider(),
        repository=InMemorySessionRepository(),
        enable_subagents=False,
    )
    out2: list[str] = []
    handler2 = SlashHandler(
        runtime=disabled, repository=disabled.repository, emit=out2.append, session_id="s1"
    )
    handler2.handle("/agents")
    assert "disabled" in "\n".join(out2)
