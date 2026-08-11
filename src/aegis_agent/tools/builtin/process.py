"""``process`` builtin tool — manage background processes from ``terminal``.

REWRITE: a thin wrapper over :class:`aegis_agent.tools.process_registry.ProcessRegistry`,
mapping the tool's ``action`` argument onto the registry's lifecycle methods and
returning their dicts as JSON.  Actions: ``list``/``poll``/``log``/``wait``/``kill``/
``write``/``submit``/``close``.  Unknown ids yield ``{status: "not_found", ...}``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from aegis_agent.models.base import ToolResult
from aegis_agent.tools import schemas
from aegis_agent.tools.process_registry import ProcessRegistry
from aegis_agent.tools.registry import ToolContext

_ACTIONS = {"list", "poll", "log", "wait", "kill", "write", "submit", "close"}


class ProcessTool:
    """Drive the shared ProcessRegistry via a single ``action`` argument."""

    definition = schemas.PROCESS

    def __init__(self, process_registry: ProcessRegistry) -> None:
        self._registry = process_registry

    def run(self, arguments: Mapping[str, Any], context: ToolContext | None = None) -> ToolResult:
        action = arguments.get("action")
        if not action or action not in _ACTIONS:
            return _error(f"process: 'action' must be one of {sorted(_ACTIONS)}.")

        if action == "list":
            return self._ok({"processes": self._registry.list_sessions()})

        session_id = arguments.get("session_id")
        if not session_id or not isinstance(session_id, str):
            return _error(f"process: action '{action}' requires 'session_id'.")

        if action == "poll":
            payload = self._registry.poll(session_id)
        elif action == "log":
            offset = _as_int(arguments.get("offset", 0), default=0)
            limit = _as_int(arguments.get("limit", 200), default=200)
            payload = self._registry.read_log(session_id, offset=max(0, offset), limit=max(1, limit))
        elif action == "wait":
            timeout = arguments.get("timeout")
            payload = self._registry.wait(session_id, timeout=_as_int(timeout, default=0) or None)
        elif action == "kill":
            payload = self._registry.kill_process(session_id)
        elif action == "write":
            payload = self._registry.write_stdin(session_id, str(arguments.get("data", "")))
        elif action == "submit":
            payload = self._registry.submit_stdin(session_id, str(arguments.get("data", "")))
        else:  # close
            payload = self._registry.close_stdin(session_id)

        is_error = payload.get("status") in {"not_found", "error"} if isinstance(payload, dict) else False
        return ToolResult(
            tool_call_id="",
            name=self.definition.name,
            content=json.dumps(payload, ensure_ascii=False),
            is_error=is_error,
        )

    def _ok(self, payload: dict) -> ToolResult:
        return ToolResult(
            tool_call_id="",
            name=self.definition.name,
            content=json.dumps(payload, ensure_ascii=False),
        )


def _as_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _error(message: str) -> ToolResult:
    return ToolResult(
        tool_call_id="",
        name="process",
        content=json.dumps({"status": "error", "error": message}, ensure_ascii=False),
        is_error=True,
    )


__all__ = ["ProcessTool"]
