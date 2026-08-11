# Portions adapted from Hermes (hermes-agent), © 2025 Nous Research.
# Licensed under the MIT License. See THIRD_PARTY_NOTICES.md.
#
# Behavioural source (near-verbatim port):
#   * ``session_lease.py`` (the user's commit 03e5adc) — pluggable
#     cross-process session leases.  Aegis adaptations: env vars renamed to
#     ``AEGIS_*``, the Redis key prefix is ``aegis:session_lease:``, and the
#     SQLite backend wraps :class:`SQLiteSessionRepository` (duck-typed).
"""可插拔的跨进程会话租约机制。

问题：两个进程同时 ``aegis --resume <id>`` 同一会话 → 都重放历史、都发模型
请求、都追加消息 → 重复请求 + user/user、assistant/assistant 交叉历史。
会话租约保证同一时刻只有一个进程有权运行某会话。

架构：

* :class:`SessionLeaseBackend` —— 抽象接口（acquire / renew / release /
  is_owner）。上层（resume 路径、心跳、熔断）只面向接口，不碰具体后端。
* :class:`SQLiteSessionLeaseBackend` —— 默认；复用会话库的 ``session_leases``
  表（短事务 + 属主令牌条件写）。
* :class:`RedisSessionLeaseBackend` —— ``SET key token NX PX ttl`` + 先验令牌
  再 PEXPIRE/DEL 的 Lua 脚本。
* :class:`SessionLeaseManager` —— 持有租约句柄，跑心跳线程（每
  ``renew_interval_s`` 续期），续期失败的瞬间触发 ``on_lost`` 熔断回调。

后端选择（见 :func:`get_lease_backend`）：

    AEGIS_SESSION_LEASE_BACKEND=sqlite|redis   （默认：sqlite）
    AEGIS_REDIS_URL=redis://localhost:6379/0

选了 Redis 但连不上时，:func:`get_lease_backend` 会**抛出**
:class:`SessionLeaseUnavailableError` —— 绝不静默降级到 SQLite，否则两个实例
各持不同后端的锁、都以为自己拥有会话（锁命名空间分裂）。

默认参数：TTL 30s，每 10s 续期。被 kill -9 的持有者在 TTL 过期后被回收。
可用 ``AEGIS_SESSION_LEASE_TTL_S`` / ``AEGIS_SESSION_LEASE_RENEW_S`` 调整
（主要供测试）。
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_LEASE_TTL_S = 30.0
DEFAULT_RENEW_INTERVAL_S = 10.0

REDIS_KEY_PREFIX = "aegis:session_lease:"


class SessionLeaseUnavailableError(RuntimeError):
    """配置的租约后端不可达。

    在后端构造 / 获取时抛出。调用方必须将其视为本次 resume 的致命错误——
    绝不降级到另一个后端（那会分裂锁命名空间）。
    """


@dataclass
class LeaseHandle:
    """``acquire`` 返回的租约所有权凭证。

    ``owner_token`` 是每次获取时生成的随机 nonce；后续所有
    renew/release/is_owner 都必须出示它，后端原子校验，所以过期持有者
    永远无法覆盖新属主。
    """

    session_id: str
    owner_token: str
    ttl_s: float
    backend: SessionLeaseBackend
    acquired_at: float = 0.0


class SessionLeaseBackend:
    """会话租约后端抽象。

    实现必须保证：同一时刻同一 session 至多一个调用方 ``acquire`` 成功
    （直到释放或 TTL 过期）；``renew``/``release`` 只对当前属主生效。
    """

    #: 诊断/日志用短名（"sqlite" / "redis"）。
    name = "abstract"

    def acquire(
        self,
        session_id: str,
        ttl_s: float = DEFAULT_LEASE_TTL_S,
    ) -> LeaseHandle | None:
        """尝试获取 ``session_id`` 的租约。

        成功返回 :class:`LeaseHandle`；被其他存活属主持有返回 ``None``；
        后端本身宕机抛 :class:`SessionLeaseUnavailableError`（调用方绝不能
        把它当作「租约空闲」）。
        """
        raise NotImplementedError

    def renew(self, handle: LeaseHandle) -> bool:
        """仅当 ``handle`` 仍是属主时续期。

        False = 所有权已丢失（过期 + 被回收）或后端不可达——两者都必须
        触发调用方的熔断器。
        """
        raise NotImplementedError

    def release(self, handle: LeaseHandle) -> None:
        """仅当 ``handle`` 是属主时释放。幂等。"""
        raise NotImplementedError

    def is_owner(self, handle: LeaseHandle) -> bool:
        """``handle`` 是否仍持有存活租约。"""
        raise NotImplementedError


# ──────────────────────────────────────────────────────────────────────────
# SQLite backend
# ──────────────────────────────────────────────────────────────────────────


class SQLiteSessionLeaseBackend(SessionLeaseBackend):
    """基于会话库 ``session_leases`` 表的租约后端。

    所有变更都走 ``SQLiteSessionRepository._execute_write``（BEGIN IMMEDIATE
    短事务）并携带属主令牌条件，因此同一时刻至多一个进程持有一个会话的租约。
    """

    name = "sqlite"

    def __init__(self, session_repo: Any):
        # ``session_repo`` 是 SQLiteSessionRepository（鸭子类型，避免测试 stub
        # 时的硬依赖环）。
        self._db = session_repo
        self._pid = os.getpid()
        self._hostname = socket.gethostname()

    def acquire(
        self,
        session_id: str,
        ttl_s: float = DEFAULT_LEASE_TTL_S,
    ) -> LeaseHandle | None:
        if not session_id:
            return None
        token = uuid.uuid4().hex
        ok = self._db.try_acquire_session_lease(
            session_id,
            token,
            ttl_seconds=ttl_s,
            pid=self._pid,
            hostname=self._hostname,
        )
        if not ok:
            return None
        return LeaseHandle(
            session_id=session_id,
            owner_token=token,
            ttl_s=ttl_s,
            backend=self,
            acquired_at=time.time(),
        )

    def renew(self, handle: LeaseHandle) -> bool:
        return bool(
            self._db.renew_session_lease(
                handle.session_id, handle.owner_token, ttl_seconds=handle.ttl_s
            )
        )

    def release(self, handle: LeaseHandle) -> None:
        self._db.release_session_lease(handle.session_id, handle.owner_token)

    def is_owner(self, handle: LeaseHandle) -> bool:
        return bool(
            self._db.is_session_lease_owner(handle.session_id, handle.owner_token)
        )


# ──────────────────────────────────────────────────────────────────────────
# Redis backend
# ──────────────────────────────────────────────────────────────────────────

# 续期：先验属主令牌再 PEXPIRE。成功返回 1；键不存在或属主不是我们就返回 0。
_REDIS_RENEW_LUA = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("PEXPIRE", KEYS[1], ARGV[2])
else
    return 0
end
"""

