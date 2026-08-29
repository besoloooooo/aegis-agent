# Portions adapted from Hermes (hermes-agent), © 2025 Nous Research.
# Licensed under the MIT License. See THIRD_PARTY_NOTICES.md.
"""Interactive terminal UI for the Aegis CLI.

A presentation layer that renders the live experience — banner, startup info
panel, "thinking" spinner, streamed assistant text, compact tool-call and
tool-result status lines — from :class:`~aegis_agent.runtime.TurnEvent` objects
the runtime emits.

Appearance is deliberately distinct from Hermes: a blue-tinted palette and a
``❯`` prompt glyph rather than Hermes' gold caduceus.
"""

from __future__ import annotations

import io
import json
import re
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.mouse_events import MouseEvent, MouseEventType
from rich.align import Align
from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.padding import Padding
from rich.panel import Panel
from rich.text import Text
from rich.theme import Theme

from aegis_agent import __version__
from aegis_agent.runtime import StopReason, TurnEvent, TurnEventKind

# ─── palette (blue-tinted — distinct from Hermes' gold) ───────────────

_BLUE = "#4A90D9"
_LIGHT_BLUE = "#7AB8F5"
_THEME = Theme({
    "aegis.blue": f"bold {_BLUE}",
    "aegis.label": _BLUE,
    "aegis.dim": "dim",
    "aegis.ok": "green",
    "aegis.err": "red",
    "aegis.warn": "yellow",
    "aegis.info": _LIGHT_BLUE,
})

# ─── kawaii spinner data (adapted from Hermes agent/display.py) ──────

_SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

_KAWAII_THINKING = [
    "(｡•́︿•̀｡)", "(◔_◔)", "(¬‿¬)", "( •_•)>⌐■-■", "(⌐■-■)",
    "(´･_･`)", "◉_◉", "(°ロ°)", "( ˘⌣˘)♡", "ヽ(>∀<☆)☆",
    "٩(๑❛ᴗ❛๑)۶", "(⊙_⊙)", "(¬_¬)", "ಠ_ಠ",
]

_THINKING_VERBS = [
    "pondering", "contemplating", "musing", "cogitating", "ruminating",
    "deliberating", "mulling", "reflecting", "processing", "reasoning",
    "analyzing", "computing", "synthesizing", "formulating", "brainstorming",
]

_TOOL_ARGS_MAX = 120
_MARKDOWN_THEME = "monokai"
_HISTORY_MAX_LINES = 4000
_RENDERED_SEGMENT_MAX_LINES = 300
_WHEEL_SCROLL_LINES = 1
_VIEWPORT_BUFFER_SIZE = 50  # Extra lines above/below viewport for smooth scrolling
_PROMPT_PLACEHOLDER = "Ask Aegis..."
_KNOWN_SLASH_COMMANDS = {
    "/agents",
    "/clear",
    "/commands",
    "/exit",
    "/help",
    "/history",
    "/new",
    "/quit",
    "/reset",
    "/retry",
    "/save",
    "/sessions",
    "/title",
    "/undo",
}
_SLASH_HINTS = {
    "/agents": "inspect subagent tasks",
    "/clear": "clear screen and start a new session",
    "/help": "show interactive commands",
    "/history": "show conversation history",
    "/new": "start a new session: /new [title]",
    "/retry": "resend the last user message",
    "/save": "dump debug snapshots",
    "/sessions": "list recorded sessions",
    "/title": "set/show session title: /title [name]",
    "/undo": "back up user turns: /undo [N]",
}


class _ThinkingRenderable:
    """Rich renderable cycling kawaii frames based on elapsed time."""

    def __init__(self) -> None:
        self._t0 = time.monotonic()

    def __rich__(self) -> Any:
        elapsed = time.monotonic() - self._t0
        frame = _SPINNER_FRAMES[int(elapsed / 0.15) % len(_SPINNER_FRAMES)]
        face = _KAWAII_THINKING[int(elapsed / 1.2) % len(_KAWAII_THINKING)]
        verb = _THINKING_VERBS[int(elapsed / 2.4) % len(_THINKING_VERBS)]
        return Text(f"  {frame} {face} {verb}…", style="aegis.dim")


