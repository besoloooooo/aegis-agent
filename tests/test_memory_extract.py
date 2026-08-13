"""Background memory extraction tests (Stage 3).

Covers the milestone acceptance list:

1.  nothing worth storing → noop
2.  new durable preference → create
3.  existing memory → update (not duplicate create)
4.  temporary task does not enter memory
5.  project-specific info not written to personal memory
6.  after create, MEMORY.md is rebuilt
7.  MEMORY.md has no duplicate index entries
8.  extractor failure does not affect the main answer
9.  main-agent wrote memory this turn → extractor skips
10. extractor cannot write outside the memory root
11. cursor only processes new messages
12. cursor loss safely falls back
"""

from __future__ import annotations

import json

from aegis_agent.memory.extractor import (
    MemoryAction,
    apply_actions,
    extract_memories,
    messages_since_cursor,
)
from aegis_agent.memory.manager import MemoryManager
from aegis_agent.memory.paths import memory_dir, memory_index_path
from aegis_agent.memory.prompt import RelevantMemoriesContributor
from aegis_agent.memory.store import (
    is_valid_memory_filename,
    load_memory_index,
    render_memory_file,
    write_memory_file,
)
from aegis_agent.models.base import Message, Role
from aegis_agent.models.fake import FakeModelProvider, FakeReply
from aegis_agent.runtime import AgentRuntime


def _msg(role: Role, content: str, cid: str) -> Message:
    return Message(role=role, content=content, client_msg_id=cid)


def _json_actions_provider(actions: list[dict]) -> FakeModelProvider:
    return FakeModelProvider(script=[FakeReply(text=json.dumps({"actions": actions}))])


def _action(**kw) -> dict:
    base = {
        "action": "create",
        "filename": "prefer-uv.md",
        "type": "feedback",
        "name": "uv preference",
        "description": "prefers uv",
        "content": "Use uv.\n\n**Why:** consistency.\n\n**How to apply:** use uv for Python env.",
    }
    base.update(kw)
    return base


class TestCursor:
    def test_cursor_processes_only_new_messages(self):
        msgs = [_msg(Role.USER, "one", "c1"), _msg(Role.ASSISTANT, "two", "c2"), _msg(Role.USER, "three", "c3")]
        got = messages_since_cursor(msgs, "c1")
        assert [m.client_msg_id for m in got] == ["c2", "c3"]

    def test_cursor_none_falls_back_to_window(self):
        msgs = [_msg(Role.USER, f"m{i}", f"c{i}") for i in range(30)]
        got = messages_since_cursor(msgs, None)
        assert got == msgs[-12:]

    def test_cursor_missing_falls_back(self):
        msgs = [_msg(Role.USER, "x", "c1"), _msg(Role.USER, "y", "c2")]
        got = messages_since_cursor(msgs, "not-there")
        assert len(got) == 2  # safe fallback, not empty


class TestExtract:
    def test_noop_when_nothing_worth_storing(self, tmp_path):
        provider = _json_actions_provider([])
        msgs = [_msg(Role.USER, "run the tests", "c1"), _msg(Role.ASSISTANT, "done", "c2")]
        result = extract_memories(provider, msgs, None, str(tmp_path))
        assert result.actions == []

    def test_new_preference_creates(self, tmp_path):
        provider = _json_actions_provider([_action()])
        msgs = [_msg(Role.USER, "please always use uv", "c1")]
        result = extract_memories(provider, msgs, None, str(tmp_path))
        assert len(result.actions) == 1
        assert result.actions[0].action == "create"
        assert result.actions[0].filename == "prefer-uv.md"

    def test_update_preferred_over_duplicate(self, tmp_path):
        # Existing memory already present → extractor should return an "update".
        write_memory_file(
            tmp_path, "prefer-uv.md",
            render_memory_file(name="uv", description="prefers uv", memory_type="feedback", body="Use uv."),
        )
        action = _action(action="update", content="Use uv AND ruff.")
        provider = _json_actions_provider([action])
        result = extract_memories(provider, [_msg(Role.USER, "use uv and ruff", "c1")], None, str(tmp_path))
        assert result.actions[0].action == "update"

    def test_project_type_rejected(self, tmp_path):
        action = _action(type="project")
        provider = _json_actions_provider([action])
        result = extract_memories(provider, [_msg(Role.USER, "x", "c1")], None, str(tmp_path))
        # project type is personal-scope excluded → action dropped.
        assert result.actions == []

    def test_unsafe_filename_rejected(self, tmp_path):
        action = _action(filename="../evil.md")
        provider = _json_actions_provider([action])
        result = extract_memories(provider, [_msg(Role.USER, "x", "c1")], None, str(tmp_path))
        assert result.actions == []


