# Portions adapted from Hermes (hermes-agent), © 2025 Nous Research.
# Licensed under the MIT License. See THIRD_PARTY_NOTICES.md.
#
# Behavioural source (decoupled and simplified):
#   * ``agent/tool_executor.py``        — dispatch a tool call, catch handler
#     exceptions and turn them into an ``{"error": ...}`` result instead of
#     crashing the loop.
#   * ``model_tools.handle_function_call`` / ``tools/registry.py:dispatch``
#     — unknown-tool → ``{"error": "Unknown tool: ..."}``.
#   * ``agent/tool_dispatch_helpers.make_tool_result_message``
#     — build a ``role=tool`` message carrying name + tool_call_id + content.
"""Tool execution: dispatch tool calls and build tool-result messages.

The executor is deliberately thin for the Stage-1 skeleton: sequential
execution, tolerant argument parsing, and a hard guarantee that a tool failure
becomes an error :class:`~aegis_agent.models.base.ToolResult` rather than an
exception that would tear down the agent loop.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable, Sequence
from typing import Any

from aegis_agent.exceptions import OperationCancelled
from aegis_agent.models.base import Message, Role, ToolCall, ToolResult
from aegis_agent.models.sanitize import repair_tool_call_arguments
from aegis_agent.tools.registry import ToolContext, ToolRegistry


class ToolExecutor:
    """Execute :class:`ToolCall` objects against a :class:`ToolRegistry`.

    Sequential and synchronous.  Concurrency, guardrails and oversized-result
    handling are intentionally out of scope for this stage (see the extraction
    plan, Stage 4).
    """

    def __init__(self, registry: ToolRegistry, context: ToolContext | None = None) -> None:
        self._registry = registry
        self._context = context or ToolContext()

    def execute(
        self,
        tool_calls: Sequence[ToolCall],
        is_cancelled: Callable[[], bool] | None = None,
    ) -> list[ToolResult]:
        """Execute a batch of tool calls in order, one result per call."""
        return [self.execute_one(tc, is_cancelled=is_cancelled) for tc in tool_calls]

    def execute_one(
        self,
        tool_call: ToolCall,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> ToolResult:
        """Execute a single tool call, never raising for tool-level failures.

        ``is_cancelled`` (when set) is injected into the tool context and
        polled by long-running tools.  A cooperative cancel raises
        :class:`~aegis_agent.exceptions.OperationCancelled`, which propagates
        out of the executor so the runtime can stop the turn without persisting
        a spurious tool result.
        """
        tool = self._registry.get(tool_call.name)
        if tool is None:
            return ToolResult(
                tool_call_id=tool_call.id,
                name=tool_call.name,
                content=_error_payload(f"Unknown tool: {tool_call.name}"),
                is_error=True,
            )

        try:
            arguments = _parse_arguments(tool_call)
        except TypeError as exc:
            return ToolResult(
                tool_call_id=tool_call.id,
                name=tool_call.name,
                content=_error_payload(f"Invalid arguments for {tool_call.name}: {exc}"),
                is_error=True,
            )

        context = self._context
        if is_cancelled is not None:
            context = dataclasses.replace(self._context, is_cancelled=is_cancelled)
            if is_cancelled():
                raise OperationCancelled("tool execution cancelled by interrupt")

        try:
            result = tool.run(arguments, context)
        except OperationCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 — convert any tool error into a result
            return ToolResult(
                tool_call_id=tool_call.id,
                name=tool_call.name,
                content=_error_payload(f"Tool execution failed: {type(exc).__name__}: {exc}"),
                is_error=True,
            )

        # Normalise: guarantee the result is correlated to this call.
        result.tool_call_id = tool_call.id
        if not result.name:
            result.name = tool_call.name
        return result

    def to_messages(self, results: Sequence[ToolResult]) -> list[Message]:
        """Convert tool results into ``role=tool`` messages for history.

        Mirrors Hermes' ``make_tool_result_message``: the message carries the
        tool ``name`` and ``tool_call_id`` alongside the content so the model
        can correlate results back to the calls they answer.
        """
        return [self.result_to_message(r) for r in results]

    @staticmethod
    def result_to_message(result: ToolResult) -> Message:
        return Message(
            role=Role.TOOL,
            content=result.content,
            tool_call_id=result.tool_call_id,
            name=result.name,
        )


def _parse_arguments(tool_call: ToolCall) -> dict[str, Any]:
    """Decode a tool call's raw argument string into a dict.

    Malformed JSON (truncated, trailing commas, literal control chars, ...)
    is repaired first via
    :func:`~aegis_agent.models.sanitize.repair_tool_call_arguments` — matching
    Hermes, which repairs rather than dropping the call — then decoded.  The
    repair pass always yields valid JSON, so ``json.loads`` cannot fail here;
    a valid-but-non-object payload raises :class:`TypeError`.
    """
    raw = tool_call.arguments
    if not raw:
        return {}
    data = json.loads(repair_tool_call_arguments(raw, tool_call.name))
    if not isinstance(data, dict):
        raise TypeError(f"tool arguments must be a JSON object, got {type(data).__name__}")
    return data


def _error_payload(message: str) -> str:
    """Encode an error as the JSON envelope the model expects."""
    return json.dumps({"error": message}, ensure_ascii=False)


__all__ = ["ToolExecutor"]
