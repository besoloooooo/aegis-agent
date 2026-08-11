"""Tests for the context-compression pipeline ported from Hermes ctx-compress-opt.

Covers the three phases (oversized tool-result offload, local micro-compaction,
per-round LLM summary) plus the single-round overflow fallback and the Aegis
boundary adapters (Message <-> dict).  All model calls go through the
deterministic FakeModelProvider (or an exploding stub) — no real API needed.
"""

from __future__ import annotations

import copy
import json

from aegis_agent.context import micro_compact
from aegis_agent.context.compress import (
    _estimate_tokens,
    _is_complete_round,
    _split_into_rounds,
    compress_context,
    dict_to_message,
    estimate_tokens,
    message_to_dict,
)
from aegis_agent.context.compress_config import (
    CONTEXT_SUMMARY_TAG,
    DUPLICATE_TOOL_RESULT_MARKER,
    PERSISTED_OUTPUT_TAG,
)
from aegis_agent.models.base import Message, Role, ToolCall
from aegis_agent.models.fake import FakeModelProvider, FakeReply

SUMMARY_TEXT = "Round summary: user asked a question and the assistant answered at length."


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _text_round(idx: int, answer_chars: int = 800) -> list[Message]:
    """A plain user/assistant round with a long answer (no tool calls)."""
    return [
        Message(role=Role.USER, content=f"q{idx}"),
        Message(role=Role.ASSISTANT, content="a" * answer_chars),
    ]


def _tool_round(idx: int, result_chars: int = 5000) -> list[Message]:
    """A round with one terminal tool call and a large tool result."""
    return [
        Message(role=Role.USER, content=f"run something {idx}"),
        Message(
            role=Role.ASSISTANT,
            content="",
            tool_calls=[ToolCall(id=f"c{idx}", name="terminal", arguments='{"command": "big"}')],
        ),
        Message(role=Role.TOOL, tool_call_id=f"c{idx}", name="terminal", content="y" * result_chars),
        Message(role=Role.ASSISTANT, content=f"done {idx}"),
    ]


def _snapshot(messages: list[Message]) -> list[Message]:
    return copy.deepcopy(messages)


def _budget_for(messages: list[Message], ratio: float) -> int:
    """Token budget relative to the measured total — robust to whichever token
    estimator is active (tiktoken when installed, chars/2.5 fallback otherwise)."""
    total = _estimate_tokens([message_to_dict(m) for m in messages])
    return max(1, int(total * ratio))


# ---------------------------------------------------------------------------
# token estimation
# ---------------------------------------------------------------------------


def test_estimate_tokens_counts_all_fields() -> None:
    small = [{"role": "user", "content": "hello world"}]
    big = [{
        "role": "assistant",
        "content": "x" * 1000,
        "reasoning_content": "r" * 500,
        "tool_calls": [{"id": "c", "function": {"name": "t", "arguments": "{}"}}],
    }]
    assert _estimate_tokens(small) > 0
    assert _estimate_tokens(big) > _estimate_tokens(small)


def test_estimate_tokens_public_accepts_messages() -> None:
    msgs = [Message(role=Role.USER, content="hello world")]
    assert estimate_tokens(msgs) == _estimate_tokens([message_to_dict(msgs[0])])


# ---------------------------------------------------------------------------
# Message <-> dict boundary adapters
# ---------------------------------------------------------------------------


def test_message_dict_roundtrip() -> None:
    msg = Message(
        role=Role.ASSISTANT,
        content="working",
        tool_calls=[ToolCall(id="c1", name="read_file", arguments='{"path": "a.py"}')],
        reasoning_content="thinking hard",
    )
    d = message_to_dict(msg)
    assert d["role"] == "assistant"
    assert d["tool_calls"][0]["function"]["name"] == "read_file"
    assert d["reasoning_content"] == "thinking hard"
    back = dict_to_message(d)
    assert back.role is Role.ASSISTANT
    assert back.content == "working"
    assert back.tool_calls == [ToolCall(id="c1", name="read_file", arguments='{"path": "a.py"}')]
    assert back.reasoning_content == "thinking hard"


def test_dict_to_message_tool_fields() -> None:
    d = {"role": "tool", "tool_call_id": "c9", "name": "terminal", "content": "out"}
    msg = dict_to_message(d)
    assert msg.role is Role.TOOL
    assert msg.tool_call_id == "c9"
    assert msg.name == "terminal"