# ─── banner ──────────────────────────────────────────────────────────

def _build_logo() -> str:
    try:
        import pyfiglet
        art = pyfiglet.figlet_format("AEGIS-AGENT", font="ansi_shadow", width=200)
    except (ImportError, RuntimeError):
        return "AEGIS-AGENT"
    return "\n".join(line.rstrip() for line in art.splitlines()).rstrip("\n")


_SHIELD_LOGO = _build_logo()


def _startup_panel(info: dict[str, int | str]) -> Panel:
    """Build a single panel showing skills, MCP, builtin tools, and memory scope."""
    parts: list[str] = []

    # Skills
    s = info.get("skills", 0)
    parts.append(f"Skills: {s} loaded" if s else "Skills: none")

    # MCP
    servers = info.get("mcp_servers", 0)
    tools = info.get("mcp_tools", 0)
    if servers:
        parts.append(f"MCP: {servers} server{'s' if servers != 1 else ''}, {tools} tool{'s' if tools != 1 else ''}")
    else:
        parts.append("MCP: none")

    # Builtin
    parts.append(f"Builtin tools: {info.get('builtin_tools', 0)}")

    # Subagents (the Agent tool)
    if info.get("subagent_types"):
        parts.append(f"Subagents: {info.get('subagent_running', 0)} running")

    # Memory (present = USER.md or MEMORY.md was loaded; scope = personal/project)
    if info.get("memory"):
        scope = info.get("memory_scope", "personal")
        if scope == "project":
            label = f"Memory: on (project {info.get('project_id', '')})".rstrip()
        else:
            label = "Memory: on (personal)"
    else:
        label = "Memory: none"
    parts.append(label)

    body = " · ".join(parts)
    return Panel(
        Text(body, style="aegis.dim"),
        border_style="aegis.info",
        padding=(0, 2),
    )


def _banner_renderable(label: str, session_id: str, startup_info: dict[str, int | str] | None = None) -> Any:
    logo = Text(_SHIELD_LOGO, style="aegis.blue")
    title = Text.assemble(("Aegis Agent  ", "aegis.blue"), (f"v{__version__}", "aegis.dim"))
    sub = Text(f"{label} · session '{session_id}'", style="aegis.dim")
    hint = Text("type a message; /help for commands; 'exit' to quit.  (←/→ move cursor, ↑/↓ history)", style="aegis.dim")
    items: list[Any] = [
        Align.center(logo),
        Align.center(title),
        Align.center(sub),
        Align.center(hint),
        Text(),
    ]
    if startup_info:
        items.append(_startup_panel(startup_info))
        items.append(Text())
    return Group(*items)


# ─── the REPL renderer ───────────────────────────────────────────────


class _HistoryControl(FormattedTextControl):
    """Formatted history content that forwards wheel events to its pane."""

    def __init__(
        self,
        text: Callable[[], Any],
        mouse_handler: Callable[[MouseEvent], Any],
    ) -> None:
        super().__init__(text=text, focusable=False)
        self._history_mouse_handler = mouse_handler

    def mouse_handler(self, mouse_event: MouseEvent) -> Any:
        result = self._history_mouse_handler(mouse_event)
        if result is not NotImplemented:
            return result
        return super().mouse_handler(mouse_event)


