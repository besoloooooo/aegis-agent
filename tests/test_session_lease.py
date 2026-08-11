"""Tests for the pluggable cross-process session lease (ported from Hermes
commit 03e5adc): SQLite backend, Redis backend (via a fake client), the
heartbeat manager, and backend selection.

Core invariant (CLAUDE.md §9): only one lease owner for the same session.
"""

from __future__ import annotations

import sys
import threading
import time
from types import SimpleNamespace

import pytest

from aegis_agent.sessions.lease import (
    SessionLeaseManager,
    SessionLeaseUnavailableError,
    SQLiteSessionLeaseBackend,
    get_lease_backend,
)
from aegis_agent.sessions.sqlite_store import SQLiteSessionRepository


@pytest.fixture
def repo(tmp_path):
    r = SQLiteSessionRepository(tmp_path / "lease.db")
    r.create_session("s")
    r.create_session("other")
    yield r
    r.close()


def _manager(repo, on_lost=None, ttl=0.4, renew=0.1):
    return SessionLeaseManager(
        SQLiteSessionLeaseBackend(repo), on_lost=on_lost, ttl_s=ttl, renew_interval_s=renew
    )


# ---------------------------------------------------------------------------
# SQLite backend
# ---------------------------------------------------------------------------


def test_two_contenders_single_winner(repo) -> None:
    backend = SQLiteSessionLeaseBackend(repo)
    first = backend.acquire("s", ttl_s=30)
    second = backend.acquire("s", ttl_s=30)
    assert first is not None
    assert second is None  # held by first


