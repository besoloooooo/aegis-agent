"""In-memory SessionRepository — Stage-1 store and test double.

A thread-safe, process-local implementation of
:class:`~aegis_agent.sessions.repository.SessionRepository`.  It enforces the
idempotency / ordering / isolation invariants so core Agent Loop behaviour can
be tested without SQLite (which arrives in Stage 2).  State is lost when the
process exits, so this does not provide cross-process resume.
"""

from __future__ import annotations

import threading
import uuid

from aegis_agent.exceptions import SessionNotFoundError
from aegis_agent.models.base import Message
from aegis_agent.sessions.models import Session


class InMemorySessionRepository:
    """Process-local session store backed by dicts."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._messages: dict[str, list[Message]] = {}
        self._lock = threading.Lock()

    def create_session(self, session_id: str | None = None, title: str | None = None) -> Session:
        with self._lock:
            sid = session_id or uuid.uuid4().hex
            existing = self._sessions.get(sid)
            if existing is not None:
                return existing
            session = Session(id=sid, title=title)
            self._sessions[sid] = session
            self._messages[sid] = []
            return session

    def get_session(self, session_id: str) -> Session | None:
        with self._lock:
            return self._sessions.get(session_id)

    def set_session_title(self, session_id: str, title: str) -> bool:
        """Update the session title (``/title``).  False when unknown."""
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return False
            session.title = title
            return True

    def rewind_from_seq(self, session_id: str, seq: int) -> int:
        """Drop the message at ``seq`` and everything after it (``/undo``).

        The in-memory store is ephemeral (nothing survives the process), so
        unlike the SQLite store's soft delete this simply truncates the tail;
        ``seq`` equals the list index, so the remaining prefix keeps its
        ordering invariant.  Returns the number of removed messages.
        """
        with self._lock:
            messages = self._messages_for(session_id)
            if seq < 0 or seq >= len(messages):
                return 0
            removed = len(messages) - seq
            del messages[seq:]
            return removed

    def append_message(self, session_id: str, message: Message) -> Message:
        with self._lock:
            messages = self._messages_for(session_id)
            # Idempotency: one persisted logical message per client_msg_id.
            if message.client_msg_id is not None:
                for existing in messages:
                    if existing.client_msg_id == message.client_msg_id:
                        return existing
            message.seq = len(messages)
            messages.append(message)
            return message

    def list_messages(self, session_id: str) -> list[Message]:
        with self._lock:
            return list(self._messages_for(session_id))

    def message_count(self, session_id: str) -> int:
        with self._lock:
            return len(self._messages_for(session_id))

    def list_sessions(self) -> list[dict]:
        """Return all sessions with their message counts (newest first)."""
        with self._lock:
            result = []
            for sid, session in self._sessions.items():
                result.append({
                    "id": sid,
                    "title": session.title,
                    "message_count": len(self._messages.get(sid, [])),
                    "created_at": session.created_at,
                })
            result.sort(key=lambda s: s["created_at"], reverse=True)
            return result

    def _messages_for(self, session_id: str) -> list[Message]:
        if session_id not in self._sessions:
            raise SessionNotFoundError(f"session not found: {session_id}")
        return self._messages[session_id]


__all__ = ["InMemorySessionRepository"]
