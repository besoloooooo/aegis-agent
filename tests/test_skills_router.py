"""Skills router tests: slug resolution, invocation message."""

from __future__ import annotations

from aegis_agent.skills.loader import SkillLoader
from aegis_agent.skills.router import DefaultSkillRouter, normalize_skill_key
from tests.test_skills_loader import _write_skill


class TestNormalizeSkillKey:
    def test_strips_leading_slash(self):
        assert normalize_skill_key("/my-skill") == "my-skill"

    def test_no_leading_slash(self):
        assert normalize_skill_key("my-skill") == "my-skill"

    def test_lowercases(self):
        assert normalize_skill_key("/My-SKILL") == "my-skill"

    def test_underscores_to_hyphens(self):
        assert normalize_skill_key("/my_skill_name") == "my-skill-name"

    def test_spaces_to_hyphens(self):
        assert normalize_skill_key("/my skill name") == "my-skill-name"

    def test_whitespace_stripped(self):
        assert normalize_skill_key("  /skill  ") == "skill"


class TestRouter:
    def test_resolve_by_exact_name(self, tmp_path):
        _write_skill(tmp_path, "hello", "Greeting skill")
        loader = SkillLoader([tmp_path])
        router = DefaultSkillRouter(loader)
        skill = router.resolve("hello")
        assert skill is not None
        assert skill.name == "hello"
        assert skill.description == "Greeting skill"

    def test_resolve_by_slug(self, tmp_path):
        _write_skill(tmp_path, "hello-skill", "A skill with hyphens")
        loader = SkillLoader([tmp_path])
        router = DefaultSkillRouter(loader)
        skill = router.resolve("/hello_skill")
        assert skill is not None
        assert skill.name == "hello-skill"

    def test_resolve_returns_none_for_unknown(self, tmp_path):
        loader = SkillLoader([tmp_path])
        router = DefaultSkillRouter(loader)
        assert router.resolve("missing") is None

    def test_invocation_message_contains_activation_note(self, tmp_path):
        _write_skill(tmp_path, "hello", "Greeting skill", body="## Instructions\nSay hello.")

        loader = SkillLoader([tmp_path])
        router = DefaultSkillRouter(loader)
        skill = router.resolve("hello")
        assert skill is not None

        msg = router.invocation_message(skill)
        assert "was invoked" in msg
        assert "Say hello" in msg
        assert "[Skill directory:" in msg

    def test_invocation_message_appends_instruction(self, tmp_path):
        _write_skill(tmp_path, "hello", "Greeting skill", body="Say hello.")

        loader = SkillLoader([tmp_path])
        router = DefaultSkillRouter(loader)
        skill = router.resolve("hello")
        assert skill is not None

        msg = router.invocation_message(skill, "to the user")
        assert msg.endswith("to the user")

    def test_invocation_message_lists_supporting_files(self, tmp_path):
        _write_skill(tmp_path, "hello", "Greeting skill")
        ref_dir = tmp_path / "hello" / "references"
        ref_dir.mkdir()
        (ref_dir / "guide.md").write_text("guide", encoding="utf-8")

        loader = SkillLoader([tmp_path])
        router = DefaultSkillRouter(loader)
        skill = router.resolve("hello")
        assert skill is not None

        msg = router.invocation_message(skill)
        assert "guide.md" in msg
        assert "[Supporting files:" in msg

    def test_invocation_message_no_supporting_files(self, tmp_path):
        _write_skill(tmp_path, "hello", "Greeting skill")

        loader = SkillLoader([tmp_path])
        router = DefaultSkillRouter(loader)
        skill = router.resolve("hello")
        assert skill is not None

        msg = router.invocation_message(skill)
        assert "[Supporting files:" not in msg