def test_dict_to_message_tolerates_non_string_content() -> None:
    assert dict_to_message({"role": "assistant", "content": None}).content == ""
    blocks = {"role": "user", "content": [{"type": "text", "text": "hi"}, {"type": "image_url"}]}
    assert dict_to_message(blocks).content == "hi\n[image]"


# ---------------------------------------------------------------------------
# round splitting / completeness
# ---------------------------------------------------------------------------


def test_split_into_rounds_groups_by_user() -> None:
    msgs = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "tool", "content": "t"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
    ]
    rounds = _split_into_rounds(msgs)
    assert len(rounds) == 2
    assert rounds[0][0]["content"] == "u1" and len(rounds[0]) == 3
    assert rounds[1][0]["content"] == "u2" and len(rounds[1]) == 2


def test_is_complete_round_rules() -> None:
    assert not _is_complete_round([])
    assert not _is_complete_round([{"role": "assistant", "content": "a"}])  # must start with user
    assert not _is_complete_round([{"role": "user", "content": "u"}])  # must end with assistant
    assert not _is_complete_round([  # trailing tool_calls -> round not finished
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "a", "tool_calls": [{"id": "c"}]},
    ])
    assert not _is_complete_round([  # empty final content
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "  "},
    ])
    assert _is_complete_round([
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "a"},
    ])


# ---------------------------------------------------------------------------
# Phase A: oversized tool-result offload (tool_budget)
# ---------------------------------------------------------------------------


def test_phase_a_persists_oversized_tool_result(tmp_path) -> None:
    big_content = "x" * 25_000  # > DEFAULT_RESULT_SIZE_CHARS (20k)
    messages = [
        Message(role=Role.SYSTEM, content="sys"),
        Message(role=Role.USER, content="run it"),
        Message(
            role=Role.ASSISTANT,
            content="",
            tool_calls=[ToolCall(id="c1", name="terminal", arguments='{"command": "big"}')],
        ),
        Message(role=Role.TOOL, tool_call_id="c1", name="terminal", content=big_content),
        Message(role=Role.ASSISTANT, content="done"),
    ]
    before = _snapshot(messages)

    result = compress_context(
        messages, FakeModelProvider(), max_tokens=1_000_000, storage_dir=str(tmp_path)
    )

    tool_msg = next(m for m in result if m.role is Role.TOOL)
    assert tool_msg.content.startswith(PERSISTED_OUTPUT_TAG)
    assert str(tmp_path) in tool_msg.content  # preview points at the persisted file
    persisted = (tmp_path / "c1.txt").read_text(encoding="utf-8")
    assert persisted == big_content  # full content preserved on disk
    assert messages == before  # input untouched


def test_phase_a_readback_of_persisted_is_hard_truncated_not_re_persisted(tmp_path) -> None:
    """A read_file of our own offload cache must not be persisted again (loop guard)."""
    cache_file = tmp_path / "c0.txt"
    cache_file.write_text("seed", encoding="utf-8")
    big_content = "z" * 25_000
    args = json.dumps({"path": str(cache_file)})
    messages = [
        Message(role=Role.USER, content="read it"),
        Message(
            role=Role.ASSISTANT,
            content="",
            tool_calls=[ToolCall(id="c9", name="read_file", arguments=args)],
        ),
        Message(role=Role.TOOL, tool_call_id="c9", name="read_file", content=big_content),
        Message(role=Role.ASSISTANT, content="done"),
    ]
    result = compress_context(
        messages, FakeModelProvider(), max_tokens=1_000_000, storage_dir=str(tmp_path)
    )
    tool_msg = next(m for m in result if m.role is Role.TOOL)
    # hard-truncated in place (third-level fallback), not re-persisted
    assert not (tmp_path / "c9.txt").exists()
    assert len(tool_msg.content) < len(big_content)
    assert PERSISTED_OUTPUT_TAG in tool_msg.content


# ---------------------------------------------------------------------------
# Phase B: micro-compaction (dedup / informative one-line summaries)
# ---------------------------------------------------------------------------


