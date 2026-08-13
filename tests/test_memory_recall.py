"""Relevance recall tests (Stage 2).

Covers the milestone acceptance list:

1.  empty memory dir → recall is a no-op
2.  frontmatter is scanned correctly (name/description/type)
3.  a bad memory file does not block the scan
4.  scan has an upper bound
5.  side query returns 0..5 memories
6.  invalid filenames are rejected
7.  `../` path escape is rejected
8.  only selected bodies are read
9.  relevant memories are injected into context
10. original history is not modified
11. already-surfaced memories are not re-injected
12. side-query failure leaves the main agent working
"""

from __future__ import annotations

import json

from aegis_agent.memory.manager import MemoryManager
from aegis_agent.memory.prompt import RelevantMemoriesContributor
from aegis_agent.memory.retriever import (
    MAX_RECALL_FILES,
    recall_memories,
    render_recall_block,
)
from aegis_agent.memory.scan import MAX_SCAN_FILES, scan_memory_files
from aegis_agent.memory.store import write_memory_file
from aegis_agent.models.base import Role
from aegis_agent.models.fake import FakeModelProvider, FakeReply
from aegis_agent.runtime import AgentRuntime


def _write_memory(home, filename, name, description, mtype, body="body"):
    """Write one memory file (used by recall fixtures)."""
    from aegis_agent.memory.store import render_memory_file

    content = render_memory_file(name=name, description=description, memory_type=mtype, body=body)
    return write_memory_file(home, filename, content)


def _json_provider(files: list[str]) -> FakeModelProvider:
    """A fake provider that returns a JSON side-query response selecting files."""
    return FakeModelProvider(script=[FakeReply(text=json.dumps({"files": files}))])


class TestScan:
    def test_empty_dir_no_candidates(self, tmp_path):
        (tmp_path / "memory").mkdir()
        assert scan_memory_files(tmp_path) == []

    def test_missing_dir_no_candidates(self, tmp_path):
        assert scan_memory_files(tmp_path) == []

    def test_scans_frontmatter_metadata(self, tmp_path):
        _write_memory(tmp_path, "prefer-uv.md", "uv", "uses uv", "feedback")
        _write_memory(tmp_path, "answer-style.md", "style", "concise Chinese", "user")
        cands = scan_memory_files(tmp_path)
        by_name = {c.filename: c for c in cands}
        assert set(by_name) == {"prefer-uv.md", "answer-style.md"}
        assert by_name["prefer-uv.md"].description == "uses uv"
        assert by_name["prefer-uv.md"].memory_type.value == "feedback"
        assert by_name["answer-style.md"].memory_type.value == "user"

    def test_excludes_index(self, tmp_path):
        (tmp_path / "memory").mkdir()
        (tmp_path / "memory" / "MEMORY.md").write_text("# Memory\n- [x](x.md) — x", encoding="utf-8")
        _write_memory(tmp_path, "real.md", "real", "a real memory", "user")
        cands = scan_memory_files(tmp_path)
        assert {c.filename for c in cands} == {"real.md"}

    def test_bad_file_does_not_block(self, tmp_path):
        _write_memory(tmp_path, "good.md", "good", "good", "user")
        (tmp_path / "memory" / "broken.md").write_text("---\nname: [unclosed\n", encoding="utf-8")
        cands = scan_memory_files(tmp_path)
        # The broken file degrades to a naive parse, not an exception.
        assert any(c.filename == "good.md" for c in cands)

    def test_scan_capped(self, tmp_path):
        # Write more than MAX_SCAN_FILES; scan returns at most the cap.
        for i in range(MAX_SCAN_FILES + 20):
            _write_memory(tmp_path, f"m{i:03}.md", f"m{i}", "x", "user")
        cands = scan_memory_files(tmp_path)
        assert len(cands) == MAX_SCAN_FILES