class TestApply:
    def test_create_writes_file_and_rebuilds_index(self, tmp_path):
        from aegis_agent.memory.types import MemoryType

        act = MemoryAction(action="create", filename="prefer-uv.md", memory_type=MemoryType.FEEDBACK,
                           name="uv", description="prefers uv", content="Use uv.")
        applied = apply_actions([act], str(tmp_path))
        assert applied == ["prefer-uv.md"]
        assert (memory_dir(tmp_path) / "prefer-uv.md").exists()
        idx = load_memory_index(memory_index_path(tmp_path))
        assert idx is not None
        assert "prefer-uv.md" in idx
        assert "prefers uv" in idx

    def test_index_idempotent_no_duplicates(self, tmp_path):
        from aegis_agent.memory.types import MemoryType
        act = MemoryAction(action="create", filename="a.md", memory_type=MemoryType.USER,
                           name="A", description="aa", content="x")
        apply_actions([act], str(tmp_path))
        # Apply again → still one index line for a.md.
        apply_actions([act], str(tmp_path))
        idx = load_memory_index(memory_index_path(tmp_path))
        assert idx is not None
        assert idx.count("a.md") == 1

    def test_cannot_write_outside_memory_root(self, tmp_path):
        from aegis_agent.memory.types import MemoryType
        # filename with traversal is refused at apply time too.
        act = MemoryAction(action="create", filename="../outside.md", memory_type=MemoryType.USER,
                           name="x", description="x", content="x")
        applied = apply_actions([act], str(tmp_path))
        assert applied == []
        assert not (tmp_path / "outside.md").exists()


class TestManagerFailureIsolation:
    def test_extractor_failure_does_not_affect_answer(self, tmp_path, repository):
        # Extractor side query fails (non-JSON); the main turn still answers.
        from aegis_agent.models.fake import FakeReply as FR

        main = FakeModelProvider(script=[FR(text="the answer")])
        runtime = AgentRuntime.with_defaults(
            provider=main,
            repository=repository,
            memory_home=str(tmp_path),
            enable_mcp=False,
            enable_memory_extract=True,
            memory_side_provider=FakeModelProvider(script=[FR(text="garbage not json")]),
        )
        result = runtime.run_turn("s1", "hello")
        assert result.final_text == "the answer"

    def test_main_agent_wrote_memory_skips_extract(self, tmp_path):
        # A create action that targets a file the main agent already wrote → skip.
        events = []
        manager = MemoryManager(
            RelevantMemoriesContributor(),
            recall_provider=None,
            extract_provider=_json_actions_provider([_action()]),
            home=str(tmp_path),
            on_event=events.append,
        )
        from aegis_agent.models.base import ToolCall

        mem_file = memory_dir(tmp_path) / "prefer-uv.md"
        tc = ToolCall(id="c1", name="write_file", arguments=json.dumps({"path": str(mem_file)}))
        manager.after_turn("s1", [], tool_calls=[tc])
        manager.drain()  # wait for the background extraction worker
        assert events[-1].skipped is True


class TestFilenameValidation:
    def test_valid_and_invalid_names(self):
        assert is_valid_memory_filename("prefer-uv.md")
        assert is_valid_memory_filename("answer-style.md")
        assert not is_valid_memory_filename("../evil.md")
        assert not is_valid_memory_filename("a/b.md")
        assert not is_valid_memory_filename("/abs.md")
        assert not is_valid_memory_filename("MEMORY.md")
        assert not is_valid_memory_filename(".hidden.md")
        assert not is_valid_memory_filename("noext")
