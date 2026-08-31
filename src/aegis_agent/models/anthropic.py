"""Native Anthropic Messages API provider.

The adapter converts Anthropic-specific message, tool, stream, and usage
shapes at the provider boundary.  The runtime continues to consume only
provider-neutral :class:`ModelEvent` objects.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from aegis_agent.events import ModelEvent
from aegis_agent.exceptions import ModelProviderError, ModelTimeoutError
from aegis_agent.models.base import Message, ModelUsage, Role, ToolCall, ToolDefinition
from aegis_agent.models.sanitize import sanitize_surrogates
from aegis_agent.models.usage import parse_anthropic_usage

ENV_API_KEY = "ANTHROPIC_API_KEY"
ENV_BASE_URL = "ANTHROPIC_BASE_URL"
ENV_MODEL = "ANTHROPIC_MODEL"

DEFAULT_MAX_TOKENS = 8192
DEFAULT_TIMEOUT = 60.0


class AnthropicProvider:
    """A native Anthropic Messages API implementation of ``ModelProvider``."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        timeout: float = DEFAULT_TIMEOUT,
        stream: bool = True,
        temperature: float | None = None,
        client: Any | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url
        self._model = model
        self._max_tokens = max_tokens
        self._timeout = timeout
        self._stream = stream
        self._temperature = temperature
        self._client = client

    @classmethod
    def from_env(
        cls,
        *,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        timeout: float = DEFAULT_TIMEOUT,
        stream: bool = True,
        temperature: float | None = None,
    ) -> AnthropicProvider:
        """Build a provider from ``ANTHROPIC_*`` environment variables."""
        from aegis_agent.env import load_dotenv

        load_dotenv()
        api_key = os.environ.get(ENV_API_KEY)
        model = os.environ.get(ENV_MODEL)
        if not api_key:
            raise ModelProviderError(f"{ENV_API_KEY} is not set; export it to use Anthropic.")
        if not model:
            raise ModelProviderError(f"{ENV_MODEL} is not set; export an Anthropic model name.")
        return cls(
            api_key=api_key,
            base_url=os.environ.get(ENV_BASE_URL),
            model=model,
            max_tokens=max_tokens,
            timeout=timeout,
            stream=stream,
            temperature=temperature,
        )

    @property
    def name(self) -> str:
        return "anthropic"

    @property
    def model(self) -> str | None:
        return self._model

    def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition] | None = None,
    ) -> Iterator[ModelEvent]:
        client = self._ensure_client()
        kwargs = self._build_kwargs(messages, tools)
        if self._stream:
            yield from self._stream_call(client, kwargs)
        else:
            yield from self._oneshot_call(client, kwargs)

    def _stream_call(self, client: Any, kwargs: dict[str, Any]) -> Iterator[ModelEvent]:
        raw = None
        try:
            raw = client.messages.create(stream=True, **kwargs)
            yield from _events_from_stream(raw)
        except Exception as exc:
            raise _wrap_error(exc) from exc
        finally:
            close = getattr(raw, "close", None)
            if callable(close):
                with suppress(Exception):  # cleanup must not mask model result
                    close()

    def _oneshot_call(self, client: Any, kwargs: dict[str, Any]) -> Iterator[ModelEvent]:
        try:
            response = client.messages.create(stream=False, **kwargs)
            yield from _events_from_response(response)
        except Exception as exc:
            raise _wrap_error(exc) from exc

    def _build_kwargs(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition] | None,
    ) -> dict[str, Any]:
        system, wire_messages = _to_wire_messages(messages)
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": wire_messages,
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = [_to_wire_tool(tool) for tool in tools]
        if self._temperature is not None:
            kwargs["temperature"] = self._temperature
        return kwargs

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from anthropic import Anthropic
        except ImportError as exc:  # pragma: no cover - declared dependency
            raise ModelProviderError("the 'anthropic' package is required for Anthropic") from exc
        client_kwargs: dict[str, Any] = {
            "api_key": self._api_key,
            "timeout": self._timeout,
        }
        if self._base_url:
            client_kwargs["base_url"] = self._base_url
        self._client = Anthropic(**client_kwargs)
        return self._client


def _to_wire_messages(messages: Sequence[Message]) -> tuple[str, list[dict[str, Any]]]:
    """Convert canonical Aegis history to Anthropic's role/content format."""
    system_parts = [sanitize_surrogates(message.content) for message in messages if message.role is Role.SYSTEM]
    system = "\n\n".join(part for part in system_parts if part)
    wire: list[dict[str, Any]] = []

    for message in messages:
        if message.role is Role.SYSTEM:
            continue
        role, blocks = _message_blocks(message)
        if wire and wire[-1]["role"] == role:
            wire[-1]["content"].extend(blocks)
        else:
            wire.append({"role": role, "content": blocks})
    return system, wire


def _message_blocks(message: Message) -> tuple[str, list[dict[str, Any]]]:
    if message.role is Role.TOOL:
        return "user", [
            {
                "type": "tool_result",
                "tool_use_id": sanitize_surrogates(message.tool_call_id or ""),
                "content": sanitize_surrogates(message.content) or "",
            }
        ]

    role = "assistant" if message.role is Role.ASSISTANT else "user"
    blocks: list[dict[str, Any]] = []
    content = sanitize_surrogates(message.content) or ""
    if content or not message.tool_calls:
        blocks.append({"type": "text", "text": content})
    if message.role is Role.ASSISTANT:
        for tool_call in message.tool_calls:
            try:
                arguments = tool_call.parsed_arguments()
            except TypeError:
                arguments = {}
            blocks.append(
                {
                    "type": "tool_use",
                    "id": sanitize_surrogates(tool_call.id),
                    "name": sanitize_surrogates(tool_call.name),
                    "input": arguments,
                }
            )
    return role, blocks


