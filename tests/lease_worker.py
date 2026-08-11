"""Subprocess worker for dual-process session-lease tests.

Usage:
    python tests/lease_worker.py DB_PATH SESSION_ID MODE [ARGS...]

Modes:
    acquire-hold SECONDS TTL RENEW
        Acquire the lease (with heartbeat manager), print ACQUIRED or
        FAILED, hold for SECONDS (heartbeat keeps it alive), then exit
        (releasing via manager.stop()).
    contend TTL
        One-shot acquire attempt with the given TTL.  Print ACQUIRED or
        FAILED and exit (releasing immediately if acquired).

All output goes to stdout, one line per event, flushed immediately.

Ported from hermes-agent tests/lease_worker.py (commit 03e5adc), adapted to
the Aegis session store / lease package.
"""

import sys
import time

from aegis_agent.sessions.lease import (
    SessionLeaseManager,
    SQLiteSessionLeaseBackend,
)
from aegis_agent.sessions.sqlite_store import SQLiteSessionRepository


def main() -> int:
    db_path, session_id, mode = sys.argv[1], sys.argv[2], sys.argv[3]
    db = SQLiteSessionRepository(db_path)
    backend = SQLiteSessionLeaseBackend(db)

    if mode == "acquire-hold":
        seconds = float(sys.argv[4])
        ttl = float(sys.argv[5])
        renew = float(sys.argv[6])
        lost = []
        mgr = SessionLeaseManager(
            backend,
            on_lost=lambda sid: lost.append(sid),
            ttl_s=ttl,
            renew_interval_s=renew,
        )
        if not mgr.acquire(session_id):
            print("FAILED", flush=True)
            return 1
        print("ACQUIRED", flush=True)
        deadline = time.time() + seconds
        while time.time() < deadline and not lost:
            time.sleep(0.05)
        if lost:
            print("LOST", flush=True)
        mgr.stop()
        print("RELEASED", flush=True)
        return 0

    if mode == "contend":
        ttl = float(sys.argv[4])
        handle = backend.acquire(session_id, ttl_s=ttl)
        if handle is None:
            print("FAILED", flush=True)
            return 1
        print("ACQUIRED", flush=True)
        backend.release(handle)
        return 0

    print(f"unknown mode: {mode}", flush=True)
    return 2


if __name__ == "__main__":
    sys.exit(main())
