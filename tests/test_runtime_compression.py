"""Tests for the Stage-11 wiring: compression inside the Agent Loop.

Covers:
  * AgentRuntime compressing the derived context before each model call while
    the persisted source history stays untouched;
  * the per-session ContentReplacementState being held across turns (frozen
    replacement decisions, byte-stable prompt prefix);
  * reasoning_content flowing from provider events into persisted messages;
  * summary-provider sampling parameters and reasoning stripping on the wire.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace

from aegis_agent.context.builder import ContextBuilder
from aegis_agent.context.compress import estimate_tokens
from aegis_agent.context.compress_config import (
    CONTEXT_SUMMARY_TAG,
    PERSISTED_OUTPUT_TAG,
)
from aegis_agent.events import collect_response
from aegis_agent.models.base import Message, Role, ToolCall
from aegis_agent.models.fake import FakeModelProvider, FakeReply
from aegis_agent.models.openai_compat import OpenAICompatibleProvider
from aegis_agent.models.stream import assemble_stream
from aegis_agent.runtime import AgentRuntime

SUMMARY_TEXT = "Round summary: user asked a question and the assistant answered at length."


class RecordingProvider:
    """Wraps another provider, recording the exact messages of each call."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.seen: list[list[Message]] = []

    @property
    def name(self) -> str:
        return "recording"

    def stream(self, messages, tools=None):
        self.seen.append(copy.deepcopy(list(messages)))
        yield from self._inner.stream(messages, tools)


def _text_rounds(count: int, answer_chars: int = 800) -> list[Message]:
    msgs: list[Message] = []
    for i in range(count):
        msgs.append(Message(role=Role.USER, content=f"q{i}"))
        msgs.append(Message(role=Role.ASSISTANT, content="a" * answer_chars))
    return msgs


def _seed(runtime: AgentRuntime, session_id: str, history: list[Message]) -> None:
    repo = runtime.repository
    repo.create_session(session_id)
    for m in history:
        repo.append_message(session_id, m)


def _runtime(provider, budget, tmp_path, summary_provider=None) -> AgentRuntime:
    return AgentRuntime.with_defaults(
        provider=provider,
        enable_skills=False,
        enable_mcp=False,
        context_token_budget=budget,
        compress_storage_dir=str(tmp_path),
        summary_provider=summary_provider,
    )


# ---------------------------------------------------------------------------
# loop wiring
# ---------------------------------------------------------------------------


def test_compression_disabled_by_default(tmp_path) -> None:
    recording = RecordingProvider(FakeModelProvider())
    runtime = AgentRuntime.with_defaults(
        provider=recording, enable_skills=False, enable_mcp=False
    )
    runtime.run_turn("s", "hello")
    assert recording.seen, "provider was called"
    sent = recording.seen[0]
    assert all(CONTEXT_SUMMARY_TAG not in m.content for m in sent)
    # the sent context ends with the raw user message (no transformation)
    assert sent[-1].content == "hello"


def test_run_turn_compresses_derived_context_before_model_call(tmp_path) -> None:
    history = _text_rounds(4)
    recording = RecordingProvider(FakeModelProvider())
    summarizer = FakeModelProvider([FakeReply(text=SUMMARY_TEXT) for _ in range(10)])

    # Budget relative to what the runtime's builder will actually produce
    # (ContextBuilder() default identity == with_defaults default prompt).
    derived = ContextBuilder().build(history)
    budget = max(1, int(estimate_tokens(derived) * 0.5))

    runtime = _runtime(recording, budget, tmp_path, summary_provider=summarizer)
    _seed(runtime, "s", history)
    before = copy.deepcopy(runtime.repository.list_messages("s"))

    result = runtime.run_turn("s", "hello")

    assert result.stop_reason.value == "final_answer"
    assert recording.seen, "provider was called"
    sent = recording.seen[0]
    # the model saw a compressed view: at least one summarized round
    assert any(CONTEXT_SUMMARY_TAG in m.content for m in sent)
    # the summarizer (not the main provider) produced the summaries
    assert summarizer.calls >= 1
    # source history is byte-identical (plus the new user/assistant messages)
    persisted = runtime.repository.list_messages("s")
    assert persisted[: len(before)] == before
    assert all(CONTEXT_SUMMARY_TAG not in m.content for m in persisted)


# ---------------------------------------------------------------------------
# cross-turn budget state (byte-stable prompt prefix)
# ---------------------------------------------------------------------------