def _to_wire_tool(tool: ToolDefinition) -> dict[str, Any]:
    return {
        "name": sanitize_surrogates(tool.name),
        "description": sanitize_surrogates(tool.description),
        "input_schema": tool.parameters,
    }


@dataclass
class _ToolUse:
    id: str
    name: str
    initial_input: dict[str, Any] = field(default_factory=dict)
    fragments: list[str] = field(default_factory=list)

    def finish(self) -> ToolCall:
        arguments = "".join(self.fragments)
        if not arguments:
            arguments = json.dumps(self.initial_input, ensure_ascii=False, separators=(",", ":"))
        return ToolCall(id=self.id, name=self.name, arguments=arguments or "{}")


def _events_from_stream(raw_events: Any) -> Iterator[ModelEvent]:
    usage: ModelUsage | None = None
    tools: dict[int, _ToolUse] = {}
    finish_reason = "stop"
    saw_done = False

    for event in raw_events:
        event_type = _field(event, "type")
        if event_type == "message_start":
            message = _field(event, "message")
            usage = parse_anthropic_usage(_field(message, "usage"), usage)
            if usage is not None:
                yield ModelEvent.usage_update(usage)
        elif event_type == "content_block_start":
            index = _integer(_field(event, "index"), 0)
            block = _field(event, "content_block")
            block_type = _field(block, "type")
            if block_type == "tool_use":
                initial = _field(block, "input")
                tools[index] = _ToolUse(
                    id=str(_field(block, "id") or ""),
                    name=str(_field(block, "name") or ""),
                    initial_input=initial if isinstance(initial, dict) else {},
                )
            elif block_type == "text" and _field(block, "text"):
                yield ModelEvent.text_delta(str(_field(block, "text")))
        elif event_type == "content_block_delta":
            index = _integer(_field(event, "index"), 0)
            delta = _field(event, "delta")
            delta_type = _field(delta, "type")
            if delta_type == "text_delta":
                yield ModelEvent.text_delta(str(_field(delta, "text") or ""))
            elif delta_type == "thinking_delta":
                yield ModelEvent.reasoning_delta(str(_field(delta, "thinking") or ""))
            elif delta_type == "input_json_delta" and index in tools:
                tools[index].fragments.append(str(_field(delta, "partial_json") or ""))
        elif event_type == "content_block_stop":
            index = _integer(_field(event, "index"), 0)
            tool = tools.pop(index, None)
            if tool is not None:
                yield ModelEvent.tool(tool.finish())
        elif event_type == "message_delta":
            delta = _field(event, "delta")
            stop_reason = _field(delta, "stop_reason")
            if stop_reason:
                finish_reason = _map_stop_reason(str(stop_reason))
            usage = parse_anthropic_usage(_field(event, "usage"), usage)
            if usage is not None:
                yield ModelEvent.usage_update(usage)
        elif event_type == "message_stop":
            for index in sorted(tools):
                yield ModelEvent.tool(tools[index].finish())
            tools.clear()
            yield ModelEvent.done(finish_reason)
            saw_done = True
        elif event_type == "error":
            error = _field(event, "error")
            raise ModelProviderError(str(_field(error, "message") or error or "Anthropic stream error"))

    if not saw_done:
        for index in sorted(tools):
            yield ModelEvent.tool(tools[index].finish())
        yield ModelEvent.done(finish_reason)


def _events_from_response(response: Any) -> Iterator[ModelEvent]:
    usage = parse_anthropic_usage(_field(response, "usage"))
    if usage is not None:
        yield ModelEvent.usage_update(usage)
    for block in _field(response, "content") or []:
        block_type = _field(block, "type")
        if block_type == "text":
            yield ModelEvent.text_delta(str(_field(block, "text") or ""))
        elif block_type == "thinking":
            yield ModelEvent.reasoning_delta(str(_field(block, "thinking") or ""))
        elif block_type == "tool_use":
            arguments = _field(block, "input")
            if not isinstance(arguments, dict):
                arguments = {}
            yield ModelEvent.tool(
                ToolCall(
                    id=str(_field(block, "id") or ""),
                    name=str(_field(block, "name") or ""),
                    arguments=json.dumps(arguments, ensure_ascii=False, separators=(",", ":")),
                )
            )
    yield ModelEvent.done(_map_stop_reason(str(_field(response, "stop_reason") or "end_turn")))


def _map_stop_reason(reason: str) -> str:
    return {
        "end_turn": "stop",
        "stop_sequence": "stop",
        "tool_use": "tool_calls",
        "max_tokens": "length",
        "model_context_window_exceeded": "length",
        "refusal": "content_filter",
    }.get(reason, reason or "stop")


def _field(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _integer(value: Any, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _wrap_error(exc: Exception) -> ModelProviderError:
    if isinstance(exc, ModelProviderError):
        return exc
    name = type(exc).__name__
    if "Timeout" in name or isinstance(exc, TimeoutError):
        return ModelTimeoutError(f"model call timed out: {exc}")
    return ModelProviderError(f"{name}: {exc}")


__all__ = [
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_TIMEOUT",
    "ENV_API_KEY",
    "ENV_BASE_URL",
    "ENV_MODEL",
    "AnthropicProvider",
]
