"""Deterministic tests for the native Anthropic Messages provider."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from aegis_agent.cli import _build_summary_provider, _select_provider
from aegis_agent.events import collect_response
from aegis_agent.exceptions import ModelProviderError, ModelTimeoutError
from aegis_agent.models.anthropic import AnthropicProvider
from aegis_agent.models.base import Message, Role, ToolCall, ToolDefinition


class _Messages:
    def __init__(self, response) -> None:
        self.response = response
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _client(response):
    messages = _Messages(response)
    return SimpleNamespace(messages=messages), messages


def _event(event_type: str, **fields):
    return SimpleNamespace(type=event_type, **fields)


def test_streaming_text_tool_and_cache_usage_are_normalized() -> None:
    events = [
        _event(
            "message_start",
            message=SimpleNamespace(
                usage=SimpleNamespace(
                    input_tokens=100,
                    output_tokens=0,
                    cache_read_input_tokens=80,
                    cache_creation_input_tokens=10,
                )
            ),
        ),
        _event(
            "content_block_start",
            index=0,
            content_block=SimpleNamespace(type="text", text=""),
        ),
        _event(
            "content_block_delta",
            index=0,
            delta=SimpleNamespace(type="text_delta", text="checking "),
        ),
        _event(
            "content_block_start",
            index=1,
            content_block=SimpleNamespace(type="tool_use", id="tool-1", name="read_file", input={}),
        ),
        _event(
            "content_block_delta",
            index=1,
            delta=SimpleNamespace(type="input_json_delta", partial_json='{"path":'),
        ),
        _event(
            "content_block_delta",
            index=1,
            delta=SimpleNamespace(type="input_json_delta", partial_json='"README.md"}'),
        ),
        _event("content_block_stop", index=1),
        _event(
            "message_delta",
            delta=SimpleNamespace(stop_reason="tool_use"),
            # Anthropic final deltas can carry zero input/cache values.  They
            # must not erase the message_start buckets.
            usage=SimpleNamespace(
                input_tokens=0,
                output_tokens=20,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
            ),
        ),
        _event("message_stop"),
    ]
    client, _ = _client(events)
    provider = AnthropicProvider(model="claude-test", client=client)

    response = collect_response(provider.stream([Message(role=Role.USER, content="read it")]))

    assert response.content == "checking "
    assert response.finish_reason == "tool_calls"
    assert response.tool_calls == [
        ToolCall(id="tool-1", name="read_file", arguments='{"path":"README.md"}')
    ]
    assert response.usage is not None
    assert response.usage.input_tokens == 100
    assert response.usage.output_tokens == 20
    assert response.usage.cache_read_tokens == 80
    assert response.usage.cache_write_tokens == 10
    assert response.usage.total_tokens is None
    assert response.usage.cost is None


def test_streaming_usage_only_on_final_event_is_retained() -> None:
    events = [
        _event(
            "message_delta",
            delta=SimpleNamespace(stop_reason="end_turn"),
            usage=SimpleNamespace(input_tokens=12, output_tokens=4),
        ),
        _event("message_stop"),
    ]
    client, _ = _client(events)
    provider = AnthropicProvider(model="claude-test", client=client)

    response = collect_response(provider.stream([Message(role=Role.USER, content="hi")]))

    assert response.usage is not None
    assert response.usage.input_tokens == 12
    assert response.usage.output_tokens == 4


def test_one_shot_response_and_usage_are_normalized() -> None:
    raw = SimpleNamespace(
        content=[
            SimpleNamespace(type="text", text="done"),
            SimpleNamespace(type="tool_use", id="call-1", name="terminal", input={"cmd": "pwd"}),
        ],
        stop_reason="max_tokens",
        usage=SimpleNamespace(
            input_tokens=30,
            output_tokens=8,
            cache_read_input_tokens=7,
            cache_creation_input_tokens=3,
        ),
    )
    client, _ = _client(raw)
    provider = AnthropicProvider(model="claude-test", client=client, stream=False)

    response = collect_response(provider.stream([Message(role=Role.USER, content="go")]))

    assert response.content == "done"
    assert response.finish_reason == "length"
    assert response.tool_calls[0].parsed_arguments() == {"cmd": "pwd"}
    assert response.usage is not None
    assert response.usage.usage_details() == {
        "input": 30,
        "output": 8,
        "cache_read_input_tokens": 7,
        "cache_creation_input_tokens": 3,
    }


def test_missing_usage_remains_none() -> None:
    raw = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="ok")],
        stop_reason="end_turn",
        usage=None,
    )
    client, _ = _client(raw)
    provider = AnthropicProvider(model="claude-test", client=client, stream=False)

    response = collect_response(provider.stream([Message(role=Role.USER, content="hi")]))

    assert response.content == "ok"
    assert response.usage is None


def test_wire_conversion_separates_system_tools_and_tool_results() -> None:
    raw = SimpleNamespace(content=[], stop_reason="end_turn", usage=None)
    client, messages_api = _client(raw)
    provider = AnthropicProvider(
        model="claude-test",
        client=client,
        stream=False,
        max_tokens=321,
        temperature=0,
    )
    history = [
        Message(role=Role.SYSTEM, content="system rules"),
        Message(role=Role.USER, content="read"),
        Message(
            role=Role.ASSISTANT,
            tool_calls=[ToolCall(id="call-1", name="read_file", arguments='{"path":"a.txt"}')],
        ),
        Message(role=Role.TOOL, tool_call_id="call-1", name="read_file", content="contents"),
    ]
    tools = [
        ToolDefinition(
            name="read_file",
            description="Read a file",
            parameters={"type": "object", "properties": {"path": {"type": "string"}}},
        )
    ]

    collect_response(provider.stream(history, tools))
    kwargs = messages_api.calls[0]

    assert kwargs["stream"] is False
    assert kwargs["system"] == "system rules"
    assert kwargs["model"] == "claude-test"
    assert kwargs["max_tokens"] == 321
    assert kwargs["temperature"] == 0
    assert kwargs["tools"] == [
        {
            "name": "read_file",
            "description": "Read a file",
            "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
        }
    ]
    assert kwargs["messages"][1]["content"] == [
        {
            "type": "tool_use",
            "id": "call-1",
            "name": "read_file",
            "input": {"path": "a.txt"},
        }
    ]
    assert kwargs["messages"][2] == {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "call-1",
                "content": "contents",
            }
        ],
    }


def test_sdk_errors_are_normalized() -> None:
    timeout_client, _ = _client(TimeoutError("slow"))
    provider = AnthropicProvider(model="claude-test", client=timeout_client, stream=False)
    with pytest.raises(ModelTimeoutError, match="timed out"):
        collect_response(provider.stream([Message(role=Role.USER, content="hi")]))

    failed_client, _ = _client(ValueError("bad response"))
    provider = AnthropicProvider(model="claude-test", client=failed_client, stream=False)
    with pytest.raises(ModelProviderError, match="ValueError: bad response"):
        collect_response(provider.stream([Message(role=Role.USER, content="hi")]))


def test_cli_selects_anthropic_and_builds_summary_provider(monkeypatch) -> None:
    monkeypatch.delenv("AEGIS_API_KEY", raising=False)
    monkeypatch.delenv("AEGIS_MODEL", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-test")

    provider, label = _select_provider("auto")
    summary = _build_summary_provider(provider)

    assert isinstance(provider, AnthropicProvider)
    assert label == "anthropic model 'claude-test'"
    assert isinstance(summary, AnthropicProvider)
    assert summary.model == "claude-test"
    assert summary._stream is False


def test_explicit_anthropic_requires_configuration(monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    monkeypatch.setattr("aegis_agent.env.load_dotenv", lambda *args, **kwargs: None)

    with pytest.raises(ModelProviderError, match="ANTHROPIC_API_KEY"):
        _select_provider("anthropic")