class TestRecall:
    def test_empty_dir_recall_noop(self, tmp_path):
        result = recall_memories(_json_provider(["x.md"]), "hi", str(tmp_path))
        assert result.memories == []
        assert result.failure_reason is not None

    def test_side_query_selects_up_to_five(self, tmp_path):
        for i in range(8):
            _write_memory(tmp_path, f"m{i}.md", f"m{i}", f"desc {i}", "user", f"body {i}")
        files = [f"m{i}.md" for i in range(8)]
        result = recall_memories(_json_provider(files), "hi", str(tmp_path))
        assert result.selected_count <= MAX_RECALL_FILES
        assert len(result.memories) <= MAX_RECALL_FILES

    def test_side_query_zero_is_noop(self, tmp_path):
        _write_memory(tmp_path, "a.md", "a", "a", "user")
        result = recall_memories(_json_provider([]), "hi", str(tmp_path))
        assert result.memories == []
        assert result.selected_count == 0

    def test_invalid_filenames_rejected(self, tmp_path):
        _write_memory(tmp_path, "a.md", "a", "a", "user")
        result = recall_memories(_json_provider(["../escape.md", "bogus.md", "a.md"]), "hi", str(tmp_path))
        assert [m.filename for m in result.memories] == ["a.md"]

    def test_path_escape_rejected(self, tmp_path):
        _write_memory(tmp_path, "a.md", "a", "a", "user")
        result = recall_memories(_json_provider(["../../etc/passwd", "/abs.md"]), "hi", str(tmp_path))
        assert result.memories == []

    def test_side_query_failure_is_noop(self, tmp_path):
        _write_memory(tmp_path, "a.md", "a", "a", "user")
        # A provider that errors (exhausted script → rule-based non-JSON) → None.
        provider = FakeModelProvider(script=[FakeReply(text="not json at all")])
        result = recall_memories(provider, "hi", str(tmp_path))
        assert result.memories == []
        assert result.failure_reason is not None

    def test_render_block_marks_memories(self, tmp_path):
        _write_memory(tmp_path, "a.md", "A", "desc A", "feedback", "body A")
        result = recall_memories(_json_provider(["a.md"]), "hi", str(tmp_path))
        block = render_recall_block(result.memories)
        assert block is not None
        assert "Relevant memories" in block
        assert 'file="a.md"' in block
        assert "feedback" in block
        assert "body A" in block


class TestRecallIntegration:
    def test_recalled_memory_injected_into_context(self, tmp_path, repository):
        _write_memory(tmp_path, "a.md", "A", "prefers uv", "feedback", "use uv always")
        runtime = AgentRuntime.with_defaults(
            repository=repository,
            memory_home=str(tmp_path),
            enable_mcp=False,
            enable_memory_recall=True,
            memory_side_provider=_json_provider(["a.md"]),
        )
        manager: MemoryManager = runtime._memory_manager
        manager.before_turn("s1", "python deps?")
        # Recall is now async: wait for the background future, then hit the
        # collect point (as run_turn does before each model request).
        manager.drain()
        manager.collect_recall("s1")
        prompt = runtime._context.system_prompt
        assert "Relevant memories" in prompt
        assert "use uv always" in prompt

    def test_original_history_not_modified(self, tmp_path, repository):
        _write_memory(tmp_path, "a.md", "A", "prefers uv", "feedback", "use uv always")
        runtime = AgentRuntime.with_defaults(
            repository=repository,
            memory_home=str(tmp_path),
            enable_mcp=False,
            enable_memory_recall=True,
            memory_side_provider=_json_provider(["a.md"]),
        )
        runtime.run_turn("s1", "hi")
        msgs = repository.list_messages("s1")
        # Only user + assistant (no injected memory message in history).
        assert [m.role for m in msgs] == [Role.USER, Role.ASSISTANT]
        assert all("use uv always" not in m.content for m in msgs)

    def test_already_surfaced_not_reinjected(self, tmp_path):
        _write_memory(tmp_path, "a.md", "A", "a", "user", "body A")
        _write_memory(tmp_path, "b.md", "B", "b", "user", "body B")
        provider = _json_provider(["a.md", "b.md"])
        home = str(tmp_path)
        # First pass surfaces both.
        r1 = recall_memories(provider, "hi", home)
        surfaced = {m.filename for m in r1.memories}
        assert surfaced == {"a.md", "b.md"}
        # Second pass with already_surfaced set → none re-injected.
        r2 = recall_memories(provider, "hi", home, already_surfaced=surfaced)
        assert r2.memories == []


class TestManagerMutexAndEvents:
    def test_main_agent_write_skips_extraction(self, tmp_path, repository):
        # A turn where the main agent writes a memory file via write_file →
        # the manager's after_turn must skip extraction.
        from aegis_agent.memory.paths import memory_dir

        events = []
        contributor = RelevantMemoriesContributor()
        manager = MemoryManager(
            contributor,
            recall_provider=None,
            extract_provider=_json_provider([]),
            home=str(tmp_path),
            on_event=events.append,
        )
        mem_file = memory_dir(str(tmp_path)) / "x.md"
        from aegis_agent.models.base import ToolCall

        tool_calls = [ToolCall(id="c1", name="write_file", arguments=json.dumps({"path": str(mem_file)}))]
        manager.after_turn("s1", [], tool_calls=tool_calls)
        manager.drain()  # wait for the background extraction worker
        assert events[-1].skipped is True
