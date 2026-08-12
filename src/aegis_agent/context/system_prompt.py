# Portions adapted from Hermes (hermes-agent), © 2025 Nous Research.
# Licensed under the MIT License. See THIRD_PARTY_NOTICES.md.
#
# Behavioural source (decoupled and simplified):
#   * ``agent/system_prompt.py:build_system_prompt_parts`` (© 2025 Nous
#     Research, MIT) assembles the system prompt from ordered tiers
#     (stable / context / volatile) whose contents are computed at build time
#     and joined with blank lines, with empty sections omitted.  This module
#     keeps that "compose ordered sections, drop the empties, rebuild each call"
#     behaviour but reduces it to a single flat list of *contributors* — the
#     minimal seam Aegis needs so subsystems (skills now, MCP guidance later)
#     can inject prompt sections without the builder knowing about them.
"""Layered, dynamic system-prompt assembly.

Aegis's original context builder prepended a single static string.  This module
generalises that into a :class:`SystemPromptBuilder`: a fixed *identity* header
followed by any number of :class:`PromptContributor` sections that are rendered
fresh on every :meth:`SystemPromptBuilder.build` call.  Rendering per call keeps
the prompt *dynamic* — a contributor can reflect state that changed since the
last turn (e.g. the set of available skills) — while contributors that have
nothing to say return ``None`` and are dropped, so the prompt never accrues
empty scaffolding.

The source-of-truth invariant (CLAUDE.md §6) is unaffected: this only shapes the
system message in the *derived* context; the stored history is never touched.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

#: The default identity/behaviour header — the first section of every prompt.
#: A de-branded adaptation of Hermes' ``DEFAULT_AGENT_IDENTITY`` (© 2025 Nous
#: Research, MIT): same helpful/direct/uncertainty-admitting persona, with the
#: Nous branding and docs-site pointer dropped (Aegis has neither).
DEFAULT_IDENTITY = (
    "You are Aegis Agent, an intelligent AI assistant. You are helpful, "
    "knowledgeable, and direct. You assist users with a wide range of tasks "
    "including answering questions, writing and editing code, analyzing "
    "information, and executing actions via your tools. You communicate "
    "clearly, admit uncertainty when appropriate, and prioritize being "
    "genuinely useful over being verbose unless otherwise directed below. Be "
    "targeted and efficient in your exploration and investigations."
)

_SECTION_SEPARATOR = "\n\n"


@runtime_checkable
class PromptContributor(Protocol):
    """A source of one optional system-prompt section.

    :meth:`render` is called on every prompt build.  Return the section text to
    include it, or ``None`` (or an empty/whitespace string) to contribute
    nothing this turn.  Implementations must be cheap and side-effect free.
    """

    def render(self) -> str | None:
        ...


class SystemPromptBuilder:
    """Assemble the system prompt from an identity header + contributor sections.

    The identity is always first; each contributor that returns non-empty text
    is appended in order.  Sections are joined with a blank line.  Because
    contributors are re-rendered on every :meth:`build`, the prompt reflects the
    current state of the subsystems that feed it.
    """

    def __init__(
        self,
        identity: str = DEFAULT_IDENTITY,
        contributors: Sequence[PromptContributor] = (),
    ) -> None:
        self._identity = identity
        self._contributors: list[PromptContributor] = list(contributors)

    def add(self, contributor: PromptContributor) -> PromptContributor:
        """Append a contributor; returns it for chaining."""
        self._contributors.append(contributor)
        return contributor

    @property
    def identity(self) -> str:
        return self._identity

    def build(self) -> str:
        """Render the identity header plus every non-empty contributor section."""
        sections: list[str] = []
        if self._identity:
            sections.append(self._identity)
        for contributor in self._contributors:
            rendered = contributor.render()
            if rendered and rendered.strip():
                sections.append(rendered.strip())
        return _SECTION_SEPARATOR.join(sections)


__all__ = ["DEFAULT_IDENTITY", "PromptContributor", "SystemPromptBuilder"]
