"""Deterministic fake model provider for tests and the interactive demo.

``FakeModelProvider`` never touches a network or a paid API.  It produces
responses two ways:

* **Scripted mode** — an explicit queue of :class:`FakeReply` objects consumed
  one per model call.  This gives tests full deterministic control over the
  exact sequence of tool calls and final answers (essential for exercising the
  "tool call → tool result → final answer" chain).
* **Rule-based fallback** — when the script is exhausted, a tiny deterministic
  rule set inspects the conversation so the interactive CLI stays usable
  without scripting every turn.  It recognises ``read``/``list``/``run``
  prefixes to emit the corresponding builtin tool call, and summarises after a
  tool result.

The provider emits :class:`~aegis_agent.events.ModelEvent` objects (optionally
chunking text into per-character deltas) so the streaming → response assembly
path is exercised end-to-end even in tests.
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field

from aegis_agent.events import ModelEvent
from aegis_agent.models.base import Message, Role, ToolCall, ToolDefinition


@dataclass
class FakeReply:
    """One scripted model response: optional text plus optional tool calls."""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    reasoning: str = ""  # optional chain-of-thought, emitted as REASONING_DELTA

    @classmethod
    def tool(cls, name: str, arguments: dict | str, *, call_id: str = "call_0") -> FakeReply:
        """Build a reply that requests a single tool call."""
        raw = arguments if isinstance(arguments, str) else json.dumps(arguments)
        return cls(tool_calls=[ToolCall(id=call_id, name=name, arguments=raw)], finish_reason="tool_calls")


class FakeModelProvider:
    """A :class:`~aegis_agent.models.base.ModelProvider` with scripted output.

    Parameters
    ----------
    script:
        Iterable of :class:`FakeReply` consumed in order, one per model call.
    chunk_text:
        When True, text is streamed one character per ``TEXT_DELTA`` event to
        exercise delta accumulation; otherwise a single delta is emitted.
    """

    def __init__(self, script: Sequence[FakeReply] | None = None, *, chunk_text: bool = False) -> None:
        self._script: deque[FakeReply] = deque(script or [])
        self._chunk_text = chunk_text
        #: number of model calls made — handy for assertions in tests
        self.calls = 0
        self._call_seq = 0

    @property
    def name(self) -> str:
        return "fake"

    def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition] | None = None,
    ) -> Iterator[ModelEvent]:
        self.calls += 1
        reply = self._next_reply(messages)
        if reply.reasoning:
            yield ModelEvent.reasoning_delta(reply.reasoning)
        if reply.text:
            if self._chunk_text:
                for ch in reply.text:
                    yield ModelEvent.text_delta(ch)
            else:
                yield ModelEvent.text_delta(reply.text)
        for i, tool_call in enumerate(reply.tool_calls):
            yield ModelEvent.tool(self._ensure_id(tool_call, i))
        yield ModelEvent.done(reply.finish_reason)

    # -- response selection -------------------------------------------------

    def _next_reply(self, messages: Sequence[Message]) -> FakeReply:
        if self._script:
            return self._script.popleft()
        return self._rule_based(messages)

    def _ensure_id(self, tool_call: ToolCall, index: int) -> ToolCall:
        """Guarantee a non-empty, unique-ish tool-call id."""
        if tool_call.id:
            return tool_call
        self._call_seq += 1
        return ToolCall(id=f"call_{self._call_seq}_{index}", name=tool_call.name, arguments=tool_call.arguments)

    # -- rule-based fallback (interactive demo) -----------------------------

    def _rule_based(self, messages: Sequence[Message]) -> FakeReply:
        last = messages[-1] if messages else Message(role=Role.USER, content="")

        # After a tool result, produce a final natural-language summary so the
        # loop terminates instead of re-calling tools forever.
        if last.role is Role.TOOL:
            preview = (last.content or "").strip().replace("\n", " ")
            if len(preview) > 200:
                preview = preview[:200] + "…"
            return FakeReply(text=f"Tool `{last.name}` returned: {preview}")

        if last.role is Role.USER:
            text = last.content.strip()
            command, _, rest = text.partition(" ")
            rest = rest.strip()
            if command == "read" and rest:
                return FakeReply.tool("read_file", {"path": rest}, call_id=self._next_id())
            if command in ("list", "ls"):
                return FakeReply.tool("list_directory", {"path": rest or "."}, call_id=self._next_id())
            if command in ("run", "shell") and rest:
                return FakeReply.tool("terminal", {"command": rest}, call_id=self._next_id())
            return FakeReply(text=f"Echo: {text}")

        return FakeReply(text="OK.")

    def _next_id(self) -> str:
        self._call_seq += 1
        return f"call_{self._call_seq}"


__all__ = ["FakeModelProvider", "FakeReply"]
