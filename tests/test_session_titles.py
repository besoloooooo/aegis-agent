from __future__ import annotations

import time

from aegis_agent.models.base import Message, Role
from aegis_agent.models.fake import FakeModelProvider, FakeReply
from aegis_agent.sessions.memory_store import InMemorySessionRepository
from aegis_agent.sessions.title_generator import SessionTitleService, generate_llm_session_title
from aegis_agent.sessions.titles import (
    fallback_session_title,
    heuristic_title_from_messages,
    heuristic_title_from_text,
    redact_title_text,
    sanitize_title,
)


def test_sanitize_title_collapses_wrappers_and_length() -> None:
    assert sanitize_title("  Title:  My   Session.  ") == "My Session"
    assert sanitize_title("\x00\x07") == ""
    assert len(sanitize_title("x" * 100)) == 60


def test_heuristic_title_uses_first_user_message() -> None:
    messages = [
        Message(role=Role.SYSTEM, content="ignore"),
        Message(role=Role.USER, content="请帮我给 session 自动生成名字。后面不用了"),
        Message(role=Role.ASSISTANT, content="ok"),
    ]
    assert heuristic_title_from_messages(messages) == "请帮我给 session 自动生成名字"


def test_heuristic_title_redacts_noisy_sensitive_text() -> None:
    text = "debug token=sk-secret https://example.com/a/b user@example.com /home/nacha/a/very/long/path/file.py"
    title = heuristic_title_from_text(text)
    assert "sk-secret" not in title
    assert "http" not in title
    assert "@" not in title
    assert "/home" not in title


def test_redact_title_text_removes_secret_assignment() -> None:
    assert "abc" not in redact_title_text("api_key=abc123 fix login")


def test_fallback_session_title_is_stable_for_timestamp() -> None:
    assert fallback_session_title(0).startswith("Session 1970-")


def test_auto_title_does_not_overwrite_manual_title() -> None:
    repo = InMemorySessionRepository()
    repo.create_session("s", title="Manual", title_source="manual")
    service = SessionTitleService(repo, enable_llm=False)
    changed = service.ensure_heuristic_title("s", [Message(role=Role.USER, content="new topic")])
    assert changed is False
    assert repo.get_session("s").title == "Manual"


def test_llm_auto_title_can_replace_heuristic_title() -> None:
    repo = InMemorySessionRepository()
    repo.create_session("s")
    service = SessionTitleService(repo, FakeModelProvider([FakeReply(text="Better Session Title")]))
    messages = [
        Message(role=Role.USER, content="please name this chat"),
        Message(role=Role.ASSISTANT, content="sure"),
    ]
    assert service.ensure_heuristic_title("s", messages) is True
    service.maybe_schedule_llm_title("s", messages)
    deadline = time.time() + 2
    while repo.get_session("s").title != "Better Session Title" and time.time() < deadline:
        time.sleep(0.01)
    assert repo.get_session("s").title == "Better Session Title"
    assert repo.get_session("s").title_source == "llm"


def test_generate_llm_session_title_sanitizes_model_output() -> None:
    provider = FakeModelProvider([FakeReply(text='Title: "Useful Debugging".')])
    title = generate_llm_session_title(provider, [Message(role=Role.USER, content="debug this")])
    assert title == "Useful Debugging"
