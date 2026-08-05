"""Skills tools tests: skills_list, skill_view."""

from __future__ import annotations

import json

from aegis_agent.skills.loader import SkillLoader
from aegis_agent.skills.tools import SkillsListTool, SkillViewTool
from tests.test_skills_loader import _write_skill


class TestSkillsListTool:
    def test_returns_all_skills(self, tmp_path):
        _write_skill(tmp_path, "hello", "A greeting skill")
        _write_skill(tmp_path, "goodbye", "A farewell skill")

        loader = SkillLoader([tmp_path])
        loader.discover()
        tool = SkillsListTool(loader)
        result = tool.run({})

        assert not result.is_error
        payload = json.loads(result.content)
        assert payload["count"] == 2
        names = {s["name"] for s in payload["skills"]}
        assert names == {"hello", "goodbye"}

    def test_filters_by_category(self, tmp_path):
        cat = tmp_path / "social"
        cat.mkdir()
        _write_skill(cat, "hello", "Greeting")
        _write_skill(tmp_path, "utils", "Utility")

        loader = SkillLoader([tmp_path])
        loader.discover()
        tool = SkillsListTool(loader)

        result = tool.run({"category": "social"})
        payload = json.loads(result.content)
        assert payload["count"] == 1
        assert payload["skills"][0]["name"] == "hello"

    def test_category_filter_is_case_insensitive(self, tmp_path):
        cat = tmp_path / "Social"
        cat.mkdir()
        _write_skill(cat, "hello", "Greeting")

        loader = SkillLoader([tmp_path])
        loader.discover()
        tool = SkillsListTool(loader)

        result = tool.run({"category": "social"})
        payload = json.loads(result.content)
        assert payload["count"] == 1

    def test_empty_when_no_skills(self, tmp_path):
        loader = SkillLoader([tmp_path])
        loader.discover()
        tool = SkillsListTool(loader)
        result = tool.run({})
        payload = json.loads(result.content)
        assert payload["count"] == 0
        assert payload["skills"] == []

    def test_missing_category_attribute_uses_general(self, tmp_path):
        _write_skill(tmp_path, "root_skill", "Root level")

        loader = SkillLoader([tmp_path])
        loader.discover()
        tool = SkillsListTool(loader)
        result = tool.run({})
        payload = json.loads(result.content)
        assert payload["skills"][0]["category"] == "general"


class TestSkillViewTool:
    def test_returns_full_skill_body(self, tmp_path):
        _write_skill(tmp_path, "hello", "A greeting skill", body="# Instructions\nSay hello.")

        loader = SkillLoader([tmp_path])
        loader.discover()
        tool = SkillViewTool(loader)
        result = tool.run({"name": "hello"})

        assert not result.is_error
        payload = json.loads(result.content)
        assert payload["name"] == "hello"
        assert "Say hello" in payload["content"]
        assert payload["directory"] == str(tmp_path / "hello")

    def test_unknown_skill_returns_error(self, tmp_path):
        loader = SkillLoader([tmp_path])
        loader.discover()
        tool = SkillViewTool(loader)
        result = tool.run({"name": "nonexistent"})

        assert result.is_error
        assert "Unknown skill" in json.loads(result.content)["error"]

    def test_missing_name_returns_error(self, tmp_path):
        loader = SkillLoader([tmp_path])
        loader.discover()
        tool = SkillViewTool(loader)
        result = tool.run({})

        assert result.is_error
        assert "missing required field" in json.loads(result.content)["error"].lower()

    def test_can_read_supporting_file(self, tmp_path):
        _write_skill(tmp_path, "hello", "A greeting skill")
        ref_dir = tmp_path / "hello" / "references"
        ref_dir.mkdir()
        (ref_dir / "guide.md").write_text("# Reference guide\nExtra info.", encoding="utf-8")

        loader = SkillLoader([tmp_path])
        loader.discover()
        tool = SkillViewTool(loader)
        result = tool.run({"name": "hello", "file_path": "references/guide.md"})

        assert not result.is_error
        payload = json.loads(result.content)
        assert "# Reference guide" in payload["content"]

    def test_absolute_file_path_is_rejected(self, tmp_path):
        _write_skill(tmp_path, "hello", "A greeting skill")

        loader = SkillLoader([tmp_path])
        loader.discover()
        tool = SkillViewTool(loader)
        result = tool.run({"name": "hello", "file_path": "/etc/passwd"})

        assert result.is_error
        assert "must be relative" in json.loads(result.content)["error"]

    def test_path_traversal_is_rejected(self, tmp_path):
        _write_skill(tmp_path, "hello", "A greeting skill")

        loader = SkillLoader([tmp_path])
        loader.discover()
        tool = SkillViewTool(loader)
        result = tool.run({"name": "hello", "file_path": "../../../etc/passwd"})

        assert result.is_error
        assert "escapes" in json.loads(result.content)["error"]

    def test_missing_supporting_file_returns_error(self, tmp_path):
        _write_skill(tmp_path, "hello", "A greeting skill")

        loader = SkillLoader([tmp_path])
        loader.discover()
        tool = SkillViewTool(loader)
        result = tool.run({"name": "hello", "file_path": "references/missing.md"})

        assert result.is_error
        assert "not found" in json.loads(result.content)["error"].lower()

    def test_error_never_raises(self, tmp_path):
        """A bad tool call must return an error result, never raise."""
        loader = SkillLoader([tmp_path])
        loader.discover()
        tool = SkillViewTool(loader)
        # All of these must return without raising
        _ = tool.run({})
        _ = tool.run({"name": None})
        _ = tool.run({"name": 42})
        _ = tool.run({"name": "hello", "file_path": "/"})
        assert True  # sanity: we got here
