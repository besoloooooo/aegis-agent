"""``terminal`` builtin tool — foreground or background shell execution.

REWRITE (behaviour-equivalent to the generic surface of Hermes'
``terminal_tool``): foreground one-shot execution, or background launch that
returns a ``session_id`` managed by the ``process`` tool.

Foreground: ``{command, timeout=60, workdir}`` → ``{output, exit_code, error}``
(``error`` is null on success; a timeout reports ``exit_code`` 124).  Combined
stdout+stderr is captured and head/tail-truncated.

Background: ``background=true`` → ``{session_id, pid, output, exit_code: 0, error: null}``
immediately; the command runs under the shared :class:`ProcessRegistry` and is
driven by the ``process`` tool.

The dangerous-command guardrail is kept (operator-only ``allow_dangerous_shell``
via :class:`ToolContext` — the model can never enable it).  Dropped Hermes
concerns: sandbox backends, gateway/session routing, approval/``force`` flag,
watch patterns, ``notify_on_complete`` chat framing.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from aegis_agent.models.base import ToolResult
from aegis_agent.tools import schemas
from aegis_agent.tools.danger import detect_dangerous_command
from aegis_agent.tools.process_registry import ProcessRegistry
from aegis_agent.tools.registry import ToolContext

_DEFAULT_TIMEOUT = 60
_MAX_TIMEOUT = 600                  # foreground clamp (mirrors FOREGROUND_MAX_TIMEOUT)
_MAX_OUTPUT_CHARS = 50_000          # combined output cap (head 40% + tail 60%)

#: Substrings that suggest a command is a long-lived server/watcher → nudge to background.
_SERVER_HINTS = (
    "run dev", "start", "serve", "uvicorn", "gunicorn", "flask run",
    "npm start", "pnpm dev", "yarn dev", "watch", "tail -f", "docker-compose up",
    "docker compose up", "jupyter", "streamlit run",
)


class TerminalTool:
    """Run shell commands in the foreground, or launch them in the background."""

    definition = schemas.TERMINAL

    def __init__(self, process_registry: ProcessRegistry) -> None:
        self._registry = process_registry

    def run(self, arguments: Mapping[str, Any], context: ToolContext | None = None) -> ToolResult:
        command = arguments.get("command")
        if not command or not isinstance(command, str):
            return _error("terminal: missing required field 'command'.")

        # Dangerous-command guardrail (ported from Hermes; operator-only).
        allowed = bool(context and context.allow_dangerous_shell)
        if not allowed:
            reason = detect_dangerous_command(command)
            if reason is not None:
                return _error(
                    f"Blocked dangerous command ({reason}): {command!r}. "
                    "This command was not executed. If it is truly required, the "
                    "operator must enable dangerous shell commands explicitly."
                )

        workdir = _resolve_workdir(arguments.get("workdir"), context)
        background = bool(arguments.get("background", False))
        use_pty = bool(arguments.get("pty", False))

        if background:
            return self._run_background(command, workdir)
        return self._run_foreground(command, workdir, arguments, use_pty)

    # -- foreground ----------------------------------------------------------

    def _run_foreground(
        self,
        command: str,
        workdir: Path | None,
        arguments: Mapping[str, Any],
        use_pty: bool,
    ) -> ToolResult:
        # Nudge long-lived server/watch commands toward background mode.
        lowered = command.lower()
        if command.rstrip().endswith("&") or any(h in lowered for h in _SERVER_HINTS):
            hint = (
                "This looks like a long-running server/watch command. Prefer "
                "background=true and then manage it with the 'process' tool."
            )
        else:
            hint = None

        timeout = _as_int(arguments.get("timeout", _DEFAULT_TIMEOUT), default=_DEFAULT_TIMEOUT)
        timeout = max(1, min(timeout, _MAX_TIMEOUT))

        if use_pty:
            note = "pty=true requested; running in a normal pipe (PTY not supported on this platform)."
        else:
            note = None

        argv = ["cmd", "/c", command] if _is_windows() else ["/bin/sh", "-c", command]
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                cwd=str(workdir) if workdir else None,
                check=False,  # exit codes are reported, never raised
            )
        except subprocess.TimeoutExpired:
            payload = {
                "output": "",
                "exit_code": 124,
                "error": f"Command timed out after {timeout}s and was killed.",
            }
            return ToolResult(tool_call_id="", name=self.definition.name, content=json.dumps(payload), is_error=True)
        except OSError as exc:
            return _error(f"Could not execute command: {exc}")

        output = (completed.stdout or "") + (completed.stderr or "")
        output = _truncate_output(output)

        payload: dict[str, Any] = {"output": output, "exit_code": completed.returncode, "error": None}
        if completed.returncode != 0:
            meaning = _exit_code_meaning(completed.returncode)
            if meaning:
                payload["exit_code_meaning"] = meaning
        if hint:
            payload["hint"] = hint
        if note:
            payload["pty_note"] = note
        is_error = completed.returncode != 0 and not payload.get("exit_code_meaning")
        return ToolResult(tool_call_id="", name=self.definition.name, content=json.dumps(payload, ensure_ascii=False), is_error=is_error)

    # -- background ----------------------------------------------------------

    def _run_background(self, command: str, workdir: Path | None) -> ToolResult:
        try:
            session = self._registry.spawn_local(
                command, cwd=str(workdir) if workdir else None
            )
        except OSError as exc:
            return _error(f"Could not launch background process: {exc}")
        payload = {
            "output": "Background process started",
            "session_id": session.id,
            "pid": session.pid,
            "exit_code": 0,
            "error": None,
        }
        return ToolResult(tool_call_id="", name=self.definition.name, content=json.dumps(payload, ensure_ascii=False))


def _truncate_output(output: str) -> str:
    """Head+tail truncation at ``_MAX_OUTPUT_CHARS`` (keep 40% head, 60% tail)."""
    if len(output) <= _MAX_OUTPUT_CHARS:
        return output
    head = int(_MAX_OUTPUT_CHARS * 0.4)
    tail = _MAX_OUTPUT_CHARS - head
    return (
        output[:head]
        + f"\n... [output truncated: {len(output) - _MAX_OUTPUT_CHARS:,} chars omitted] ...\n"
        + output[-tail:]
    )


def _exit_code_meaning(code: int) -> str | None:
    """Human note for non-error non-zero exit codes (grep/diff/find conventions)."""
    if code == 1:
        return "exit code 1 — often 'no matches' / 'files differ' for grep/diff/find; not necessarily an error."
    if code == 124:
        return "exit code 124 — command timed out."
    return None


def _is_windows() -> bool:
    import platform
    return platform.system() == "Windows"


def _resolve_workdir(workdir_arg: Any, context: ToolContext | None) -> Path | None:
    if workdir_arg and isinstance(workdir_arg, str):
        expanded = Path(workdir_arg).expanduser()
        if expanded.is_absolute():
            return expanded
        base = Path(context.cwd) if context is not None else Path.cwd()
        return base / expanded
    return Path(context.cwd) if context is not None else None


def _as_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _error(message: str) -> ToolResult:
    return ToolResult(
        tool_call_id="",
        name="terminal",
        content=json.dumps({"output": "", "exit_code": -1, "error": message}, ensure_ascii=False),
        is_error=True,
    )


__all__ = ["TerminalTool"]
