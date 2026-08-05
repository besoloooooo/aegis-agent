# Portions adapted from Hermes (hermes-agent), © 2025 Nous Research.
# Licensed under the MIT License. See THIRD_PARTY_NOTICES.md.
#
# Behavioural source (decoupled and simplified):
#   * ``agent/conversation_loop.py`` (the per-call ``api_messages`` build,
#     ~lines 964-1058) — the model is sent a *derived copy* of the source
#     history each call: internal fields are stripped and the system prompt is
#     prepended, while the original ``messages`` list is never mutated.
"""Build the derived context sent to the model.

The core invariant (from the extraction plan §3.4): **the source messages are
the source of truth and are never mutated.**  The context builder produces a
fresh, derived view on every model call — prepending the system prompt and
dropping internal bookkeeping fields (``client_msg_id``, ``seq``) that must not
leak to the model.  Context compression (a later stage) will only ever alter
this derived view, never the originals.

The system prompt is produced by a :class:`SystemPromptBuilder` (see
``system_prompt.py``), re-rendered on every call so contributed sections (e.g.
the skills index) stay in sync with current state.  For back-compatibility the
constructor still accepts a plain string.
"""

from __future__ import annotations

from collections.abc import Sequence

from aegis_agent.context.system_prompt import DEFAULT_IDENTITY, SystemPromptBuilder
from aegis_agent.models.base import Message, Role

#: Retained for back-compatibility (older callers/tests import this name).
#: Identical to :data:`~aegis_agent.context.system_prompt.DEFAULT_IDENTITY`.
DEFAULT_SYSTEM_PROMPT = DEFAULT_IDENTITY


class ContextBuilder:
    """Derive the per-call message list from the source-of-truth history."""

    def __init__(self, system_prompt: str | SystemPromptBuilder | None = None) -> None:
        """Configure the system-prompt source.

        Accepts a :class:`SystemPromptBuilder` (the dynamic path), a plain
        string (wrapped into a builder with no contributors), or ``None`` (the
        default identity).  An empty string disables the system message.
        """
        if isinstance(system_prompt, SystemPromptBuilder):
            self._prompt_builder = system_prompt
        elif system_prompt is None:
            self._prompt_builder = SystemPromptBuilder()
        else:
            self._prompt_builder = SystemPromptBuilder(identity=system_prompt)

    @property
    def prompt_builder(self) -> SystemPromptBuilder:
        return self._prompt_builder

    @property
    def system_prompt(self) -> str:
        """The system prompt as it renders right now (identity + contributors)."""
        return self._prompt_builder.build()

    def build(self, messages: Sequence[Message]) -> list[Message]:
        """Return a new derived list: system prompt + cleaned copies of history.

        The input sequence and its messages are left untouched.  Each returned
        message is a copy with internal fields (``client_msg_id``, ``seq``)
        cleared.  The system prompt is rendered fresh so any dynamic
        contributor sections reflect current state.
        """
        derived: list[Message] = []
        system_prompt = self._prompt_builder.build()
        if system_prompt:
            derived.append(Message(role=Role.SYSTEM, content=system_prompt))
        for message in messages:
            derived.append(self._derive(message))
        return derived

    @staticmethod
    def _derive(message: Message) -> Message:
        """Copy a message into its model-facing form (internal fields stripped)."""
        return Message(
            role=message.role,
            content=message.content,
            tool_calls=list(message.tool_calls),
            tool_call_id=message.tool_call_id,
            name=message.name,
            client_msg_id=None,
            seq=None,
        )


__all__ = ["DEFAULT_SYSTEM_PROMPT", "ContextBuilder", "SystemPromptBuilder"]
