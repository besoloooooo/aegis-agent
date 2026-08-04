"""``run_shell`` builtin tool — controlled shell command execution.

REWRITE (behaviour-equivalent to the minimal surface of Hermes' ``terminal``
tool): ``{command, timeout=30, workdir}`` → ``{output, exit_code}`` or
``{"error": ...}``.

"Controlled" for the Stage-1 skeleton means:
  * a timeout is always enforced (default 30s, hard-capped at 120s);
  * combined stdout+stderr is captured and capped at 50,000 characters
    (mirroring Hermes' ``MAX_OUTPUT_CHARS``);
  * a non-zero exit code or a timeout is reported in the payload, never raised.
Hermes-only concerns (PTY, background jobs, watch patterns, remote backends,
dangerous-command approval) are out of scope for this stage.
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
from aegis_agent.tools.registry import ToolContext

_DEFAULT_TIMEOUT = 30
_MAX_TIMEOUT = 120
_MAX_OUTPUT_CHARS = 50_000


class RunShellTool:
    """Run a shell command with a mandatory timeout and capped output."""

    definition = schemas.RUN_SHELL

    def run(self, arguments: Mapping[str, Any], context: ToolContext | None = None) -> ToolResult:
        command = arguments.get("command")
        if not command or not isinstance(command, str):
            return _error("run_shell: missing required field 'command'.")

        # Dangerous-command guardrail (ported from Hermes).  Blocked by
        # default; only an operator-supplied ToolContext can allow it — never
        # the model, since this is not exposed as a tool argument.
        allowed = bool(context and context.allow_dangerous_shell)
        if not allowed:
            reason = detect_dangerous_command(command)
            if reason is not None:
                return _error(
                    f"Blocked dangerous command ({reason}): {command!r}. "
                    "This command was not executed. If it is truly required, the "
                    "operator must enable dangerous shell commands explicitly."
                )

        timeout = _as_int(arguments.get("timeout", _DEFAULT_TIMEOUT), default=_DEFAULT_TIMEOUT)
        timeout = max(1, min(timeout, _MAX_TIMEOUT))

        workdir = _resolve_workdir(arguments.get("workdir"), context)

        try:
            completed = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(workdir) if workdir else None,
                check=False,  # exit codes are reported in the payload, never raised
            )
        except subprocess.TimeoutExpired:
            return _error(f"Command timed out after {timeout}s and was killed.")
        except OSError as exc:
            return _error(f"Could not execute command: {exc}")

        output = (completed.stdout or "") + (completed.stderr or "")
        if len(output) > _MAX_OUTPUT_CHARS:
            output = output[:_MAX_OUTPUT_CHARS] + f"\n... [output truncated at {_MAX_OUTPUT_CHARS:,} chars]"

        payload = {"output": output, "exit_code": completed.returncode}
        return ToolResult(tool_call_id="", name=self.definition.name, content=json.dumps(payload, ensure_ascii=False))


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
    return ToolResult(tool_call_id="", name="run_shell", content=json.dumps({"error": message}, ensure_ascii=False), is_error=True)


__all__ = ["RunShellTool"]
