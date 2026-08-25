"""Best-effort automatic session title generation.

The service in this module derives session metadata from the original message
log.  It never mutates messages and never blocks the agent turn on a model call.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Sequence
from dataclasses import dataclass

from aegis_agent.events import collect_response
from aegis_agent.models.base import Message, ModelProvider, Role
from aegis_agent.sessions.titles import (
    heuristic_title_from_messages,
    redact_title_text,
    sanitize_title,
)

logger = logging.getLogger(__name__)

_TITLE_PROMPT = """Generate a short title for this Aegis Agent session.

Rules:
- Output only the title text, no explanation.
- Use 3-7 words or a short Chinese phrase.
- Keep the user's language.
- Do not use quotes, a Title: prefix, or trailing punctuation.
- Do not include secrets, full URLs, full local paths, emails, phone numbers, or API keys.
- Describe the task, not the fact that this is a conversation.

User message:
{user}

Assistant response:
{assistant}
"""

_MAX_EXCERPT_CHARS = 800


@dataclass(frozen=True)
class TitleGenerationResult:
    """Result of a title-generation attempt."""

    title: str | None
    source: str
    error: str | None = None


def _first_text(messages: Sequence[Message], role: Role) -> str:
    for message in messages:
        if message.role is role and (message.content or "").strip():
            return message.content
    return ""


def generate_llm_session_title(provider: ModelProvider, messages: Sequence[Message]) -> str | None:
    """Generate a title with the configured model provider, returning ``None`` on failure."""
    user = redact_title_text(_first_text(messages, Role.USER))[:_MAX_EXCERPT_CHARS]
    assistant = redact_title_text(_first_text(messages, Role.ASSISTANT))[:_MAX_EXCERPT_CHARS]
    if not user:
        return None
    prompt = _TITLE_PROMPT.format(user=user, assistant=assistant or "(no assistant response yet)")
    try:
        response = collect_response(provider.stream([Message(role=Role.USER, content=prompt)], tools=[]))
    except Exception as exc:  # noqa: BLE001 — title generation is best-effort
        logger.debug("session title generation failed: %s", exc)
        return None
    title = sanitize_title(response.content or "")
    return title or None


class SessionTitleService:
    """Small scheduler that keeps session titles useful without blocking turns."""

    def __init__(self, repository, provider: ModelProvider | None = None, *, enable_llm: bool = True) -> None:
        self._repository = repository
        self._provider = provider
        self._enable_llm = enable_llm and provider is not None
        self._threads: list[threading.Thread] = []
        self._scheduled: set[str] = set()
        self._attempted: set[str] = set()
        self._lock = threading.Lock()

    def ensure_heuristic_title(self, session_id: str, messages: Sequence[Message]) -> bool:
        """Write a heuristic title when the store supports auto titles."""
        title = heuristic_title_from_messages(messages)
        if not title:
            return False
        set_auto = getattr(self._repository, "set_auto_session_title", None)
        if not callable(set_auto):
            return False
        return bool(set_auto(session_id, title, source="heuristic"))

    def maybe_schedule_llm_title(self, session_id: str, messages: Sequence[Message]) -> None:
        """Start one background LLM title job for this session when useful."""
        if not self._enable_llm or self._provider is None:
            return
        if not any(m.role is Role.USER and (m.content or "").strip() for m in messages):
            return
        with self._lock:
            if session_id in self._scheduled or session_id in self._attempted:
                return
            self._scheduled.add(session_id)
            self._attempted.add(session_id)
        snapshot = list(messages)
        thread = threading.Thread(
            target=self._generate_and_store,
            args=(session_id, snapshot),
            name=f"aegis-title-{session_id[:8]}",
            daemon=True,
        )
        self._threads.append(thread)
        thread.start()

    def shutdown(self, timeout: float = 0.2) -> None:
        """Briefly drain title jobs; never block CLI exit for long."""
        for thread in list(self._threads):
            thread.join(timeout=timeout)

    def _generate_and_store(self, session_id: str, messages: Sequence[Message]) -> None:
        try:
            title = generate_llm_session_title(self._provider, messages) if self._provider else None
            if not title:
                return
            set_auto = getattr(self._repository, "set_auto_session_title", None)
            if callable(set_auto):
                set_auto(session_id, title, source="llm")
        finally:
            with self._lock:
                self._scheduled.discard(session_id)


__all__ = [
    "SessionTitleService",
    "TitleGenerationResult",
    "generate_llm_session_title",
]
