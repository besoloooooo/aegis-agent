# Portions adapted from Hermes (hermes-agent), © 2025 Nous Research.
# Licensed under the MIT License. See THIRD_PARTY_NOTICES.md.
#
# Behavioural source (adapted and simplified):
#   * ``agent/skill_utils.py:parse_frontmatter`` (© 2025 Nous Research, MIT) —
#     splits a document on ``---`` fences, YAML-parses the frontmatter block,
#     and falls back to a naive ``key: value`` scan when YAML is unavailable or
#     malformed.  Aegis keeps that behaviour but always has PyYAML available and
#     uses the plain ``yaml.safe_load`` loader.
"""Parse ``SKILL.md`` frontmatter.

A ``SKILL.md`` file starts with a YAML block delimited by ``---`` fences,
followed by a markdown body::

    ---
    name: my-skill
    description: What it does.
    ---
    # Instructions
    ...

:func:`parse_frontmatter` returns ``(frontmatter_dict, body)``.  When the
document has no leading fence, the whole text is treated as the body and the
frontmatter is empty.  Malformed YAML degrades to a naive line scan rather than
raising, so one bad skill never breaks discovery of the others.
"""

from __future__ import annotations

import re

import yaml  # type: ignore[import-untyped]

# Matches a leading ``---\n ... \n---`` block.  ``[ \t]*`` tolerates trailing
# whitespace on the fence lines; ``re.DOTALL`` lets ``.`` span the YAML body.
_FRONTMATTER_RE = re.compile(
    r"^---[ \t]*\r?\n(?P<yaml>.*?)\r?\n---[ \t]*\r?\n?(?P<body>.*)$",
    re.DOTALL,
)


def parse_frontmatter(content: str) -> tuple[dict, str]:
    """Split ``content`` into ``(frontmatter, body)``.

    Returns an empty frontmatter dict and the original content as the body when
    there is no leading ``---`` fence.  A YAML parse error (or a non-mapping
    top-level document) falls back to a naive ``key: value`` scan of the
    frontmatter block.
    """
    if not content:
        return {}, ""

    match = _FRONTMATTER_RE.match(content)
    if match is None:
        return {}, content

    raw_yaml = match.group("yaml")
    body = match.group("body")

    try:
        loaded = yaml.safe_load(raw_yaml)
    except yaml.YAMLError:
        return _naive_frontmatter(raw_yaml), body

    if isinstance(loaded, dict):
        return loaded, body
    # Valid YAML but not a mapping (e.g. a bare list/scalar): fall back so a
    # caller still gets whatever ``key: value`` pairs it can.
    return _naive_frontmatter(raw_yaml), body


def _naive_frontmatter(raw_yaml: str) -> dict:
    """Best-effort ``key: value`` parse for malformed YAML frontmatter."""
    result: dict[str, str] = {}
    for line in raw_yaml.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key:
            result[key] = value
    return result


__all__ = ["parse_frontmatter"]
