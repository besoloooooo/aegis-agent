"""Session title helpers.

Titles are derived session metadata: they help humans recognise a session in
lists, but they never replace or mutate the source message log.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, datetime

from aegis_agent.models.base import Message, Role

MAX_TITLE_LENGTH = 60

_SECRET_RE = re.compile(
    r"(?i)\b(?:api[_-]?key|token|secret|password|passwd|bearer)\b\s*[:=]\s*\S+"
)
_URL_RE = re.compile(r"https?://\S+")
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_PATH_RE = re.compile(r"(?:[A-Za-z]:\\|/)[^\s]{20,}")
_CODE_FENCE_RE = re.compile(r"```(?:[^`]|`(?!``))*```", re.DOTALL)


def sanitize_title(raw: str) -> str:
    """Normalise a session title; ``""`` means invalid.

    Strips control/non-printable characters, collapses whitespace, removes common
    model-produced wrappers, and caps the length so titles stay one-line and safe
    to echo in lists.
    """
    cleaned = "".join(ch for ch in raw if ch.isprintable())
    cleaned = " ".join(cleaned.split())
    cleaned = cleaned.strip(" \t\r\n'\"“”‘’.。.!！")
    for prefix in ("Title:", "title:", "标题：", "标题:"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):]
            break
    cleaned = cleaned.strip(" \t\r\n'\"“”‘’.。.!！")
    return cleaned[:MAX_TITLE_LENGTH]


def redact_title_text(text: str) -> str:
    """Remove sensitive/noisy substrings before title generation."""
    redacted = _SECRET_RE.sub("", text)
    redacted = _URL_RE.sub("", redacted)
    redacted = _EMAIL_RE.sub("", redacted)
    redacted = _PATH_RE.sub("", redacted)
    return redacted


def fallback_session_title(created_at: float | None = None) -> str:
    """Return a stable non-empty fallback title based on local time."""
    if created_at is None:
        dt = datetime.now(UTC).astimezone()
    else:
        dt = datetime.fromtimestamp(created_at, tz=UTC).astimezone()
    return f"Session {dt:%Y-%m-%d %H:%M}"


def heuristic_title_from_text(text: str) -> str:
    """Derive a short title from user text without calling a model."""
    cleaned = _CODE_FENCE_RE.sub(" ", text)
    cleaned = redact_title_text(cleaned)
    if not cleaned.strip():
        return ""

    # Prefer a sentence/question-sized prefix over an arbitrary mid-sentence cut.
    first_line = cleaned.splitlines()[0] if "\n" in cleaned else cleaned
    for sep in ("。", "？", "?", "！", "!", "."):
        head, found, _tail = first_line.partition(sep)
        if found and head.strip():
            first_line = head.strip()
            break
    return sanitize_title(first_line)


def heuristic_title_from_messages(messages: Sequence[Message]) -> str | None:
    """Return a heuristic title from the first non-empty user message."""
    for message in messages:
        if message.role is not Role.USER:
            continue
        title = heuristic_title_from_text(message.content or "")
        if title:
            return title
    return None


__all__ = [
    "MAX_TITLE_LENGTH",
    "fallback_session_title",
    "heuristic_title_from_messages",
    "heuristic_title_from_text",
    "redact_title_text",
    "sanitize_title",
]