def test_concurrent_acquire_exactly_one_winner(repo, tmp_path) -> None:
    """N racing 'processes' (separate repo connections): exactly one acquires."""
    winners = []

    def contender(i):
        r = SQLiteSessionRepository(tmp_path / "lease.db")
        try:
            handle = SQLiteSessionLeaseBackend(r).acquire("s", ttl_s=30)
            if handle is not None:
                winners.append(i)
        finally:
            r.close()

    threads = [threading.Thread(target=contender, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(winners) == 1


def test_different_sessions_lease_in_parallel(repo) -> None:
    backend = SQLiteSessionLeaseBackend(repo)
    assert backend.acquire("s", ttl_s=30) is not None
    assert backend.acquire("other", ttl_s=30) is not None


def test_release_allows_immediate_takeover(repo) -> None:
    backend = SQLiteSessionLeaseBackend(repo)
    first = backend.acquire("s", ttl_s=30)
    assert first is not None
    backend.release(first)
    assert backend.acquire("s", ttl_s=30) is not None


def test_ttl_expiry_allows_takeover(repo) -> None:
    backend = SQLiteSessionLeaseBackend(repo)
    first = backend.acquire("s", ttl_s=0.15)
    assert first is not None
    time.sleep(0.25)  # kill -9 模拟：持有者没有释放，靠 TTL 过期回收
    second = backend.acquire("s", ttl_s=30)
    assert second is not None
    assert second.owner_token != first.owner_token


def test_stale_owner_cannot_renew_release_or_pass_is_owner(repo) -> None:
    backend = SQLiteSessionLeaseBackend(repo)
    stale = backend.acquire("s", ttl_s=0.15)
    time.sleep(0.25)
    fresh = backend.acquire("s", ttl_s=30)
    assert fresh is not None
    assert backend.renew(stale) is False
    backend.release(stale)  # no-op: must not delete the fresh owner's row
    assert backend.is_owner(stale) is False
    assert backend.is_owner(fresh) is True


def test_renew_extends_lease(repo) -> None:
    backend = SQLiteSessionLeaseBackend(repo)
    handle = backend.acquire("s", ttl_s=0.3)
    for _ in range(4):
        time.sleep(0.15)
        assert backend.renew(handle) is True
    # total elapsed 0.6s > original 0.3s TTL — renewals kept it alive
    assert backend.is_owner(handle) is True


# ---------------------------------------------------------------------------
# manager (heartbeat + circuit breaker)
# ---------------------------------------------------------------------------


def test_heartbeat_keeps_lease_alive_past_ttl(repo) -> None:
    mgr = _manager(repo, ttl=0.3, renew=0.1)
    assert mgr.acquire("s")
    time.sleep(0.6)  # well past the TTL; heartbeat must have renewed
    assert mgr.active and mgr.is_owner()
    mgr.stop()


def test_manager_stop_releases_for_immediate_takeover(repo) -> None:
    mgr = _manager(repo)
    assert mgr.acquire("s")
    mgr.stop()
    assert _manager(repo).acquire("s")


def test_on_lost_fires_exactly_once_when_lease_reclaimed(repo) -> None:
    losses = []
    mgr = _manager(repo, on_lost=losses.append, ttl=0.4, renew=0.05)
    assert mgr.acquire("s")
    # 确定性模拟「本进程停顿超过 TTL（如 GC stall / SIGSTOP），租约被回收」：
    # 直接把 expires_at 改成过去，另一进程随即回收；本进程下一次续期必然失败。
    repo._execute_write(
        lambda conn: conn.execute(
            "UPDATE session_leases SET expires_at = 0 WHERE session_id = 's'"
        )
    )
    assert SQLiteSessionLeaseBackend(repo).acquire("s", ttl_s=30) is not None
    deadline = time.time() + 2.0
    while not losses and time.time() < deadline:
        time.sleep(0.05)
    assert losses == ["s"]
    assert mgr.lost and not mgr.active
    mgr.stop()


def test_switch_session_moves_lease(repo) -> None:
    mgr = _manager(repo)
    assert mgr.acquire("s")
    assert mgr.switch_session("other")
    # old session is free, new one is held
    assert SQLiteSessionLeaseBackend(repo).acquire("s", ttl_s=30) is not None
    assert SQLiteSessionLeaseBackend(repo).acquire("other", ttl_s=30) is None
    mgr.stop()


def test_switch_session_failure_keeps_old_lease(repo) -> None:
    blocker = SQLiteSessionLeaseBackend(repo).acquire("other", ttl_s=30)
    assert blocker is not None
    mgr = _manager(repo)
    assert mgr.acquire("s")
    assert mgr.switch_session("other") is False
    assert mgr.active and mgr.session_id == "s"  # old lease kept
    mgr.stop()


# ---------------------------------------------------------------------------
# backend selection
# ---------------------------------------------------------------------------


def test_default_backend_is_sqlite(repo, monkeypatch) -> None:
    monkeypatch.delenv("AEGIS_SESSION_LEASE_BACKEND", raising=False)
    backend = get_lease_backend(repo)
    assert isinstance(backend, SQLiteSessionLeaseBackend)


def test_unknown_backend_raises(repo, monkeypatch) -> None:
    monkeypatch.setenv("AEGIS_SESSION_LEASE_BACKEND", "etcd")
    with pytest.raises(SessionLeaseUnavailableError):
        get_lease_backend(repo)


def test_redis_backend_without_package_raises_no_fallback(repo, monkeypatch) -> None:
    """redis-py 未安装且选了 redis → 抛出；绝不静默降级 SQLite。"""
    monkeypatch.setenv("AEGIS_SESSION_LEASE_BACKEND", "redis")
    monkeypatch.setitem(sys.modules, "redis", None)  # force ImportError
    with pytest.raises(SessionLeaseUnavailableError):
        get_lease_backend(repo)


# ---------------------------------------------------------------------------
# Redis backend (fake client implementing the exact used surface)
# ---------------------------------------------------------------------------


class FakeRedisClient:
    """In-memory stand-in for redis-py: set(NX/PX)/get/register_script/ping."""

    def __init__(self, reachable=True):
        self._store: dict[str, list] = {}  # key -> [value, expire_at_monotonic|None]
        self._reachable = reachable

    def ping(self):
        if not self._reachable:
            raise ConnectionError("redis is down")
        return True

    def register_script(self, lua: str):
        def call(keys=None, args=None):
            key = keys[0]
            if "PEXPIRE" in lua:  # renew: owner-checked PEXPIRE
                if self.get(key) == args[0]:
                    self._expire(key, int(args[1]))
                    return 1
                return 0
            # release: owner-checked DEL
            if self.get(key) == args[0]:
                self._store.pop(key, None)
                return 1
            return 0

        return call

    def set(self, key, value, nx=False, px=None):
        if nx and self.get(key) is not None:
            return None
        self._store[key] = [value, None]
        if px:
            self._expire(key, int(px))
        return True

    def get(self, key):
        entry = self._store.get(key)
        if entry is None:
            return None
        value, expire_at = entry
        if expire_at is not None and time.monotonic() >= expire_at:
            self._store.pop(key, None)
            return None
        return value

    def _expire(self, key, ms: int) -> None:
        if key in self._store:
            self._store[key][1] = time.monotonic() + ms / 1000.0


@pytest.fixture
def fake_redis_module(monkeypatch):
    """Inject a fake ``redis`` module; returns a factory of reachable clients."""
    clients: list[FakeRedisClient] = []

    def from_url(url, **kwargs):
        client = FakeRedisClient()
        clients.append(client)
        return client

    module = SimpleNamespace(Redis=SimpleNamespace(from_url=from_url))
    monkeypatch.setitem(sys.modules, "redis", module)
    return clients


def _redis_backend(monkeypatch, fake_redis_module, url="redis://fake/0"):
    from aegis_agent.sessions.lease import RedisSessionLeaseBackend

    return RedisSessionLeaseBackend(url)


def test_redis_acquire_renew_release_cycle(fake_redis_module) -> None:
    backend = _redis_backend(None, fake_redis_module)
    handle = backend.acquire("s", ttl_s=30)
    assert handle is not None
    assert backend.acquire("s", ttl_s=30) is None  # NX held
    assert backend.renew(handle) is True
    assert backend.is_owner(handle) is True
    backend.release(handle)
    assert backend.is_owner(handle) is False
    assert backend.acquire("s", ttl_s=30) is not None  # free again


def test_redis_ttl_expiry_and_stale_owner(fake_redis_module) -> None:
    backend = _redis_backend(None, fake_redis_module)
    stale = backend.acquire("s", ttl_s=0.15)
    time.sleep(0.25)
    fresh = backend.acquire("s", ttl_s=30)
    assert fresh is not None
    assert backend.renew(stale) is False       # Lua owner check rejects
    backend.release(stale)                      # must not delete fresh owner's key
    assert backend.is_owner(fresh) is True


def test_redis_unreachable_raises_instead_of_fallback(monkeypatch) -> None:
    def from_url(url, **kwargs):
        return FakeRedisClient(reachable=False)

    monkeypatch.setitem(
        sys.modules, "redis", SimpleNamespace(Redis=SimpleNamespace(from_url=from_url))
    )
    from aegis_agent.sessions.lease import RedisSessionLeaseBackend

    with pytest.raises(SessionLeaseUnavailableError):
        RedisSessionLeaseBackend("redis://down/0")


def test_redis_disconnect_mid_run_trips_breaker(fake_redis_module) -> None:
    backend = _redis_backend(None, fake_redis_module)
    losses = []
    mgr = SessionLeaseManager(backend, on_lost=losses.append, ttl_s=0.4, renew_interval_s=0.05)
    assert mgr.acquire("s")
    # 中途宕机：renew 抛异常 → fail closed → on_lost
    client = fake_redis_module[0]
    client._reachable = False

    def boom(keys=None, args=None):
        raise ConnectionError("connection lost")

    mgr._handle.backend._renew_script = boom
    deadline = time.time() + 2.0
    while not losses and time.time() < deadline:
        time.sleep(0.05)
    assert losses == ["s"]
    assert mgr.lost
    mgr.stop()