class _FullscreenShell:
    """prompt_toolkit full-screen shell with scrollable output and fixed input."""

    def __init__(self, *, theme: Theme) -> None:
        from prompt_toolkit.application import Application
        from prompt_toolkit.document import Document
        from prompt_toolkit.formatted_text import ANSI
        from prompt_toolkit.history import FileHistory
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.keys import Keys
        from prompt_toolkit.layout import Layout
        from prompt_toolkit.layout.containers import HSplit, Window
        from prompt_toolkit.layout.scrollable_pane import ScrollablePane
        from prompt_toolkit.lexers import DynamicLexer
        from prompt_toolkit.styles import Style
        from prompt_toolkit.widgets import TextArea

        self._ANSI = ANSI
        self._Document = Document

        self._theme = theme
        self._lines: list[str] = []
        self._live_renderable: Any | None = None
        self._lock = threading.RLock()
        self._ready = threading.Event()
        self._submitted = threading.Event()
        self._closed = threading.Event()
        self._last_input: str | None = None
        self._last_default = ""
        self._follow_output = True
        # Absolute position in the complete history. ScrollablePane itself
        # only receives a viewport-sized slice, so its vertical_scroll is a
        # separate, slice-local coordinate.
        self._history_scroll = 0
        self._last_max_scroll = 0
        self._thread: threading.Thread | None = None

        hist_dir = Path.home() / ".aegis"
        try:
            hist_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

        style = Style([
            ("aegis.label", _BLUE),
            ("aegis.dim", "ansibrightblack"),
            ("aegis.info", _LIGHT_BLUE),
            ("aegis.ok", "ansigreen"),
            ("aegis.err", "ansired"),
            ("aegis.warn", "ansiyellow"),
            ("marker", f"bold {_BLUE}"),
            ("slash.known", f"bold {_LIGHT_BLUE}"),
            ("slash.unknown", "ansiyellow"),
            ("string", "ansimagenta"),
            ("path", "ansicyan"),
            ("mention", "ansigreen"),
            ("tag", "ansiblue"),
            ("input-frame", _BLUE),
        ])

        kb = KeyBindings()

        @kb.add("enter")
        def _accept(event) -> None:
            text = self.input_area.buffer.text.strip()
            if not text:
                return
            self._last_input = text
            self.input_area.buffer.append_to_history()
            self.input_area.buffer.reset()
            self._submitted.set()
            event.app.invalidate()

        @kb.add("c-c")
        def _sigint(event) -> None:
            self._last_input = None
            self._submitted.set()
            event.app.invalidate()

        @kb.add(Keys.ScrollUp, is_global=True)
        @kb.add("pageup", is_global=True)
        def _scroll_up(event) -> None:
            self._scroll_history(-5)

        @kb.add(Keys.ScrollDown, is_global=True)
        @kb.add("pagedown", is_global=True)
        def _scroll_down(event) -> None:
            self._scroll_history(5)

        @kb.add(Keys.ControlEnd, is_global=True)
        def _scroll_to_bottom(event) -> None:
            self._jump_to_bottom()

        @kb.add("home")
        def _scroll_top(event) -> None:
            if event.app.layout.current_control is self.input_area.control:
                event.app.current_buffer.cursor_position = 0
            else:
                self._history_scroll = 0
                self.scroll.vertical_scroll = 0
                self._follow_output = False
                event.app.invalidate()

        @kb.add("end")
        def _scroll_bottom(event) -> None:
            if event.app.layout.current_control is self.input_area.control:
                event.app.current_buffer.cursor_position = len(event.app.current_buffer.text)
            else:
                self._jump_to_bottom()

        self.output_control = _HistoryControl(
            self._formatted_output,
            self._handle_history_mouse,
        )
        output_window = Window(
            self.output_control,
            wrap_lines=True,
            always_hide_cursor=True,
            allow_scroll_beyond_bottom=False,
        )
        self.scroll = ScrollablePane(
            output_window,
            show_scrollbar=False,
            display_arrows=False,
            keep_cursor_visible=False,
            keep_focused_window_visible=False,
        )

        self.input_area = TextArea(
            height=1,
            multiline=False,
            wrap_lines=False,
            history=FileHistory(str(hist_dir / "history")),
            lexer=DynamicLexer(lambda: _AegisInputLexer()),
            prompt=self._prompt_fragments,
            focus_on_click=True,
        )
        root = HSplit([
            self.scroll,
            Window(char="─", height=1, style="class:aegis.dim"),
            self.input_area,
        ])
        self.app: Application[None] = Application(
            layout=Layout(root, focused_element=self.input_area),
            key_bindings=kb,
            mouse_support=True,
            full_screen=True,
            style=style,
            refresh_interval=0.1,
        )

    def start(self) -> bool:
        if self._thread is not None:
            return True
        self._thread = threading.Thread(target=self._run, name="aegis-tui", daemon=True)
        self._thread.start()
        return self._ready.wait(timeout=2.0)

    def _run(self) -> None:
        self._ready.set()
        try:
            self.app.run(set_exception_handler=False, handle_sigint=False)
        finally:
            self._closed.set()

    def prompt(self, default: str = "") -> str | None:
        if not self.start():
            return None
        self._last_default = default
        if default:
            self.input_area.buffer.set_document(self._Document(default, cursor_position=len(default)), bypass_readonly=True)
        else:
            self.input_area.buffer.reset()
        self._submitted.clear()
        self.invalidate()
        while not self._submitted.wait(timeout=0.05):
            if self._closed.is_set():
                return None
        text = self._last_input
        if text is not None:
            self._follow_output = True
            self.print_renderable(Group(
                Text("you❯", style="aegis.blue"),
                Padding(Text(text), (0, 0, 1, 2)),
            ))
        return text

    def print_renderable(self, renderable: Any) -> None:
        self.write_ansi(_render_to_ansi(renderable, theme=self._theme))

    def write_plain(self, text: str) -> None:
        self._append_lines(text)

    def write_ansi(self, text: str) -> None:
        self._append_lines(text)

    def set_live(self, renderable: Any | None) -> None:
        with self._lock:
            self._live_renderable = renderable
        self.invalidate()

    def clear_screen(self) -> None:
        with self._lock:
            self._lines.clear()
            self._follow_output = True
            self._history_scroll = 0
            self._last_max_scroll = 0
        self.scroll.vertical_scroll = 0
        self.invalidate()

    def close(self) -> None:
        try:
            self.app.exit()
        except Exception:  # noqa: BLE001,S110 - shutdown must stay best-effort.
            pass
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    def invalidate(self) -> None:
        try:
            self.app.invalidate()
        except Exception:  # noqa: BLE001,S110 - rendering invalidation is best-effort.
            pass

    def _append_lines(self, text: str) -> None:
        if not text:
            return
        with self._lock:
            for line in text.splitlines() or [text]:
                self._lines.append(line.rstrip("\n"))
            if len(self._lines) > _HISTORY_MAX_LINES:
                removed = len(self._lines) - _HISTORY_MAX_LINES
                del self._lines[:removed]
                if not self._follow_output:
                    self._history_scroll = max(0, self._history_scroll - removed)
        self.invalidate()

    def _prompt_fragments(self):
        return [("class:marker", "❯ ")]

    def _handle_history_mouse(self, mouse_event: MouseEvent) -> Any:
        if mouse_event.event_type is MouseEventType.SCROLL_UP:
            self._scroll_history(-_WHEEL_SCROLL_LINES)
            return None
        if mouse_event.event_type is MouseEventType.SCROLL_DOWN:
            self._scroll_history(_WHEEL_SCROLL_LINES)
            return None
        return NotImplemented

    def _scroll_history(self, delta: int) -> None:
        current = min(self._history_scroll, self._last_max_scroll)
        target = max(0, min(self._last_max_scroll, current + delta))
        self._history_scroll = target
        if delta < 0:
            self._follow_output = False
        elif delta > 0:
            self._follow_output = target >= self._last_max_scroll
        self.invalidate()

    def _jump_to_bottom(self) -> None:
        self._history_scroll = self._last_max_scroll
        self._follow_output = True
        self.invalidate()

    def _formatted_output(self):
        with self._lock:
            lines = list(self._lines)
            live = self._live_renderable
        if live is not None:
            live_text = _render_to_ansi(live, theme=self._theme).rstrip("\n")
            if live_text:
                lines.extend(live_text.splitlines())
        if not lines:
            return [("class:aegis.dim", "")]

        # Viewport clipping: only render visible lines plus a buffer for smooth scrolling
        viewport_height = self._history_viewport_height()
        self._last_max_scroll = max(0, len(lines) - viewport_height)

        if self._follow_output:
            self._history_scroll = self._last_max_scroll
        else:
            self._history_scroll = min(
                self._history_scroll,
                self._last_max_scroll,
            )

        # Calculate viewport range with buffer for smooth scrolling
        scroll_pos = self._history_scroll

        # Start from scroll position (with buffer above)
        start = max(0, scroll_pos - _VIEWPORT_BUFFER_SIZE)
        # End at scroll position + viewport height (with buffer below)
        end = min(len(lines), scroll_pos + viewport_height + _VIEWPORT_BUFFER_SIZE)

        # Only render the visible portion plus buffer
        visible_lines = lines[start:end]
        # ScrollablePane sees only visible_lines, so translate the absolute
        # history position into that slice's coordinate system. Reusing the
        # absolute value here makes a long stream appear frozen or blank once
        # the slice starts beyond line zero.
        self.scroll.vertical_scroll = scroll_pos - start
        text = "\n".join(visible_lines)
        return self._ANSI(text)

    def _history_viewport_height(self) -> int:
        """Rows available above the fixed separator and one-line composer."""
        try:
            return max(1, self.app.output.get_size().rows - 2)
        except Exception:  # noqa: BLE001 - terminal probing is best-effort.
            return 22

