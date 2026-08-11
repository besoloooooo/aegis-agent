"""Streaming model events and stream-to-response assembly.

A :class:`ModelEvent` is the atomic unit a :class:`~aegis_agent.models.base.ModelProvider`
yields while producing a response.  Providers stream fine-grained events; the
runtime folds them into a single :class:`~aegis_agent.models.base.ChatResponse`
via :func:`collect_response`, so downstream code (loop, persistence) handles
one uniform shape whether the backend truly streams or not.

This mirrors Hermes' ``chat_completion_helpers`` behaviour, where streamed
chunks (content deltas, tool-call fragments) are accumulated and then rebuilt
into a pseudo non-streaming response so the rest of the agent loop treats
streamed and non-streamed responses identically.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum

from aegis_agent.exceptions import ModelProviderError, OperationCancelled
from aegis_agent.models.base import ChatResponse, ToolCall


class ModelEventKind(str, Enum):
    """The kinds of events a provider can emit during one model call."""

    TEXT_DELTA = "text_delta"      # incremental assistant text
    REASONING_DELTA = "reasoning_delta"  # incremental chain-of-thought text
    TOOL_CALL = "tool_call"        # a complete tool-call request
    DONE = "done"                  # terminal event carrying the finish reason
    ERROR = "error"                # terminal event carrying an error message


@dataclass
class ModelEvent:
    """One streamed model event. Exactly one payload field is set per kind."""

    kind: ModelEventKind
    text: str = ""                 # TEXT_DELTA / REASONING_DELTA payload
    tool_call: ToolCall | None = None  # TOOL_CALL payload
    finish_reason: str | None = None   # DONE payload
    error: str | None = None           # ERROR payload

    @classmethod
    def text_delta(cls, text: str) -> ModelEvent:
        return cls(kind=ModelEventKind.TEXT_DELTA, text=text)

    @classmethod
    def reasoning_delta(cls, text: str) -> ModelEvent:
        return cls(kind=ModelEventKind.REASONING_DELTA, text=text)

    @classmethod
    def tool(cls, tool_call: ToolCall) -> ModelEvent:
        return cls(kind=ModelEventKind.TOOL_CALL, tool_call=tool_call)

    @classmethod
    def done(cls, finish_reason: str = "stop") -> ModelEvent:
        return cls(kind=ModelEventKind.DONE, finish_reason=finish_reason)

    @classmethod
    def failed(cls, error: str) -> ModelEvent:
        return cls(kind=ModelEventKind.ERROR, error=error)


def collect_response(
    events: Iterable[ModelEvent],
    *,
    is_cancelled: Callable[[], bool] | None = None,
    on_event: Callable[[ModelEvent], None] | None = None,
) -> ChatResponse:
    """Fold a stream of events into one :class:`ChatResponse`.

    Text deltas are concatenated, tool calls collected in order, and the
    terminal ``DONE`` event supplies the finish reason.  An ``ERROR`` event
    raises :class:`ModelProviderError`.  If the stream ends without ``DONE``,
    the response is still returned with the default ``"stop"`` finish reason.

    When ``is_cancelled`` is provided it is polled before each event; if it
    returns True, consumption stops immediately and :class:`OperationCancelled`
    is raised so the partially-streamed response is discarded rather than
    returned.  This lets a Ctrl+C / cancel event interrupt a long stream
    mid-flight, not just between model calls.

    When ``on_event`` is provided, every event is forwarded to it *as it
    arrives*, before being folded.  This is the seam a live UI hooks into to
    render streamed text and tool calls incrementally — the runtime still
    receives one fully-assembled :class:`ChatResponse` (so persistence/loop
    logic is unchanged), but the caller can observe the flow.  ``on_event`` is
    invoked *before* ``is_cancelled`` is polled for that event, and is NOT
    invoked for the synthetic nothing-to-fold case.
    """
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    finish_reason = "stop"
    for event in events:
        if on_event is not None:
            on_event(event)
        if is_cancelled is not None and is_cancelled():
            raise OperationCancelled("model stream cancelled by interrupt")
        if event.kind is ModelEventKind.TEXT_DELTA:
            text_parts.append(event.text)
        elif event.kind is ModelEventKind.REASONING_DELTA:
            reasoning_parts.append(event.text)
        elif event.kind is ModelEventKind.TOOL_CALL:
            if event.tool_call is not None:
                tool_calls.append(event.tool_call)
        elif event.kind is ModelEventKind.DONE:
            finish_reason = event.finish_reason or "stop"
        elif event.kind is ModelEventKind.ERROR:
            raise ModelProviderError(event.error or "model provider reported an error")
    return ChatResponse(
        content="".join(text_parts),
        tool_calls=tool_calls,
        finish_reason=finish_reason,
        reasoning_content="".join(reasoning_parts),
    )


__all__ = ["ModelEvent", "ModelEventKind", "collect_response"]