def test_micro_compact_deduplicates_identical_tool_results() -> None:
    big = "payload " * 100  # 800 chars, > TOOL_RESULT_MIN_CHARS
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "terminal", "arguments": '{"command": "ls"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "terminal", "content": big},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c2", "function": {"name": "terminal", "arguments": '{"command": "ls"}'}}]},
        {"role": "tool", "tool_call_id": "c2", "name": "terminal", "content": big},
        {"role": "user", "content": "u3"},
        {"role": "assistant", "content": "final answer"},
    ]
    before = copy.deepcopy(msgs)
    result = micro_compact.micro_compact(msgs, max_tokens=50)
    # the older duplicate (idx 3) is replaced with a back-reference placeholder;
    # the newest copy (idx 6, inside the protected tail) keeps its content
    assert result[3]["content"] == DUPLICATE_TOOL_RESULT_MARKER
    assert result[6]["content"] == big
    assert msgs == before  # input not mutated


def test_micro_compact_summarizes_old_tool_results_informatively() -> None:
    content = '{"exit_code": 0, "output": "' + "o" * 500 + '"}'
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "terminal", "arguments": '{"command": "npm test"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "terminal", "content": content},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "u3"},
        {"role": "assistant", "content": "a3"},
        {"role": "user", "content": "u4"},
        {"role": "assistant", "content": "a4"},
    ]
    result = micro_compact.micro_compact(msgs, max_tokens=10)
    assert result[3]["content"].startswith("[terminal] ran `npm test` -> exit 0")
    assert msgs[3]["content"] == content  # original untouched


# ---------------------------------------------------------------------------
# Phase C: per-round LLM summary
# ---------------------------------------------------------------------------


def test_phase_c_summarizes_oldest_rounds_until_within_limit(tmp_path) -> None:
    messages = [Message(role=Role.SYSTEM, content="sys")]
    for i in range(5):
        messages.extend(_text_round(i))
    before = _snapshot(messages)

    provider = FakeModelProvider([FakeReply(text=SUMMARY_TEXT) for _ in range(10)])
    max_tokens = _budget_for(messages, 0.5)
    result = compress_context(
        messages, provider, max_tokens=max_tokens, storage_dir=str(tmp_path)
    )

    summaries = [m for m in result if m.role is Role.ASSISTANT and CONTEXT_SUMMARY_TAG in m.content]
    assert summaries, "expected at least one round to be summarized"
    assert provider.calls == len(summaries)  # one model call per summarized round
    # original user questions of summarized rounds are preserved
    summarized_users = [m for m in result if m.role is Role.USER]
    assert any(m.content == "q0" for m in summarized_users)
    # the last (current) round is never summarized
    assert result[-1].content == "a" * 800
    assert result[0].role is Role.SYSTEM
    assert estimate_tokens(result) <= max_tokens
    assert messages == before  # source history untouched


def test_phase_c_summary_failure_keeps_original_round(tmp_path) -> None:
    class ExplodingProvider:
        @property
        def name(self) -> str:
            return "exploding"

        def stream(self, messages, tools=None):
            raise RuntimeError("boom")

    messages = [Message(role=Role.SYSTEM, content="sys")]
    for i in range(4):
        messages.extend(_text_round(i))
    before = _snapshot(messages)

    result = compress_context(
        messages, ExplodingProvider(), max_tokens=_budget_for(messages, 0.5), storage_dir=str(tmp_path)
    )
    # no summary was injected; every original answer survives
    assert all(CONTEXT_SUMMARY_TAG not in m.content for m in result)
    for i in range(4):
        assert any(m.content == "a" * 800 for m in result)
    assert messages == before


def test_phase_c_unusable_summary_keeps_original_round(tmp_path) -> None:
    provider = FakeModelProvider([FakeReply(text="I'm sorry, I cannot do that.") for _ in range(10)])
    messages = [Message(role=Role.SYSTEM, content="sys")]
    for i in range(4):
        messages.extend(_text_round(i))

    result = compress_context(
        messages, provider, max_tokens=_budget_for(messages, 0.5), storage_dir=str(tmp_path)
    )
    assert all(CONTEXT_SUMMARY_TAG not in m.content for m in result)


