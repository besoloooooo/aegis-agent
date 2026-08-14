# Portions adapted from Hermes (hermes-agent), © 2025 Nous Research.
# Licensed under the MIT License. See THIRD_PARTY_NOTICES.md.
#
# ADAPT of Hermes ``tools/process_registry.py`` — the **local-only** subset.
# Kept: the in-memory ``_running``/``_finished`` registry model, ``ProcessSession``
# with a per-session rolling 200KB output buffer + daemon reader thread,
# ``spawn_local`` (``subprocess.Popen`` + ``os.setsid`` process group),
# the eight lifecycle actions (list/poll/log/wait/kill/write/submit/close),
# TTL + LRU pruning, the orphaned-pipe ``_reconcile_local_exit`` fix, and
# psutil / ``taskkill /T /F`` tree-kill.
# Dropped (Hermes coupling): sandbox backends (``spawn_via_env``), PTY via
# ptyprocess, watch-pattern rate limiting + global circuit breaker, gateway
# notification routing, crash-recovery checkpoint file, per-profile HOME
# isolation and provider-secret env scrubbing.  The shell wrappers
# (``[shell, -lic, "set +m; <cmd>"]``) and login-shell env handling are replaced
# with a plain ``/bin/sh -c`` / ``cmd /c`` invocation.
"""In-memory registry of managed background processes.

Tracks processes spawned via the ``terminal`` tool with ``background=true``:
output is buffered in a rolling 200KB window by a daemon reader thread, and the
registry exposes the eight lifecycle actions the ``process`` tool drives.  This
is deliberately a *local-only* port — processes always run on the host via
``subprocess.Popen``.

Everything here is a plain (host) subprocess; there is no sandbox abstraction.
"""

from __future__ import annotations

import logging
import os
import platform
import re
import signal
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from aegis_agent.exceptions import OperationCancelled

logger = logging.getLogger(__name__)

_IS_WINDOWS = platform.system() == "Windows"

# Limits
MAX_OUTPUT_CHARS = 200_000      # 200KB rolling output buffer
FINISHED_TTL_SECONDS = 1800     # Keep finished processes for 30 minutes
MAX_PROCESSES = 64              # Max tracked processes (LRU eviction of finished)
_WAIT_DEFAULT_TIMEOUT = 180     # Default/clamp ceiling for wait()


# ANSI/CSI escape sequences — stripped from output shown to the model.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _resolve_safe_cwd(cwd: str | None) -> str:
    """Return ``cwd`` if it exists as a directory, else the nearest existing ancestor.

    Guards against a deleted working directory wedging ``subprocess.Popen``.
    """
    if cwd and os.path.isdir(cwd):
        return cwd
    parent = os.path.dirname(cwd) if cwd else os.getcwd()
    while parent:
        if os.path.isdir(parent):
            return parent
        nxt = os.path.dirname(parent)
        if nxt == parent:
            break
        parent = nxt
    return os.getcwd()


def _spawn_argv(command: str) -> list[str]:
    """Build the argv used to run ``command`` through a shell."""
    if _IS_WINDOWS:
        return ["cmd", "/c", command]
    return ["/bin/sh", "-c", command]


@dataclass
class ProcessSession:
    """A tracked background process with output buffering."""

    id: str                                          # "proc_xxxxxxxxxxxx"
    command: str
    pid: int | None = None                           # OS process id
    process: subprocess.Popen | None = None          # local Popen handle
    cwd: str | None = None
    started_at: float = 0.0
    exited: bool = False
    exit_code: int | None = None                     # None while running
    output_buffer: str = ""                          # rolling (last MAX_OUTPUT_CHARS)
    max_output_chars: int = MAX_OUTPUT_CHARS
    notify_on_complete: bool = False                 # record a completion event on exit
    completed_notified: bool = False                 # completion already recorded
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _reader_thread: threading.Thread | None = field(default=None, repr=False)


