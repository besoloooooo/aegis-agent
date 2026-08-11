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

__all__ = [
    "InMemorySessionRepository",
    "LeaseHandle",
    "SQLiteSessionRepository",
    "Session",
    "SessionLeaseBackend",
    "SessionLeaseManager",
    "SessionLeaseUnavailableError",
    "SessionRepository",
    "get_lease_backend",
]
