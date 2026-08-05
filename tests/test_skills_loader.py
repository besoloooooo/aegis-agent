"""Skills loader tests: discovery, platform gating, validation."""

from __future__ import annotations

import os
import sys

from aegis_agent.skills.loader import MAX_NAME_LENGTH, SkillLoader, _matches_platform


def _write_skill(dir_, name, description, body="# Instructions\n", *, extra_frontmatter=""):
    """Write a minimal SKILL.md into ``dir_``."""
    skill_dir = dir_ / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_md = skill_dir / "SKILL.md"
    lines = [
        "---",
        f"name: {name}",
        f"description: {description}",
    ]
    if extra_frontmatter:
        lines.append(extra_frontmatter)
    lines += [
        "---",
        body,
    ]
    skill_md.write_text("\n".join(lines), encoding="utf-8")
    return skill_md


class TestSkillLoader:
    def test_discovers_skills_from_single_dir(self, tmp_path):
        _write_skill(tmp_path, "hello", "a greeting skill")
        _write_skill(tmp_path, "goodbye", "a farewell skill")

        loader = SkillLoader([tmp_path])
        skills = loader.discover()
        names = {s.name for s in skills}
        assert names == {"hello", "goodbye"}

    def test_derives_category_from_parent_directory(self, tmp_path):
        cat = tmp_path / "utilities"
        cat.mkdir()
        _write_skill(cat, "fmt", "Formats text")

        loader = SkillLoader([tmp_path])
        skills = loader.discover()
        assert skills[0].category == "utilities"

    def test_metas_returns_compact_index(self, tmp_path):
        _write_skill(tmp_path, "hello", "a greeting skill")

        loader = SkillLoader([tmp_path])
        metas = loader.metas()
        assert len(metas) == 1
        assert metas[0].name == "hello"
        assert metas[0].description == "a greeting skill"
        assert metas[0].category == ""

    def test_missing_dir_returns_empty(self, tmp_path):
        loader = SkillLoader([tmp_path / "nonexistent"])
        skills = loader.discover()
        assert skills == []
        assert loader.metas() == []

    def test_no_dir_returns_empty(self):
        loader = SkillLoader([])
        assert loader.discover() == []

    def test_skips_skill_missing_name(self, tmp_path):
        (tmp_path / "bad").mkdir()
        (tmp_path / "bad" / "SKILL.md").write_text(
            "---\ndescription: no name here\n---\nBody.", encoding="utf-8"
        )
        loader = SkillLoader([tmp_path])
        assert loader.discover() == []

    def test_skips_skill_missing_description(self, tmp_path):
        (tmp_path / "bad").mkdir()
        (tmp_path / "bad" / "SKILL.md").write_text(
            "---\nname: nodesc\n---\nBody.", encoding="utf-8"
        )
        loader = SkillLoader([tmp_path])
        assert loader.discover() == []

    def test_name_too_long_is_skipped(self, tmp_path):
        long_name = "x" * (MAX_NAME_LENGTH + 1)
        _write_skill(tmp_path, long_name, "desc")
        loader = SkillLoader([tmp_path])
        assert loader.discover() == []

    def test_name_exactly_at_limit_is_accepted(self, tmp_path):
        name = "y" * MAX_NAME_LENGTH
        _write_skill(tmp_path, name, "desc")
        loader = SkillLoader([tmp_path])
        assert len(loader.discover()) == 1

    def test_description_truncated_at_limit(self, tmp_path):
        from aegis_agent.skills.loader import MAX_DESCRIPTION_LENGTH

        long_desc = "z" * (MAX_DESCRIPTION_LENGTH + 100)
        _write_skill(tmp_path, "test", long_desc)
        loader = SkillLoader([tmp_path])
        skills = loader.discover()
        assert len(skills) == 1
        assert len(skills[0].description) == MAX_DESCRIPTION_LENGTH

    def test_name_collision_first_wins(self, tmp_path):
        dir_a = tmp_path / "a"
        dir_a.mkdir()
        dir_b = tmp_path / "b"
        dir_b.mkdir()
        _write_skill(dir_a, "collision", "first")
        _write_skill(dir_b, "collision", "second")

        loader = SkillLoader([dir_a, dir_b])
        skills = loader.discover()
        assert len(skills) == 1
        assert skills[0].description == "first"

    def test_get_by_name(self, tmp_path):
        _write_skill(tmp_path, "hello", "greeting")
        loader = SkillLoader([tmp_path])
        loader.discover()
        assert loader.get("hello") is not None
        assert loader.get("nonexistent") is None

    def test_discover_cached_on_repeat_call(self, tmp_path):
        _write_skill(tmp_path, "hello", "greeting")
        loader = SkillLoader([tmp_path])
        first = loader.discover()
        second = loader.discover()
        assert first is second  # same list object (cached)

    def test_force_discovers_again(self, tmp_path):
        _write_skill(tmp_path, "hello", "greeting")
        loader = SkillLoader([tmp_path])
        first = loader.discover()
        second = loader.discover(force=True)
        assert first[0].name == second[0].name
        # Force returns a fresh list
        assert first is not second

    def test_unreadable_skill_is_skipped(self, tmp_path):
        (tmp_path / "bad").mkdir()
        skill_md = tmp_path / "bad" / "SKILL.md"
        skill_md.write_text("---\nname: bad\ndescription: desc\n---\nbody", encoding="utf-8")
        # Make it unreadable by removing read permission
        os.chmod(skill_md, 0o000)
        try:
            loader = SkillLoader([tmp_path])
            assert loader.discover() == []
        finally:
            os.chmod(skill_md, 0o644)

    def test_excluded_dirs_are_skipped(self, tmp_path):
        # A SKILL.md inside .git should not be discovered
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        _write_skill(git_dir, "hidden", "should not be seen")
        loader = SkillLoader([tmp_path])
        assert loader.discover() == []

    def test_default_skills_dirs_from_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AEGIS_SKILLS_DIR", str(tmp_path))
        from aegis_agent.skills.loader import default_skills_dirs

        dirs = default_skills_dirs()
        assert dirs == [tmp_path]

    def test_default_skills_dirs_no_env(self, monkeypatch):
        monkeypatch.delenv("AEGIS_SKILLS_DIR", raising=False)
        from aegis_agent.skills.loader import default_skills_dirs

        dirs = default_skills_dirs()
        assert len(dirs) == 1
        assert dirs[0].name == "skills"


class TestPlatformGate:
    def test_empty_platforms_always_matches(self):

        assert _matches_platform({})
        assert _matches_platform({"platforms": []})

    def test_matching_platform(self, monkeypatch):

        monkeypatch.setattr(sys, "platform", "linux")
        assert _matches_platform({"platforms": ["linux"]})

    def test_nonmatching_platform_is_excluded(self, monkeypatch):

        monkeypatch.setattr(sys, "platform", "linux")
        assert not _matches_platform({"platforms": ["darwin", "win32"]})

    def test_macos_maps_to_darwin(self, monkeypatch):

        monkeypatch.setattr(sys, "platform", "darwin")
        assert _matches_platform({"platforms": ["macos"]})
        assert _matches_platform({"platforms": ["mac"]})
        assert _matches_platform({"platforms": ["osx"]})

    def test_windows_maps_to_win32(self, monkeypatch):

        monkeypatch.setattr(sys, "platform", "win32")
        assert _matches_platform({"platforms": ["windows"]})
        assert _matches_platform({"platforms": ["win"]})
