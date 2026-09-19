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
import re
import shlex
import subprocess
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from aegis_agent.exceptions import OperationCancelled
from aegis_agent.models.base import ToolResult
from aegis_agent.tools import schemas
from aegis_agent.tools.danger import detect_dangerous_command
from aegis_agent.tools.process_registry import ProcessRegistry
from aegis_agent.tools.registry import ToolContext
from aegis_agent.tools.shell import build_shell_argv

_DEFAULT_TIMEOUT = 60
_MAX_TIMEOUT = 600                  # foreground clamp (mirrors FOREGROUND_MAX_TIMEOUT)
_MAX_OUTPUT_CHARS = 50_000          # combined output cap (head 40% + tail 60%)

_LONG_RUNNING_EXECUTABLES = {
    "daphne",
    "gunicorn",
    "hypercorn",
    "jupyter",
    "nodemon",
    "ptw",
    "pytest-watch",
    "serve",
    "streamlit",
    "uvicorn",
    "vite",
    "watch",
    "watchexec",
}
_SHELL_WRAPPERS = {"command", "exec", "nohup", "sudo", "time"}
_PYTHON_RE = re.compile(r"python(?:\d+(?:\.\d+)*)?(?:\.exe)?$")


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
        is_cancelled = context.is_cancelled if context is not None else None
        return self._run_foreground(command, workdir, arguments, use_pty, is_cancelled)

    # -- foreground ----------------------------------------------------------

    def _run_foreground(
        self,
        command: str,
        workdir: Path | None,
        arguments: Mapping[str, Any],
        use_pty: bool,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> ToolResult:
        # Nudge long-lived server/watch commands toward background mode.
        if _looks_long_running(command):
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

        argv = build_shell_argv(command)
        try:
            proc = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(workdir) if workdir else None,
            )
        except OSError as exc:
            return _error(f"Could not execute command: {exc}")

        try:
            returncode, output = _wait_and_drain(proc, timeout, is_cancelled)
        except OperationCancelled:
            # Cooperative cancel: the child is already killed.  Propagate so
            # the runtime stops the turn without persisting a partial result.
            raise
        except subprocess.TimeoutExpired as exc:
            partial_output = _as_text(exc.output) if exc.output else ""
            partial_stderr = _as_text(exc.stderr) if exc.stderr else ""
            payload = {
                "output": partial_output + partial_stderr,
                "exit_code": 124,
                "error": f"Command timed out after {timeout}s and was killed.",
            }
            return ToolResult(tool_call_id="", name=self.definition.name, content=json.dumps(payload), is_error=True)

        output = _truncate_output(output)

        payload: dict[str, Any] = {
            "output": output,
            "exit_code": returncode,
            "error": None if returncode == 0 else f"Command exited with status {returncode}.",
        }
        if returncode != 0:
            meaning = _exit_code_meaning(command, returncode)
            if meaning:
                payload["exit_code_meaning"] = meaning
        if hint:
            payload["hint"] = hint
        if note:
            payload["pty_note"] = note
        is_error = returncode != 0
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


def _exit_code_meaning(command: str, code: int) -> str | None:
    """Return command-aware context without overriding non-zero error status."""
    if code == 1:
        executable_names = {
            _basename(invocation[0])
            for words in _command_segments(command)
            if (invocation := _unwrap_invocation(words))
        }
        if executable_names & {"grep", "egrep", "fgrep", "rg"}:
            return "exit code 1 — grep-style commands use this when no lines were selected."
        if executable_names & {"cmp", "diff"}:
            return "exit code 1 — diff-style commands use this when inputs differ."
        return "exit code 1 — a non-zero, command-specific failure or negative result."
    if code == 124:
        return "exit code 124 — command timed out."
    return None


def _looks_long_running(command: str) -> bool:
    """Detect server/watch invocations from command words, never argument substrings."""
    if _ends_with_background_operator(command):
        return True
    return any(_invocation_looks_long_running(words) for words in _command_segments(command))


def _ends_with_background_operator(command: str) -> bool:
    try:
        lexer = shlex.shlex(command, posix=not _is_windows(), punctuation_chars=";&|()")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return False
    return bool(tokens and tokens[-1] == "&")


def _command_segments(command: str) -> list[list[str]]:
    """Split a shell command at control operators while respecting quotes."""
    try:
        lexer = shlex.shlex(command, posix=not _is_windows(), punctuation_chars=";&|()")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return []
    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token and all(char in ";&|()" for char in token):
            if current:
                segments.append(current)
                current = []
        else:
            current.append(token)
    if current:
        segments.append(current)
    return segments


