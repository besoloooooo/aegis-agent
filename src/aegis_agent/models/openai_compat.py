# Portions adapted from Hermes (hermes-agent), © 2025 Nous Research.
# Licensed under the MIT License. See THIRD_PARTY_NOTICES.md.
#
# Behavioural source (decoupled and simplified):
#   * ``agent/chat_completion_helpers.py`` — the OpenAI-compatible transport:
#     ``client.chat.completions.create(**kwargs)`` for both non-streaming and
#     streaming (``stream=True``) modes; streamed chunks are reassembled into a
#     uniform response. Failover / rate-guard / credential-pool concerns are
#     intentionally dropped for this stage.
"""OpenAI-compatible model provider.

Talks to any OpenAI Chat Completions-compatible endpoint (OpenAI, OpenRouter,
Ollama, vLLM, LM Studio, ...) via the official ``openai`` SDK.  Configuration
is read from the environment (``AEGIS_API_KEY`` / ``AEGIS_BASE_URL`` /
``AEGIS_MODEL``) so no secrets live in code.

The provider only implements the :class:`~aegis_agent.models.base.ModelProvider`
Protocol — the runtime never imports it directly, preserving the rule that the
Agent Loop does not depend on a concrete provider.  It supports both true
streaming (default) and a non-streaming mode; both are normalised to the same
:class:`~aegis_agent.events.ModelEvent` stream so downstream code is uniform.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Sequence
from typing import Any

from aegis_agent.events import ModelEvent
from aegis_agent.exceptions import ModelProviderError, ModelTimeoutError
from aegis_agent.models.base import Message, Role, ToolCall, ToolDefinition
from aegis_agent.models.sanitize import sanitize_surrogates
from aegis_agent.models.stream import assemble_stream

ENV_API_KEY = "AEGIS_API_KEY"
ENV_BASE_URL = "AEGIS_BASE_URL"
ENV_MODEL = "AEGIS_MODEL"

DEFAULT_TIMEOUT = 60.0


class OpenAICompatibleProvider:
    """A :class:`ModelProvider` backed by an OpenAI-compatible endpoint.

    Parameters mirror the environment configuration but can be supplied
    explicitly (mainly for tests, which inject a fake client).  The ``client``
    parameter allows dependency-injecting any object exposing
    ``chat.completions.create(**kwargs)`` — the entire surface this provider
    depends on — so tests never touch the network.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        stream: bool = True,
        client: Any | None = None,
        max_tokens: int | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url
        self._model = model
        self._timeout = timeout
        self._stream = stream
        self._max_tokens = max_tokens
        self._client = client  # lazily built from env/args when None

    @classmethod
    def from_env(cls, *, stream: bool = True, timeout: float = DEFAULT_TIMEOUT) -> OpenAICompatibleProvider:
        """Build a provider from the ``AEGIS_*`` environment variables.

        A project-local ``.env`` file is loaded first (the real environment
        still wins), so configuration saved in ``.env`` is picked up without
        being exported.  Raises :class:`ModelProviderError` when required
        configuration is missing, with an actionable message naming the
        variable.
        """
        from aegis_agent.env import load_dotenv

        load_dotenv()
        api_key = os.environ.get(ENV_API_KEY)
        base_url = os.environ.get(ENV_BASE_URL)
        model = os.environ.get(ENV_MODEL)
        if not api_key:
            raise ModelProviderError(f"{ENV_API_KEY} is not set; export it to use the OpenAI-compatible provider.")
        if not model:
            raise ModelProviderError(f"{ENV_MODEL} is not set; export it (e.g. 'gpt-4o-mini').")
        return cls(api_key=api_key, base_url=base_url, model=model, stream=stream, timeout=timeout)

    @property
    def name(self) -> str:
        return "openai-compatible"

    @property
    def model(self) -> str | None:
        return self._model

    def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition] | None = None,
    ) -> Iterator[ModelEvent]:
        """Call the endpoint and yield ModelEvents (streaming or one-shot)."""
        client = self._ensure_client()
        kwargs = self._build_kwargs(messages, tools)

        if self._stream:
            yield from self._stream_call(client, kwargs)
        else:
            yield from self._oneshot_call(client, kwargs)

    # -- transport ----------------------------------------------------------

    def _stream_call(self, client: Any, kwargs: dict[str, Any]) -> Iterator[ModelEvent]:
        try:
            raw = client.chat.completions.create(stream=True, **kwargs)
            yield from assemble_stream(raw)
        except Exception as exc:
            raise _wrap_error(exc) from exc

    def _oneshot_call(self, client: Any, kwargs: dict[str, Any]) -> Iterator[ModelEvent]:
        try:
            response = client.chat.completions.create(stream=False, **kwargs)
        except Exception as exc:
            raise _wrap_error(exc) from exc
        yield from _events_from_response(response)

    def _build_kwargs(
        self, messages: Sequence[Message], tools: Sequence[ToolDefinition] | None
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": [_to_wire_message(m) for m in messages],
        }
        if tools:
            kwargs["tools"] = [t.to_openai() for t in tools]
        if self._max_tokens is not None:
            kwargs["max_tokens"] = self._max_tokens
        return kwargs

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - openai is a declared dep
            raise ModelProviderError("the 'openai' package is required for the OpenAI-compatible provider") from exc
        self._client = OpenAI(api_key=self._api_key, base_url=self._base_url, timeout=self._timeout)
        return self._client


