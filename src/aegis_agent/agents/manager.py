"""Subagent task manager — lifecycle, background execution and notification.

This is the Aegis analogue of Claude Code's ``LocalAgentTask`` +
``registerAsyncAgent`` + ``enqueueAgentNotification``, adapted to Aegis's
in-process, synchronous-loop architecture (no async event loop; background work
runs on threads).

Responsibilities:

* **Task state** — every spawned subagent (foreground or background) becomes a
  :class:`SubagentTask` with a lifecycle ``running → completed/failed/killed``.
* **Background execution** — ``run_in_background=True`` runs the subagent on a
  daemon thread and returns immediately, so the Main Agent's turn is not
  blocked.
* **Completion notification (no polling)** — when a background subagent
  finishes, a notification is pushed onto a queue.  The Main Agent's runtime
  drains that queue **between turns** and the CLI feeds the notification back
  in as the next input, so the model learns of completions without any polling.
* **Limits** — a concurrency cap on simultaneously-running subagents and a
  nesting-depth guard (so a fork/nested chain cannot recurse forever).

Notifications are idempotent: each task notifies at most once (its
``notified`` flag is set atomically before enqueueing), mirroring Claude's
guard against duplicate task-notifications.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from enum import Enum

from aegis_agent.agents.definitions import AgentDefinition
from aegis_agent.agents.runner import SubagentResult, SubagentRunner, SubagentStatus
from aegis_agent.models.base import Message
from aegis_agent.tools.registry import ToolContext

#: Hard cap on concurrently-RUNNING subagents (foreground + background).
DEFAULT_MAX_CONCURRENT = 8

#: Maximum subagent nesting depth.  Depth 0 = spawned by the Main Agent.  A
#: definition with ``allow_agent_tool=True`` may spawn children only while the
#: running depth is below this cap; at the cap the Agent tool is withheld.
DEFAULT_MAX_DEPTH = 1


class TaskStatus(str, Enum):
    """Lifecycle of a subagent task (mirrors Claude's running→terminal set)."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    KILLED = "killed"


_TERMINAL = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.KILLED}


@dataclass
class SubagentTask:
    """Book-keeping record for one spawned subagent.

    ``agent_id`` / ``parent_agent_id`` / ``depth`` form an explicit lineage
    chain (replacing the earlier ``threading.local`` depth, which broke across
    background threads): the Main Agent is ``agent_id="main"`` at depth 0, each
    spawned subagent records who spawned it and one deeper level.
    """

    task_id: str
    agent_type: str
    description: str
    depth: int
    background: bool
    agent_id: str = ""
    parent_agent_id: str = "main"
    status: TaskStatus = TaskStatus.RUNNING
    result: SubagentResult | None = None
    error: str | None = None
    notified: bool = False
    cancel: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = field(default=None, repr=False)


@dataclass
class TaskNotification:
    """A completed/failed/killed background subagent, ready to tell the parent."""

    task_id: str
    agent_type: str
    description: str
    status: TaskStatus
    output: str = ""
    error: str | None = None

    def render(self) -> str:
        """Render the notification text injected back into the Main turn."""
        if self.status is TaskStatus.COMPLETED:
            body = self.output or "(no output)"
            return (
                f"[subagent task {self.task_id} ({self.agent_type}) completed] "
                f"{self.description}\n\n{body}"
            )
        reason = self.error or "unknown error"
        verb = "failed" if self.status is TaskStatus.FAILED else "was stopped"
        return (
            f"[subagent task {self.task_id} ({self.agent_type}) {verb}] "
            f"{self.description}\n\nerror: {reason}"
        )


