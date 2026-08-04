# Portions adapted from Hermes (hermes-agent), © 2025 Nous Research.
# Licensed under the MIT License. See THIRD_PARTY_NOTICES.md.
#
# Behavioural source (decoupled and simplified):
#   * ``agent/display.py`` → ``KawaiiSpinner`` (lines 559-783): braille/emoji
#     frame animation, kawaii faces + "thinking verbs".  Reduced to a single
#     rich ``Live``-driven status renderable (no daemon thread, no ``\r``
#     animation, no skin engine / ``patch_stdout``).
#   * ``hermes_cli/banner.py``: welcome ASCII logo + session line.  Aegis uses
#     its own shield artwork and a teal palette (Hermes uses gold).
#   * ``cli.py``: prompt_toolkit ``PromptSession`` for the input (cursor
#     movement, history, Ctrl-A/E, up/down recall).  Reduced to a single
#     ``PromptSession`` with a styled marker + file history; no full-screen
#     ``Application``/``HSplit``/completion widget.
"""Interactive terminal UI for the Aegis CLI.

A presentation layer that renders the live experience — banner, "thinking"
spinner, streamed assistant text, tool-call and tool-result panels — from
:class:`~aegis_agent.runtime.TurnEvent` objects the runtime emits.

Input is handled by :mod:`prompt_toolkit`, so the user gets full line editing:
left/right cursor movement, Ctrl-A / Ctrl-E, up/down history recall, and
yank/kill.  Output is rendered with :mod:`rich` (panels, styled text, a
``Live``-driven spinner).  When stdin/stdout is not a TTY (tests, pipes) the
input falls back to plain :func:`input` and the spinner is skipped, keeping
captured output clean.

Appearance is deliberately distinct from Hermes: a teal shield palette and a
``❯`` prompt glyph rather than Hermes' gold caduceus.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rich.align import Align
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.text import Text
from rich.theme import Theme

from aegis_agent import __version__
from aegis_agent.runtime import StopReason, TurnEvent, TurnEventKind

if TYPE_CHECKING:
    from rich.console import RenderableType

# ─── palette (teal shield — distinct from Hermes' gold) ───────────────────

_TEAL = "#2EAAC8"
_THEME = Theme({
    "aegis.teal": f"bold { _TEAL}",
    "aegis.label": _TEAL,
    "aegis.dim": "dim",
    "aegis.ok": "green",
    "aegis.err": "red",
    "aegis.warn": "yellow",
})

# ─── kawaii spinner data (adapted from Hermes agent/display.py) ────────────

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

_TOOL_RESULT_MAX = 800


class _ThinkingRenderable:
    """A rich renderable that cycles kawaii frames based on elapsed time.

    ADAPT of Hermes' ``KawaiiSpinner`` animation, but driven by rich ``Live``'s
    refresh loop instead of a daemon thread writing ``\\r`` — so it composes
    cleanly with rich output (no raw-escape interleaving).
    """

    def __init__(self) -> None:
        self._t0 = time.monotonic()

    def __rich__(self) -> RenderableType:
        elapsed = time.monotonic() - self._t0
        # Braille frame spins briskly; the kawaii face and verb change on a
        # calmer cadence so the "thinking mood" reads as alive but not jittery.
        frame = _SPINNER_FRAMES[int(elapsed / 0.15) % len(_SPINNER_FRAMES)]
        face = _KAWAII_THINKING[int(elapsed / 1.2) % len(_KAWAII_THINKING)]
        verb = _THINKING_VERBS[int(elapsed / 2.4) % len(_THINKING_VERBS)]
        return Text(f"  {frame} {face} {verb}…", style="aegis.dim")


# ─── banner ────────────────────────────────────────────────────────────────

# Big ANSI Shadow figlet banner (the rounded-box █ + ╗╔╚═ style).  Rendered in
# bold teal to match the project icon (assets/aegis-agent.png); the PNG itself
# is project branding for docs — terminals can't display it reliably, so the
# CLI uses ASCII art.  pyfiglet generates the art at startup so the title can be
# changed without re-drawing glyphs by hand.
def _build_logo() -> str:
    try:
        import pyfiglet

        art = pyfiglet.figlet_format("AEGIS-AGENT", font="ansi_shadow", width=200)
    except (ImportError, RuntimeError):  # pragma: no cover - dep missing/bad
        return "AEGIS-AGENT"
    # pyfiglet pads every line to the width of the widest; strip trailing pad
    # so centre-alignment behaves on narrow terminals.
    return "\n".join(line.rstrip() for line in art.splitlines()).rstrip("\n")


_SHIELD_LOGO = _build_logo()


def _banner_renderable(*, label: str, session_id: str) -> RenderableType:
    logo = Text(_SHIELD_LOGO, style="aegis.teal")
    title = Text.assemble(("Aegis Agent  ", "aegis.teal"), (f"v{__version__}", "aegis.dim"))
    sub = Text(f"{label} · session '{session_id}'", style="aegis.dim")
    hint = Text("type a message; 'exit' to quit.  (←/→ move cursor, ↑/↓ history)", style="aegis.dim")
    return Group(
        Align.center(logo),
        Align.center(title),
        Align.center(sub),
        Align.center(hint),
        Text(),
    )


# ─── the REPL renderer ─────────────────────────────────────────────────────


class _TurnState:
    """Per-turn render state: live spinner + whether assistant text started."""

    def __init__(self) -> None:
        self.live: Live | None = None
        self.started_text: bool = False

    def stop_spinner(self) -> None:
        if self.live is not None:
            self.live.stop()
            self.live = None

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
    """Renders :class:`TurnEvent` objects to a terminal stream.

    Constructed once per REPL session; :meth:`on_event_factory` returns the
    callback handed to :meth:`AgentRuntime.run_turn`.  Streaming text is
    printed inline as it arrives; tool calls/results get styled lines/panels.
    """

    def __init__(self, *, console: Console | None = None) -> None:
        self._console = console if console is not None else Console(theme=_THEME)
        self._is_tty = self._console.is_terminal and sys.stdin.isatty()
        self._session = self._build_prompt_session()

    # -- lifecycle --------------------------------------------------------

    def banner(self, *, label: str, session_id: str) -> None:
        self._console.print(_banner_renderable(label=label, session_id=session_id))

    def prompt(self) -> str | None:
        """Read one input line with full editing; ``None`` on EOF / Ctrl-C."""
        if self._is_tty and self._session is not None:
            try:
                text = self._session.prompt([("class:marker", "❯ ")])
            except (EOFError, KeyboardInterrupt):
                self._console.print()
                return None
            return text.strip()
        # Non-interactive fallback (tests, pipes): plain input().
        self._console.print("❯ ", style="aegis.teal", end="", highlight=False)
        try:
            return input().strip()
        except (EOFError, KeyboardInterrupt):
            self._console.print()
            return None

    def say(self, text: str) -> None:
        self._console.print(f"aegis❯ {text}", style="aegis.label", highlight=False)

    def info(self, text: str) -> None:
        self._console.print(text, style="aegis.dim", highlight=False)

    # -- per-turn streaming ----------------------------------------------

    def begin_turn(self) -> _TurnState:
        state = _TurnState()
        if self._is_tty:
            state.start_spinner(self._console)
        return state

    def on_event_factory(self, state: _TurnState) -> Callable[[TurnEvent], None]:
        def _handle(event: TurnEvent) -> None:
            self._render_event(state, event)

        return _handle

    def _render_event(self, state: _TurnState, event: TurnEvent) -> None:
        if event.kind is TurnEventKind.TEXT_DELTA:
            if not state.started_text:
                state.stop_spinner()
                self._console.print("aegis❯ ", style="aegis.label", end="", highlight=False)
                state.started_text = True
            # markup=False so model output containing '[' is not parsed; soft_wrap
            # so long lines wrap to the terminal width.
            self._console.print(event.text, end="", markup=False, highlight=False, soft_wrap=True)
        elif event.kind is TurnEventKind.TOOL_CALL:
            if event.tool_call is not None:
                state.stop_spinner()
                if state.started_text:
                    self._console.print()
                    state.started_text = False
                tc = event.tool_call
                self._console.print(
                    f"  🔧 {tc.name}  {_truncate(tc.arguments)}",
                    style="aegis.dim",
                    highlight=False,
                )
        elif event.kind is TurnEventKind.TOOL_RESULT:
            if event.tool_result is not None:
                state.stop_spinner()
                if state.started_text:
                    self._console.print()
                    state.started_text = False
                self._render_tool_result(event.tool_result)
                # Anticipate the next model call with a fresh thinking spinner.
                if self._is_tty:
                    state.start_spinner(self._console)
        elif event.kind is TurnEventKind.ERROR:
            state.stop_spinner()
            if state.started_text:
                self._console.print()
                state.started_text = False
            self._console.print(f"aegis✗ {event.error or 'error'}", style="aegis.err", highlight=False)
        elif event.kind is TurnEventKind.TURN_END:
            state.stop_spinner()
            if state.started_text:
                self._console.print()
                state.started_text = False
            stop = event.stop_reason or ""
            if stop == StopReason.MAX_ITERATIONS.value:
                self._console.print("  (stopped: max iterations reached)", style="aegis.warn")
            elif stop == StopReason.INTERRUPTED.value:
                self._console.print("  (interrupted)", style="aegis.warn")

    def _render_tool_result(self, result: Any) -> None:
        content = getattr(result, "content", "") or ""
        name = getattr(result, "name", "?")
        is_error = bool(getattr(result, "is_error", False))
        if len(content) > _TOOL_RESULT_MAX:
            content = content[:_TOOL_RESULT_MAX] + f" … ({len(content)} chars)"
        border = "aegis.err" if is_error else "aegis.dim"
        title = f"{'✗' if is_error else '↳'} {name}"
        self._console.print(
            Panel(content, title=title, border_style=border, expand=False, padding=(0, 1)),
            highlight=False,
        )

    # -- shutdown ---------------------------------------------------------

    def bye(self) -> None:
        self._console.print("bye.", style="aegis.dim", highlight=False)

    # -- prompt_toolkit session ------------------------------------------

    def _build_prompt_session(self):
        """Build a prompt_toolkit PromptSession with history, or None.

        Returns None when not interactive so :meth:`prompt` falls back to
        plain :func:`input` (keeps tests / piped runs clean).
        """
        if not self._is_tty:
            return None
        try:
            from prompt_toolkit import PromptSession
            from prompt_toolkit.history import FileHistory
            from prompt_toolkit.styles import Style
        except ImportError:  # pragma: no cover - prompt_toolkit is a declared dep
            return None

        hist_dir = Path.home() / ".aegis"
        try:
            hist_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        style = Style([("marker", f"bold { _TEAL}")])
        try:
            return PromptSession(
                history=FileHistory(str(hist_dir / "history")),
                style=style,
            )
        except OSError:  # pragma: no cover - defensive: fall back to input()
            return None


def _truncate(s: str, limit: int = 240) -> str:
    if not s:
        return ""
    if len(s) > limit:
        return s[:limit] + "…"
    return s


__all__ = ["Tui", "_ThinkingRenderable"]
