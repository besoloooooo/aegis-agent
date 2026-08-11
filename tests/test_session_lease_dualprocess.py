"""Dual-process session-lease tests (ported from hermes tests/test_session_lease.py
commit 03e5adc, adapted to the Aegis store/lease API).

Each contender is a real OS process running tests/lease_worker.py against a
shared SQLite file — this exercises the lease under genuine cross-process
contention (separate connections, WAL, real kill -9), which in-process
fixtures cannot fully reproduce.
"""

from __future__ import annotations

import itertools
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from aegis_agent.sessions.lease import SessionLeaseManager, SQLiteSessionLeaseBackend
from aegis_agent.sessions.sqlite_store import SQLiteSessionRepository

WORKER = Path(__file__).parent / "lease_worker.py"


def _sid() -> str:
    return uuid.uuid4().hex[:12]


def _spawn_worker(db_path, session_id: str, mode: str, *args: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, str(WORKER), str(db_path), session_id, mode, *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _read_line(proc: subprocess.Popen, timeout: float = 15.0) -> str:
    """Read one stdout line with a deadline (a hung worker fails the test)."""
    lines: list[str] = []

    def _reader():
        line = proc.stdout.readline() if proc.stdout else ""
        if line:
            lines.append(line.strip())

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()
    reader.join(timeout)
    if lines:
        return lines[0]
    stderr = ""
    try:
        proc.kill()
        _, stderr = proc.communicate(timeout=5)
    except Exception:  # noqa: BLE001, S110 — 尽力收集诊断信息
        pass
    raise AssertionError(
        f"worker (pid {proc.pid}) produced no line in {timeout}s; "
        f"exit={proc.poll()}; stderr={stderr.strip()[:500]}"
    )


def test_dual_process_resume_single_winner(tmp_path) -> None:
    """Two processes resume the same session concurrently — one wins."""
    db_path = tmp_path / "state.db"
    SQLiteSessionRepository(db_path).close()  # create schema
    sid = _sid()

    holder = _spawn_worker(db_path, sid, "acquire-hold", "4", "2", "0.5")
    assert _read_line(holder) == "ACQUIRED"

    contender = _spawn_worker(db_path, sid, "contend", "2")
    out, _ = contender.communicate(timeout=15)
    assert "FAILED" in out

    holder.terminate()
    holder.wait(timeout=10)


def test_dual_process_parallel_different_sessions(tmp_path) -> None:
    db_path = tmp_path / "state.db"
    SQLiteSessionRepository(db_path).close()
    sid_a, sid_b = _sid(), _sid()

    holder = _spawn_worker(db_path, sid_a, "acquire-hold", "4", "2", "0.5")
    assert _read_line(holder) == "ACQUIRED"

    contender = _spawn_worker(db_path, sid_b, "contend", "2")
    out, _ = contender.communicate(timeout=15)
    assert "ACQUIRED" in out

    holder.terminate()
    holder.wait(timeout=10)


def test_dual_process_clean_exit_allows_immediate_takeover(tmp_path) -> None:
    db_path = tmp_path / "state.db"
    SQLiteSessionRepository(db_path).close()
    sid = _sid()

    holder = _spawn_worker(db_path, sid, "acquire-hold", "1.5", "30", "0.5")
    assert _read_line(holder) == "ACQUIRED"
    holder.wait(timeout=20)  # normal exit → stop() releases the lease
    assert holder.returncode == 0

    contender = _spawn_worker(db_path, sid, "contend", "30")
    out, _ = contender.communicate(timeout=15)
    assert "ACQUIRED" in out


def test_dual_process_kill9_ttl_takeover(tmp_path) -> None:
    """SIGKILL the holder; the contender wins once the TTL expires."""
    db_path = tmp_path / "state.db"
    SQLiteSessionRepository(db_path).close()
    sid = _sid()

    holder = _spawn_worker(db_path, sid, "acquire-hold", "60", "1.0", "0.2")
    assert _read_line(holder) == "ACQUIRED"
    holder.send_signal(signal.SIGKILL)
    holder.wait(timeout=10)

    time.sleep(1.3)  # wait out the TTL
    contender = _spawn_worker(db_path, sid, "contend", "30")
    out, _ = contender.communicate(timeout=15)
    assert "ACQUIRED" in out


def test_dual_process_contender_blocked_before_ttl(tmp_path) -> None:
    """A contender arriving BEFORE the kill-9 TTL expiry must be refused."""
    db_path = tmp_path / "state.db"
    SQLiteSessionRepository(db_path).close()
    sid = _sid()

    holder = _spawn_worker(db_path, sid, "acquire-hold", "60", "5", "1")
    assert _read_line(holder) == "ACQUIRED"
    holder.send_signal(signal.SIGKILL)
    holder.wait(timeout=10)

    contender = _spawn_worker(db_path, sid, "contend", "30")
    out, _ = contender.communicate(timeout=15)
    assert "FAILED" in out


def test_no_interleaved_history_after_lease_loss(tmp_path) -> None:
    """The losing instance stops writing; history never gets user/user or
    assistant/assistant adjacency from two live writers."""
    from aegis_agent.models.base import Message, Role

    db_path = tmp_path / "state.db"
    db = SQLiteSessionRepository(db_path)
    sid = _sid()
    db.create_session(sid)

    def _write_turn(tag: str, gate) -> int:
        """Append one user+assistant pair while gate() allows (breaker check)."""
        written = 0
        for role in (Role.USER, Role.ASSISTANT):
            if not gate():
                break
            db.append_message(sid, Message(role=role, content=f"{role.value} from {tag}"))
            written += 1
        return written

    # Instance A owns the session and writes a turn.
    mgr_a = SessionLeaseManager(
        SQLiteSessionLeaseBackend(db), ttl_s=0.3, renew_interval_s=0.1
    )
    assert mgr_a.acquire(sid)
    assert _write_turn("A", gate=lambda: mgr_a.active) == 2

    # A freezes (GC pause / SIGSTOP equivalent) — B takes over.
    mgr_a.stop()  # clean stand-in for "A lost ownership"
    db_b = SQLiteSessionRepository(db_path)
    mgr_b = SessionLeaseManager(
        SQLiteSessionLeaseBackend(db_b), ttl_s=5, renew_interval_s=0.1
    )
    assert mgr_b.acquire(sid)

    # A wakes up and tries to keep writing — its breaker blocks every row.
    mgr_a._lost = True  # what A's next renew would have discovered
    assert _write_turn("A", gate=lambda: mgr_a.active) == 0

    # B resumes legitimately and writes its turn.
    assert _write_turn("B", gate=lambda: mgr_b.active) == 2

    roles = [
        row["role"]
        for row in db._conn.execute(
            "SELECT role FROM messages WHERE session_id = ? ORDER BY seq", (sid,)
        ).fetchall()
    ]
    for prev, cur in itertools.pairwise(roles):
        assert not (prev == cur == "user"), f"user/user adjacency: {roles}"
        assert not (prev == cur == "assistant"), f"assistant/assistant adjacency: {roles}"
    mgr_b.stop()
    db.close()
    db_b.close()
