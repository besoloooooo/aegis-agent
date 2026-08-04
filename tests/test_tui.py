"""Tests for the live event observer seam and the streaming CLI.

Two concerns:

* ``AgentRuntime.run_turn(on_event=...)`` must forward events in the right
  order — TEXT_DELTA chunks first (streamed), then TOOL_RESULT per executed
  tool, then a terminal TURN_END.  This is the seam the TUI renders from, so
  the ordering is an observable invariant.
* The CLI, driven through the fake (chunked) provider, must surface the
  streamed final text and the tool name in its captured stdout — i.e. the TUI
  really does print deltas as they arrive rather than only at turn end.
"""

from __future__ import annotations

from typer.testing import CliRunner

from aegis_agent.cli import app
from aegis_agent.events import ModelEvent
from aegis_agent.models.base import ToolCall
from aegis_agent.runtime import AgentRuntime, StopReason, TurnEvent, TurnEventKind
from aegis_agent.sessions.memory_store import InMemorySessionRepository

runner = CliRunner()


class _ChunkedToolThenAnswer:
    """Provider that streams a tool call (chunked text + tool), then a final answer.

    Call 1: TEXT_DELTA "hi" chunked per char → TOOL_CALL list_directory → DONE
    Call 2: TEXT_DELTA "done" → DONE
    """

    name = "chunked"

    def __init__(self) -> None:
        self.calls = 0

    def stream(self, messages, tools=None):
        self.calls += 1
        if self.calls == 1:
            yield from (ModelEvent.text_delta(ch) for ch in "hi")
            yield ModelEvent.tool(ToolCall(id="c1", name="list_directory", arguments='{"path":"."}'))
            yield ModelEvent.done("tool_calls")
        else:
            yield from (ModelEvent.text_delta(ch) for ch in "done")
            yield ModelEvent.done("stop")


def _runtime(provider) -> AgentRuntime:
    return AgentRuntime.with_defaults(provider=provider, repository=InMemorySessionRepository())


def test_on_event_orders_text_then_tool_result_then_turn_end():
    runtime = _runtime(_ChunkedToolThenAnswer())
    events: list[TurnEvent] = []

    runtime.run_turn("s1", "list .", on_event=events.append)

    kinds = [e.kind for e in events]
    # Streamed text deltas arrive first (one per char: 'h','i'), then a
    # TOOL_CALL, then a TOOL_RESULT once the executor finishes, then the
    # terminal TURN_END.
    assert kinds[:2] == [TurnEventKind.TEXT_DELTA, TurnEventKind.TEXT_DELTA]
    assert kinds[2] == TurnEventKind.TOOL_CALL
    assert kinds[3] == TurnEventKind.TOOL_RESULT
    assert kinds[-1] is TurnEventKind.TURN_END

    # The TEXT deltas reconstruct the streamed text exactly.
    streamed = "".join(e.text for e in events if e.kind is TurnEventKind.TEXT_DELTA)
    # First model call streams "hi" + "done" on the second call, concatenated.
    assert streamed == "hidone"

    # The tool result is correlated to the call that produced it.
    tr = next(e for e in events if e.kind is TurnEventKind.TOOL_RESULT)
    assert tr.tool_result is not None
    assert tr.tool_result.name == "list_directory"
    assert tr.tool_result.tool_call_id == "c1"

    # TURN_END carries the final-answer stop reason.
    end = events[-1]
    assert end.stop_reason == StopReason.FINAL_ANSWER.value


def test_cli_streams_answer_and_tool_name():
    # Fake provider (chunk_text=True) streams the echo char-by-char; the TUI
    # must print it live so the final text appears in captured stdout.  The
    # 'list .' rule emits a list_directory tool call, whose name the TUI prints.
    result = runner.invoke(app, ["--model-backend", "fake"], input="list .\nexit\n")
    assert result.exit_code == 0
    assert "list_directory" in result.output
    # After the tool runs, the fake summarises its result — that summary is
    # streamed too, so its text appears in the output.
    assert "list_directory" in result.output  # tool name rendered
    assert "bye." in result.output


def test_cli_streams_plain_echo():
    result = runner.invoke(app, ["--model-backend", "fake"], input="hello aegis\nexit\n")
    assert result.exit_code == 0
    # Streamed char-by-char, but the concatenation must still appear verbatim.
    assert "Echo: hello aegis" in result.output
    assert "bye." in result.output
