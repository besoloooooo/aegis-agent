"""Live-Redis integration tests for RedisSessionLeaseBackend.

Skipped unless BOTH:
  * the ``redis`` package is installed (``uv sync --extra redis``), and
  * ``AEGIS_TEST_REDIS_URL`` points at a reachable server, e.g. the one from
    ``tests/docker-compose.redis.yml``::

        docker compose -f tests/docker-compose.redis.yml up -d
        AEGIS_TEST_REDIS_URL=redis://localhost:6379/0 uv run pytest -m integration
        docker compose -f tests/docker-compose.redis.yml down

These are opt-in: the default suite never touches a real server.
"""

from __future__ import annotations

import os
import time
import uuid

import pytest

pytestmark = pytest.mark.integration

_redis_url = os.environ.get("AEGIS_TEST_REDIS_URL")
redis = pytest.importorskip("redis", reason="redis package not installed (uv sync --extra redis)")
if not _redis_url:
    pytest.skip("AEGIS_TEST_REDIS_URL not set", allow_module_level=True)

from aegis_agent.sessions.lease import (
    RedisSessionLeaseBackend,
    SessionLeaseManager,
)


def _sid() -> str:
    return uuid.uuid4().hex[:12]


@pytest.fixture
def backend():
    try:
        b = RedisSessionLeaseBackend(_redis_url)
    except Exception as exc:  # noqa: BLE001 — 服务器不可达时跳过整个集成测试
        pytest.skip(f"redis unreachable at {_redis_url}: {exc}")
    yield b
    # best-effort cleanup of any keys this run created
    try:
        for key in b._client.scan_iter(f"{b._key('')}*"):
            b._client.delete(key)
    except Exception:  # noqa: BLE001, S110 — 清理失败不影响测试结果
        pass


def test_live_acquire_renew_release_cycle(backend) -> None:
    sid = _sid()
    handle = backend.acquire(sid, ttl_s=30)
    assert handle is not None
    assert backend.acquire(sid, ttl_s=30) is None  # NX held by the first owner
    assert backend.renew(handle) is True
    assert backend.is_owner(handle) is True
    backend.release(handle)
    assert backend.is_owner(handle) is False
    assert backend.acquire(sid, ttl_s=30) is not None


def test_live_ttl_expiry_takeover_and_stale_owner_rejected(backend) -> None:
    sid = _sid()
    stale = backend.acquire(sid, ttl_s=0.3)
    assert stale is not None
    time.sleep(0.5)  # real server-side TTL expiry
    fresh = backend.acquire(sid, ttl_s=30)
    assert fresh is not None
    assert backend.renew(stale) is False   # Lua owner check rejects the stale token
    backend.release(stale)                  # must not delete the fresh owner's key
    assert backend.is_owner(fresh) is True


def test_live_heartbeat_keeps_lease_alive_past_ttl(backend) -> None:
    sid = _sid()
    mgr = SessionLeaseManager(backend, ttl_s=0.4, renew_interval_s=0.1)
    assert mgr.acquire(sid)
    time.sleep(0.9)  # > TTL; heartbeat renewals must have kept it alive
    assert mgr.active and mgr.is_owner()
    mgr.stop()


def test_live_key_deleted_externally_trips_breaker(backend) -> None:
    """The key vanishing mid-run (flushall / admin DEL) must trip on_lost."""
    sid = _sid()
    losses = []
    mgr = SessionLeaseManager(
        backend, on_lost=losses.append, ttl_s=30, renew_interval_s=0.1
    )
    assert mgr.acquire(sid)
    backend._client.delete(backend._key(sid))  # external wipe
    deadline = time.time() + 3.0
    while not losses and time.time() < deadline:
        time.sleep(0.05)
    assert losses == [sid]
    mgr.stop()
