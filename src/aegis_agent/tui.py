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

import json
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from rich.align import Align
from rich.console import Console, Group
from rich.live import Live
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


def _startup_panel(info: dict[str, int]) -> Panel:
    """Build a single panel showing skills, MCP, and builtin tools counts."""
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

    # Memory (present = USER.md or MEMORY.md was loaded)
    parts.append("Memory: on" if info.get("memory") else "Memory: none")

    body = " · ".join(parts)
    return Panel(
        Text(body, style="aegis.dim"),
        border_style="aegis.info",
        padding=(0, 2),
    )


def _banner_renderable(label: str, session_id: str, startup_info: dict[str, int] | None = None) -> Any:
    logo = Text(_SHIELD_LOGO, style="aegis.blue")
    title = Text.assemble(("Aegis Agent  ", "aegis.blue"), (f"v{__version__}", "aegis.dim"))
    sub = Text(f"{label} · session '{session_id}'", style="aegis.dim")
    hint = Text("type a message; 'exit' to quit.  (←/→ move cursor, ↑/↓ history)", style="aegis.dim")
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


class _TurnState:
    """Per-turn render state."""

    def __init__(self) -> None:
        self.live: Live | None = None
        self.started_text: bool = False
        self._pending_tool_call: str | None = None  # tool name displayed at TOOL_CALL

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
    """Renders :class:`TurnEvent` objects to a terminal stream."""

    def __init__(self, *, console: Console | None = None) -> None:
        self._console = console if console is not None else Console(theme=_THEME)
        self._is_tty = self._console.is_terminal and sys.stdin.isatty()
        self._session = self._build_prompt_session()

    # -- lifecycle --------------------------------------------------------

    def banner(self, label: str, session_id: str, startup_info: dict[str, int] | None = None) -> None:
        self._console.print(_banner_renderable(label=label, session_id=session_id, startup_info=startup_info))

    def prompt(self) -> str | None:
        if self._is_tty and self._session is not None:
            try:
                text = self._session.prompt([("class:marker", "❯ ")])
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
            self._console.print(event.text, end="", markup=False, highlight=False, soft_wrap=True)
        elif event.kind is TurnEventKind.TOOL_CALL:
            if event.tool_call is not None:
                state.stop_spinner()
                if state.started_text:
                    self._console.print()
                    state.started_text = False
                tc = event.tool_call
                state._pending_tool_call = tc.name
                self._console.print(
                    f"  🔧 {tc.name}  {_truncate(tc.arguments, _TOOL_ARGS_MAX)}",
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
                state._pending_tool_call = None
                if self._is_tty:
                    state.start_spinner(self._console)
        elif event.kind is TurnEventKind.ERROR:
            state.stop_spinner()
            if state.started_text:
                self._console.print()
                state.started_text = False
            self._console.print(f"  ⚠ {event.error or 'error'}", style="aegis.err", highlight=False)
        elif event.kind is TurnEventKind.TURN_END:
            state.stop_spinner()
            if state.started_text:
                self._console.print()
                state.started_text = False
            stop = event.stop_reason or ""
            if stop == StopReason.MAX_ITERATIONS.value:
                self._console.print("  (max iterations reached)", style="aegis.warn")
            elif stop == StopReason.INTERRUPTED.value:
                self._console.print("  (interrupted)", style="aegis.warn")

    def _render_tool_result(self, result: Any) -> None:
        """Show a compact status line instead of dumping raw JSON."""
        content = getattr(result, "content", "") or ""
        name = getattr(result, "name", "?")
        is_error = bool(getattr(result, "is_error", False))

        if is_error:
            # Show the error message briefly (first line only, truncated).
            err_msg = _first_line(content)
            self._console.print(
                f"  ✗ {name}: {err_msg}",
                style="aegis.err",
                highlight=False,
            )
        else:
            # Try to extract a meaningful one-liner from the result.
            summary = _tool_result_summary(name, content)
            self._console.print(
                f"  ✓ {name}{summary}",
                style="aegis.ok",
                highlight=False,
            )

    # -- shutdown ---------------------------------------------------------

    def bye(self) -> None:
        self._console.print("bye.", style="aegis.dim", highlight=False)

    # -- prompt_toolkit session ------------------------------------------

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
        style = Style([("marker", f"bold {_BLUE}")])
        try:
            return PromptSession(
                history=FileHistory(str(hist_dir / "history")),
                style=style,
            )
        except OSError:
            return None


# ─── helpers ──────────────────────────────────────────────────────────


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