class _TurnState:
    """Per-turn render state."""

    def __init__(self) -> None:
        self.live: Live | None = None
        self.shell: _FullscreenShell | None = None
        self.started_text: bool = False
        self._text_buffer: list[str] = []
        self._pending_tool_call: str | None = None  # tool name displayed at TOOL_CALL

    def append_text(self, text: str) -> None:
        self._text_buffer.append(text)
        self.started_text = True

    def peek_text(self) -> str:
        return "".join(self._text_buffer)

    def pop_text(self) -> str:
        text = self.peek_text()
        self._text_buffer.clear()
        self.started_text = False
        return text

    def stop_spinner(self) -> None:
        if self.live is not None:
            self.live.stop()
            self.live = None
        if self.shell is not None:
            self.shell.set_live(None)

    def start_spinner(self, console: Console) -> None:
        if self.live is not None:
            return
        self.live = Live(
            _ThinkingRenderable(),
            console=console,
            refresh_per_second=10,
            transient=True,
        )
        self.live.start()


class Tui:
    """Renders :class:`TurnEvent` objects to a terminal stream."""

    def __init__(self, *, console: Console | None = None) -> None:
        self._console = console if console is not None else Console(theme=_THEME)
        self._is_tty = self._console.is_terminal and sys.stdin.isatty()
        self._shell = self._build_fullscreen_shell() if self._is_tty and console is None else None
        self._session = None if self._shell is not None else self._build_prompt_session()

    # -- lifecycle --------------------------------------------------------

    def banner(self, label: str, session_id: str, startup_info: dict[str, int | str] | None = None) -> None:
        if self._shell is not None:
            self._shell.print_renderable(_banner_renderable(label=label, session_id=session_id, startup_info=startup_info))
            return
        self._console.print(_banner_renderable(label=label, session_id=session_id, startup_info=startup_info))

    def prompt(self, default: str = "") -> str | None:
        """Read one input line; ``default`` prefills the composer (``/undo``)."""
        if self._shell is not None:
            return self._shell.prompt(default=default)
        if self._is_tty and self._session is not None:
            try:
                text = self._session.prompt([("class:marker", "❯ ")], default=default)
            except (EOFError, KeyboardInterrupt):
                self._console.print()
                return None
            return text.strip()
        self._console.print("❯ ", style="aegis.blue", end="", highlight=False)
        try:
            return input().strip()
        except (EOFError, KeyboardInterrupt):
            self._console.print()
            return None

    def say(self, text: str) -> None:
        if self._shell is not None:
            self._shell.print_renderable(Text(f"aegis❯ {text}", style="aegis.label"))
            return
        self._console.print(f"aegis❯ {text}", style="aegis.label", highlight=False)

    def info(self, text: str) -> None:
        if self._shell is not None:
            self._shell.print_renderable(Text(text, style="aegis.dim"))
            return
        self._console.print(text, style="aegis.dim", highlight=False)

    def out(self, text: str) -> None:
        """Print a multi-line block verbatim (help/history/session tables)."""
        if self._shell is not None:
            self._shell.write_plain(text)
            return
        self._console.print(text, highlight=False)

    def clear_screen(self) -> None:
        """Clear the terminal (the ``/clear`` command)."""
        if self._shell is not None:
            self._shell.clear_screen()
            return
        self._console.clear()

    # -- per-turn streaming ----------------------------------------------

    def begin_turn(self) -> _TurnState:
        state = _TurnState()
        if self._shell is not None:
            state.shell = self._shell
            self._shell.set_live(_ThinkingRenderable())
        elif self._is_tty:
            state.start_spinner(self._console)
        return state

    def on_event_factory(self, state: _TurnState) -> Callable[[TurnEvent], None]:
        def _handle(event: TurnEvent) -> None:
            self._render_event(state, event)
        return _handle

    def _render_event(self, state: _TurnState, event: TurnEvent) -> None:
        if event.kind is TurnEventKind.TEXT_DELTA:
            # Clear the thinking spinner only for the first text delta. Doing
            # this for every chunk briefly replaced the live Markdown with an
            # empty frame, which could expose the banner/history for one paint.
            if not state.started_text:
                state.stop_spinner()
            if self._shell is not None:
                state.append_text(event.text)
                self._shell.set_live(_assistant_markdown_renderable(state.peek_text()))
            elif self._is_tty:
                state.append_text(event.text)
            else:
                if not state.started_text:
                    self._console.print("aegis❯ ", style="aegis.label", end="", highlight=False)
                    state.started_text = True
                self._console.print(event.text, end="", markup=False, highlight=False, soft_wrap=True)
        elif event.kind is TurnEventKind.TOOL_CALL:
            if event.tool_call is not None:
                state.stop_spinner()
                self._flush_assistant_text(state)
                tc = event.tool_call
                state._pending_tool_call = tc.name
                self._emit(
                    Text.assemble(
                        ("  🔧 ", "aegis.dim"),
                        (tc.name, "aegis.info"),
                        ("  ", "aegis.dim"),
                        (_truncate(tc.arguments, _TOOL_ARGS_MAX), "aegis.dim"),
                    )
                )
        elif event.kind is TurnEventKind.TOOL_RESULT:
            if event.tool_result is not None:
                state.stop_spinner()
                self._flush_assistant_text(state)
                self._render_tool_result(event.tool_result)
                state._pending_tool_call = None
                if self._shell is not None:
                    self._shell.set_live(_ThinkingRenderable())
                elif self._is_tty:
                    state.start_spinner(self._console)
        elif event.kind is TurnEventKind.ERROR:
            state.stop_spinner()
            self._flush_assistant_text(state)
            self._emit(Text(f"  ⚠ {event.error or 'error'}", style="aegis.err"))
        elif event.kind is TurnEventKind.TURN_END:
            state.stop_spinner()
            self._flush_assistant_text(state)
            stop = event.stop_reason or ""
            if stop == StopReason.MAX_ITERATIONS.value:
                self._emit(Text("  (max iterations reached)", style="aegis.warn"))
            elif stop == StopReason.INTERRUPTED.value:
                self._emit(Text("  (interrupted)", style="aegis.warn"))

    def _flush_assistant_text(self, state: _TurnState) -> None:
        if not state.started_text:
            return
        text = state.pop_text()
        if self._shell is not None:
            self._shell.print_renderable(_assistant_markdown_renderable(text))
        elif self._is_tty:
            self._console.print(Text("aegis❯", style="aegis.label"), highlight=False)
            _render_markdown_text(self._console, text)
        else:
            self._console.print()

    def _render_tool_result(self, result: Any) -> None:
        """Show a compact status line instead of dumping raw JSON."""
        content = getattr(result, "content", "") or ""
        name = getattr(result, "name", "?")
        is_error = bool(getattr(result, "is_error", False))

        if is_error:
            # Show the error message briefly (first line only, truncated).
            err_msg = _first_line(content)
            self._emit(Text(f"  ✗ {name}: {err_msg}", style="aegis.err"))
        else:
            # Try to extract a meaningful one-liner from the result.
            summary = _tool_result_summary(name, content)
            self._emit(Text(f"  ✓ {name}{summary}", style="aegis.ok"))

    def _emit(self, renderable: Any) -> None:
        """Keep full-screen output inside history; otherwise print normally."""
        if self._shell is not None:
            self._shell.print_renderable(renderable)
            return
        self._console.print(renderable, highlight=False)

    # -- shutdown ---------------------------------------------------------

    def bye(self) -> None:
        if self._shell is not None:
            self._shell.print_renderable(Text("bye.", style="aegis.dim"))
            self._shell.close()
            return
        self._console.print("bye.", style="aegis.dim", highlight=False)

    # -- prompt_toolkit session ------------------------------------------

    def _build_fullscreen_shell(self):
        try:
            shell = _FullscreenShell(theme=_THEME)
        except Exception:  # noqa: BLE001 - unsupported terminals fall back.
            return None
        return shell if shell.start() else None

    def _build_prompt_session(self):
        if not self._is_tty:
            return None
        try:
            from prompt_toolkit import PromptSession
            from prompt_toolkit.history import FileHistory
            from prompt_toolkit.styles import Style
        except ImportError:
            return None

        hist_dir = Path.home() / ".aegis"
        try:
            hist_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        style = Style([
            ("marker", f"bold {_BLUE}"),
            ("slash.known", f"bold {_LIGHT_BLUE}"),
            ("slash.unknown", "ansiyellow"),
            ("string", "ansimagenta"),
            ("path", "ansicyan"),
            ("mention", "ansigreen"),
            ("tag", "ansiblue"),
            ("bottom-toolbar", "reverse"),
        ])
        try:
            return PromptSession(
                history=FileHistory(str(hist_dir / "history")),
                lexer=_AegisInputLexer(),
                multiline=False,
                bottom_toolbar=lambda: _slash_hint(self._session.default_buffer.text) if self._session else "",
                mouse_support=True,
                placeholder=_PROMPT_PLACEHOLDER,
                style=style,
            )
        except OSError:
            return None


