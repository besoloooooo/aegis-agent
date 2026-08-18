"""Project-scoped long-term memory tests.

Covers the milestone acceptance list:

1.  default (no project) is still the personal scope
2.  both scopes read the same global ``USER.md``
3.  project scope does NOT read personal ``MEMORY.md`` / personal ``memory/*.md``
4.  project recall only searches the current project's memory dir
5.  project extraction writes only the current project's memory dir
6.  two different projects are fully isolated
7.  the same project (and its subdirs / git root) yields a stable project-id
8.  personal memory / session / search behaviour is unchanged
"""

from __future__ import annotations

import json

from aegis_agent.memory.extractor import apply_actions, extract_memories
from aegis_agent.memory.paths import (
    MemoryScopeKind,
    memory_dir,
    project_home,
    project_id,
    projects_dir,
    resolve_scope,
)
from aegis_agent.memory.retriever import recall_memories
from aegis_agent.memory.scan import scan_memory_files
from aegis_agent.memory.store import (
    rebuild_index,
    render_memory_file,
    write_memory_file,
)
from aegis_agent.models.base import Message, Role
from aegis_agent.models.fake import FakeModelProvider, FakeReply
from aegis_agent.runtime import AgentRuntime

# ── helpers ─────────────────────────────────────────────────────────────────


def _write_memory(home, filename, name, description, mtype, body="body"):
    """Write one memory file into ``home``'s memory dir (used by fixtures)."""
    content = render_memory_file(name=name, description=description, memory_type=mtype, body=body)
    return write_memory_file(home, filename, content)


def _json_provider(files: list[str]) -> FakeModelProvider:
    return FakeModelProvider(script=[FakeReply(text=json.dumps({"files": files}))])


def _json_actions_provider(actions: list[dict]) -> FakeModelProvider:
    return FakeModelProvider(script=[FakeReply(text=json.dumps({"actions": actions}))])


def _msg(role: Role, content: str, cid: str) -> Message:
    return Message(role=role, content=content, client_msg_id=cid)


def _prompt_of(runtime: AgentRuntime) -> str:
    return runtime._context.system_prompt


# ── scope resolution ─────────────────────────────────────────────────────────


class TestScopeResolution:
    def test_default_is_personal(self, tmp_path):
        scope = resolve_scope(None, home=str(tmp_path / "home"))
        assert scope.kind is MemoryScopeKind.PERSONAL
        assert scope.project_id is None
        assert scope.memory_home == tmp_path / "home"

    def test_project_scope_sets_memory_home_and_global_profile(self, tmp_path):
        home = tmp_path / "home"
        scope = resolve_scope("/repo/proj", home=str(home))
        assert scope.kind is MemoryScopeKind.PROJECT
        assert scope.project_id is not None
        # Memory goes under <home>/projects/<id>; USER.md stays global.
        assert scope.memory_home == projects_dir(home) / scope.project_id
        assert scope.profile_path == home / "USER.md"

    def test_project_home_layout(self, tmp_path):
        home = tmp_path / "home"
        ph = project_home("/repo/proj", home=str(home))
        assert ph == projects_dir(home) / project_id("/repo/proj")
        assert memory_dir(ph) == ph / "memory"


# ── project-id stability ─────────────────────────────────────────────────────


class TestProjectId:
    def test_same_path_is_stable(self):
        assert project_id("/repo/proj") == project_id("/repo/proj")

    def test_different_paths_differ(self):
        assert project_id("/repo/a") != project_id("/repo/b")

    def test_git_root_canonicalises_subdirs(self, tmp_path):
        root = tmp_path / "repo"
        (root / ".git").mkdir(parents=True)
        sub = root / "src" / "deep"
        sub.mkdir(parents=True)
        # Any subdirectory of the same git root maps to the same id.
        assert project_id(root) == project_id(sub)

    def test_no_git_falls_back_to_resolved_path(self, tmp_path):
        root = tmp_path / "plain"
        root.mkdir()
        assert project_id(root) == project_id(root.resolve())


# ── shared global USER.md ────────────────────────────────────────────────────


class TestSharedProfile:
    def test_both_scopes_inject_global_user_profile(self, tmp_path, repository):
        home = tmp_path / "home"
        home.mkdir()
        (home / "USER.md").write_text("I am Alice, prefer uv.", encoding="utf-8")
        proj = tmp_path / "repo"
        proj.mkdir()

        personal = AgentRuntime.with_defaults(
            repository=repository, memory_home=str(home), enable_mcp=False
        )
        project = AgentRuntime.with_defaults(
            repository=repository,
            memory_home=str(home),
            memory_project=str(proj),
            enable_mcp=False,
        )
        assert "Alice" in _prompt_of(personal)
        assert "Alice" in _prompt_of(project)
        assert personal.startup_info.get("memory_scope") == "personal"
        assert project.startup_info.get("memory_scope") == "project"
        assert project.startup_info.get("project_id")


