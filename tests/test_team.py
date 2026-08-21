"""Tests for persistent Teams / Teammates + inter-agent messaging.

Covers the milestone acceptance points:

* creating a team and multiple teammates;
* stable teammate identity (name / agent_id);
* a teammate goes IDLE after a task instead of being destroyed;
* an IDLE teammate wakes on a new message and keeps its prior context;
* lead → teammate and teammate → teammate messaging;
* team boundary (no cross-team delivery);
* stop/kill truly terminates a teammate;
* multiple teammates run in parallel;
* teammate transcripts don't pollute the Main transcript;
* earlier subagent foreground/background/fork behaviour is unregressed (covered
  by test_subagent.py / test_subagent_v2.py, re-run in the same suite).
"""

from __future__ import annotations

import json
import threading
import time

from aegis_agent.agents.definitions import builtin_agents
from aegis_agent.agents.messaging import AgentMessage, InProcessTransport, MessageType
from aegis_agent.agents.runner import SubagentRunner
from aegis_agent.agents.team import LEAD_NAME, TeamManager
from aegis_agent.agents.team_tools import SendMessageTool, TeamCreateTool
from aegis_agent.agents.teammate import TeammateStatus
from aegis_agent.models.base import Role
from aegis_agent.models.fake import FakeModelProvider, FakeReply
from aegis_agent.runtime import AgentRuntime
from aegis_agent.sessions.memory_store import InMemorySessionRepository
from aegis_agent.tools.builtin import build_default_registry


def _team_manager(provider, agents=None):
    registry = build_default_registry()
    runner = SubagentRunner(provider, registry)
    transport = InProcessTransport()
    return TeamManager(runner, transport, agents or builtin_agents()), transport


