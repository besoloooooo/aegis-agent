"""Core model-facing data structures and the :class:`ModelProvider` Protocol.

This module is intentionally a *leaf* inside the package: it depends only on
:mod:`aegis_agent.exceptions` (and the standard library) so every other module
(models, tools, context, sessions, runtime) can import these types without
creating import cycles.

The canonical conversational types live here — ``Message``, ``ToolCall``,
``ToolResult`` — because they are the lingua franca exchanged between the
model provider, the tool executor, the context builder and the session
repository.  There is exactly one ``Message`` type; the session layer stores
these objects rather than defining a parallel record type.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:  # avoid a runtime import cycle (events imports this module)
    from aegis_agent.events import ModelEvent


class Role(str, Enum):
    """Conversation roles, mirroring the OpenAI chat wire format."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass
class ToolCall:
    """A single tool invocation requested by the model.

    ``arguments`` is the *raw* JSON object string exactly as the model emitted
    it (models stream arguments as text fragments).  Use
    :meth:`parsed_arguments` to decode it tolerantly.
    """

    id: str
    name: str
    arguments: str = ""

    def parsed_arguments(self) -> dict[str, Any]:
        """Decode ``arguments`` into a dict, tolerating malformed input.

        Returns an empty dict when the payload is missing or not valid JSON,
        and raises :class:`TypeError` when it is valid JSON but not an object.
        Callers that only want best-effort behaviour should catch TypeError.
        """
        if not self.arguments:
            return {}
        try:
            data = json.loads(self.arguments)
        except (json.JSONDecodeError, TypeError):
            return {}
        if not isinstance(data, dict):
            raise TypeError(f"tool arguments must be a JSON object, got {type(data).__name__}")
        return data


@dataclass
class ToolResult:
    """The outcome of executing one :class:`ToolCall`.

    ``content`` is the string the model will see (usually a JSON-encoded
    payload produced by the tool).  ``is_error`` marks failures so the runtime
    / model can distinguish an error envelope from a normal result.
    """

    tool_call_id: str
    name: str
    content: str = ""
    is_error: bool = False


@dataclass
class Message:
    """One conversational message — the source-of-truth unit of history.

    ``tool_calls`` is populated on assistant messages that request tools.
    ``tool_call_id`` / ``name`` are populated on tool (role=TOOL) messages so
    they can be correlated back to the originating call.

    ``client_msg_id`` is the idempotency key used by the session repository to
    guarantee "one persisted logical message per client message ID".
    ``seq`` is the per-session monotonic position, assigned by the repository
    on append.  Both are *internal* fields: the context builder strips them
    from the derived view sent to the model.

    ``reasoning_content`` carries the model's chain-of-thought when the
    provider returns one (e.g. DeepSeek-style reasoners).  It is persisted with
    the message so the context compressor can account for (and progressively
    clear) it, but it is never echoed back onto the wire — see
    ``openai_compat._to_wire_message``.
    """

    role: Role
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None
    reasoning_content: str = ""
    client_msg_id: str | None = None
    seq: int | None = None


@dataclass
class ToolDefinition:
    """A tool's name/description/JSON-Schema, as advertised to the model."""

    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)

    def to_openai(self) -> dict[str, Any]:
        """Return the OpenAI chat-completions tool wrapper for this definition."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class ChatResponse:
    """A fully-assembled model response (from streaming or one-shot).

    ``content`` is the concatenated assistant text; ``tool_calls`` the parsed
    tool requests (empty for a plain final answer); ``finish_reason`` mirrors
    the provider's stop reason ("stop", "tool_calls", "length", ...).
    ``reasoning_content`` is the concatenated chain-of-thought when the
    provider streams one (empty otherwise).
    """

    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    reasoning_content: str = ""


@runtime_checkable
class ModelProvider(Protocol):
    """Structural interface every model backend must satisfy.

    The runtime only depends on this Protocol — never on a concrete provider
    — so a deterministic :class:`~aegis_agent.models.fake.FakeModelProvider`
    and a future OpenAI-compatible provider are interchangeable.

    Implementations stream :class:`~aegis_agent.events.ModelEvent` objects;
    the runtime assembles them into a :class:`ChatResponse` via
    :func:`aegis_agent.events.collect_response`.  Streaming is the primary
    contract (mirroring Hermes, where streamed chunks are reassembled into a
    uniform response) so the same path serves both real streaming backends and
    trivial one-shot fakes.
    """

    @property
    def name(self) -> str:
        """Provider identifier (e.g. "fake", "openai-compatible")."""
        ...

    def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition] | None = None,
    ) -> Iterator[ModelEvent]:
        """Yield :class:`ModelEvent` objects for one model call.

        ``messages`` is the derived context (system prompt + history) built by
        the context builder; ``tools`` is the advertised tool schema list.
        """
        ...


__all__ = [
    "ChatResponse",
    "Message",
    "ModelProvider",
    "Role",
    "ToolCall",
    "ToolDefinition",
    "ToolResult",
]
