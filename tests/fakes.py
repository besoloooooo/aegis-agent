"""Test doubles that emulate the OpenAI SDK's chat-completions surface.

These let the OpenAI-compatible provider tests run entirely offline: they build
the exact object shapes (``ChatCompletionChunk`` / ``ChatCompletion``) the
provider reads, with no network and no real tokens.
"""

from __future__ import annotations

from collections.abc import Iterable
from types import SimpleNamespace
from typing import Any


def _fn(name: str | None, arguments: str | None) -> SimpleNamespace:
    return SimpleNamespace(name=name, arguments=arguments)


def make_tool_call_delta(index: int, *, id=None, name=None, arguments=None) -> SimpleNamespace:
    """One streamed tool-call fragment inside a chunk's delta."""
    return SimpleNamespace(index=index, id=id, function=_fn(name, arguments))


def make_chunk(*, content=None, tool_calls=None, finish_reason=None) -> SimpleNamespace:
    """One ``ChatCompletionChunk``-shaped object."""
    delta = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(delta=delta, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice])


def make_usage_only_chunk() -> SimpleNamespace:
    """A trailing chunk with empty choices (usage-only), which must be ignored."""
    return SimpleNamespace(choices=[])


def make_completion(*, content=None, tool_calls=None, finish_reason="stop") -> SimpleNamespace:
    """A non-streaming ``ChatCompletion``-shaped object."""
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(message=message, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice])


def make_completion_tool_call(id: str, name: str, arguments: str) -> SimpleNamespace:
    return SimpleNamespace(id=id, function=_fn(name, arguments))


class FakeCompletions:
    def __init__(self, outer: FakeOpenAIClient) -> None:
        self._outer = outer

    def create(self, *, stream: bool = False, **kwargs: Any) -> Any:
        self._outer.calls.append({"stream": stream, **kwargs})
        if self._outer.raise_exc is not None:
            raise self._outer.raise_exc
        result = self._outer.next_result()
        if stream:
            return iter(result)  # an iterable of chunks
        return result  # a single completion object


class FakeChat:
    def __init__(self, outer: FakeOpenAIClient) -> None:
        self.completions = FakeCompletions(outer)


class FakeOpenAIClient:
    """Minimal stand-in for ``openai.OpenAI`` exposing ``chat.completions.create``.

    ``results`` is a list consumed one entry per call: for streaming calls each
    entry is an iterable of chunk objects; for non-streaming calls it's a single
    completion object.  ``raise_exc`` makes every call raise (transport error).
    """

    def __init__(self, results: Iterable[Any] | None = None, *, raise_exc: Exception | None = None) -> None:
        self._results = list(results or [])
        self.raise_exc = raise_exc
        self.calls: list[dict] = []
        self.chat = FakeChat(self)

    def next_result(self) -> Any:
        if not self._results:
            raise AssertionError("FakeOpenAIClient ran out of scripted results")
        return self._results.pop(0)
