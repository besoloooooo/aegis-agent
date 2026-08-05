"""Context-builder invariants: source-of-truth message list is never mutated."""

from __future__ import annotations

from aegis_agent.context.builder import ContextBuilder
from aegis_agent.context.system_prompt import SystemPromptBuilder
from aegis_agent.models.base import Message, Role
from aegis_agent.skills.loader import SkillLoader
from aegis_agent.skills.prompt import SkillsIndexContributor
from tests.test_skills_loader import _write_skill


def test_source_messages_untouched_with_plain_string():
    """The original message list must be unchanged after build."""
    source = [
        Message(role=Role.USER, content="hello", client_msg_id="abc", seq=1),
    ]
    original = _snapshot(source)
    builder = ContextBuilder()
    derived = builder.build(source)

    # Derived strips internal fields
    assert derived[1].client_msg_id is None
    assert derived[1].seq is None
    # Source is unchanged
    assert _snapshot(source) == original


def test_source_messages_untouched_with_prompt_builder():
    """Source invariant must hold with a dynamic SystemPromptBuilder."""
    source = [
        Message(role=Role.USER, content="hello", client_msg_id="xyz", seq=5),
    ]
    original = _snapshot(source)
    prompt_builder = SystemPromptBuilder()
    builder = ContextBuilder(prompt_builder)
    builder.build(source)
    assert _snapshot(source) == original


def test_source_untouched_with_skills_contributor(tmp_path):
    """The skills index is injected into the derived prompt; source is untouched."""
    _write_skill(tmp_path, "test-skill", "For testing")

    source = [
        Message(role=Role.USER, content="use the skill", client_msg_id="sk", seq=1),
    ]
    original = _snapshot(source)

    loader = SkillLoader([tmp_path])
    loader.discover()
    prompt_builder = SystemPromptBuilder()
    prompt_builder.add(SkillsIndexContributor(loader))
    builder = ContextBuilder(prompt_builder)
    derived = builder.build(source)

    # Skills index is in the system prompt (first message)
    assert derived[0].role == Role.SYSTEM
    assert "test-skill" in derived[0].content
    assert "For testing" in derived[0].content

    # Source unchanged
    assert _snapshot(source) == original


def test_backward_compatible_string_construction():
    """Passing a plain string to ContextBuilder still works."""
    builder = ContextBuilder("Custom prompt.")
    assert builder.system_prompt == "Custom prompt."
    derived = builder.build([Message(role=Role.USER, content="hi")])
    assert derived[0].content == "Custom prompt."


def test_backward_compatible_none_construction():
    """None falls back to the default prompt."""
    builder = ContextBuilder(None)
    from aegis_agent.context.system_prompt import DEFAULT_IDENTITY

    assert builder.system_prompt == DEFAULT_IDENTITY


def test_build_with_empty_system_prompt():
    """An empty string disables the system message."""
    builder = ContextBuilder("")
    derived = builder.build([Message(role=Role.USER, content="hi")])
    assert derived[0].role == Role.USER  # no system msg prepended


def _snapshot(messages):
    """Return a hashable snapshot for before/after comparison."""
    return [(m.role.value, m.content, m.client_msg_id, m.seq, len(m.tool_calls)) for m in messages]
