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
"""

from __future__ import annotations

from collections.abc import Sequence

from aegis_agent.models.base import Message, Role

DEFAULT_SYSTEM_PROMPT = (
    "You are Aegis Agent, a helpful assistant. You can call the provided "
    "tools to read files, list directories, and run shell commands when that "
    "helps answer the user. Otherwise answer directly."
)


class ContextBuilder:
    """Derive the per-call message list from the source-of-truth history."""

    def __init__(self, system_prompt: str | None = None) -> None:
        self._system_prompt = system_prompt if system_prompt is not None else DEFAULT_SYSTEM_PROMPT

    @property
    def system_prompt(self) -> str:
        return self._system_prompt

    def build(self, messages: Sequence[Message]) -> list[Message]:
        """Return a new derived list: system prompt + cleaned copies of history.

        The input sequence and its messages are left untouched.  Each returned
        message is a copy with internal fields (``client_msg_id``, ``seq``)
        cleared.
        """
        derived: list[Message] = []
        if self._system_prompt:
            derived.append(Message(role=Role.SYSTEM, content=self._system_prompt))
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


__all__ = ["DEFAULT_SYSTEM_PROMPT", "ContextBuilder"]
