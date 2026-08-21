"""Persistent teammates — long-lived agents with a continuous context.

Unlike a one-shot subagent (run → result → destroyed), a teammate *stays
alive*: it runs a turn, goes IDLE, blocks cheaply on its inbox, and wakes to
run the next turn **on top of the same transcript**.  The loop is the Aegis
analogue of Claude Code's ``runInProcessTeammate`` — but reuses the shared
:class:`~aegis_agent.agents.runner.SubagentRunner` (hence the shared
:class:`~aegis_agent.runtime.AgentRuntime`) for each turn rather than any new
loop, and waits on a :class:`threading.Event` (via the transport) instead of
polling a file mailbox.

Lifecycle::

    CREATED → RUNNING → IDLE → (message arrives) → RUNNING → IDLE → … → STOPPED
                    ↘ FAILED (a turn raises) — the teammate reports and returns
                      to IDLE rather than dying, so one bad turn never takes
                      down the team.

Context continuity: the teammate owns ONE ``InMemorySessionRepository`` and ONE
session for its whole life.  Each turn calls ``SubagentRunner.run`` with that
same repo/session, so the model sees the full prior conversation every time —
there is no per-wakeup fresh agent.
"""

from __future__ import annotations

import logging
import threading
import uuid
from enum import Enum

from aegis_agent.agents.definitions import AgentDefinition
from aegis_agent.agents.messaging import AgentMessage, AgentTransport, MessageType
from aegis_agent.agents.runner import SubagentResult, SubagentRunner, SubagentStatus
from aegis_agent.models.base import Message
from aegis_agent.sessions.memory_store import InMemorySessionRepository
from aegis_agent.tools.registry import ToolContext

logger = logging.getLogger(__name__)


class TeammateStatus(str, Enum):
    """Lifecycle of a persistent teammate."""

    CREATED = "created"
    RUNNING = "running"
    IDLE = "idle"
    FAILED = "failed"
    STOPPED = "stopped"


class PersistentTeammate:
    """A long-lived teammate running on its own thread with its own transcript.

    Created by :class:`~aegis_agent.agents.team.TeamManager`; not constructed
    directly.  ``start`` launches the loop thread; ``stop`` signals shutdown and
    wakes the loop so it exits promptly.  Everything else is internal.
    """

    def __init__(
        self,
        *,
        name: str,
        team_id: str,
        definition: AgentDefinition,
        runner: SubagentRunner,
        transport: AgentTransport,
        lead_id: str,
        team_tools: list | None = None,
        on_idle=None,
        on_stopped=None,
    ) -> None:
        self.name = name
        self.team_id = team_id
        self.definition = definition
        self.agent_id = f"{team_id}/{name}-{uuid.uuid4().hex[:6]}"
        self.lead_id = lead_id
        self.session_id = f"team-{team_id}-{name}"
        self._runner = runner
        self._transport = transport
        # Tools bound to THIS teammate's identity (e.g. its own send_message),
        # merged into its registry on every turn.
        self._team_tools = list(team_tools or ())
        self._on_idle = on_idle          # callback(teammate, SubagentResult)
        self._on_stopped = on_stopped    # callback(teammate)
        # The single long-lived transcript for this teammate.
        self._repository = InMemorySessionRepository()
        self._stop = threading.Event()
        self._status = TeammateStatus.CREATED
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.last_error: str | None = None

    # -- introspection -----------------------------------------------------

    @property
    def status(self) -> TeammateStatus:
        with self._lock:
            return self._status

    def _set_status(self, status: TeammateStatus) -> None:
        with self._lock:
            self._status = status

    def transcript(self) -> list[Message]:
        """The teammate's full private transcript (seq order)."""
        try:
            return self._repository.list_messages(self.session_id)
        except Exception:  # noqa: BLE001 — session may not exist yet
            return []

    # -- lifecycle ---------------------------------------------------------

    def start(self, initial: AgentMessage | None = None) -> None:
        """Start the teammate loop on a daemon thread.

        ``initial`` (usually the lead's first task) is delivered as the first
        message before the loop begins waiting.
        """
        self._transport.reopen_recipient(self._address)  # type: ignore[attr-defined]
        if initial is not None:
            self._transport.send(initial)
        self._thread = threading.Thread(
            target=self._loop, name=f"aegis-teammate-{self.name}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal the teammate to stop and wake it so it exits promptly."""
        self._stop.set()
        self._transport.close_recipient(self._address)  # type: ignore[attr-defined]

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    @property
    def _address(self) -> str:
        """The transport address this teammate listens on (its name)."""
        return self.name

    # -- the loop ------------------------------------------------------------

    def _loop(self) -> None:
        """RUNNING → IDLE → wait → RUNNING → … until stopped.

        Each iteration takes the next message (blocking on the transport's
        event — no busy poll), runs ONE turn on the persistent session, then
        goes IDLE and notifies the lead.  A turn that raises marks the teammate
        FAILED but keeps the loop alive (one bad turn doesn't kill the team);
        only ``stop()`` (or a SHUTDOWN message) exits the loop.
        """
        while not self._stop.is_set():
            message = self._transport.receive(self._address, timeout=0.5)
            if message is None:
                # Timeout (still idle) or the inbox was closed (stop requested).
                continue
            if message.type is MessageType.SHUTDOWN:
                break

            self._set_status(TeammateStatus.RUNNING)
            result = self._run_turn(message)
            if result.status is SubagentStatus.FAILED:
                self.last_error = result.error
                self._set_status(TeammateStatus.FAILED)
            else:
                self._set_status(TeammateStatus.IDLE)
            if self._on_idle is not None:
                try:
                    self._on_idle(self, result)
                except Exception:
                    logger.warning("teammate %s idle callback failed", self.name, exc_info=True)

        self._set_status(TeammateStatus.STOPPED)
        if self._on_stopped is not None:
            try:
                self._on_stopped(self)
            except Exception:
                logger.warning("teammate %s stopped callback failed", self.name, exc_info=True)

    def _run_turn(self, message: AgentMessage) -> SubagentResult:
        """Run one turn on the persistent session for an inbound message."""
        prompt = message.render_for_recipient()
        context = ToolContext(is_cancelled=self._stop.is_set)
        try:
            return self._runner.run(
                self.definition,
                prompt,
                parent_context=context,
                cancel_event=self._stop,
                repository=self._repository,
                session_id=self.session_id,
                extra_tools=self._team_tools,
            )
        except Exception as exc:  # noqa: BLE001 — a failing turn must not crash the loop
            return SubagentResult(
                agent_type=self.definition.name,
                status=SubagentStatus.FAILED,
                error=f"{type(exc).__name__}: {exc}",
            )


__all__ = ["PersistentTeammate", "TeammateStatus"]
