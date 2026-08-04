"""In-memory SessionRepository invariant tests."""

from __future__ import annotations

import pytest

from aegis_agent.exceptions import SessionNotFoundError
from aegis_agent.models.base import Message, Role
from aegis_agent.sessions.memory_store import InMemorySessionRepository


def _msg(content, client_msg_id=None):
    return Message(role=Role.USER, content=content, client_msg_id=client_msg_id)


def test_create_and_get_session():
    repo = InMemorySessionRepository()
    session = repo.create_session("s1", title="demo")
    assert session.id == "s1"
    assert repo.get_session("s1") is session
    assert repo.get_session("nope") is None


def test_append_assigns_monotonic_seq():
    repo = InMemorySessionRepository()
    repo.create_session("s1")
    repo.append_message("s1", _msg("a"))
    repo.append_message("s1", _msg("b"))
    repo.append_message("s1", _msg("c"))
    seqs = [m.seq for m in repo.list_messages("s1")]
    assert seqs == [0, 1, 2]


def test_append_is_idempotent_on_client_msg_id():
    repo = InMemorySessionRepository()
    repo.create_session("s1")
    msg = _msg("hello", client_msg_id="abc123")
    first = repo.append_message("s1", msg)
    # appending the same logical message again must not duplicate it
    again = repo.append_message("s1", Message(role=Role.USER, content="hello", client_msg_id="abc123"))
    assert repo.message_count("s1") == 1
    assert first is again


def test_distinct_client_msg_ids_both_persist():
    repo = InMemorySessionRepository()
    repo.create_session("s1")
    repo.append_message("s1", _msg("one", client_msg_id="id-1"))
    repo.append_message("s1", _msg("two", client_msg_id="id-2"))
    assert repo.message_count("s1") == 2


def test_append_to_unknown_session_raises():
    repo = InMemorySessionRepository()
    with pytest.raises(SessionNotFoundError):
        repo.append_message("ghost", _msg("x"))


def test_sessions_isolated():
    repo = InMemorySessionRepository()
    repo.create_session("A")
    repo.create_session("B")
    repo.append_message("A", _msg("only-in-A"))
    assert repo.message_count("A") == 1
    assert repo.message_count("B") == 0
    assert repo.list_messages("B") == []
