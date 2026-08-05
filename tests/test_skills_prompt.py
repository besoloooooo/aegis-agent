"""Skills prompt contributor and SystemPromptBuilder tests."""

from __future__ import annotations

from aegis_agent.context.system_prompt import (
    DEFAULT_IDENTITY,
    SystemPromptBuilder,
)
from aegis_agent.skills.loader import SkillLoader
from aegis_agent.skills.prompt import SkillsIndexContributor


class _StaticContributor:
    """A trivial contributor returning a fixed string."""

    def __init__(self, text: str | None) -> None:
        self._text = text

    def render(self) -> str | None:
        return self._text


class TestSystemPromptBuilder:
    def test_no_contributors_produces_identity_only(self):
        builder = SystemPromptBuilder()
        result = builder.build()
        assert result == DEFAULT_IDENTITY

    def test_custom_identity(self):
        builder = SystemPromptBuilder(identity="Custom identity.")
        assert builder.build() == "Custom identity."

    def test_contributor_section_appended(self):
        builder = SystemPromptBuilder()
        builder.add(_StaticContributor("Extra guidance."))
        result = builder.build()
        assert result.startswith(DEFAULT_IDENTITY)
        assert "\n\nExtra guidance." in result
        assert not result.endswith("\n\n")

    def test_none_contributor_is_dropped(self):
        builder = SystemPromptBuilder()
        builder.add(_StaticContributor(None))
        result = builder.build()
        assert result == DEFAULT_IDENTITY

    def test_empty_string_contributor_is_dropped(self):
        builder = SystemPromptBuilder()
        builder.add(_StaticContributor("  \n "))
        result = builder.build()
        assert result == DEFAULT_IDENTITY

    def test_multiple_contributors_joined_with_newlines(self):
        builder = SystemPromptBuilder()
        builder.add(_StaticContributor("Section A"))
        builder.add(_StaticContributor("Section B"))
        result = builder.build()
        parts = result.split("\n\n")
        assert len(parts) == 3
        assert parts[0] == DEFAULT_IDENTITY
        assert parts[1] == "Section A"
        assert parts[2] == "Section B"

    def test_sections_are_stripped_of_leading_trailing_whitespace(self):
        builder = SystemPromptBuilder()
        builder.add(_StaticContributor("  \n  padded  \n  "))
        result = builder.build()
        assert "\n\npadded" in result

    def test_builder_with_empty_identity(self):
        builder = SystemPromptBuilder(identity="")
        builder.add(_StaticContributor("Only section."))
        result = builder.build()
        assert result == "Only section."


class TestSkillsIndexContributor:
    def test_renders_index_when_skills_exist(self, tmp_path):
        from tests.test_skills_loader import _write_skill

        _write_skill(tmp_path, "hello", "A greeting skill")
        _write_skill(tmp_path, "goodbye", "A farewell skill")

        loader = SkillLoader([tmp_path])
        loader.discover()
        contributor = SkillsIndexContributor(loader)
        result = contributor.render()

        assert result is not None
        assert "hello" in result
        assert "goodbye" in result
        assert "A greeting skill" in result
        assert "A farewell skill" in result
        assert "<available_skills>" in result
        assert "</available_skills>" in result

    def test_renders_none_when_no_skills(self, tmp_path):
        loader = SkillLoader([tmp_path])
        loader.discover()
        contributor = SkillsIndexContributor(loader)
        assert contributor.render() is None

    def test_skills_grouped_by_category(self, tmp_path):
        from tests.test_skills_loader import _write_skill

        cat_a = tmp_path / "category-a"
        cat_a.mkdir()
        cat_b = tmp_path / "category-b"
        cat_b.mkdir()
        _write_skill(cat_a, "skill1", "First")
        _write_skill(cat_b, "skill2", "Second")

        loader = SkillLoader([tmp_path])
        loader.discover()
        contributor = SkillsIndexContributor(loader)
        result = contributor.render()

        assert result is not None
        assert "### category-a" in result
        assert "### category-b" in result
        assert "skill1" in result
        assert "skill2" in result

    def test_skills_without_category_use_general(self, tmp_path):
        from tests.test_skills_loader import _write_skill

        # Skill directly in the root has no parent folder category
        _write_skill(tmp_path, "root_skill", "Root level")

        loader = SkillLoader([tmp_path])
        loader.discover()
        contributor = SkillsIndexContributor(loader)
        result = contributor.render()

        assert result is not None
        assert "### general" in result