# 释放：先验属主令牌再 DEL。
_REDIS_RELEASE_LUA = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("DEL", KEYS[1])
else
    return 0
end
"""


class RedisSessionLeaseBackend(SessionLeaseBackend):
    """基于 Redis 的租约后端：``SET key token NX PX ttl``。

    续期与释放都是先校验属主令牌再变更的 Lua 脚本，因此过期持有者永远无法
    延长或删除已被新属主回收的租约。``redis-py`` 是可选依赖；缺包或连不上
    都在构造时抛 :class:`SessionLeaseUnavailableError` —— 绝不降级到 SQLite
    （两个后端 = 两个互相独立的锁命名空间）。
    """

    name = "redis"

    def __init__(self, url: str, *, socket_timeout_s: float = 2.0):
        try:
            import redis  # type: ignore[import-not-found]
        except ImportError as exc:
            raise SessionLeaseUnavailableError(
                "AEGIS_SESSION_LEASE_BACKEND=redis requires the 'redis' "
                "package (uv add --optional redis / pip install redis). "
                "Refusing to fall back to SQLite — mixed backends would not "
                "be mutually exclusive."
            ) from exc
        self._redis_mod = redis
        self._client = redis.Redis.from_url(
            url,
            socket_timeout=socket_timeout_s,
            socket_connect_timeout=socket_timeout_s,
            retry_on_timeout=False,
            decode_responses=True,
        )
        try:
            self._client.ping()
        except Exception as exc:
            raise SessionLeaseUnavailableError(
                f"Redis lease backend unreachable at {url}: {exc}. "
                "Refusing to fall back to SQLite — mixed backends would "
                "not be mutually exclusive."
            ) from exc
        self._url = url
        self._renew_script = self._client.register_script(_REDIS_RENEW_LUA)
        self._release_script = self._client.register_script(_REDIS_RELEASE_LUA)

    @staticmethod
    def _key(session_id: str) -> str:
        return f"{REDIS_KEY_PREFIX}{session_id}"

    def acquire(
        self,
        session_id: str,
        ttl_s: float = DEFAULT_LEASE_TTL_S,
    ) -> LeaseHandle | None:
        if not session_id:
            return None
        token = uuid.uuid4().hex
        try:
            ok = self._client.set(
                self._key(session_id),
                token,
                nx=True,
                px=max(1, int(ttl_s * 1000)),
            )
        except Exception as exc:
            raise SessionLeaseUnavailableError(
                f"Redis lease acquire failed for {session_id}: {exc}"
            ) from exc
        if not ok:
            return None
        return LeaseHandle(
            session_id=session_id,
            owner_token=token,
            ttl_s=ttl_s,
            backend=self,
            acquired_at=time.time(),
        )

    def renew(self, handle: LeaseHandle) -> bool:
        try:
            result = self._renew_script(
                keys=[self._key(handle.session_id)],
                args=[handle.owner_token, max(1, int(handle.ttl_s * 1000))],
            )
            return bool(result)
        except Exception as exc:  # noqa: BLE001 — Redis 异常必须 fail closed 触发熔断
            logger.warning(
                "Redis lease renew failed for %s: %s",
                handle.session_id, exc,
            )
            # 关闭失败：不可续期的租约必须触发熔断，而不是默默继续写。
            return False

    def release(self, handle: LeaseHandle) -> None:
        try:
            self._release_script(
                keys=[self._key(handle.session_id)],
                args=[handle.owner_token],
            )
        except Exception as exc:  # noqa: BLE001 — 释放失败只记日志（TTL 兜底回收）
            logger.warning(
                "Redis lease release failed for %s: %s",
                handle.session_id, exc,
            )

    def is_owner(self, handle: LeaseHandle) -> bool:
        try:
            value = self._client.get(self._key(handle.session_id))
        except Exception as exc:  # noqa: BLE001 — 查询失败按不持有处理（fail closed）
            logger.warning(
                "Redis lease is_owner check failed for %s: %s",
                handle.session_id, exc,
            )
            return False
        return value is not None and value == handle.owner_token


# ──────────────────────────────────────────────────────────────────────────
# Backend factory
# ──────────────────────────────────────────────────────────────────────────

BACKEND_ENV_VAR = "AEGIS_SESSION_LEASE_BACKEND"
REDIS_URL_ENV_VAR = "AEGIS_REDIS_URL"
DEFAULT_REDIS_URL = "redis://localhost:6379/0"


def get_lease_backend(session_repo: Any = None) -> SessionLeaseBackend:
    """按配置构造租约后端。

    ``sqlite``（默认）包装给定的 :class:`SQLiteSessionRepository`（缺省时按
    默认路径新建）。``redis`` 连接 ``AEGIS_REDIS_URL``，连不上抛
    :class:`SessionLeaseUnavailableError` —— 绝不静默降级 SQLite。
    """
    backend = os.getenv(BACKEND_ENV_VAR, "sqlite").strip().lower()
    if backend == "sqlite":
        if session_repo is None:
            from aegis_agent.sessions.sqlite_store import SQLiteSessionRepository

            session_repo = SQLiteSessionRepository()
        return SQLiteSessionLeaseBackend(session_repo)
    if backend == "redis":
        url = os.getenv(REDIS_URL_ENV_VAR, DEFAULT_REDIS_URL)
        return RedisSessionLeaseBackend(url)
    raise SessionLeaseUnavailableError(
        f"Unknown {BACKEND_ENV_VAR}={backend!r} (expected 'sqlite' or 'redis')"
    )


# ──────────────────────────────────────────────────────────────────────────
# Lease manager (heartbeat + circuit breaker)
# ──────────────────────────────────────────────────────────────────────────


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        value = float(raw)
        return value if value > 0 else default
    except ValueError:
        return default


class SessionLeaseManager:
    """持有一个会话租约并用心跳线程保活。

    生命周期::

        mgr = SessionLeaseManager(backend, on_lost=...)
        mgr.acquire(session_id)      # -> bool
        ... 运行 agent ...
        mgr.stop()                   # 停心跳 + 释放

    心跳每 ``renew_interval_s``（默认 10）续期一次，TTL 为 ``ttl_s``
    （默认 30）。第一次续期失败就翻转 :attr:`lost` 并恰好触发一次
    ``on_lost`` —— agent 熔断器挂在上面，停止模型请求、工具执行与消息
    写入。``stop()`` 幂等，可安全在退出处理器里调用（正常退出、Ctrl+C、
    -q 路径、gateway 关闭）。
    """

    def __init__(
        self,
        backend: SessionLeaseBackend,
        on_lost: Callable[[str], None] | None = None,
        ttl_s: float | None = None,
        renew_interval_s: float | None = None,
    ):
        self._backend = backend
        self._on_lost = on_lost
        self._ttl_s = (
            ttl_s
            if ttl_s is not None
            else _env_float("AEGIS_SESSION_LEASE_TTL_S", DEFAULT_LEASE_TTL_S)
        )
        self._renew_interval_s = (
            renew_interval_s
            if renew_interval_s is not None
            else _env_float(
                "AEGIS_SESSION_LEASE_RENEW_S", DEFAULT_RENEW_INTERVAL_S
            )
        )
        self._handle: LeaseHandle | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._lost = False

    # ── introspection ────────────────────────────────────────────────

    @property
    def session_id(self) -> str | None:
        return self._handle.session_id if self._handle else None

    @property
    def lost(self) -> bool:
        """续期失败过一次即为 True（租约不再是我们的）。"""
        return self._lost

    @property
    def active(self) -> bool:
        """持有租约且心跳在跑时为 True。"""
        return self._handle is not None and not self._lost

    @property
    def backend(self) -> SessionLeaseBackend:
        return self._backend

    def is_owner(self) -> bool:
        handle = self._handle
        if handle is None or self._lost:
            return False
        return self._backend.is_owner(handle)

    # ── lifecycle ────────────────────────────────────────────────────

    def acquire(self, session_id: str) -> bool:
        """获取租约并启动心跳。被他人持有时返回 False。"""
        with self._lock:
            if self._handle is not None and self._handle.session_id == session_id:
                return not self._lost
        # 防御：绝不同时持有两个租约。stop() 会 join 心跳线程，所以必须在
        # self._lock 之外执行（心跳要拿锁读 handle）。
        self.stop()
        handle = self._backend.acquire(session_id, ttl_s=self._ttl_s)
        if handle is None:
            return False
        with self._lock:
            self._lost = False
            self._handle = handle
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._heartbeat_loop,
                name=f"session-lease-{session_id[:16]}",
                daemon=True,
            )
            self._thread.start()
            return True

    def switch_session(self, new_session_id: str) -> bool:
        """把租约迁到 ``new_session_id``。

        先获取新租约，成功才释放旧的。新租约获取失败则保留旧租约并返回
        False —— 调用方必须将其视为租约丢失并停止写入。
        """
        with self._lock:
            old_handle = self._handle
            if old_handle is not None and old_handle.session_id == new_session_id:
                return not self._lost
        new_handle = self._backend.acquire(new_session_id, ttl_s=self._ttl_s)
        if new_handle is None:
            return False
        with self._lock:
            old_handle = self._handle
            self._handle = new_handle
            self._lost = False
            need_thread = self._thread is None or not self._thread.is_alive()
            if need_thread:
                self._stop_event.clear()
                self._thread = threading.Thread(
                    target=self._heartbeat_loop,
                    name=f"session-lease-{new_session_id[:16]}",
                    daemon=True,
                )
                self._thread.start()
        if old_handle is not None:
            try:
                self._backend.release(old_handle)
            except Exception as exc:  # noqa: BLE001 — 旧租约释放失败由 TTL 兜底
                logger.debug("old session lease release failed: %s", exc)
        return True

    def stop(self) -> None:
        """停心跳并释放租约。幂等。"""
        with self._lock:
            thread = self._stop_locked(release=True)
        # 在锁外 join：心跳需要拿锁才能观察到 handle 已清空并退出。
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def _stop_locked(self, *, release: bool) -> threading.Thread | None:
        """摘下线程 + 释放句柄。调用方必须持有 ``self._lock``。

        返回被摘下的心跳线程，调用方在**释放锁之后**再 join 它。
        """
        self._stop_event.set()
        thread = self._thread
        self._thread = None
        handle = self._handle
        self._handle = None
        if release and handle is not None and not self._lost:
            try:
                self._backend.release(handle)
            except Exception as exc:  # noqa: BLE001 — 释放失败由 TTL 兜底回收
                logger.debug("session lease release failed: %s", exc)
        return thread

    # ── heartbeat ────────────────────────────────────────────────────

    def _heartbeat_loop(self) -> None:
        # 第一次续期在一个间隔之后（acquire 已设置 expires_at）。
        while not self._stop_event.wait(self._renew_interval_s):
            with self._lock:
                handle = self._handle
            if handle is None:
                return
            try:
                ok = self._backend.renew(handle)
            except Exception as exc:  # noqa: BLE001 — 心跳异常按续期失败处理（fail closed）
                logger.warning(
                    "session lease heartbeat error for %s: %s",
                    handle.session_id, exc,
                )
                ok = False
            if not ok:
                self._mark_lost(handle.session_id)
                return

    def _mark_lost(self, session_id: str) -> None:
        callback = None
        with self._lock:
            if self._lost:
                return
            self._lost = True
            callback = self._on_lost
        logger.warning(
            "session lease LOST for %s (backend=%s) — circuit breaker open",
            session_id, self._backend.name,
        )
        if callback is not None:
            try:
                callback(session_id)
            except Exception as exc:  # noqa: BLE001 — 熔断回调异常不应影响租约状态
                logger.debug("on_lost callback failed: %s", exc)


__all__ = [
    "BACKEND_ENV_VAR",
    "DEFAULT_LEASE_TTL_S",
    "DEFAULT_RENEW_INTERVAL_S",
    "LeaseHandle",
    "RedisSessionLeaseBackend",
    "SQLiteSessionLeaseBackend",
    "SessionLeaseBackend",
    "SessionLeaseManager",
    "SessionLeaseUnavailableError",
    "get_lease_backend",
]