def _invocation_looks_long_running(words: list[str]) -> bool:
    words = _unwrap_invocation(words)
    if not words:
        return False
    executable = _basename(words[0])
    args = [word.lower() for word in words[1:]]

    if executable in _LONG_RUNNING_EXECUTABLES:
        return True
    if _PYTHON_RE.fullmatch(executable):
        try:
            module = args[args.index("-m") + 1]
        except (ValueError, IndexError):
            return False
        return module in {
            "flask",
            "http.server",
            "jupyter",
            "streamlit",
            "uvicorn",
            "watchdog.watchmedo",
        }
    if executable == "flask":
        return "run" in args
    if executable in {"npm", "pnpm", "yarn", "bun"}:
        scripts = [arg for arg in args if not arg.startswith("-")]
        if scripts[:1] == ["run"]:
            scripts = scripts[1:]
        return bool(scripts and scripts[0] in {"dev", "serve", "start", "watch"})
    if executable in {"docker", "podman"}:
        return len(args) >= 2 and args[0] == "compose" and args[1] == "up" and "-d" not in args
    if executable in {"docker-compose", "podman-compose"}:
        return bool(args and args[0] == "up" and "-d" not in args)
    if executable == "tail":
        return "-f" in args or "--follow" in args
    if executable in {"cargo", "dotnet"}:
        return bool(args and args[0] == "watch")
    if executable in {"next", "rails"}:
        return bool(args and args[0] in {"dev", "server", "s"})
    if executable in {"tsc", "webpack"}:
        return "--watch" in args
    return False


def _unwrap_invocation(words: list[str]) -> list[str]:
    remaining = list(words)
    while remaining:
        while remaining and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", remaining[0]):
            remaining.pop(0)
        if not remaining:
            return []
        executable = _basename(remaining[0])
        if executable in _SHELL_WRAPPERS:
            remaining.pop(0)
            while remaining and remaining[0].startswith("-"):
                remaining.pop(0)
            continue
        if executable == "env":
            remaining.pop(0)
            while remaining and (
                remaining[0].startswith("-")
                or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", remaining[0])
            ):
                remaining.pop(0)
            continue
        if (
            executable in {"uv", "poetry", "pipenv"}
            and len(remaining) > 1
            and remaining[1].lower() == "run"
        ):
            remaining = remaining[2:]
            continue
        if executable in {"npx", "pnpx"}:
            remaining.pop(0)
            while remaining and remaining[0].startswith("-"):
                remaining.pop(0)
            continue
        break
    return remaining


def _basename(value: str) -> str:
    return value.replace("\\", "/").rsplit("/", 1)[-1].lower()


def _wait_and_drain(
    proc: subprocess.Popen,
    timeout: int,
    is_cancelled: Callable[[], bool] | None,
) -> tuple[int, str]:
    """Wait for ``proc``, draining output and honouring cancel/deadline.

    Returns ``(returncode, combined_output)``.  stdout/stderr are drained
    incrementally via ``communicate(timeout=...)`` so a chatty child cannot
    wedge on a full pipe buffer.  Raises
    :class:`~aegis_agent.exceptions.OperationCancelled` when ``is_cancelled``
    fires and :class:`subprocess.TimeoutExpired` on deadline — in both cases
    the child is killed first.
    """
    deadline = time.monotonic() + timeout
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    while True:
        if is_cancelled is not None and is_cancelled():
            _kill_proc(proc)
            raise OperationCancelled("terminal command cancelled by interrupt")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _kill_proc(proc)
            raise subprocess.TimeoutExpired(
                proc.args,
                timeout,
                output="".join(stdout_parts),
                stderr="".join(stderr_parts),
            )
        try:
            out, err = proc.communicate(timeout=min(0.2, remaining))
            if out:
                stdout_parts[:] = [_as_text(out)]
            if err:
                stderr_parts[:] = [_as_text(err)]
            break
        except subprocess.TimeoutExpired as exc:
            # Python exposes cumulative partial output on each timeout and may
            # expose it as bytes even with ``text=True``.  Replace, don't append.
            if exc.output:
                stdout_parts[:] = [_as_text(exc.output)]
            if exc.stderr:
                stderr_parts[:] = [_as_text(exc.stderr)]
            continue
    return proc.returncode, "".join(stdout_parts) + "".join(stderr_parts)


def _as_text(value: str | bytes) -> str:
    """Normalize fragments exposed by ``TimeoutExpired`` before joining."""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _kill_proc(proc: subprocess.Popen) -> None:
    """Kill ``proc`` and reap it, best-effort (never raises)."""
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        pass


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