def test_budget_state_held_per_session_across_turns(tmp_path) -> None:
    # Five parallel results of 18k chars: each is UNDER the level-1 per-result
    # threshold (20k) but the batch (90k) exceeds the level-2 turn budget (80k),
    # so exactly the aggregate path records a frozen replacement in the state.
    batch: list[Message] = [
        Message(role=Role.USER, content="run five"),
        Message(
            role=Role.ASSISTANT,
            content="",
            tool_calls=[
                ToolCall(id=f"c{i}", name="terminal", arguments=f'{{"command": "job{i}"}}')
                for i in range(5)
            ],
        ),
    ]
    batch += [
        Message(role=Role.TOOL, tool_call_id=f"c{i}", name="terminal", content="x" * 18_000)
        for i in range(5)
    ]
    batch.append(Message(role=Role.ASSISTANT, content="done"))

    recording = RecordingProvider(FakeModelProvider())
    runtime = _runtime(recording, 1_000_000, tmp_path)  # only phase A ever acts
    _seed(runtime, "s", batch)

    runtime.run_turn("s", "first")
    runtime.run_turn("s", "second")

    def tool_contents(call_msgs: list[Message]) -> list[str]:
        return [m.content for m in call_msgs if m.role is Role.TOOL]

    first, second = tool_contents(recording.seen[0]), tool_contents(recording.seen[1])
    assert first == second  # frozen decisions replayed byte-identically
    replaced = [c for c in first if c.startswith(PERSISTED_OUTPUT_TAG)]
    assert len(replaced) == 1  # 5x18k=90k over the 80k turn budget -> largest one offloaded
    # white-box: one state per session, holding the frozen replacement
    assert len(runtime._budget_states) == 1
    assert runtime._budget_states["s"].replacements, "level-2 replacement was recorded"
    persisted_id = next(iter(runtime._budget_states["s"].replacements))
    assert (tmp_path / f"{persisted_id}.txt").exists()
    # source history still carries all five full 18k results
    persisted = runtime.repository.list_messages("s")
    assert sum(1 for m in persisted if m.role is Role.TOOL and len(m.content) == 18_000) == 5


def test_budget_state_isolated_between_sessions(tmp_path) -> None:
    recording = RecordingProvider(FakeModelProvider())
    runtime = _runtime(recording, 1_000_000, tmp_path)
    runtime.run_turn("a", "hi")
    runtime.run_turn("b", "hi")
    assert set(runtime._budget_states) == {"a", "b"}


# ---------------------------------------------------------------------------
# reasoning_content
# ---------------------------------------------------------------------------


def test_reasoning_content_persisted_with_assistant_message(tmp_path) -> None:
    recording = RecordingProvider(
        FakeModelProvider([FakeReply(text="the answer", reasoning="chain of thought")])
    )
    runtime = _runtime(recording, None, tmp_path)
    runtime.run_turn("s", "think for me")
    persisted = runtime.repository.list_messages("s")
    assistant = persisted[-1]
    assert assistant.role is Role.ASSISTANT
    assert assistant.reasoning_content == "chain of thought"


def test_stream_assembler_captures_reasoning_delta() -> None:
    chunks = [
        SimpleNamespace(choices=[SimpleNamespace(
            delta=SimpleNamespace(content=None, reasoning_content="think", tool_calls=None),
            finish_reason=None,
        )]),
        SimpleNamespace(choices=[SimpleNamespace(
            delta=SimpleNamespace(content="Hello", reasoning_content=None, tool_calls=None),
            finish_reason="stop",
        )]),
    ]
    response = collect_response(assemble_stream(chunks))
    assert response.content == "Hello"
    assert response.reasoning_content == "think"
    assert response.finish_reason == "stop"


def test_wire_message_omits_reasoning_and_sends_sampling_params() -> None:
    completions = SimpleNamespace(kwargs=None)

    def create(**kwargs):
        completions.kwargs = kwargs
        return SimpleNamespace(choices=[])

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    provider = OpenAICompatibleProvider(
        model="m", client=client, stream=False, temperature=0.0, max_tokens=1500
    )
    collect_response(provider.stream([
        Message(role=Role.USER, content="hi", reasoning_content="not echoed")
    ]))
    kwargs = completions.kwargs
    assert kwargs["temperature"] == 0.0
    assert kwargs["max_tokens"] == 1500
    assert "reasoning_content" not in kwargs["messages"][0]


def test_one_shot_response_captures_reasoning() -> None:
    message = SimpleNamespace(content="answer", reasoning_content="deep thought", tool_calls=None)
    choice = SimpleNamespace(message=message, finish_reason="stop")

    def create(**kwargs):
        return SimpleNamespace(choices=[choice])

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    provider = OpenAICompatibleProvider(model="m", client=client, stream=False)
    response = collect_response(provider.stream([Message(role=Role.USER, content="hi")]))
    assert response.content == "answer"
    assert response.reasoning_content == "deep thought"


# ---------------------------------------------------------------------------
# CLI summary-provider seam
# ---------------------------------------------------------------------------


def test_build_summary_provider_returns_none_for_fake() -> None:
    from aegis_agent.cli import _build_summary_provider

    assert _build_summary_provider(FakeModelProvider()) is None