def test_preexisting_summary_region_is_protected(tmp_path) -> None:
    """A second compression pass must not re-summarize or drop earlier summaries."""
    messages = [Message(role=Role.SYSTEM, content="sys")]
    # already-compressed region: user + [Context Summary] assistant
    messages.append(Message(role=Role.USER, content="old question"))
    messages.append(Message(role=Role.ASSISTANT, content=f"{CONTEXT_SUMMARY_TAG}\nold summary"))
    for i in range(4):
        messages.extend(_text_round(i))
    before = _snapshot(messages)

    provider = FakeModelProvider([FakeReply(text=SUMMARY_TEXT) for _ in range(10)])
    result = compress_context(
        messages, provider, max_tokens=_budget_for(messages, 0.5), storage_dir=str(tmp_path)
    )

    assert provider.calls >= 1  # compression actually ran
    old_summary = [m for m in result if "old summary" in m.content]
    assert len(old_summary) == 1  # survived exactly once, not duplicated or dropped
    assert result[1].content == "old question"  # still at the head, right after system
    assert messages == before


# ---------------------------------------------------------------------------
# single-round overflow fallback
# ---------------------------------------------------------------------------


def test_single_round_overflow_hard_truncates_tool_results(tmp_path) -> None:
    messages = [Message(role=Role.SYSTEM, content="sys")]
    messages.extend(_tool_round(0, result_chars=5000))
    before = _snapshot(messages)

    result = compress_context(
        messages, FakeModelProvider(), max_tokens=_budget_for(messages, 0.5), storage_dir=str(tmp_path)
    )
    tool_msgs = [m for m in result if m.role is Role.TOOL]
    assert len(tool_msgs) == 1  # message kept, content truncated (not deleted)
    assert "[TRUNCATED:" in tool_msgs[0].content
    assert len(tool_msgs[0].content) < 5000
    assert estimate_tokens(result) <= _budget_for(messages, 0.5)
    assert messages == before


def test_historical_reasoning_cleared_current_round_kept(tmp_path) -> None:
    """Phase B clears reasoning_content in historical rounds only (public API level)."""
    messages = [Message(role=Role.SYSTEM, content="sys")]
    for i in range(4):
        messages.append(Message(role=Role.USER, content=f"q{i}"))
        # varied text so both estimators (tiktoken / chars) account it proportionally
        reasoning = f"reasoning about step {i} with some detail. " * 100
        messages.append(Message(role=Role.ASSISTANT, content=f"answer {i}", reasoning_content=reasoning))
    before = _snapshot(messages)

    # budget strictly between "all reasoning kept" and "first round's cleared"
    dicts = [message_to_dict(m) for m in messages]
    full = _estimate_tokens(dicts)
    cleared = copy.deepcopy(dicts)
    cleared[2]["reasoning_content"] = ""  # first assistant message (index 2)
    budget = (full + _estimate_tokens(cleared)) // 2

    result = compress_context(messages, FakeModelProvider(), max_tokens=budget, storage_dir=str(tmp_path))
    assistants = [m for m in result if m.role is Role.ASSISTANT]
    assert len(assistants) == 4
    assert assistants[0].reasoning_content == ""       # historical round: cleared
    assert assistants[1].reasoning_content != ""       # protected tail: kept
    assert assistants[-1].reasoning_content != ""      # current round: kept
    assert estimate_tokens(result) <= budget
    assert messages == before


def test_single_round_overflow_deletes_tool_groups_atomically(tmp_path) -> None:
    """Last-resort deletion removes assistant+tool groups whole (no orphans)."""
    messages = [Message(role=Role.SYSTEM, content="sys")]
    # one round: user + several tool-call pairs with medium results + long final
    messages.append(Message(role=Role.USER, content="work"))
    for i in range(4):
        messages.append(Message(
            role=Role.ASSISTANT,
            content="",
            tool_calls=[ToolCall(id=f"g{i}", name="other_tool", arguments="{}")],
        ))
        # 'other_tool' is not in any compactable allowlist -> survives softer steps
        messages.append(Message(role=Role.TOOL, tool_call_id=f"g{i}", name="other_tool",
                                content="r" * 400))
    messages.append(Message(role=Role.ASSISTANT, content="final " + "f" * 100))
    before = _snapshot(messages)

    result = compress_context(
        messages, FakeModelProvider(), max_tokens=120, storage_dir=str(tmp_path)
    )
    # protocol integrity: every surviving tool message has its assistant call, and
    # every surviving assistant tool_call has its tool result
    ids_with_results = {m.tool_call_id for m in result if m.role is Role.TOOL}
    ids_requested = {tc.id for m in result for tc in m.tool_calls}
    assert ids_with_results == ids_requested
    assert messages == before