# ── strict scope isolation ───────────────────────────────────────────────────


class TestScopeIsolation:
    def _lay_out(self, tmp_path):
        """Return (home, proj_a, proj_b) with personal + project memory seeded."""
        home = tmp_path / "home"
        home.mkdir()
        (home / "USER.md").write_text("I am Alice.", encoding="utf-8")
        # Personal memory + index.
        _write_memory(home, "prefer-search.md", "search", "prefer grep", "feedback")
        rebuild_index(home)
        # Project A memory + index.
        proj_a = tmp_path / "repo-a"
        proj_a.mkdir()
        _write_memory(project_home(proj_a, home), "architecture.md", "arch", "modular", "project")
        rebuild_index(project_home(proj_a, home))
        # Project B memory + index.
        proj_b = tmp_path / "repo-b"
        proj_b.mkdir()
        _write_memory(
            project_home(proj_b, home), "dataset-rules.md", "data", "no PII", "project"
        )
        rebuild_index(project_home(proj_b, home))
        return home, proj_a, proj_b

    def test_project_prompt_excludes_personal_index(self, tmp_path, repository):
        home, proj_a, _ = self._lay_out(tmp_path)
        runtime = AgentRuntime.with_defaults(
            repository=repository,
            memory_home=str(home),
            memory_project=str(proj_a),
            enable_mcp=False,
        )
        prompt = _prompt_of(runtime)
        # USER.md present; project behaviour section present; personal memory absent.
        assert "Alice" in prompt
        assert "project scope" in prompt.lower()
        assert "architecture.md" in prompt
        assert "prefer-search.md" not in prompt

    def test_project_recall_only_sees_project(self, tmp_path):
        home, proj_a, _ = self._lay_out(tmp_path)
        ph = project_home(proj_a, home)
        cands = scan_memory_files(ph)
        assert {c.filename for c in cands} == {"architecture.md"}

        result = recall_memories(_json_provider(["architecture.md", "prefer-search.md"]), "arch", str(ph))
        assert [m.filename for m in result.memories] == ["architecture.md"]

    def test_project_extract_writes_only_project(self, tmp_path):
        home, proj_a, _ = self._lay_out(tmp_path)
        ph = project_home(proj_a, home)
        provider = _json_actions_provider(
            [
                {
                    "action": "create",
                    "filename": "constraint.md",
                    "type": "project",
                    "name": "constraint",
                    "description": "use x",
                    "content": "Use x.\n\n**Why:** consistency.\n\n**How to apply:** always.",
                }
            ]
        )
        result = extract_memories(provider, [_msg(Role.USER, "use x", "c1")], None, str(ph), project=True)
        assert len(result.actions) == 1
        applied = apply_actions(result.actions, str(ph))
        assert applied == ["constraint.md"]
        assert (memory_dir(ph) / "constraint.md").exists()
        # Personal memory dir untouched.
        assert not (memory_dir(home) / "constraint.md").exists()

    def test_project_rejects_user_type(self, tmp_path):
        home, proj_a, _ = self._lay_out(tmp_path)
        ph = project_home(proj_a, home)
        provider = _json_actions_provider(
            [
                {
                    "action": "create",
                    "filename": "who-is-user.md",
                    "type": "user",
                    "name": "user",
                    "description": "profile",
                    "content": "Alice is an engineer.",
                }
            ]
        )
        result = extract_memories(provider, [_msg(Role.USER, "about me", "c1")], None, str(ph), project=True)
        assert result.actions == []

    def test_projects_fully_isolated(self, tmp_path):
        home, proj_a, proj_b = self._lay_out(tmp_path)
        a = scan_memory_files(project_home(proj_a, home))
        b = scan_memory_files(project_home(proj_b, home))
        assert {c.filename for c in a} == {"architecture.md"}
        assert {c.filename for c in b} == {"dataset-rules.md"}

    def test_personal_still_excludes_project_memory(self, tmp_path):
        home, _, _ = self._lay_out(tmp_path)
        personal = scan_memory_files(home)
        assert {c.filename for c in personal} == {"prefer-search.md"}


# ── no regression to personal / session behaviour ────────────────────────────


class TestNoRegression:
    def test_personal_runtime_turn_still_persists(self, repository):
        runtime = AgentRuntime.with_defaults(
            provider=FakeModelProvider(script=[FakeReply(text="hi back")]),
            repository=repository,
            enable_mcp=False,
        )
        result = runtime.run_turn("s-proj", "hi")
        assert result.final_text == "hi back"
        roles = [m.role for m in result.messages]
        assert roles == [Role.USER, Role.ASSISTANT]
