"""Session subsystem: data models, the repository Protocol, and the stores."""

from __future__ import annotations

from aegis_agent.sessions.lease import (
    LeaseHandle,
    SessionLeaseBackend,
    SessionLeaseManager,
    SessionLeaseUnavailableError,
    get_lease_backend,
)
from aegis_agent.sessions.memory_store import InMemorySessionRepository
from aegis_agent.sessions.models import Session
from aegis_agent.sessions.repository import SessionRepository
from aegis_agent.sessions.sqlite_store import SQLiteSessionRepository
from aegis_agent.sessions.title_generator import SessionTitleService
from aegis_agent.sessions.titles import (
    MAX_TITLE_LENGTH,
    fallback_session_title,
    heuristic_title_from_messages,
    heuristic_title_from_text,
    redact_title_text,
    sanitize_title,
)

__all__ = [
    "MAX_TITLE_LENGTH",
    "InMemorySessionRepository",
    "LeaseHandle",
    "SQLiteSessionRepository",
    "Session",
    "SessionLeaseBackend",
    "SessionLeaseManager",
    "SessionLeaseUnavailableError",
    "SessionRepository",
    "SessionTitleService",
    "fallback_session_title",
    "get_lease_backend",
    "heuristic_title_from_messages",
    "heuristic_title_from_text",
    "redact_title_text",
    "sanitize_title",
]