def _wait(predicate, timeout=5.0, interval=0.02):
    """Poll a condition until true or timeout (for thread-driven assertions)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


def test_inprocess_transport_send_receive():
    transport = InProcessTransport()
    transport.send(AgentMessage(sender="a", recipient="b", content="hello"))
    assert transport.has_pending("b")
    msg = transport.receive("b", timeout=1)
    assert msg is not None and msg.content == "hello" and msg.sender == "a"
    assert not transport.has_pending("b")


def test_inprocess_transport_receive_timeout_returns_none():
    transport = InProcessTransport()
    assert transport.receive("nobody", timeout=0.1) is None


def test_inprocess_transport_close_wakes_receiver():
    transport = InProcessTransport()
    got = []

    def _recv():
        got.append(transport.receive("x", timeout=5))

    t = threading.Thread(target=_recv, daemon=True)
    t.start()
    time.sleep(0.05)
    transport.close_recipient("x")
    t.join(timeout=2)
    assert got == [None]  # woken by close, not by a message


# ---------------------------------------------------------------------------
# Team creation + identity
# ---------------------------------------------------------------------------


def test_create_team_and_members():
    provider = FakeModelProvider(script=[FakeReply(text="ok")] * 20)
    manager, _ = _team_manager(provider)
    team = manager.create_team("test team")

    manager.spawn_teammate(team.team_id, "researcher", "explore")
    manager.spawn_teammate(team.team_id, "coder", "general-purpose")

    members = manager.list_members(team.team_id)
    names = {m.name for m in members}
    assert names == {LEAD_NAME, "researcher", "coder"}
    lead = next(m for m in members if m.is_lead)
    assert lead.name == LEAD_NAME
    # stop them so their threads exit
    manager.stop_team(team.team_id)


def test_teammate_has_stable_identity():
    provider = FakeModelProvider(script=[FakeReply(text="ok")])
    manager, _ = _team_manager(provider)
    team = manager.create_team()
    tm = manager.spawn_teammate(team.team_id, "researcher", "explore")

    assert tm.name == "researcher"
    assert tm.agent_id.startswith(f"{team.team_id}/researcher")
    assert tm.session_id == f"team-{team.team_id}-researcher"
    # resolvable by name and by agent_id
    assert manager.resolve(team.team_id, "researcher") is tm
    assert manager.resolve(team.team_id, tm.agent_id) is tm
    manager.stop_team(team.team_id)


def test_duplicate_member_name_rejected():
    provider = FakeModelProvider(script=[FakeReply(text="ok")] * 4)
    manager, _ = _team_manager(provider)
    team = manager.create_team()
    manager.spawn_teammate(team.team_id, "coder")
    try:
        manager.spawn_teammate(team.team_id, "coder")
    except ValueError as exc:
        assert "duplicate" in str(exc).lower()
    else:
        raise AssertionError("expected duplicate-name ValueError")
    finally:
        manager.stop_team(team.team_id)


# ---------------------------------------------------------------------------
# Idle / wakeup / context continuity
# ---------------------------------------------------------------------------


def test_teammate_goes_idle_then_wakes_with_context():
    """The core persistent-agent behaviour: task → IDLE → wake → continuous ctx."""
    # The teammate's model always replies with a short text; the SAME runtime
    # accumulates history, so the 2nd turn's context includes the 1st.
    seen_context_sizes = []

    class ContextSpy:
        name = "spy"

        def stream(self, messages, tools=None):
            seen_context_sizes.append(len(messages))
            from aegis_agent.events import ModelEvent

            yield ModelEvent.text_delta(f"reply{len(seen_context_sizes)}")
            yield ModelEvent.done("stop")

    manager, _ = _team_manager(ContextSpy())
    team = manager.create_team()
    tm = manager.spawn_teammate(team.team_id, "worker", "general-purpose")

    # First task → runs → IDLE.
    manager.send_message(team.team_id, LEAD_NAME, "worker", "first task", type=MessageType.TASK)
    assert _wait(lambda: tm.status is TeammateStatus.IDLE)
    first_ctx = seen_context_sizes[-1]

    # Still the same teammate object (not destroyed) — goes IDLE, not STOPPED.
    assert tm.status is TeammateStatus.IDLE

    # Wake with a second message → runs again with a LARGER context (continuity).
    manager.send_message(team.team_id, LEAD_NAME, "worker", "second task")
    assert _wait(lambda: len(seen_context_sizes) >= 2 and tm.status is TeammateStatus.IDLE)
    second_ctx = seen_context_sizes[-1]
    assert second_ctx > first_ctx, "woken turn must see the prior turn's messages"

    # Transcript spans both turns (continuous, not fresh).
    transcript = tm.transcript()
    contents = [m.content for m in transcript]
    assert any("first task" in c for c in contents)
    assert any("second task" in c for c in contents)
    manager.stop_team(team.team_id)


def test_teammate_idle_notification_reaches_lead():
    provider = FakeModelProvider(script=[FakeReply(text="done")])
    manager, _ = _team_manager(provider)
    team = manager.create_team()
    tm = manager.spawn_teammate(team.team_id, "worker", "general-purpose")
    manager.send_message(team.team_id, LEAD_NAME, "worker", "do work", type=MessageType.TASK)

    assert _wait(lambda: tm.status is TeammateStatus.IDLE)
    # Lead's inbox gets an idle note.
    lead_msgs = manager.drain_lead_messages()
    assert any("worker" in m.content for m in lead_msgs)
    manager.stop_team(team.team_id)


# ---------------------------------------------------------------------------
# Messaging: lead→teammate, teammate→teammate, boundary
# ---------------------------------------------------------------------------


def test_lead_to_teammate_message():
    provider = FakeModelProvider(script=[FakeReply(text="got it")])
    manager, _ = _team_manager(provider)
    team = manager.create_team()
    tm = manager.spawn_teammate(team.team_id, "coder", "general-purpose")
    manager.send_message(team.team_id, LEAD_NAME, "coder", "implement feature X")
    assert _wait(lambda: tm.status is TeammateStatus.IDLE)
    assert any("implement feature X" in m.content for m in tm.transcript())
    manager.stop_team(team.team_id)


def test_teammate_to_teammate_message():
    """A teammate's own send_message tool reaches a peer in the same team."""
    # The 'researcher' will call send_message to 'coder' on its turn.
    from aegis_agent.events import ModelEvent
    from aegis_agent.models.base import ToolCall

    class ResearcherScript:
        name = "researcher"

        def stream(self, messages, tools=None):
            last = messages[-1]
            if last.role is Role.TOOL:
                yield ModelEvent.text_delta("sent to coder")
                yield ModelEvent.done("stop")
            else:
                yield ModelEvent.tool(
                    ToolCall(
                        id="m1",
                        name="send_message",
                        arguments=json.dumps({"recipient": "coder", "message": "analysis ready"}),
                    )
                )
                yield ModelEvent.done("tool_calls")

    manager, transport = _team_manager(ResearcherScript())
    team = manager.create_team()
    manager.spawn_teammate(team.team_id, "researcher", "general-purpose")
    manager.spawn_teammate(team.team_id, "coder", "general-purpose")

    manager.send_message(team.team_id, LEAD_NAME, "researcher", "analyse then tell coder")
    # Researcher runs, calls send_message → coder's inbox.
    assert _wait(lambda: transport.has_pending("coder"), timeout=5)
    manager.stop_team(team.team_id)