class ProcessRegistry:
    """In-memory registry of background processes (local-only port)."""

    _SHELL_NOISE_SUBSTRINGS = (
        "cannot set terminal process group",
        "no job control in this shell",
        "tcsetattr: Inappropriate ioctl for device",
    )

    def __init__(self) -> None:
        self._running: dict[str, ProcessSession] = {}
        self._finished: dict[str, ProcessSession] = {}
        self._lock = threading.Lock()

    # ----- spawn -----------------------------------------------------------

    def spawn_local(
        self,
        command: str,
        cwd: str | None = None,
        env_vars: dict | None = None,
        notify_on_complete: bool = False,
    ) -> ProcessSession:
        """Spawn a background process on the host and start its reader thread."""
        session = ProcessSession(
            id=f"proc_{uuid.uuid4().hex[:12]}",
            command=command,
            cwd=_resolve_safe_cwd(cwd),
            started_at=time.time(),
            notify_on_complete=notify_on_complete,
        )

        env = dict(os.environ)
        env.update(env_vars or {})
        # Force unbuffered output so progress is visible during background runs.
        env["PYTHONUNBUFFERED"] = "1"

        popen_kwargs: dict = {}
        if _IS_WINDOWS:
            popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            popen_kwargs["preexec_fn"] = os.setsid  # new process group → tree-kill

        proc = subprocess.Popen(
            _spawn_argv(command),
            text=True,
            cwd=session.cwd,
            env=env,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE,  # keep a pipe so write/submit/close work
            **popen_kwargs,
        )
        session.process = proc
        session.pid = proc.pid

        try:
            reader = threading.Thread(
                target=self._reader_loop,
                args=(session,),
                daemon=True,
                name=f"proc-reader-{session.id}",
            )
            session._reader_thread = reader
            reader.start()

            with self._lock:
                self._prune_if_needed()
                self._running[session.id] = session
        except Exception:
            # Post-Popen setup failed — kill the orphan before re-raising.
            self._kill_popen_tree(proc)
            try:
                proc.wait(timeout=5)
            except (subprocess.TimeoutExpired, OSError):
                pass
            raise

        return session

    # ----- reader / lifecycle ----------------------------------------------

    def _reader_loop(self, session: ProcessSession) -> None:
        """Daemon thread: drain the child stdout into the rolling buffer, then reap."""
        first_chunk = True
        try:
            while True:
                chunk = session.process.stdout.read(4096) if session.process and session.process.stdout else ""
                if not chunk:
                    break
                if first_chunk:
                    chunk = self._clean_shell_noise(chunk)
                    first_chunk = False
                with session._lock:
                    session.output_buffer += chunk
                    if len(session.output_buffer) > session.max_output_chars:
                        session.output_buffer = session.output_buffer[-session.max_output_chars:]
        except Exception as exc:  # noqa: BLE001 — reader must never kill the process
            logger.debug("Process stdout reader ended: %s", exc)
        finally:
            try:
                if session.process:
                    session.process.wait(timeout=5)
            except (subprocess.TimeoutExpired, OSError) as exc:
                logger.debug("Process wait timed out/failed: %s", exc)
            session.exited = True
            if session.process:
                session.exit_code = session.process.returncode
            self._move_to_finished(session)

    @staticmethod
    def _clean_shell_noise(text: str) -> str:
        """Strip shell-startup warnings from the beginning of output."""
        lines = text.split("\n")
        while lines and any(n in lines[0] for n in ProcessRegistry._SHELL_NOISE_SUBSTRINGS):
            lines.pop(0)
        return "\n".join(lines)

    def _move_to_finished(self, session: ProcessSession) -> None:
        """Move a session from running to finished (idempotent)."""
        with self._lock:
            was_running = self._running.pop(session.id, None) is not None
            self._finished[session.id] = session
        if was_running and session.notify_on_complete and not session.completed_notified:
            session.completed_notified = True

    def _reconcile_local_exit(self, session: ProcessSession | None) -> None:
        """Flip ``exited`` when the direct child exited but the reader is stuck.

        If a descendant holds the stdout pipe open, the reader's blocking
        ``read()`` never sees EOF and poll() would report "running" forever.
        When the direct child's ``poll()`` reports an exit, drain what's readable
        and mark the session exited.
        """
        if session is None or session.exited:
            return
        proc = session.process
        if proc is None:
            return
        try:
            rc = proc.poll()
        except OSError:
            return
        if rc is None:
            return  # direct child genuinely still running

        drained = ""
        stdout = proc.stdout
        if stdout is not None and not _IS_WINDOWS:
            try:
                import fcntl
                fd = stdout.fileno()
                flags = fcntl.fcntl(fd, fcntl.F_GETFL)
                fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
                try:
                    chunk = stdout.read()
                    if chunk:
                        drained = chunk if isinstance(chunk, str) else chunk.decode("utf-8", "replace")
                except (BlockingIOError, OSError, ValueError):
                    pass
                finally:
                    try:
                        fcntl.fcntl(fd, fcntl.F_SETFL, flags)
                    except OSError:
                        pass
            except OSError as exc:
                logger.debug("Non-blocking drain failed for %s: %s", session.id, exc)

        with session._lock:
            if drained:
                session.output_buffer += drained
                if len(session.output_buffer) > session.max_output_chars:
                    session.output_buffer = session.output_buffer[-session.max_output_chars:]
            session.exited = True
            session.exit_code = rc
        self._move_to_finished(session)

    def get(self, session_id: str) -> ProcessSession | None:
        """Get a session by id (running or finished)."""
        with self._lock:
            return self._running.get(session_id) or self._finished.get(session_id)

    # ----- actions ----------------------------------------------------------

    def list_sessions(self) -> list[dict]:
        """List all running and recently-finished processes."""
        with self._lock:
            all_sessions = list(self._running.values()) + list(self._finished.values())
        result = []
        for s in all_sessions:
            entry = {
                "session_id": s.id,
                "command": s.command[:200],
                "cwd": s.cwd,
                "pid": s.pid,
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(s.started_at)),
                "uptime_seconds": int(time.time() - s.started_at),
                "status": "exited" if s.exited else "running",
                "output_preview": s.output_buffer[-200:] if s.output_buffer else "",
            }
            if s.exited:
                entry["exit_code"] = s.exit_code
            result.append(entry)
        return result

    def poll(self, session_id: str) -> dict:
        """Check status and get a short output preview."""
        session = self.get(session_id)
        if session is None:
            return {"status": "not_found", "error": f"No process with ID {session_id}"}
        self._reconcile_local_exit(session)
        with session._lock:
            preview = _strip_ansi(session.output_buffer[-1000:]) if session.output_buffer else ""
        result = {
            "session_id": session.id,
            "command": session.command,
            "status": "exited" if session.exited else "running",
            "pid": session.pid,
            "uptime_seconds": int(time.time() - session.started_at),
            "output_preview": preview,
        }
        if session.exited:
            result["exit_code"] = session.exit_code
        return result

    def read_log(self, session_id: str, offset: int = 0, limit: int = 200) -> dict:
        """Read the buffered output, paginated by lines."""
        session = self.get(session_id)
        if session is None:
            return {"status": "not_found", "error": f"No process with ID {session_id}"}
        with session._lock:
            full_output = _strip_ansi(session.output_buffer)
        lines = full_output.splitlines()
        total_lines = len(lines)
        selected = lines[-limit:] if (offset == 0 and limit > 0) else lines[offset:offset + limit]
        return {
            "session_id": session.id,
            "status": "exited" if session.exited else "running",
            "output": "\n".join(selected),
            "total_lines": total_lines,
            "showing": f"{len(selected)} lines",
        }

    def wait(
        self,
        session_id: str,
        timeout: int | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> dict:
        """Block until the process exits, times out, or the clamp is hit.

        ``is_cancelled`` (when set) is polled each tick; if it fires,
        :class:`~aegis_agent.exceptions.OperationCancelled` is raised so the
        caller can stop waiting immediately instead of blocking to the clamp.
        """
        requested = timeout
        timeout_note = None
        if requested and requested > _WAIT_DEFAULT_TIMEOUT:
            effective = _WAIT_DEFAULT_TIMEOUT
            timeout_note = f"Requested wait of {requested}s was clamped to {_WAIT_DEFAULT_TIMEOUT}s"
        else:
            effective = requested or _WAIT_DEFAULT_TIMEOUT

        session = self.get(session_id)
        if session is None:
            return {"status": "not_found", "error": f"No process with ID {session_id}"}

        deadline = time.monotonic() + effective
        while time.monotonic() < deadline:
            if is_cancelled is not None and is_cancelled():
                raise OperationCancelled("process wait cancelled by interrupt")
            self._reconcile_local_exit(session)
            if session.exited:
                result = {
                    "status": "exited",
                    "exit_code": session.exit_code,
                    "output": _strip_ansi(session.output_buffer[-2000:]),
                }
                if timeout_note:
                    result["timeout_note"] = timeout_note
                return result
            time.sleep(0.2)

        result = {"status": "timeout", "output": _strip_ansi(session.output_buffer[-1000:])}
        result["timeout_note"] = timeout_note or f"Waited {effective}s, process still running"
        return result

    def kill_process(self, session_id: str) -> dict:
        """Kill a background process (process tree)."""
        session = self.get(session_id)
        if session is None:
            return {"status": "not_found", "error": f"No process with ID {session_id}"}
        if session.exited:
            return {"status": "already_exited", "exit_code": session.exit_code}

        try:
            if session.process is None:
                return {"status": "error", "error": "No process handle available to kill"}
            self._kill_popen_tree(session.process)
            session.exited = True
            session.exit_code = -15  # SIGTERM
            self._move_to_finished(session)
            return {"status": "killed", "session_id": session.id}
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "error": str(exc)}

    @staticmethod
    def _kill_popen_tree(proc: subprocess.Popen) -> None:
        """Terminate a local process and its children."""
        if _IS_WINDOWS:
            try:
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                    capture_output=True, timeout=10, check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                proc.kill()
            return
        # POSIX: kill the whole process group (spawned via os.setsid).
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.terminate()
            except OSError:
                proc.kill()

    def write_stdin(self, session_id: str, data: str) -> dict:
        """Send raw data to a running process's stdin (no newline appended)."""
        session = self.get(session_id)
        if session is None:
            return {"status": "not_found", "error": f"No process with ID {session_id}"}
        if session.exited:
            return {"status": "already_exited", "error": "Process has already finished"}
        if not session.process or not session.process.stdin:
            return {"status": "error", "error": "Process stdin not available (closed)"}
        try:
            session.process.stdin.write(data)
            session.process.stdin.flush()
            return {"status": "ok", "bytes_written": len(data)}
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "error": str(exc)}

    def submit_stdin(self, session_id: str, data: str = "") -> dict:
        """Send data + newline (like pressing Enter)."""
        return self.write_stdin(session_id, data + "\n")

    def close_stdin(self, session_id: str) -> dict:
        """Close the process's stdin / send EOF without killing it."""
        session = self.get(session_id)
        if session is None:
            return {"status": "not_found", "error": f"No process with ID {session_id}"}
        if session.exited:
            return {"status": "already_exited", "error": "Process has already finished"}
        if not session.process or not session.process.stdin:
            return {"status": "error", "error": "Process stdin not available (closed)"}
        try:
            session.process.stdin.close()
            return {"status": "ok", "message": "stdin closed"}
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "error": str(exc)}

    # ----- cleanup ----------------------------------------------------------

    def _prune_if_needed(self) -> None:
        """Drop expired / oldest finished sessions over the cap. Must hold ``_lock``."""
        now = time.time()
        expired = [
            sid for sid, s in self._finished.items()
            if (now - s.started_at) > FINISHED_TTL_SECONDS
        ]
        for sid in expired:
            del self._finished[sid]
        total = len(self._running) + len(self._finished)
        if total >= MAX_PROCESSES and self._finished:
            oldest = min(self._finished, key=lambda sid: self._finished[sid].started_at)
            del self._finished[oldest]


__all__ = [
    "FINISHED_TTL_SECONDS",
    "MAX_OUTPUT_CHARS",
    "MAX_PROCESSES",
    "ProcessRegistry",
    "ProcessSession",
]