class SubagentManager:
    """Owns subagent task state, background threads, limits and notifications."""

    def __init__(
        self,
        runner: SubagentRunner,
        agents: dict[str, AgentDefinition],
        *,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT,
        max_depth: int = DEFAULT_MAX_DEPTH,
    ) -> None:
        self._runner = runner
        self._agents = dict(agents)
        self._max_concurrent = max_concurrent
        self._max_depth = max_depth
        self._lock = threading.Lock()
        self._tasks: dict[str, SubagentTask] = {}
        self._notifications: list[TaskNotification] = []

    # -- introspection -----------------------------------------------------

    @property
    def agents(self) -> dict[str, AgentDefinition]:
        return dict(self._agents)

    def tasks(self) -> list[SubagentTask]:
        """Snapshot of all tasks (most recent first), for ``/agents``."""
        with self._lock:
            return list(reversed(list(self._tasks.values())))

    def running_count(self) -> int:
        with self._lock:
            return sum(1 for t in self._tasks.values() if t.status is TaskStatus.RUNNING)

    # -- spawning ----------------------------------------------------------

    def spawn(
        self,
        definition: AgentDefinition,
        prompt: str,
        *,
        background: bool = False,
        parent_context: ToolContext | None = None,
        parent_messages: list[Message] | None = None,
        description: str | None = None,
        parent_agent_id: str = "main",
        parent_depth: int = 0,
    ) -> SubagentResult | SubagentTask:
        """Spawn a subagent.

        Foreground (default): runs to completion on the calling thread and
        returns the :class:`SubagentResult`.  Background: starts a daemon thread
        and returns the :class:`SubagentTask` immediately; completion is
        delivered via :meth:`drain_notifications`.

        ``parent_agent_id`` / ``parent_depth`` describe the *spawning* agent so
        the child's lineage is explicit (thread-safe across background threads).
        The Main Agent spawns at depth 0; a nested subagent passes its own
        id/depth.

        Enforces the concurrency cap and nesting-depth guard; a violation
        returns a FAILED :class:`SubagentResult` rather than raising, so the
        calling agent loop keeps running.
        """
        depth = parent_depth + 1
        if depth > self._max_depth:
            return SubagentResult(
                agent_type=definition.name,
                status=SubagentStatus.FAILED,
                error=(
                    f"maximum subagent nesting depth ({self._max_depth}) exceeded; "
                    "this subagent may not spawn further subagents."
                ),
            )

        task_id = f"task-{uuid.uuid4().hex[:8]}"
        task = SubagentTask(
            task_id=task_id,
            agent_type=definition.name,
            description=description or _short(prompt),
            depth=depth,
            background=background,
            agent_id=task_id,
            parent_agent_id=parent_agent_id,
        )

        parent_cancel = parent_context.is_cancelled if parent_context is not None else None
        child_cancel_event = task.cancel
        child_ctx = None
        if parent_context is not None:
            import dataclasses

            def _is_cancelled() -> bool:
                if child_cancel_event.is_set():
                    return True
                return parent_cancel is not None and parent_cancel()

            child_ctx = dataclasses.replace(parent_context, is_cancelled=_is_cancelled)

        with self._lock:
            if sum(1 for t in self._tasks.values() if t.status is TaskStatus.RUNNING) >= self._max_concurrent:
                return SubagentResult(
                    agent_type=definition.name,
                    status=SubagentStatus.FAILED,
                    error=(
                        f"subagent concurrency limit ({self._max_concurrent}) reached; "
                        "wait for a running subagent to finish."
                    ),
                )
            self._tasks[task_id] = task

        if background:
            thread = threading.Thread(
                target=self._run_and_notify,
                args=(task, definition, prompt, child_ctx, parent_messages),
                name=f"aegis-subagent-{task_id}",
                daemon=True,
            )
            task.thread = thread
            thread.start()
            return task

        # Foreground: run inline; no notification (the result returns directly).
        result = self._execute(task, definition, prompt, child_ctx, parent_messages)
        return result

    # -- execution ---------------------------------------------------------

    def _execute(
        self,
        task: SubagentTask,
        definition: AgentDefinition,
        prompt: str,
        child_ctx: ToolContext | None,
        parent_messages: list[Message] | None,
    ) -> SubagentResult:
        """Run the subagent (shared by foreground and background paths).

        The task's ``cancel`` event is passed straight to the runner so a kill
        actually aborts the in-flight model call / tool execution, not just the
        recorded status.
        """
        try:
            result = self._runner.run(
                definition,
                prompt,
                parent_context=child_ctx,
                parent_messages=parent_messages,
                cancel_event=task.cancel,
            )
        except Exception as exc:  # noqa: BLE001 — never let a subagent crash the parent
            result = SubagentResult(
                agent_type=definition.name,
                status=SubagentStatus.FAILED,
                error=f"{type(exc).__name__}: {exc}",
            )

        if task.cancel.is_set() and result.status is not SubagentStatus.COMPLETED:
            result.status = SubagentStatus.KILLED
            if result.error is None:
                result.error = "subagent cancelled"

        with self._lock:
            # A task killed mid-run is already marked KILLED by ``kill()``; a
            # body that raced to completion must not resurrect it to COMPLETED.
            if task.status is not TaskStatus.KILLED:
                task.status = _to_task_status(result.status)
            task.result = result
            task.error = result.error
        return result

    def _run_and_notify(self, task, definition, prompt, child_ctx, parent_messages) -> None:
        result = self._execute(task, definition, prompt, child_ctx, parent_messages)
        self._enqueue_notification(task, result)

    # -- notification ------------------------------------------------------

    def _enqueue_notification(self, task: SubagentTask, result: SubagentResult) -> None:
        """Queue a completion notification exactly once per task."""
        with self._lock:
            if task.notified:
                return
            task.notified = True
            self._notifications.append(
                TaskNotification(
                    task_id=task.task_id,
                    agent_type=task.agent_type,
                    description=task.description,
                    status=task.status,
                    output=result.output,
                    error=result.error,
                )
            )

    def drain_notifications(self) -> list[TaskNotification]:
        """Pop all pending notifications (called by the runtime between turns).

        Returns them oldest-first.  The Main Agent's runtime calls this after
        each turn so the CLI can feed completions back in as the next input —
        the push model that avoids polling.
        """
        with self._lock:
            drained = self._notifications
            self._notifications = []
            return drained

    def has_pending_notifications(self) -> bool:
        with self._lock:
            return bool(self._notifications)

    # -- control -----------------------------------------------------------

    def kill(self, task_id: str) -> bool:
        """Request cancellation of a running task.  Returns False if unknown/done.

        Sets the task's cancel event (propagated to the child runtime's
        cooperative-cancel callback when a parent context is present) AND marks
        the task KILLED immediately, mirroring Claude's ``killAsyncAgent`` which
        transitions status on the running task rather than waiting for the loop
        to unwind.  A body that later completes cannot overwrite the KILLED
        status (see ``_execute``).
        """
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.status in _TERMINAL:
                return False
            task.cancel.set()
            task.status = TaskStatus.KILLED
            return True


def _to_task_status(status: SubagentStatus) -> TaskStatus:
    return {
        SubagentStatus.COMPLETED: TaskStatus.COMPLETED,
        SubagentStatus.FAILED: TaskStatus.FAILED,
        SubagentStatus.KILLED: TaskStatus.KILLED,
    }[status]


def _short(text: str, limit: int = 60) -> str:
    one_line = " ".join(text.split())
    return one_line if len(one_line) <= limit else one_line[: limit - 1] + "…"


__all__ = [
    "DEFAULT_MAX_CONCURRENT",
    "DEFAULT_MAX_DEPTH",
    "SubagentManager",
    "SubagentTask",
    "TaskNotification",
    "TaskStatus",
]
