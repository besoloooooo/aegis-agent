"""Skills frontmatter parser tests."""

from __future__ import annotations

from aegis_agent.skills.frontmatter import parse_frontmatter


def test_valid_frontmatter_returns_parsed_yaml_and_body():
    content = """---
name: hello
description: a greeting skill
---
# Instructions
Say hello."""
    fm, body = parse_frontmatter(content)
    assert fm["name"] == "hello"
    assert fm["description"] == "a greeting skill"
    assert "# Instructions" in body
    assert "Say hello" in body


def test_no_frontmatter_treats_whole_text_as_body():
    content = "Just some markdown\nwithout any frontmatter."
    fm, body = parse_frontmatter(content)
    assert fm == {}
    assert body == content


def test_empty_content_returns_empty():
    fm, body = parse_frontmatter("")
    assert fm == {}
    assert body == ""


def test_missing_closing_fence_treats_whole_as_body():
    content = """---
name: orphan
description: no closing fence
"""
    fm, body = parse_frontmatter(content)
    assert fm == {}
    assert body == content


def test_malformed_yaml_falls_back_to_naive_scan():
    content = """---
name: hello
description: "unclosed quote
---
# Body."""
    fm, _body = parse_frontmatter(content)
    # Naive fallback should still extract key:value lines
    assert fm["name"] == "hello"
    assert "unclosed" in fm["description"]


def test_non_mapping_yaml_falls_back_to_naive_scan():
    content = """---
- list item 1
- list item 2
---
# Body."""
    _fm, body = parse_frontmatter(content)
    # YAML loads a list, which is coerced to naive scan (empty)
    assert "# Body" in body


def test_frontmatter_with_crlf_line_endings():
    content = "---\r\nname: crlf\r\ndescription: windows style\r\n---\r\nBody here."
    fm, body = parse_frontmatter(content)
    assert fm["name"] == "crlf"
    assert "Body here." in body


def test_frontmatter_yaml_list_value():
    content = """---
name: multi
description: a skill
tags: [one, two, three]
---
Body."""
    fm, _body = parse_frontmatter(content)
    assert fm["tags"] == ["one", "two", "three"]


def test_body_preserves_markdown_formatting():
    content = """---
name: md
description: markdown skill
---
# Heading

Some **bold** and *italic* text.

- list item 1
- list item 2"""
    _fm, body = parse_frontmatter(content)
    assert "# Heading" in body
    assert "**bold**" in body
    assert "- list item 1" in body