# ─── helpers ──────────────────────────────────────────────────────────


def _markdown_renderable(text: str) -> Padding:
    return Padding(
        Markdown(text, code_theme=_MARKDOWN_THEME, hyperlinks=False),
        (0, 0, 1, 2),
    )


def _assistant_markdown_renderable(text: str) -> Group:
    return Group(
        Text("aegis❯", style="aegis.label"),
        _markdown_renderable(text),
    )


def _render_markdown_text(console: Console, text: str) -> None:
    """Render assistant text as Markdown, falling back to safe plain text."""
    if not text:
        return
    try:
        console.print(_markdown_renderable(text))
    except Exception:  # noqa: BLE001 — UI rendering must never break a turn.
        console.print(text, markup=False, highlight=False, soft_wrap=True)


def _render_to_ansi(renderable: Any, *, theme: Theme) -> str:
    capture = io.StringIO()
    console = Console(
        file=capture,
        force_terminal=True,
        color_system="truecolor",
        theme=theme,
        # Reserve the rightmost cell for ScrollablePane's scrollbar. Rendering
        # at the full terminal width made panels wrap by one cell as soon as
        # the scrollbar appeared, doubling their height and losing the tail.
        width=max(40, min(159, _terminal_width() - 1)),
    )
    try:
        console.print(renderable, highlight=False)
    except Exception:  # noqa: BLE001 — UI rendering must never break a turn.
        console.print(str(renderable), markup=False, highlight=False, soft_wrap=True)
    return _strip_unsupported_ansi(capture.getvalue())


