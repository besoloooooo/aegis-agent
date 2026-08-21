"""Agent messaging — message structure and the transport abstraction.

This module deliberately separates *what a message is* (:class:`AgentMessage`)
from *how it moves* (:class:`AgentTransport`), so the Team layer never depends
on a concrete queue or file.  The first (and only) implementation here is
:class:`InProcessTransport` — same-process, backed by a per-recipient queue
plus a wakeup :class:`threading.Event` so an idle teammate blocks cheaply and
is woken the moment a message arrives (no model polling of an inbox).

The abstraction is the seam for future transports:

* ``InProcessTransport``     — this module (same process, queue + event);
* ``FileMailboxTransport``   — Claude's disk JSON mailbox (cross-process);
* ``A2ATransport``           — remote agent-to-agent (out of scope for now).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable


class MessageType(str, Enum):
    """The message kinds the first version supports.  Kept deliberately small."""

    MESSAGE = "message"    # ordinary peer / lead → teammate text
    TASK = "task"          # a work assignment (lead → teammate)
    SHUTDOWN = "shutdown"  # ask the recipient to stop


@dataclass
class AgentMessage:
    """One structured message between agents.

    ``sender`` / ``recipient`` are stable agent names within a team (e.g.
    ``"team-lead"``, ``"coder"``).  ``content`` is the text body.  ``type``
    distinguishes a plain message from a task assignment or a shutdown signal.
    """

    sender: str
    recipient: str
    content: str
    type: MessageType = MessageType.MESSAGE
    message_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.time)

    def render_for_recipient(self) -> str:
        """Render as the text injected into the recipient's next turn.

        Mirrors Claude's ``<teammate-message>`` wrapper so the receiving model
        can tell an inter-agent message apart from a fresh user instruction.
        """
        tag = self.type.value
        return f"<agent-message from=\"{self.sender}\" type=\"{tag}\">\n{self.content}\n</agent-message>"


@runtime_checkable
class AgentTransport(Protocol):
    """How messages move between agents.

    Implementations deliver :class:`AgentMessage` to a recipient's inbox and
    let a recipient take the next message, blocking until one arrives (or a
    stop is requested).  The Team layer depends only on this protocol.
    """

    def send(self, message: AgentMessage) -> None:
        """Deliver ``message`` to its recipient's inbox (non-blocking)."""
        ...

    def receive(self, recipient: str, *, timeout: float | None = None) -> AgentMessage | None:
        """Take the next message for ``recipient``.

        Blocks (cheaply — event wait, not a busy poll) until a message is
        available, ``timeout`` elapses, or :meth:`close_recipient` is called.
        Returns ``None`` on timeout / close.
        """
        ...

    def has_pending(self, recipient: str) -> bool:
        """Whether ``recipient`` has unread messages waiting."""
        ...

    def close_recipient(self, recipient: str) -> None:
        """Wake any blocked :meth:`receive` for ``recipient`` so it can stop."""
        ...


class InProcessTransport:
    """Same-process transport: a per-recipient FIFO + a wakeup event.

    Each recipient gets a :class:`collections.deque` inbox guarded by one lock,
    and a :class:`threading.Event` that is set whenever a message lands.
    :meth:`receive` waits on that event, so an idle teammate consumes no model
    tokens and no busy-loop CPU while waiting — exactly the "low-cost block,
    wake on arrival" the design calls for.
    """

    def __init__(self) -> None:
        import threading
        from collections import deque

        self._lock = threading.Lock()
        self._inboxes: dict[str, deque[AgentMessage]] = {}
        self._events: dict[str, threading.Event] = {}
        self._closed: set[str] = set()
        self._threading = threading
        self._deque = deque

    def _event_for(self, recipient: str):
        event = self._events.get(recipient)
        if event is None:
            event = self._threading.Event()
            self._events[recipient] = event
        return event

    def send(self, message: AgentMessage) -> None:
        with self._lock:
            inbox = self._inboxes.setdefault(message.recipient, self._deque())
            inbox.append(message)
            self._closed.discard(message.recipient)
            self._event_for(message.recipient).set()

    def receive(self, recipient: str, *, timeout: float | None = None) -> AgentMessage | None:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                inbox = self._inboxes.get(recipient)
                if inbox:
                    message = inbox.popleft()
                    if not inbox:
                        self._event_for(recipient).clear()
                    return message
                if recipient in self._closed:
                    return None
                event = self._event_for(recipient)
            # Block until a message arrives (event set) or timeout/close.
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            if remaining is not None and remaining <= 0:
                return None
            event.wait(timeout=remaining)
            if deadline is not None and time.monotonic() >= deadline:
                with self._lock:
                    inbox = self._inboxes.get(recipient)
                    if inbox:
                        return inbox.popleft()
                return None

    def has_pending(self, recipient: str) -> bool:
        with self._lock:
            return bool(self._inboxes.get(recipient))

    def close_recipient(self, recipient: str) -> None:
        with self._lock:
            self._closed.add(recipient)
            self._event_for(recipient).set()  # wake any blocked receiver

    def reopen_recipient(self, recipient: str) -> None:
        """Clear the closed flag (used when a teammate is restarted)."""
        with self._lock:
            self._closed.discard(recipient)


__all__ = [
    "AgentMessage",
    "AgentTransport",
    "InProcessTransport",
    "MessageType",
]
