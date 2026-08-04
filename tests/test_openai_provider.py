"""OpenAICompatibleProvider tests — all offline via a fake client.

None of these consume real tokens; they inject a :class:`FakeOpenAIClient`.
The one test that would touch a real endpoint is marked ``integration`` and
skipped unless ``AEGIS_RUN_INTEGRATION`` is set.
"""

from __future__ import annotations

import os

import pytest

from aegis_agent.events import collect_response
from aegis_agent.exceptions import ModelProviderError, ModelTimeoutError
from aegis_agent.models.base import Message, Role
from aegis_agent.models.openai_compat import (
    ENV_API_KEY,
    ENV_MODEL,
    OpenAICompatibleProvider,
    _to_wire_message,
)
from aegis_agent.models.sanitize import sanitize_surrogates
from tests.fakes import (
    FakeOpenAIClient,
    make_chunk,
    make_completion,
    make_completion_tool_call,
    make_tool_call_delta,
)


def _provider(client, *, stream=True):
    return OpenAICompatibleProvider(api_key="k", base_url="http://x", model="m", stream=stream, client=client)


def _user(text):
    return [Message(role=Role.USER, content=text)]


# -- streaming ---------------------------------------------------------------


def test_streaming_text_response():
    client = FakeOpenAIClient(results=[[make_chunk(content="hi "), make_chunk(content="there"), make_chunk(finish_reason="stop")]])
    provider = _provider(client, stream=True)
    response = collect_response(provider.stream(_user("q")))
    assert response.content == "hi there"
    assert client.calls[0]["stream"] is True


def test_streaming_tool_call():
    chunks = [
        make_chunk(tool_calls=[make_tool_call_delta(0, id="c1", name="read_file", arguments='{"path":"a"}')]),
        make_chunk(finish_reason="tool_calls"),
    ]
    provider = _provider(FakeOpenAIClient(results=[chunks]), stream=True)
    response = collect_response(provider.stream(_user("read a")))
    assert response.tool_calls[0].name == "read_file"


# -- non-streaming -----------------------------------------------------------


def test_non_streaming_text_response():
    client = FakeOpenAIClient(results=[make_completion(content="answer")])
    provider = _provider(client, stream=False)
    response = collect_response(provider.stream(_user("q")))
    assert response.content == "answer"
    assert client.calls[0]["stream"] is False


def test_non_streaming_tool_call():
    completion = make_completion(
        tool_calls=[make_completion_tool_call("c1", "run_shell", '{"command":"ls"}')],
        finish_reason="tool_calls",
    )
    provider = _provider(FakeOpenAIClient(results=[completion]), stream=False)
    response = collect_response(provider.stream(_user("run ls")))
    assert response.tool_calls[0].name == "run_shell"
    assert response.tool_calls[0].parsed_arguments() == {"command": "ls"}


# -- error / timeout normalisation ------------------------------------------


def test_transport_error_becomes_model_provider_error():
    client = FakeOpenAIClient(raise_exc=RuntimeError("connection reset"))
    provider = _provider(client, stream=True)
    with pytest.raises(ModelProviderError):
        collect_response(provider.stream(_user("q")))


def test_timeout_becomes_model_timeout_error():
    class APITimeoutError(Exception):
        pass

    client = FakeOpenAIClient(raise_exc=APITimeoutError("deadline exceeded"))
    provider = _provider(client, stream=True)
    with pytest.raises(ModelTimeoutError):
        collect_response(provider.stream(_user("q")))


# -- config from env ---------------------------------------------------------


def test_from_env_requires_api_key(monkeypatch):
    monkeypatch.delenv(ENV_API_KEY, raising=False)
    monkeypatch.setenv(ENV_MODEL, "m")
    monkeypatch.setattr("aegis_agent.env.load_dotenv", lambda *a, **kw: False)
    with pytest.raises(ModelProviderError):
        OpenAICompatibleProvider.from_env()


def test_from_env_requires_model(monkeypatch):
    monkeypatch.setenv(ENV_API_KEY, "k")
    monkeypatch.delenv(ENV_MODEL, raising=False)
    monkeypatch.setattr("aegis_agent.env.load_dotenv", lambda *a, **kw: False)
    with pytest.raises(ModelProviderError):
        OpenAICompatibleProvider.from_env()


def test_from_env_builds_provider(monkeypatch):
    monkeypatch.setattr("aegis_agent.env.load_dotenv", lambda *a, **kw: False)
    monkeypatch.setenv(ENV_API_KEY, "k")
    monkeypatch.setenv(ENV_MODEL, "gpt-4o-mini")
    monkeypatch.setenv("AEGIS_BASE_URL", "http://localhost:1234/v1")
    provider = OpenAICompatibleProvider.from_env()
    assert provider.model == "gpt-4o-mini"


# -- wire-format mapping -----------------------------------------------------


def test_wire_message_tool_result_shape():
    msg = Message(role=Role.TOOL, content='{"ok":1}', name="read_file", tool_call_id="c1")
    wire = _to_wire_message(msg)
    assert wire["role"] == "tool"
    assert wire["tool_call_id"] == "c1"
    assert wire["name"] == "read_file"


def test_wire_message_assistant_tool_calls_shape():
    from aegis_agent.models.base import ToolCall

    msg = Message(role=Role.ASSISTANT, content="", tool_calls=[ToolCall(id="c1", name="t", arguments='{"a":1}')])
    wire = _to_wire_message(msg)
    assert wire["tool_calls"][0]["id"] == "c1"
    assert wire["tool_calls"][0]["function"]["name"] == "t"


# -- surrogate cleaning -----------------------------------------------------


def test_scrub_passes_clean_text():
    assert sanitize_surrogates("hello") == "hello"
    assert sanitize_surrogates("你好") == "你好"
    assert sanitize_surrogates("") == ""


def test_scrub_replaces_lone_surrogates():
    contaminated = "abc\udce5def"
    scrubbed = sanitize_surrogates(contaminated)
    assert "\udce5" not in scrubbed
    assert scrubbed == "abc�def"  # U+FFFD replacement character


def test_scrub_replaces_unpaired_high_surrogate():
    # A lone HIGH surrogate (D800-DBFF) is also invalid UTF-8; a
    # surrogateescape-based scrub would still crash on it, the full-range
    # regex does not.
    assert sanitize_surrogates("x\ud800y") == "x�y"


def test_wire_message_scrubs_contaminated_content():
    txt = "hello\udce5world"
    msg = Message(role=Role.USER, content=txt)
    wire = _to_wire_message(msg)
    assert "\udce5" not in wire["content"]
    assert "�" in wire["content"]


def test_wire_message_scrubs_tool_call_names_and_args():
    from aegis_agent.models.base import ToolCall

    msg = Message(role=Role.ASSISTANT, content="", tool_calls=[ToolCall(id="c1", name="t\udce5ool", arguments='{"a":"\udce5"}')])
    wire = _to_wire_message(msg)
    fc = wire["tool_calls"][0]["function"]
    assert "\udce5" not in fc["name"]
    assert "\udce5" not in fc["arguments"]


# -- optional real-API integration (skipped by default) ---------------------


@pytest.mark.skipif(
    not os.environ.get("AEGIS_RUN_INTEGRATION"),
    reason="integration test; set AEGIS_RUN_INTEGRATION=1 (and AEGIS_* config) to run against a real endpoint",
)
def test_real_endpoint_smoke():  # pragma: no cover - opt-in only
    provider = OpenAICompatibleProvider.from_env()
    response = collect_response(provider.stream(_user("Say the single word: ping")))
    assert isinstance(response.content, str)