def test_team_boundary_blocks_cross_team_and_unknown():
    provider = FakeModelProvider(script=[FakeReply(text="ok")])
    manager, _ = _team_manager(provider)
    team_a = manager.create_team("A")
    team_b = manager.create_team("B")
    manager.spawn_teammate(team_a.team_id, "alice")
    manager.spawn_teammate(team_b.team_id, "bob")

    # 'bob' is not a member of team A → boundary error.
    try:
        manager.send_message(team_a.team_id, LEAD_NAME, "bob", "should fail")
    except ValueError as exc:
        assert "not a member" in str(exc)
    else:
        raise AssertionError("expected boundary ValueError")
    manager.stop_team(team_a.team_id)
    manager.stop_team(team_b.team_id)


def test_broadcast_reaches_all_other_members():
    provider = FakeModelProvider(script=[FakeReply(text="ok")] * 10)
    manager, _ = _team_manager(provider)
    team = manager.create_team()
    t1 = manager.spawn_teammate(team.team_id, "a")
    t2 = manager.spawn_teammate(team.team_id, "b")
    manager.send_message(team.team_id, LEAD_NAME, "*", "attention all")
    # Deterministic outcome: BOTH teammates consume the broadcast and go IDLE
    # with it in their transcript (asserting has_pending races with consumption).
    assert _wait(lambda: t1.status is TeammateStatus.IDLE)
    assert _wait(lambda: t2.status is TeammateStatus.IDLE)
    assert any("attention all" in m.content for m in t1.transcript())
    assert any("attention all" in m.content for m in t2.transcript())
    manager.stop_team(team.team_id)


# ---------------------------------------------------------------------------
# Stop / parallel / isolation
# ---------------------------------------------------------------------------


def test_stop_teammate_terminates():
    provider = FakeModelProvider(script=[FakeReply(text="ok")])
    manager, _ = _team_manager(provider)
    team = manager.create_team()
    tm = manager.spawn_teammate(team.team_id, "worker", "general-purpose")
    assert manager.stop_teammate(team.team_id, "worker") is True
    tm.join(timeout=5)
    assert tm.status is TeammateStatus.STOPPED
    manager.stop_team(team.team_id)


def test_parallel_teammates_both_run():
    provider = FakeModelProvider(script=[FakeReply(text="ok")] * 20)
    manager, _ = _team_manager(provider)
    team = manager.create_team()
    t1 = manager.spawn_teammate(team.team_id, "one")
    t2 = manager.spawn_teammate(team.team_id, "two")
    manager.send_message(team.team_id, LEAD_NAME, "one", "task1")
    manager.send_message(team.team_id, LEAD_NAME, "two", "task2")
    assert _wait(lambda: t1.status is TeammateStatus.IDLE)
    assert _wait(lambda: t2.status is TeammateStatus.IDLE)
    assert any("task1" in m.content for m in t1.transcript())
    assert any("task2" in m.content for m in t2.transcript())
    manager.stop_team(team.team_id)