def _terminal_width() -> int:
    try:
        return Console().size.width
    except Exception:  # noqa: BLE001 - terminal probing is best-effort.
        return 100


def _strip_unsupported_ansi(text: str) -> str:
    # prompt_toolkit's ANSI parser handles SGR colour/style sequences, but Rich
    # can also emit cursor/control codes for some renderables. Keep the scroll
    # buffer text-only plus SGR to avoid corrupting the fullscreen layout.
    return re.sub(r"\x1b\[(?![0-9;]*m)[0-?]*[ -/]*[@-~]", "", text)


class _AegisInputLexer(Lexer):
    """Small prompt_toolkit lexer for slash commands and common prompt tokens."""

    def lex_document(self, document):
        def _line(lineno: int):
            return _highlight_input_line(document.lines[lineno])

        return _line


def _highlight_input_line(line: str) -> list[tuple[str, str]]:
    """Return prompt_toolkit style/text fragments for one composer line."""
    if not line:
        return [("", "")]

    fragments: list[tuple[str, str]] = []
    i = 0
    while i < len(line):
        ch = line[i]
        if ch.isspace():
            j = i + 1
            while j < len(line) and line[j].isspace():
                j += 1
            fragments.append(("", line[i:j]))
            i = j
            continue
        if ch in {'"', "'"}:
            quote = ch
            j = i + 1
            while j < len(line) and line[j] != quote:
                j += 1
            if j < len(line):
                j += 1
            fragments.append(("class:string", line[i:j]))
            i = j
            continue
        j = i + 1
        while j < len(line) and not line[j].isspace() and line[j] not in {'"', "'"}:
            j += 1
        token = line[i:j]
        fragments.append((_input_token_style(token, at_start=i == 0), token))
        i = j
    return fragments


