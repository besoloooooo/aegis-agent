"""SessionRepository Protocol — the persistence abstraction.

The runtime depends only on this interface, never on a concrete store, so the
in-memory implementation (this stage) and a future SQLite implementation are
interchangeable.  The contract encodes the invariants from CLAUDE.md §9:

* ``append_message`` is **idempotent** on ``message.client_msg_id`` — appending
  a message whose ``client_msg_id`` is already present in the session returns
  the existing record instead of creating a duplicate.
* messages within a session are **monotonically ordered** — each stored message
  gets a ``seq`` of 0, 1, 2, ... assigned on append.
* sessions are **isolated** — one session's messages never leak into another.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from aegis_agent.models.base import Message
from aegis_agent.sessions.models import Session


@runtime_checkable
class SessionRepository(Protocol):
    """Structural interface for session persistence."""

    def create_session(self, session_id: str | None = None, title: str | None = None) -> Session:
        """Create and return a new session. Generates an id when not given."""
        ...

    def get_session(self, session_id: str) -> Session | None:
        """Return the session metadata, or ``None`` if it does not exist."""
        ...

    def append_message(self, session_id: str, message: Message) -> Message:
        """Append ``message`` to the session and return the stored record.

        Idempotent on ``message.client_msg_id``.  Assigns ``message.seq``.
        Raises :class:`~aegis_agent.exceptions.SessionNotFoundError` when the
        session does not exist.
        """
        ...

    def list_messages(self, session_id: str) -> list[Message]:
        """Return the session's messages in ``seq`` order (a copy)."""
        ...

    def message_count(self, session_id: str) -> int:
        """Return the number of stored messages in the session."""
        ...


__all__ = ["SessionRepository"]
