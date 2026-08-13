"""Personal long-term memory tests (Stage 1).

Covers the milestone acceptance list:

1. USER.md present → injected
2. USER.md absent → startup unaffected
3. MEMORY.md present → injected as an index
4. MEMORY.md absent → startup unaffected
5. MEMORY.md over 200 lines → truncated
6. MEMORY.md over 25 KB → truncated
7. USER.md vs MEMORY.md have distinct semantics in the prompt
8. the memory behaviour section joins the existing prompt
9. memory does not affect session/resume/persistence
"""

from __future__ import annotations

from pathlib import Path

from aegis_agent.context.system_prompt import DEFAULT_IDENTITY, SystemPromptBuilder
from aegis_agent.memory.paths import (
    aegis_home,
    memory_dir,
    memory_index_path,
    user_profile_path,
)
from aegis_agent.memory.prompt import (
    MEMORY_BEHAVIOR_GUIDANCE,
    MemoryBehaviorContributor,
    MemoryIndexContributor,
    UserProfileContributor,
    default_memory_index_contributor,
    default_user_profile_contributor,
)
from aegis_agent.memory.store import (
    MAX_ENTRYPOINT_BYTES,
    MAX_ENTRYPOINT_LINES,
    load_memory_index,
    load_user_profile,
    parse_memory_file,
    truncate_entrypoint_content,
)
from aegis_agent.memory.types import MemoryType
from aegis_agent.models.base import Role
from aegis_agent.models.fake import FakeReply
from aegis_agent.runtime import AgentRuntime

# ── fixtures / helpers ──────────────────────────────────────────────────────


def _write_home(tmp_path: Path, *, user: str | None = None, index: str | None = None) -> Path:
    """Lay out a memory home under tmp_path; return the home dir."""
    home = tmp_path / "aegis_home"
    home.mkdir()
    if user is not None:
        (home / "USER.md").write_text(user, encoding="utf-8")
    if index is not None:
        mem = home / "memory"
        mem.mkdir()
        (mem / "MEMORY.md").write_text(index, encoding="utf-8")
    return home


# ── paths ───────────────────────────────────────────────────────────────────


class TestPaths:
    def test_explicit_home_layout(self, tmp_path):
        home = tmp_path / "h"
        assert aegis_home(home) == home
        assert user_profile_path(home) == home / "USER.md"
        assert memory_dir(home) == home / "memory"
        assert memory_index_path(home) == home / "memory" / "MEMORY.md"

    def test_aegis_home_env_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AEGIS_HOME", str(tmp_path / "envhome"))
        assert aegis_home() == tmp_path / "envhome"

    def test_memory_dir_env_override_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AEGIS_MEMORY_DIR", str(tmp_path / "mem"))
        # Even with a different home, the direct override wins.
        assert memory_dir(tmp_path / "other") == tmp_path / "mem"


# ── types ─────────────────────────────────────────────────────────────────


class TestMemoryType:
    def test_four_kinds(self):
        assert {t.value for t in MemoryType} == {"user", "feedback", "project", "reference"}

    def test_parse_case_insensitive(self):
        assert MemoryType.parse("  Feedback ") is MemoryType.FEEDBACK

    def test_parse_unknown_is_none(self):
        assert MemoryType.parse("bogus") is None
        assert MemoryType.parse(None) is None
        assert MemoryType.parse(123) is None


# ── store: parsing + truncation ──────────────────────────────────────────────


class TestStore:
    def test_parse_memory_file(self, tmp_path):
        f = tmp_path / "prefer-uv.md"
        f.write_text(
            "---\nname: prefer_uv\ndescription: uses uv\ntype: feedback\n---\n"
            "Body text here.\n",
            encoding="utf-8",
        )
        entry = parse_memory_file(f)
        assert entry is not None
        assert entry.name == "prefer_uv"
        assert entry.description == "uses uv"
        assert entry.memory_type is MemoryType.FEEDBACK
        assert "Body text here." in entry.body

    def test_parse_missing_name_falls_back_to_stem(self, tmp_path):
        f = tmp_path / "note.md"
        f.write_text("no frontmatter here", encoding="utf-8")
        entry = parse_memory_file(f)
        assert entry is not None
        assert entry.name == "note"
        assert entry.memory_type is None

    def test_truncate_by_lines(self):
        content = "\n".join(f"line {i}" for i in range(MAX_ENTRYPOINT_LINES + 50))
        out = truncate_entrypoint_content(content)
        assert out.count("\n") < MAX_ENTRYPOINT_LINES + 50
        assert "truncated" in out
        assert "200-line" in out

    def test_truncate_by_bytes(self):
        # One long line under the line cap but well over the byte cap.
        content = "x" * (MAX_ENTRYPOINT_BYTES + 5000)
        out = truncate_entrypoint_content(content)
        assert len(out.encode("utf-8")) <= MAX_ENTRYPOINT_BYTES + len(
            "\n\n[... truncated: ...]"
        ) + 200
        assert "truncated" in out
        assert "KB" in out

    def test_no_truncation_when_small(self):
        content = "short and sweet"
        assert truncate_entrypoint_content(content) == content

    def test_load_absent_returns_none(self, tmp_path):
        assert load_user_profile(tmp_path / "nope.md") is None
        assert load_memory_index(tmp_path / "nope.md") is None

    def test_load_empty_returns_none(self, tmp_path):
        f = tmp_path / "USER.md"
        f.write_text("   \n\n  ", encoding="utf-8")
        assert load_user_profile(f) is None