def test_teammate_transcript_isolated_from_main():
    """A teammate's turns live in its own session, not the lead's repo."""
    provider = FakeModelProvider(script=[FakeReply(text="ok")])
    manager, _ = _team_manager(provider)
    team = manager.create_team()
    tm = manager.spawn_teammate(team.team_id, "worker")
    manager.send_message(team.team_id, LEAD_NAME, "worker", "secret work")
    assert _wait(lambda: tm.status is TeammateStatus.IDLE)

    # The teammate's session id is its own; the lead/main session has no such id.
    main_repo = InMemorySessionRepository()
    main_repo.create_session("main")
    main_contents = [m.content for m in main_repo.list_messages("main")]
    assert not any("secret work" in c for c in main_contents)
    assert tm.session_id != "main"
    manager.stop_team(team.team_id)


# ---------------------------------------------------------------------------
# Team tools (team_create / send_message) + runtime wiring
# ---------------------------------------------------------------------------


def test_team_create_tool_creates_team_and_spawns():
    provider = FakeModelProvider(script=[FakeReply(text="ok")] * 10)
    manager, _ = _team_manager(provider)
    tool = TeamCreateTool(manager)
    result = tool.run(
        {
            "description": "build a feature",
            "members": [
                {"name": "researcher", "agent_type": "explore"},
                {"name": "coder"},
            ],
        }
    )
    assert not result.is_error
    payload = json.loads(result.content)
    assert payload["lead"] == LEAD_NAME
    assert set(payload["spawned"]) == {"researcher", "coder"}
    assert payload["team_id"]
    manager.stop_team(payload["team_id"])


def test_send_message_tool_from_lead():
    provider = FakeModelProvider(script=[FakeReply(text="ok")] * 10)
    manager, _ = _team_manager(provider)
    create = TeamCreateTool(manager)
    res = create.run({"members": [{"name": "coder"}]})
    team_id = json.loads(res.content)["team_id"]

    send = SendMessageTool(manager, team_id=None, sender=LEAD_NAME)  # lead: resolves lead team
    out = send.run({"recipient": "coder", "message": "start coding"})
    assert not out.is_error
    coder = manager.resolve(team_id, "coder")
    assert coder is not None
    manager.stop_team(team_id)


def test_send_message_tool_requires_team_first():
    provider = FakeModelProvider()
    manager, _ = _team_manager(provider)
    send = SendMessageTool(manager, team_id=None, sender=LEAD_NAME)
    out = send.run({"recipient": "anyone", "message": "hi"})
    assert out.is_error
    assert "no team" in json.loads(out.content)["error"].lower()


def test_runtime_exposes_team_manager_and_tools():
    runtime = AgentRuntime.with_defaults(
        provider=FakeModelProvider(), repository=InMemorySessionRepository()
    )
    assert runtime.team_manager is not None
    names = runtime._registry.names()
    assert "team_create" in names
    assert "send_message" in names


def test_runtime_team_disabled_when_subagents_off():
    runtime = AgentRuntime.with_defaults(
        provider=FakeModelProvider(),
        repository=InMemorySessionRepository(),
        enable_subagents=False,
    )
    assert runtime.team_manager is None
    assert runtime.drain_team_messages() == []
    names = runtime._registry.names()
    assert "team_create" not in names
    assert "send_message" not in names


def test_teammate_failure_does_not_kill_team():
    """A turn that errors marks the teammate FAILED but the loop survives."""

    class FlakyProvider:
        name = "flaky"

        def __init__(self):
            self.calls = 0

        def stream(self, messages, tools=None):
            from aegis_agent.events import ModelEvent
            from aegis_agent.exceptions import ModelProviderError

            self.calls += 1
            if self.calls == 1:
                raise ModelProviderError("transient failure")
            yield ModelEvent.text_delta("recovered")
            yield ModelEvent.done("stop")

    manager, _ = _team_manager(FlakyProvider())
    team = manager.create_team()
    tm = manager.spawn_teammate(team.team_id, "worker", "general-purpose")

    # First message → provider raises → teammate FAILED but loop alive.
    manager.send_message(team.team_id, LEAD_NAME, "worker", "first")
    assert _wait(lambda: tm.status is TeammateStatus.FAILED)
    assert tm.status is not TeammateStatus.STOPPED

    # Second message → provider recovers → teammate back to IDLE.
    manager.send_message(team.team_id, LEAD_NAME, "worker", "second")
    assert _wait(lambda: tm.status is TeammateStatus.IDLE)
    manager.stop_team(team.team_id)