# -- wire helpers -----------------------------------------------------------


def _to_wire_message(message: Message) -> dict[str, Any]:
    """Convert an internal Message into an OpenAI chat message dict.

    All strings are scrubbed of lone surrogates before serialisation so
    models that produce invalid UTF-8 content (e.g. certain DashScope / Qwen
    responses) do not break subsequent turns when the contaminated history
    is sent back as context.  See :mod:`aegis_agent.models.sanitize`.
    """
    wire: dict[str, Any] = {"role": message.role.value}
    wire["content"] = sanitize_surrogates(message.content) or ""
    if message.role is Role.ASSISTANT and message.tool_calls:
        wire["tool_calls"] = [
            {
                "id": sanitize_surrogates(tc.id),
                "type": "function",
                "function": {
                    "name": sanitize_surrogates(tc.name),
                    "arguments": sanitize_surrogates(tc.arguments) or "{}",
                },
            }
            for tc in message.tool_calls
        ]
    if message.role is Role.TOOL:
        wire["tool_call_id"] = sanitize_surrogates(message.tool_call_id) or ""
        if message.name:
            wire["name"] = sanitize_surrogates(message.name)
    return wire


def _events_from_response(response: Any) -> Iterator[ModelEvent]:
    """Turn a non-streaming ChatCompletion into the same ModelEvent stream."""
    choice = _first(response.choices) if getattr(response, "choices", None) else None
    if choice is None:
        yield ModelEvent.done("stop")
        return
    message = getattr(choice, "message", None)
    content = getattr(message, "content", None) if message is not None else None
    if content:
        yield ModelEvent.text_delta(content)
    tool_calls = getattr(message, "tool_calls", None) if message is not None else None
    for tc in tool_calls or []:
        function = getattr(tc, "function", None)
        yield ModelEvent.tool(
            ToolCall(
                id=getattr(tc, "id", "") or "",
                name=getattr(function, "name", "") or "",
                arguments=getattr(function, "arguments", "") or "",
            )
        )
    yield ModelEvent.done(getattr(choice, "finish_reason", None) or "stop")


def _wrap_error(exc: Exception) -> ModelProviderError:
    """Normalise an SDK/transport exception into an Aegis error type."""
    if isinstance(exc, ModelProviderError):
        return exc
    name = type(exc).__name__
    if "Timeout" in name or isinstance(exc, TimeoutError):
        return ModelTimeoutError(f"model call timed out: {exc}")
    return ModelProviderError(f"{name}: {exc}")


def _first(seq: Any) -> Any | None:
    return seq[0] if seq else None


__all__ = [
    "DEFAULT_TIMEOUT",
    "ENV_API_KEY",
    "ENV_BASE_URL",
    "ENV_MODEL",
    "OpenAICompatibleProvider",
]
