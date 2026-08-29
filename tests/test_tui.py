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

import io

import pytest
from prompt_toolkit.data_structures import Point
from prompt_toolkit.formatted_text import fragment_list_to_text, to_formatted_text
from prompt_toolkit.keys import Keys
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
from rich.console import Console
from rich.text import Text
from typer.testing import CliRunner

from aegis_agent.cli import app
from aegis_agent.events import ModelEvent
from aegis_agent.models.base import ToolCall, ToolResult
from aegis_agent.runtime import AgentRuntime, StopReason, TurnEvent, TurnEventKind
from aegis_agent.sessions.memory_store import InMemorySessionRepository
from aegis_agent.tui import (
    _PROMPT_PLACEHOLDER,
    _THEME,
    _VIEWPORT_BUFFER_SIZE,
    Tui,
    _FullscreenShell,
    _highlight_input_line,
    _render_markdown_text,
    _render_to_ansi,
    _slash_hint,
)

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Keep the real ~/.aegis/.env (API keys) out of CliRunner runs.

    The CLI loads the user-level dotenv at startup; without this isolation a
    developer machine's keys (e.g. TAVILY_API_KEY) leak into ``os.environ``
    for the whole pytest process and flip later tests (web backends) onto
    live-network code paths.
    """
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


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


def test_render_markdown_text_handles_common_markdown():
    console = Console(record=True, force_terminal=True, width=100)

    _render_markdown_text(
        console,
        "# Title\n\n- **bold** and `code`\n\n```python\nprint('hi')\n```",
    )

    output = console.export_text(styles=True)
    assert "Title" in output
    assert "bold" in output
    assert "code" in output
    assert "print" in output
    assert "\x1b[" in output


def test_input_highlight_line_marks_slash_and_paths():
    fragments = _highlight_input_line('/undo 2 "quoted" ./src @agent #tag')

    assert ("class:slash.known", "/undo") in fragments
    assert ("class:string", '"quoted"') in fragments
    assert ("class:path", "./src") in fragments
    assert ("class:mention", "@agent") in fragments
    assert ("class:tag", "#tag") in fragments


def test_prompt_placeholder_does_not_duplicate_bottom_help():
    assert "/help" not in _PROMPT_PLACEHOLDER


def test_slash_hint_reports_known_and_unknown_commands():
    assert "back up" in _slash_hint("/undo 2")
    assert "unknown slash command" in _slash_hint("/does-not-exist")
    assert "/help" in _slash_hint("hello")


def test_fullscreen_history_follows_tail_without_overscrolling(monkeypatch):
    shell = _FullscreenShell(theme=_THEME)
    monkeypatch.setattr(shell, "_history_viewport_height", lambda: 5)

    shell._append_lines("\n".join(f"line {i}" for i in range(10)))
    shell._formatted_output()

    assert shell.scroll.vertical_scroll == 5
    assert shell._last_max_scroll == 5


def test_fullscreen_history_does_not_snap_back_after_scroll_up(monkeypatch):
    shell = _FullscreenShell(theme=_THEME)
    monkeypatch.setattr(shell, "_history_viewport_height", lambda: 5)
    shell._append_lines("\n".join(f"line {i}" for i in range(10)))
    shell._formatted_output()
    shell._follow_output = False
    shell._history_scroll = 2

    shell._append_lines("new output")
    shell._formatted_output()

    assert shell.scroll.vertical_scroll == 2
    assert shell._history_scroll == 2
    assert shell._last_max_scroll == 6


class _RecordingShell:
    def __init__(self) -> None:
        self.rendered = []
        self.live = None
        self.live_updates = []

    def set_live(self, renderable) -> None:
        self.live = renderable
        self.live_updates.append(renderable)

    def print_renderable(self, renderable) -> None:
        self.rendered.append(renderable)


def test_fullscreen_tool_status_stays_inside_history():
    tui = Tui(console=Console(file=io.StringIO(), force_terminal=False))
    shell = _RecordingShell()
    tui._shell = shell
    state = tui.begin_turn()

    tui._render_event(
        state,
        TurnEvent(
            kind=TurnEventKind.TOOL_CALL,
            tool_call=ToolCall(id="c1", name="list_directory", arguments='{"path":"."}'),
        ),
    )
    tui._render_event(
        state,
        TurnEvent.from_tool_result(
            ToolResult(
                tool_call_id="c1",
                name="list_directory",
                content='{"entries": [1, 2]}',
            )
        ),
    )

    rendered_text = "\n".join(str(item) for item in shell.rendered)
    assert "🔧 list_directory" in rendered_text
    assert "✓ list_directory" in rendered_text


def test_fullscreen_streaming_preview_is_markdown_rendered():
    tui = Tui(console=Console(file=io.StringIO(), force_terminal=False))
    shell = _RecordingShell()
    tui._shell = shell
    state = tui.begin_turn()

    tui._render_event(
        state,
        TurnEvent(
            kind=TurnEventKind.TEXT_DELTA,
            text="# Live title\n\n- **bold while streaming**",
        ),
    )

    assert shell.live is not None
    preview = _render_to_ansi(shell.live, theme=_THEME)
    assert "Live title" in preview
    assert "bold while streaming" in preview
    assert "**" not in preview

    updates_before_next_delta = len(shell.live_updates)
    tui._render_event(
        state,
        TurnEvent(kind=TurnEventKind.TEXT_DELTA, text="\n\nMore text"),
    )
    new_updates = shell.live_updates[updates_before_next_delta:]
    assert new_updates
    assert None not in new_updates


def test_fullscreen_mouse_wheel_scrolls_history(monkeypatch):
    shell = _FullscreenShell(theme=_THEME)
    monkeypatch.setattr(shell, "_history_viewport_height", lambda: 5)
    shell._append_lines("\n".join(f"line {i}" for i in range(10)))
    shell._formatted_output()
    assert shell.scroll.vertical_scroll == 5
    assert shell._history_scroll == 5

    scroll_up = MouseEvent(
        position=Point(x=0, y=0),
        event_type=MouseEventType.SCROLL_UP,
        button=MouseButton.NONE,
        modifiers=frozenset(),
    )
    scroll_down = MouseEvent(
        position=Point(x=0, y=0),
        event_type=MouseEventType.SCROLL_DOWN,
        button=MouseButton.NONE,
        modifiers=frozenset(),
    )

    assert shell.output_control.mouse_handler(scroll_up) is None
    shell._formatted_output()
    assert shell.scroll.vertical_scroll == 4
    assert shell._history_scroll == 4
    assert shell._follow_output is False
    assert shell.output_control.mouse_handler(scroll_down) is None
    shell._formatted_output()
    assert shell.scroll.vertical_scroll == 5
    assert shell._history_scroll == 5
    assert shell._follow_output is True


def test_viewport_clipping_translates_absolute_scroll_to_slice(monkeypatch):
    shell = _FullscreenShell(theme=_THEME)
    monkeypatch.setattr(shell, "_history_viewport_height", lambda: 5)
    shell._append_lines("\n".join(f"line {i}" for i in range(200)))

    rendered = shell._formatted_output()
    text = fragment_list_to_text(to_formatted_text(rendered))

    assert shell._last_max_scroll == 195
    assert shell._history_scroll == 195
    assert shell.scroll.vertical_scroll == 50
    assert "line 199" in text
    assert "line 0\n" not in text

    shell._scroll_history(-1)
    rendered = shell._formatted_output()
    text = fragment_list_to_text(to_formatted_text(rendered))

    assert shell._history_scroll == 194
    assert shell.scroll.vertical_scroll == 50
    assert "line 194" in text


def test_viewport_clipping_keeps_live_status_at_history_tail(monkeypatch):
    shell = _FullscreenShell(theme=_THEME)
    monkeypatch.setattr(shell, "_history_viewport_height", lambda: 5)
    shell._append_lines("\n".join(f"line {i}" for i in range(200)))
    shell.set_live(Text("THINKING STATUS"))

    rendered = shell._formatted_output()
    text = fragment_list_to_text(to_formatted_text(rendered))

    assert shell._follow_output is True
    assert shell._history_scroll == shell._last_max_scroll
    assert "THINKING STATUS" in text


def test_stream_growth_does_not_freeze_scrolled_viewport(monkeypatch):
    shell = _FullscreenShell(theme=_THEME)
    monkeypatch.setattr(shell, "_history_viewport_height", lambda: 8)
    shell._append_lines("\n".join(f"history {i}" for i in range(300)))
    shell._formatted_output()

    for _ in range(20):
        shell._scroll_history(-1)
        shell._formatted_output()

    anchored_scroll = shell._history_scroll
    assert shell._follow_output is False

    for chunk in range(100):
        shell.set_live(Text("\n".join(f"stream {i}" for i in range(chunk + 1))))
        rendered = shell._formatted_output()
        text = fragment_list_to_text(to_formatted_text(rendered))

        assert shell._history_scroll == anchored_scroll
        assert 0 <= shell.scroll.vertical_scroll <= _VIEWPORT_BUFFER_SIZE
        assert f"history {anchored_scroll}" in text


def test_ctrl_end_jumps_to_bottom_and_restores_follow(monkeypatch):
    shell = _FullscreenShell(theme=_THEME)
    assert not shell.scroll.show_scrollbar()
    monkeypatch.setattr(shell, "_history_viewport_height", lambda: 5)
    shell._append_lines("\n".join(f"line {i}" for i in range(200)))
    shell._formatted_output()
    shell._scroll_history(-20)
    shell._formatted_output()

    assert shell._follow_output is False
    assert shell._history_scroll < shell._last_max_scroll
    assert shell.app.key_bindings.get_bindings_for_keys((Keys.ControlEnd,))

    shell._jump_to_bottom()
    shell._formatted_output()

    assert shell._follow_output is True
    assert shell._history_scroll == shell._last_max_scroll