def _input_token_style(token: str, *, at_start: bool) -> str:
    if at_start and token.startswith("/"):
        return "class:slash.known" if token in _KNOWN_SLASH_COMMANDS else "class:slash.unknown"
    if token.startswith("@"):
        return "class:mention"
    if token.startswith("#"):
        return "class:tag"
    if _looks_like_path(token):
        return "class:path"
    return ""


def _looks_like_path(token: str) -> bool:
    if token in {".", "..", "~"}:
        return True
    return token.startswith(("./", "../", "~/", "/")) or "\\" in token


def _slash_hint(text: str) -> str:
    stripped = text.lstrip()
    if not stripped.startswith("/"):
        return "Enter to send · /help for commands"
    command = stripped.split(maxsplit=1)[0]
    if command in _SLASH_HINTS:
        return _SLASH_HINTS[command]
    if command in _KNOWN_SLASH_COMMANDS:
        return "known command"
    return "unknown slash command: sent to skills/model if no command matches"


def _truncate(s: str, limit: int = _TOOL_ARGS_MAX) -> str:
    if not s:
        return ""
    if len(s) > limit:
        return s[:limit] + "…"
    return s


def _first_line(s: str, limit: int = 120) -> str:
    """Return the first non-empty line of *s*, truncated to *limit*."""
    for line in s.splitlines():
        stripped = line.strip()
        if stripped:
            return _truncate(stripped, limit)
    return ""


