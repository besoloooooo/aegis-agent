"""Stream-assembly tests: text deltas and streamed tool-call fragments."""

from __future__ import annotations

import json

from aegis_agent.events import ModelEventKind, collect_response
from aegis_agent.models.stream import assemble_stream
from tests.fakes import make_chunk, make_tool_call_delta, make_usage_only_chunk


def _collect(chunks):
    return collect_response(assemble_stream(chunks))


def test_streamed_text_reassembled():
    chunks = [
        make_chunk(content="Hel"),
        make_chunk(content="lo, "),
        make_chunk(content="world"),
        make_chunk(finish_reason="stop"),
    ]
    response = _collect(chunks)
    assert response.content == "Hello, world"
    assert response.tool_calls == []
    assert response.finish_reason == "stop"


def test_usage_only_chunk_ignored():
    chunks = [make_chunk(content="hi"), make_usage_only_chunk(), make_chunk(finish_reason="stop")]
    assert _collect(chunks).content == "hi"


def test_tool_call_arguments_fragmented_across_chunks():
    # name delivered once, arguments split into fragments.
    chunks = [
        make_chunk(tool_calls=[make_tool_call_delta(0, id="call_1", name="read_file")]),
        make_chunk(tool_calls=[make_tool_call_delta(0, arguments='{"pa')]),
        make_chunk(tool_calls=[make_tool_call_delta(0, arguments='th": "a.txt"')]),
        make_chunk(tool_calls=[make_tool_call_delta(0, arguments="}")]),
        make_chunk(finish_reason="tool_calls"),
    ]
    response = _collect(chunks)
    assert len(response.tool_calls) == 1
    tc = response.tool_calls[0]
    assert tc.id == "call_1"
    assert tc.name == "read_file"
    # finish() runs the repair pass; valid JSON passes through unchanged.
    assert json.loads(tc.arguments) == {"path": "a.txt"}
    assert tc.parsed_arguments() == {"path": "a.txt"}
    assert response.finish_reason == "tool_calls"


def test_repeated_name_fragments_not_concatenated():
    # Some providers resend the full name on each fragment; must not duplicate.
    chunks = [
        make_chunk(tool_calls=[make_tool_call_delta(0, id="c1", name="run_shell", arguments="{")]),
        make_chunk(tool_calls=[make_tool_call_delta(0, name="run_shell", arguments='"command": "ls"}')]),
        make_chunk(finish_reason="tool_calls"),
    ]
    response = _collect(chunks)
    assert response.tool_calls[0].name == "run_shell"  # not "run_shellrun_shell"
    assert response.tool_calls[0].parsed_arguments() == {"command": "ls"}


def test_multiple_tool_calls_in_one_response():
    chunks = [
        make_chunk(tool_calls=[make_tool_call_delta(0, id="c1", name="read_file", arguments='{"path":"a"}')]),
        make_chunk(tool_calls=[make_tool_call_delta(1, id="c2", name="list_directory", arguments='{"path":"."}')]),
        make_chunk(finish_reason="tool_calls"),
    ]
    response = _collect(chunks)
    assert [tc.name for tc in response.tool_calls] == ["read_file", "list_directory"]
    assert [tc.id for tc in response.tool_calls] == ["c1", "c2"]


def test_interleaved_multi_tool_call_fragments():
    # Two tool calls whose fragments arrive interleaved by index.
    chunks = [
        make_chunk(tool_calls=[make_tool_call_delta(0, id="c1", name="read_file")]),
        make_chunk(tool_calls=[make_tool_call_delta(1, id="c2", name="run_shell")]),
        make_chunk(tool_calls=[make_tool_call_delta(0, arguments='{"path":"x"}')]),
        make_chunk(tool_calls=[make_tool_call_delta(1, arguments='{"command":"pwd"}')]),
        make_chunk(finish_reason="tool_calls"),
    ]
    response = _collect(chunks)
    assert response.tool_calls[0].parsed_arguments() == {"path": "x"}
    assert response.tool_calls[1].parsed_arguments() == {"command": "pwd"}


def test_text_then_tool_call():
    chunks = [
        make_chunk(content="let me check"),
        make_chunk(tool_calls=[make_tool_call_delta(0, id="c1", name="list_directory", arguments="{}")]),
        make_chunk(finish_reason="tool_calls"),
    ]
    response = _collect(chunks)
    assert response.content == "let me check"
    assert response.tool_calls[0].name == "list_directory"


def test_done_event_emitted_last():
    chunks = [make_chunk(content="x"), make_chunk(finish_reason="stop")]
    events = list(assemble_stream(chunks))
    assert events[-1].kind is ModelEventKind.DONE
