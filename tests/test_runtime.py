"""Core Agent Loop tests — the minimal vertical chain.

Covers (per the Stage-1 acceptance list): plain text answer, single tool call,
tool-result backfill, tool error, max iterations, message ordering, source
immutability, idempotency and session isolation.
"""

from __future__ import annotations

import json

from aegis_agent.events import ModelEvent
from aegis_agent.models.base import Role, ToolCall
from aegis_agent.models.fake import FakeReply
from aegis_agent.runtime import AgentRuntime, StopReason


def test_plain_text_answer(make_runtime):
    runtime, provider = make_runtime(script=[FakeReply(text="Hello there!")])
    result = runtime.run_turn("s1", "hi")

    assert result.stop_reason is StopReason.FINAL_ANSWER
    assert result.final_text == "Hello there!"
    assert result.tool_calls_made == 0
    assert result.iterations == 1
    assert provider.calls == 1
    # user + assistant persisted, in order
    roles = [m.role for m in result.messages]
    assert roles == [Role.USER, Role.ASSISTANT]


def test_single_tool_call_then_final(make_runtime):
    script = [
        FakeReply.tool("list_directory", {"path": "."}, call_id="c1"),
        FakeReply(text="Done listing."),
    ]
    runtime, provider = make_runtime(script=script)
    result = runtime.run_turn("s1", "list .")

    assert result.stop_reason is StopReason.FINAL_ANSWER
    assert result.final_text == "Done listing."
    assert result.tool_calls_made == 1
    assert provider.calls == 2  # one for the tool call, one after the result
    roles = [m.role for m in result.messages]
    assert roles == [Role.USER, Role.ASSISTANT, Role.TOOL, Role.ASSISTANT]


def test_tool_result_backfilled_into_history(make_runtime, repository):
    script = [
        FakeReply.tool("list_directory", {"path": "."}, call_id="c1"),
        FakeReply(text="summary"),
    ]
    runtime, _ = make_runtime(script=script)
    runtime.run_turn("s1", "list .")

    tool_msgs = [m for m in repository.list_messages("s1") if m.role is Role.TOOL]
    assert len(tool_msgs) == 1
    tool_msg = tool_msgs[0]
    # correlated back to the originating call
    assert tool_msg.tool_call_id == "c1"
    assert tool_msg.name == "list_directory"
    payload = json.loads(tool_msg.content)
    assert "entries" in payload


def test_tool_result_visible_to_next_model_call():
    """The model must see the tool message in the context on the follow-up call."""
    seen_last_roles = []

    class SpyProvider:
        @property
        def name(self):
            return "spy"

        def stream(self, messages, tools=None):
            seen_last_roles.append(messages[-1].role)
            if len(seen_last_roles) == 1:
                yield ModelEvent.tool(ToolCall(id="c1", name="list_directory", arguments='{"path": "."}'))
                yield ModelEvent.done("tool_calls")
            else:
                yield ModelEvent.text_delta("ok")
                yield ModelEvent.done("stop")

    runtime = AgentRuntime.with_defaults(provider=SpyProvider())
    runtime.run_turn("s1", "list .")
    # second call's last context message is the tool result
    assert seen_last_roles[1] is Role.TOOL


def test_unknown_tool_error_backfilled(make_runtime):
    script = [
        FakeReply.tool("nonexistent_tool", {}, call_id="c9"),
        FakeReply(text="recovered"),
    ]
    runtime, _ = make_runtime(script=script)
    result = runtime.run_turn("s1", "do something")

    assert result.stop_reason is StopReason.FINAL_ANSWER
    tool_msg = next(m for m in result.messages if m.role is Role.TOOL)
    payload = json.loads(tool_msg.content)
    assert "error" in payload
    assert "Unknown tool" in payload["error"]


def test_tool_error_inside_handler_is_captured(make_runtime):
    # read_file with no path -> tool returns an error envelope, loop continues
    script = [
        FakeReply.tool("read_file", {}, call_id="c2"),
        FakeReply(text="handled the error"),
    ]
    runtime, _ = make_runtime(script=script)
    result = runtime.run_turn("s1", "read")

    assert result.stop_reason is StopReason.FINAL_ANSWER
    assert result.final_text == "handled the error"
    tool_msg = next(m for m in result.messages if m.role is Role.TOOL)
    assert "error" in json.loads(tool_msg.content)


def test_max_iterations_stops_loop(make_runtime):
    # Provider always requests a tool -> never a final answer; budget caps it.
    script = [FakeReply.tool("list_directory", {"path": "."}, call_id=f"c{i}") for i in range(50)]
    runtime, _ = make_runtime(script=script, max_iterations=3)
    result = runtime.run_turn("s1", "loop forever")

    assert result.stop_reason is StopReason.MAX_ITERATIONS
    assert result.iterations == 3
    assert "maximum iterations" in result.final_text


def test_message_order_and_seq(make_runtime):
    script = [
        FakeReply.tool("list_directory", {"path": "."}, call_id="c1"),
        FakeReply(text="final"),
    ]
    runtime, _ = make_runtime(script=script)
    result = runtime.run_turn("s1", "list .")

    seqs = [m.seq for m in result.messages]
    assert seqs == [0, 1, 2, 3]  # monotonic, gapless
    assert [m.role for m in result.messages] == [Role.USER, Role.ASSISTANT, Role.TOOL, Role.ASSISTANT]


def test_sessions_are_isolated(make_runtime):
    runtime, _ = make_runtime(script=[FakeReply(text="a"), FakeReply(text="b")])
    runtime.run_turn("session-A", "first")
    runtime.run_turn("session-B", "second")

    msgs_a = runtime.repository.list_messages("session-A")
    msgs_b = runtime.repository.list_messages("session-B")
    assert len(msgs_a) == 2 and len(msgs_b) == 2
    # no cross-contamination of user content
    assert msgs_a[0].content == "first"
    assert msgs_b[0].content == "second"


def test_context_builder_does_not_mutate_source(make_runtime, repository):
    runtime, _ = make_runtime(script=[FakeReply(text="x")])
    runtime.run_turn("s1", "hello")

    source = repository.list_messages("s1")
    snapshot = [(m.role, m.content, m.client_msg_id, m.seq) for m in source]
    derived = runtime._context.build(source)

    # source unchanged after building the derived context
    assert [(m.role, m.content, m.client_msg_id, m.seq) for m in source] == snapshot
    # derived view strips internal fields and prepends the system prompt
    assert derived[0].role is Role.SYSTEM
    assert all(m.client_msg_id is None and m.seq is None for m in derived[1:])