def _tool_result_summary(name: str, content: str) -> str:
    """Try to produce a human-readable one-liner from JSON tool results."""
    if not content or not content.strip():
        return ""

    # For known tools, extract a meaningful field.
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        txt = content.strip()
        return f" — {_truncate(txt, 60)}" if txt else ""

    if not isinstance(data, dict):
        return ""

    # read_file / skill_view
    if name in ("read_file", "skill_view") and "content" in data:
        lines = str(data.get("content", "")).splitlines()
        return f" — {_truncate(lines[0] if lines else '', 50)}"

    # skills_list
    if name == "skills_list" and "count" in data:
        return f" — {data['count']} skill(s)"

    # list_directory
    if name == "list_directory" and "count" in data:
        return f" — {data['count']} entries"

    # terminal
    if name == "terminal" and "exit_code" in data:
        code = data["exit_code"]
        if "session_id" in data:  # background launch
            return f" — background {data['session_id']}"
        output = str(data.get("output", "")).strip()
        if output:
            return f" — exit {code}, {_truncate(output, 50)}"
        return f" — exit {code}"

    # MCP tools: they return {"result": ...}
    if "result" in data:
        result = data["result"]
        if isinstance(result, str):
            try:
                inner = json.loads(result)
            except (json.JSONDecodeError, TypeError):
                return f" — {_truncate(str(result), 60)}"
            if isinstance(inner, dict):
                # Try common MCP fields
                for key in ("city", "status", "count", "province"):
                    if key in inner:
                        return f" — {inner[key]}"
                return f" — {_truncate(str(list(inner.keys())), 60)}"
        return ""

    if "error" in data:
        return f" — {_truncate(str(data['error']), 60)}"

    return ""


__all__ = ["Tui"]
