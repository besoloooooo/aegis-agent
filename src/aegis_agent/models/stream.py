# Portions adapted from Hermes (hermes-agent), © 2025 Nous Research.
# Licensed under the MIT License. See THIRD_PARTY_NOTICES.md.
#
# Behavioural source (decoupled and simplified):
#   * ``agent/chat_completion_helpers.py`` streamed tool-call assembly
#     (~lines 1828-1891): function NAME is assigned (some providers resend the
#     full name each chunk, so concatenation would duplicate it) while
#     ARGUMENTS are concatenated across chunks; tool-call slots are keyed by a
#     remapped index so a provider reusing index 0 for parallel calls still
#     yields distinct calls.
"""Assemble streamed OpenAI chat-completion chunks into ModelEvents.

An OpenAI-compatible streaming response arrives as a sequence of *chunks*; the
assistant's text and each tool call are delivered in fragments spread across
many chunks.  :class:`StreamAssembler` folds those fragments back into whole
values and emits :class:`~aegis_agent.events.ModelEvent` objects:

* text deltas are forwarded as they arrive (``TEXT_DELTA``);
* tool-call fragments are accumulated and, once the stream ends, emitted as
  complete ``TOOL_CALL`` events (name + full argument JSON);
* a terminal ``DONE`` event carries the finish reason.

Keeping this as its own module (independent of any HTTP client) lets it be
unit-tested with hand-built chunk objects and reused by any future provider.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any

from aegis_agent.events import ModelEvent
from aegis_agent.models.base import ToolCall
from aegis_agent.models.sanitize import repair_tool_call_arguments


@dataclass
class _ToolCallAccumulator:
    """Mutable per-slot accumulator for one in-flight tool call."""

    id: str = ""
    name: str = ""
    arguments: str = ""


def assemble_stream(chunks: Iterable[Any]) -> Iterator[ModelEvent]:
    """Yield :class:`ModelEvent` objects from OpenAI-style streaming chunks.

    ``chunks`` is any iterable of objects shaped like OpenAI
    ``ChatCompletionChunk`` — each exposing ``choices[0].delta`` with optional
    ``.content`` and ``.tool_calls`` (each tool-call delta having ``.index``,
    ``.id`` and ``.function.name`` / ``.function.arguments``), plus
    ``choices[0].finish_reason``.  Robust to empty ``choices`` (usage-only
    trailing chunks) and missing attributes.
    """
    assembler = StreamAssembler()
    for chunk in chunks:
        yield from assembler.feed(chunk)
    yield from assembler.finish()


class StreamAssembler:
    """Stateful accumulator turning stream chunks into events.

    Text is emitted incrementally; tool calls are buffered and flushed by
    :meth:`finish` so the loop only ever sees complete tool calls with fully
    concatenated argument strings.
    """

    def __init__(self) -> None:
        # Ordered map: slot index → accumulator. Insertion order = emit order.
        self._tool_calls: dict[int, _ToolCallAccumulator] = {}
        self._finish_reason: str | None = None
        self._done = False

    def feed(self, chunk: Any) -> Iterator[ModelEvent]:
        """Process one chunk, yielding any text-delta events it carries."""
        choice = _first_choice(chunk)
        if choice is None:
            return

        finish_reason = getattr(choice, "finish_reason", None)
        if finish_reason:
            self._finish_reason = finish_reason

        delta = getattr(choice, "delta", None)
        if delta is None:
            return

        content = getattr(delta, "content", None)
        if content:
            yield ModelEvent.text_delta(content)

        tool_call_deltas = getattr(delta, "tool_calls", None)
        if tool_call_deltas:
            for tc_delta in tool_call_deltas:
                self._accumulate_tool_call(tc_delta)

    def finish(self) -> Iterator[ModelEvent]:
        """Flush accumulated tool calls, then emit the terminal DONE event.

        Argument strings are passed through
        :func:`~aegis_agent.models.sanitize.repair_tool_call_arguments` before
        emission (mirroring Hermes, which repairs at stream finalisation): a
        truncated or malformed argument payload is repaired where possible so
        the tool call can still execute with best-effort arguments.
        """
        if self._done:
            return
        self._done = True
        for acc in self._tool_calls.values():
            if not acc.name:
                # A slot with arguments but no name is unusable — skip it.
                continue
            arguments = repair_tool_call_arguments(acc.arguments, acc.name) if acc.arguments else ""
            yield ModelEvent.tool(ToolCall(id=acc.id or _synthetic_id(acc), name=acc.name, arguments=arguments))
        yield ModelEvent.done(self._finish_reason or "stop")

    # -- internals ----------------------------------------------------------

    def _accumulate_tool_call(self, tc_delta: Any) -> None:
        index = getattr(tc_delta, "index", None)
        if index is None:
            index = 0
        acc = self._tool_calls.setdefault(index, _ToolCallAccumulator())

        delta_id = getattr(tc_delta, "id", None)
        if delta_id:
            acc.id = delta_id

        function = getattr(tc_delta, "function", None)
        if function is None:
            return
        name = getattr(function, "name", None)
        if name:
            # ASSIGNMENT, not concatenation — names are atomic; some providers
            # resend the full name on every fragment.
            acc.name = name
        arguments = getattr(function, "arguments", None)
        if arguments:
            # CONCATENATION — argument JSON arrives split across chunks.
            acc.arguments += arguments


def _first_choice(chunk: Any) -> Any | None:
    choices = getattr(chunk, "choices", None)
    if not choices:
        return None
    return choices[0]


def _synthetic_id(acc: _ToolCallAccumulator) -> str:
    """Fallback id when a provider omits tool-call ids entirely."""
    return f"call_{abs(hash((acc.name, acc.arguments))) & 0xFFFFFFFF:08x}"


__all__ = ["StreamAssembler", "assemble_stream"]