# ── prompt contributors ─────────────────────────────────────────────────────


class TestContributors:
    def test_behavior_renders_when_enabled(self):
        assert MemoryBehaviorContributor().render() == MEMORY_BEHAVIOR_GUIDANCE

    def test_behavior_none_when_disabled(self):
        assert MemoryBehaviorContributor(enabled=False).render() is None

    def test_user_profile_injected_when_present(self, tmp_path):
        home = _write_home(tmp_path, user="I am Alice, a backend engineer.")
        out = default_user_profile_contributor(home).render()
        assert out is not None
        assert "USER.md" in out
        assert "Alice" in out

    def test_user_profile_none_when_absent(self, tmp_path):
        home = _write_home(tmp_path)  # no USER.md
        assert default_user_profile_contributor(home).render() is None

    def test_memory_index_injected_when_present(self, tmp_path):
        home = _write_home(tmp_path, index="# Memory\n\n- [uv](prefer-uv.md) — uses uv")
        out = default_memory_index_contributor(home).render()
        assert out is not None
        assert "MEMORY.md" in out
        assert "index" in out.lower()
        assert "prefer-uv.md" in out

    def test_memory_index_none_when_absent(self, tmp_path):
        home = _write_home(tmp_path)  # no memory/MEMORY.md
        assert default_memory_index_contributor(home).render() is None

    def test_index_over_200_lines_truncated(self, tmp_path):
        big = "# Memory\n" + "\n".join(f"- [m{i}](m{i}.md) — x" for i in range(300))
        home = _write_home(tmp_path, index=big)
        out = default_memory_index_contributor(home).render()
        assert out is not None
        assert "truncated" in out

    def test_index_over_25kb_truncated(self, tmp_path):
        big = "# Memory\n" + ("- [m](m.md) — " + "y" * 200 + "\n") * 200
        assert len(big.encode("utf-8")) > MAX_ENTRYPOINT_BYTES
        home = _write_home(tmp_path, index=big)
        out = default_memory_index_contributor(home).render()
        assert out is not None
        assert "truncated" in out

    def test_profile_and_index_have_distinct_semantics(self, tmp_path):
        home = _write_home(
            tmp_path,
            user="I am Alice.",
            index="# Memory\n\n- [uv](prefer-uv.md) — uses uv",
        )
        builder = SystemPromptBuilder()
        builder.add(UserProfileContributor(user_profile_path(home)))
        builder.add(MemoryIndexContributor(memory_index_path(home)))
        prompt = builder.build()
        # Both present, clearly labelled, and the profile appears before the index.
        assert "User profile (USER.md)" in prompt
        assert "Memory index (MEMORY.md)" in prompt
        assert prompt.index("User profile (USER.md)") < prompt.index("Memory index (MEMORY.md)")
        # The index is explicitly NOT the bodies.
        assert "not the memory bodies" in prompt


# ── runtime wiring ──────────────────────────────────────────────────────────


def _prompt_of(runtime: AgentRuntime) -> str:
    return runtime._context.system_prompt  # inspecting the built prompt


class TestRuntimeWiring:
    def test_behavior_section_joins_existing_prompt(self, tmp_path, repository):
        runtime = AgentRuntime.with_defaults(
            repository=repository, memory_home=str(tmp_path / "empty"), enable_mcp=False
        )
        prompt = _prompt_of(runtime)
        # Identity still leads; the memory section is appended.
        assert prompt.startswith(DEFAULT_IDENTITY)
        assert "# Memory" in prompt

    def test_user_and_index_injected_into_runtime_prompt(self, tmp_path, repository):
        home = _write_home(
            tmp_path,
            user="I am Alice, prefer uv.",
            index="# Memory\n\n- [uv](prefer-uv.md) — uses uv",
        )
        runtime = AgentRuntime.with_defaults(
            repository=repository, memory_home=str(home), enable_mcp=False
        )
        prompt = _prompt_of(runtime)
        assert "Alice" in prompt
        assert "prefer-uv.md" in prompt
        assert runtime.startup_info.get("memory") == 1

    def test_no_memory_disables_all_sections(self, tmp_path, repository):
        home = _write_home(tmp_path, user="I am Alice.", index="# Memory\n\n- [x](x.md) — y")
        runtime = AgentRuntime.with_defaults(
            repository=repository,
            memory_home=str(home),
            enable_memory=False,
            enable_mcp=False,
        )
        prompt = _prompt_of(runtime)
        assert "# Memory" not in prompt
        assert "Alice" not in prompt
        assert runtime.startup_info.get("memory") == 0

    def test_absent_files_do_not_break_startup(self, tmp_path, repository):
        # Construction with a nonexistent home succeeds; only the behaviour
        # section is contributed (no profile/index files present).
        runtime = AgentRuntime.with_defaults(
            repository=repository, memory_home=str(tmp_path / "nothing"), enable_mcp=False
        )
        assert runtime.startup_info.get("memory") == 0
        assert "# Memory" in _prompt_of(runtime)

    def test_memory_does_not_affect_session_persistence(self, make_runtime):
        # A normal scripted turn still persists user+assistant in order, unaffected
        # by memory being wired in (make_runtime enables memory via with_defaults).
        runtime, _ = make_runtime(script=[FakeReply(text="hi back")])
        result = runtime.run_turn("s-mem", "hi")
        assert result.final_text == "hi back"
        roles = [m.role for m in result.messages]
        assert roles == [Role.USER, Role.ASSISTANT]
        # And the built prompt contains the memory behaviour section.
        assert "# Memory" in _prompt_of(runtime)
