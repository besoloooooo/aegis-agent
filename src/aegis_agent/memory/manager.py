# Behavioural reference (adapted and simplified):
#   * Claude Code Auto Memory — the manager wires the two write/read channels
#     around a turn: relevance recall *prefetched* before the model answers
#     (``query.ts`` ``startRelevantMemoryPrefetch`` → collect point) and
#     background extraction *fire-and-forget* after the final reply
#     (``stopHooks.ts`` → ``extractMemories``).  See
#     ``Claude-Code/docs/08-memory.md``.
#
# Aegis differences (see the development log):
#   * Recall injection is via a stateful ``PromptContributor`` instead of an
#     attachment message — the derived context rebuilds the system prompt each
#     call, so the contributor is set at the collect point and read on the next
#     build.  Nothing in ``run_turn``'s loop body changes; the loop just adds a
#     non-blocking collect point.
#   * Extraction runs on a single background worker thread, serialised by a
#     queue (mirroring Claude's stash queue), and a ``drain()`` lets the CLI wait
#     for in-flight work before exit.  Extraction never blocks a turn.
"""Coordinate recall (pre-turn) and extraction (post-turn) for one runtime.

:class:`MemoryManager` owns the per-session recall/extract state — the
``already_surfaced`` dedup set, the extraction cursor, and the pending recall
future — and exposes the hooks the runtime calls:

* :meth:`before_turn` — *start* a background recall for the query (non-blocking).
* :meth:`collect_recall` — a non-blocking collect point the runtime calls before
  each model request: if the recall finished, inject it; otherwise skip (aligning
  Claude Code's "didn't make it in time → skip" semantics).
* :meth:`after_turn` — enqueue a background extraction for after the final reply.
* :meth:`drain` — wait for in-flight recall/extract work to finish (CLI exit).

Both channels are best-effort: any failure is caught, recorded as a telemetry
event, and never allowed to break the main turn.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from aegis_agent.memory.extractor import (
    ExtractionResult,
    apply_actions,
    extract_memories,
)
from aegis_agent.memory.paths import memory_dir
from aegis_agent.memory.prompt import RelevantMemoriesContributor
from aegis_agent.memory.retriever import (
    RecallResult,
    recall_memories,
    render_recall_block,
)
from aegis_agent.models.base import Message, ModelProvider, ToolCall

logger = logging.getLogger(__name__)

#: Tool names whose ``path`` argument can land a write inside the memory dir.
_WRITE_TOOL_NAMES = frozenset({"write_file", "patch"})


@dataclass
class MemoryEvent:
    """A lightweight observability event (``memory.recall`` / ``memory.extract``)."""

    kind: str  # "memory.recall" | "memory.extract"
    candidate_count: int = 0
    selected_count: int = 0
    selected_files: list[str] = field(default_factory=list)
    reviewed_messages: int = 0
    extract_action_count: int = 0
    applied_files: list[str] = field(default_factory=list)
    skipped: bool = False
    failure_reason: str | None = None


class _SessionState:
    """Per-session recall/extract bookkeeping."""

    def __init__(self) -> None:
        self.already_surfaced: set[str] = set()
        self.cursor: str | None = None  # last extracted message client_msg_id
        self.pending_recall: Future[RecallResult] | None = None


@dataclass
class _ExtractTask:
    """One queued extraction job (a snapshot of a finished turn)."""

    session_id: str
    messages: list[Message]
    tool_calls: list[ToolCall]


class MemoryManager:
    """Wire recall and extraction around a runtime's turns.

    Parameters
    ----------
    contributor:
        The :class:`RelevantMemoriesContributor` already registered on the
        system-prompt builder; recall sets its block at the collect point, and it
        is cleared after the turn so recall stays per-turn.
    recall_provider / extract_provider:
        The (usually cheaper) providers for the side queries.  Either may be
        ``None`` to disable that channel.
    home:
        The memory home; ``None`` uses the default (``$AEGIS_HOME``/``~/.aegis``).
    on_event:
        Optional telemetry sink invoked with :class:`MemoryEvent` objects.
    """

    def __init__(
        self,
        contributor: RelevantMemoriesContributor,
        *,
        recall_provider: ModelProvider | None = None,
        extract_provider: ModelProvider | None = None,
        home: str | None = None,
        on_event: Callable[[MemoryEvent], None] | None = None,
    ) -> None:
        self._contributor = contributor
        self._recall_provider = recall_provider
        self._extract_provider = extract_provider
        self._home = home
        self._on_event = on_event
        self._sessions: dict[str, _SessionState] = {}
        self._sessions_lock = threading.Lock()

        # Recall runs on a small pool; extraction runs on ONE worker thread so
        # writes are serialised (mirrors Claude's stash queue — no file lock is
        # needed because only one extraction mutates the memory dir at a time).
        self._recall_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="aegis-recall")
        self._extract_queue: queue.Queue[_ExtractTask | None] = queue.Queue()
        self._extract_thread: threading.Thread | None = None

    # -- state ------------------------------------------------------------

    def _state(self, session_id: str) -> _SessionState:
        with self._sessions_lock:
            st = self._sessions.get(session_id)
            if st is None:
                st = _SessionState()
                self._sessions[session_id] = st
            return st

    def _emit(self, event: MemoryEvent) -> None:
        if self._on_event is not None:
            try:
                self._on_event(event)
            except Exception:
                logger.debug("memory event sink raised", exc_info=True)

    # -- recall (pre-turn) ------------------------------------------------

    def before_turn(self, session_id: str, user_query: str) -> None:
        """Start a background recall for ``user_query``; return immediately.

        Clears any injected block first, then (if a recall provider is
        configured) submits the recall to the pool and stores its future.  The
        result is applied by :meth:`collect_recall` once it finishes — if it is
        not done by the next model request it is skipped (Claude Code's
        "didn't make it in time → skip").
        """
        self._contributor.clear()
        if self._recall_provider is None:
            return
        state = self._state(session_id)
        # Snapshot the dedup set so the background thread never races the main
        # thread's later writes to ``already_surfaced``.
        surfaced = set(state.already_surfaced)
        try:
            future = self._recall_executor.submit(
                self._run_recall, user_query, surfaced
            )
            state.pending_recall = future
        except Exception:
            logger.debug("memory recall submit failed", exc_info=True)

    def _run_recall(self, user_query: str, surfaced: set[str]) -> RecallResult:
        try:
            return recall_memories(
                self._recall_provider,  # type: ignore[arg-type]  # provider checked by caller
                user_query,
                self._home,
                already_surfaced=surfaced,
            )
        except Exception:
            logger.debug("memory recall failed", exc_info=True)
            return RecallResult(failure_reason="recall_error")

    def collect_recall(self, session_id: str) -> None:
        """Non-blocking collect point: inject the recall if it has finished.

        Called before each model request.  If the pending recall is still
        running, do nothing (skip this turn's injection).  Once done, render the
        block, set the contributor, and record the surfaced filenames.
        """
        state = self._state(session_id)
        future = state.pending_recall
        if future is None:
            return
        if not future.done():
            return  # still running — skip, never block the main loop
        state.pending_recall = None
        try:
            result = future.result()
        except Exception:  # noqa: BLE001
            return
        if result is None or not result.memories:
            if result is not None and result.failure_reason:
                self._emit(
                    MemoryEvent(
                        kind="memory.recall",
                        candidate_count=result.candidate_count,
                        failure_reason=result.failure_reason,
                    )
                )
            return
        self._contributor.set_block(render_recall_block(result.memories))
        for m in result.memories:
            state.already_surfaced.add(m.filename)
        self._emit(
            MemoryEvent(
                kind="memory.recall",
                candidate_count=result.candidate_count,
                selected_count=result.selected_count,
                selected_files=result.selected_files,
                failure_reason=result.failure_reason,
            )
        )

    def note_surfaced(self, session_id: str, filenames: Sequence[str]) -> None:
        """Record memories the main agent read itself so recall won't re-inject.

        Best-effort dedup channel #2 (in addition to ``already_surfaced`` for
        auto-recalled files): callers that can detect the main agent reading a
        memory file directly may pass its basename here.
        """
        state = self._state(session_id)
        for f in filenames:
            state.already_surfaced.add(Path(f).name)

    # -- extraction (post-turn) -------------------------------------------

    def after_turn(
        self,
        session_id: str,
        messages: Sequence[Message],
        *,
        tool_calls: Sequence[ToolCall] = (),
        extract: bool = True,
    ) -> None:
        """Enqueue a background extraction; return immediately (fire-and-forget).

        Always clears the per-turn recall block first (so it never leaks into
        the next turn).  When ``extract`` is False (interrupted / errored turn)
        it stops there — a partial turn is not mined for memory.  Otherwise the
        turn is snapshotted onto the serial extraction queue; the worker runs it
        asynchronously.
        """
        self._contributor.clear()  # recall is per-turn; never leak into the next
        if self._extract_provider is None or not extract:
            return
        self._ensure_extract_worker()
        self._extract_queue.put(
            _ExtractTask(session_id=session_id, messages=list(messages), tool_calls=list(tool_calls))
        )

    def drain(self, timeout: float | None = None) -> None:
        """Wait for in-flight recall + extraction work to finish (CLI exit).

        Stops the extraction worker after the queue empties (a ``None`` sentinel
        is processed last), then shuts down the recall pool.  Extraction work
        submitted after a drain restarts the worker lazily.
        """
        # Signal the worker to stop once the queue is empty, then join.
        if self._extract_thread is not None and self._extract_thread.is_alive():
            self._extract_queue.put(None)
            self._extract_thread.join(timeout)
            self._extract_thread = None
        self._recall_executor.shutdown(wait=True, cancel_futures=True)

    # -- extraction worker -------------------------------------------------

    def _ensure_extract_worker(self) -> None:
        if self._extract_thread is not None and self._extract_thread.is_alive():
            return
        thread = threading.Thread(target=self._extract_worker, name="aegis-extract", daemon=True)
        self._extract_thread = thread
        thread.start()

    def _extract_worker(self) -> None:
        """Single worker: pull tasks and run them serially (one at a time)."""
        while True:
            task = self._extract_queue.get()
            try:
                if task is None:  # sentinel → stop
                    return
                try:
                    self._run_extraction(task)
                except Exception:
                    logger.debug("memory extraction failed", exc_info=True)
            finally:
                self._extract_queue.task_done()

    def _run_extraction(self, task: _ExtractTask) -> None:
        state = self._state(task.session_id)
        if self._main_agent_wrote_memory(task.tool_calls):
            # Mutex (mirrors Claude Code): the main agent already wrote a memory
            # file this turn — skip, but still advance the cursor.
            self._advance_cursor(state, task.messages)
            self._emit(MemoryEvent(kind="memory.extract", skipped=True))
            return

        result: ExtractionResult = extract_memories(
            self._extract_provider,  # type: ignore[arg-type]  # checked by caller
            task.messages,
            state.cursor,
            self._home,
        )
        applied: list[str] = []
        if result.actions:
            applied = apply_actions(result.actions, self._home)
        self._advance_cursor(state, task.messages)
        self._emit(
            MemoryEvent(
                kind="memory.extract",
                reviewed_messages=result.reviewed_messages,
                extract_action_count=len(result.actions),
                applied_files=applied,
                failure_reason=result.failure_reason,
            )
        )

    # -- helpers ----------------------------------------------------------

    def _advance_cursor(self, state: _SessionState, messages: Sequence[Message]) -> None:
        for m in reversed(messages):
            if m.client_msg_id:
                state.cursor = m.client_msg_id
                return

    def _main_agent_wrote_memory(self, tool_calls: Sequence[ToolCall]) -> bool:
        """True if any tool call in the turn wrote into the memory directory."""
        if not tool_calls:
            return False
        try:
            mem_root = memory_dir(self._home).resolve()
        except OSError:
            return False
        for tc in tool_calls:
            if tc.name not in _WRITE_TOOL_NAMES:
                continue
            try:
                args = tc.parsed_arguments()
            except TypeError:
                continue
            path_arg = args.get("path")
            if not isinstance(path_arg, str) or not path_arg:
                continue
            try:
                target = Path(path_arg).expanduser().resolve()
            except OSError:
                continue
            if target == mem_root or mem_root in target.parents:
                return True
        return False


__all__ = ["MemoryEvent", "MemoryManager"]
