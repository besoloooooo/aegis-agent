# Portions adapted from Hermes (hermes-agent), © 2025 Nous Research.
# Licensed under the MIT License. See THIRD_PARTY_NOTICES.md.
#
# Behavioural sources (port with adaptation):
#   * ``hermes_state.py`` (the user's session-persistence commits 5a51f55 /
#     181e078 / 03e5adc) — the connection setup (WAL + short busy timeout +
#     manual transactions), ``_execute_write`` (BEGIN IMMEDIATE + jitter
#     retry + periodic TRUNCATE checkpoint), message-level idempotent
#     ``append_message`` (UNIQUE(session_id, client_msg_id) + ON CONFLICT DO
#     NOTHING + same-transaction counter update), the ``session_snapshots``
#     fast-resume checkpoint (zlib + CRC32, history_version invalidation,
#     snapshot+tail replay with full-replay fallback), and the
#     ``session_leases`` table methods.
#
# Aegis adaptations:
#   * implements the :class:`~aegis_agent.sessions.repository.SessionRepository`
#     Protocol and stores the canonical ``Message`` dataclass (Hermes stores
#     OpenAI-shaped dicts with many provider-specific columns; Aegis trims the
#     schema to the fields ``Message`` actually has);
#   * dropped: FTS5 search, titles/archives, rewind/undo, compression lineage
#     (Aegis compression never rewrites source history), token billing,
#     platform message ids, codex/multimodal extras;
#   * ``parent_session_id`` / compression-chain resume walking is NOT ported —
#     Aegis compression only affects the derived context, so sessions are never
#     forked.  ``history_version`` is kept so future history-rewrite paths
#     (/undo etc.) invalidate stale snapshots exactly like Hermes.
"""SQLite 会话存储：消息级幂等持久化 + 快恢复快照 + 会话租约表。

四张表：
  sessions          —— 会话元信息（history_version 用于快照失效判断）
  messages          —— 一行一条消息；UNIQUE(session_id, client_msg_id) 幂等键；
                       seq 会话内单调序号；active 软删除标记
  session_snapshots —— 恢复加速起点（checkpoint）；snapshot_json 是 zlib 压缩的
                       消息 dict 列表 + CRC32 校验；恢复 = 最新有效快照 + 尾部增量
  session_leases    —— 会话租约（见 sessions/lease.py）

关键不变式（与 CLAUDE.md §9 对齐）：
  * 一个 client_msg_id 最多落一行（ON CONFLICT DO NOTHING + 计数只在真插入时累加）；
  * 会话内消息按 seq 单调有序（序号在写事务内现算）；
  * COMMIT 返回 = 已落盘（WAL fsync），硬崩溃只丢未 COMMIT 的最后一条；
  * 快照缺失/损坏/history_version 不符 → 一律降级全量重放，绝不牺牲正确性换速度；
  * 会话间严格隔离。
"""

from __future__ import annotations

import json
import logging
import os
import random
import sqlite3
import threading
import time
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from aegis_agent.exceptions import SessionNotFoundError
from aegis_agent.models.base import Message, Role, ToolCall
from aegis_agent.sessions.models import Session

logger = logging.getLogger(__name__)

T = TypeVar("T")

DEFAULT_DB_PATH = Path.home() / ".aegis" / "state.db"

# WAL 不兼容的文件系统报错特征（NFS/SMB/FUSE）。
_WAL_INCOMPAT_MARKERS = ("locking protocol", "disk i/o error")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    source TEXT,                        -- 来源：cli / test …
    title TEXT,
    created_at REAL NOT NULL,
    ended_at REAL,
    end_reason TEXT,
    message_count INTEGER NOT NULL DEFAULT 0,  -- 冗余计数（与 INSERT 同事务累加）
    history_version INTEGER NOT NULL DEFAULT 0 -- 历史改写世代号：不符的旧快照自动失效
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, -- 自增主键 = 插入顺序（恢复排序的另一保障）
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL,
    content TEXT,
    tool_call_id TEXT,
    tool_calls TEXT,              -- assistant 发起的工具调用（JSON）
    tool_name TEXT,
    reasoning_content TEXT,       -- 思维链（持久化但永不回传 wire，见 openai_compat）
    timestamp REAL NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,  -- 软删除标记（预留 /undo；恢复只取 active=1）
    client_msg_id TEXT,           -- 稳定消息身份（幂等键）
    seq INTEGER                   -- 会话内单调序号（0,1,2…），事务内现算
);

-- 会话恢复的「加速起点」（Checkpoint）。messages 表才是完整可信日志；
-- 快照只用于跳过已快照前缀的反序列化，任何失效场景都降级全量重放。
CREATE TABLE IF NOT EXISTS session_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    last_seq INTEGER NOT NULL,        -- 快照覆盖到的最后一条消息的 seq
    history_version INTEGER NOT NULL, -- 生成时的 sessions.history_version；不符即失效
    snapshot_json BLOB NOT NULL,      -- zlib 压缩的消息 dict 列表（含 client_msg_id/seq）
    checksum TEXT NOT NULL,           -- 解压后明文的 CRC32（防撕裂写/磁盘位衰减）
    created_at REAL NOT NULL
);

-- 会话租约：同一时刻只允许一个进程恢复/写入某会话（见 sessions/lease.py）。
CREATE TABLE IF NOT EXISTS session_leases (
    session_id TEXT PRIMARY KEY,
    owner_token TEXT NOT NULL,     -- 随机 nonce，租约所有权凭证
    pid INTEGER,
    hostname TEXT,
    acquired_at REAL NOT NULL,
    heartbeat_at REAL NOT NULL,
    expires_at REAL NOT NULL       -- 过期后可被他人回收（kill -9 兜底）
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
CREATE INDEX IF NOT EXISTS idx_snapshots_session ON session_snapshots(session_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_session_leases_expires ON session_leases(expires_at);
"""

# 幂等键唯一索引：部分索引（无 client_msg_id 的行豁免），保证重复 flush / 多写者 /
# 重试都只落一行。
IDEMPOTENCY_INDEX_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_client_id
ON messages(session_id, client_msg_id)
WHERE client_msg_id IS NOT NULL
"""


class SQLiteSessionRepository:
    """SQLite-backed :class:`SessionRepository` with snapshot fast-resume.

    Thread-safe (single connection guarded by a lock; writes serialised via
    BEGIN IMMEDIATE) and crash-durable (WAL; COMMIT = fsync).
    """

    # ── 写竞争调参（移植自 hermes_state.SessionDB）──
    # SQLite 内置 busy handler 是确定性重试，多写者会形成 convoy；这里把
    # busy 超时压到 1s，撞锁后在应用层做随机 jitter 重试，自然错开竞争写者。
    _WRITE_MAX_RETRIES = 15
    _WRITE_RETRY_MIN_S = 0.020   # 20ms
    _WRITE_RETRY_MAX_S = 0.150   # 150ms
    _CHECKPOINT_EVERY_N_WRITES = 50

    def __init__(self, db_path: str | os.PathLike[str] | None = None, *, source: str = "cli"):
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self._source = source
        self._lock = threading.Lock()
        self._write_count = 0
        self._last_snapshot_seq: dict[str, int] = {}

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # 连接配置（移植要点）：
        #   check_same_thread=False —— 允许主线程写、其它线程读同一连接；线程安全靠
        #     self._lock + BEGIN IMMEDIATE 串行化，不靠 sqlite3 的连接锁。
        #   timeout=1.0 —— SQLite 内部 busy 超时只 1 秒，撞锁不死等，抛 locked 交给
        #     _execute_write 的 jitter 重试。
        #   isolation_level=None —— 关掉 sqlite3 模块的隐式事务，事务边界自己管。
        self._conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            timeout=1.0,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._apply_wal_with_fallback()
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    # ── 连接与事务原语（PORT）────────────────────────────────────────────

    def _apply_wal_with_fallback(self) -> str:
        """开 WAL；NFS/SMB/FUSE 不支持（locking protocol）则退回 DELETE 模式。"""
        try:
            row = self._conn.execute("PRAGMA journal_mode").fetchone()
            if row and row[0] == "wal":
                return "wal"
        except sqlite3.OperationalError:
            pass
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            return "wal"
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if not any(marker in msg for marker in _WAL_INCOMPAT_MARKERS):
                raise
            logger.warning(
                "%s: WAL unsupported on this filesystem (%s); falling back to "
                "journal_mode=DELETE (reduced concurrency).",
                self.db_path,
                exc,
            )
            self._conn.execute("PRAGMA journal_mode=DELETE")
            return "delete"

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA_SQL)
            self._conn.executescript(IDEMPOTENCY_INDEX_SQL)
            self._conn.commit()

    def _execute_write(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """把一次写包成「原子 + 并发安全 + 可重试 + 自动 checkpoint」的事务。

        BEGIN IMMEDIATE 进事务时就抢 WAL 写锁（不是 COMMIT 才抢），锁竞争在开头
        暴露；fn 抛异常整批回滚；COMMIT 返回 = 已 fsync 落盘。撞锁时随机
        jitter（20~150ms）退避重试，打散多写者 convoy。
        """
        last_err: Exception | None = None
        for attempt in range(self._WRITE_MAX_RETRIES):
            try:
                with self._lock:
                    self._conn.execute("BEGIN IMMEDIATE")
                    try:
                        result = fn(self._conn)
                        self._conn.commit()
                    except BaseException:
                        try:
                            self._conn.rollback()
                        except Exception:  # noqa: BLE001, S110 — 回滚失败不应掩盖原异常
                            pass
                        raise
                self._write_count += 1
                if self._write_count % self._CHECKPOINT_EVERY_N_WRITES == 0:
                    self._try_wal_checkpoint()
                return result
            except sqlite3.OperationalError as exc:
                err_msg = str(exc).lower()
                if "locked" in err_msg or "busy" in err_msg:
                    last_err = exc
                    if attempt < self._WRITE_MAX_RETRIES - 1:
                        time.sleep(
                            random.uniform(self._WRITE_RETRY_MIN_S, self._WRITE_RETRY_MAX_S)
                        )
                        continue
                raise
        raise last_err or sqlite3.OperationalError("database is locked after max retries")

    def _try_wal_checkpoint(self) -> None:
        """每 N 次写做一次 TRUNCATE checkpoint：合并 -wal 回主库并截断。尽力而为。"""
        try:
            with self._lock:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        except Exception:  # noqa: BLE001, S110 — 只影响文件大小，不影响正确性
            pass

    def close(self) -> None:
        """关闭连接；先做一次 TRUNCATE checkpoint 帮助收缩 -wal。"""
        with self._lock:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:  # noqa: BLE001, S110 — 关闭路径尽力而为
                pass
            self._conn.close()

    # ── Message <-> row / dict 编解码 ────────────────────────────────────

    @staticmethod
    def _message_dict(m: Message) -> dict:
        """Message → 可 JSON 序列化的 dict（快照与行解码共用同一形状）。"""
        return {
            "role": m.role.value,
            "content": m.content,
            "tool_calls": [
                {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                for tc in m.tool_calls
            ],
            "tool_call_id": m.tool_call_id,
            "name": m.name,
            "reasoning_content": m.reasoning_content,
            "client_msg_id": m.client_msg_id,
            "seq": m.seq,
        }

    @staticmethod
    def _message_from_dict(d: dict) -> Message:
        """dict → Message。与 _message_dict 互逆（含 client_msg_id / seq）。"""
        try:
            role = Role(d.get("role", "user"))
        except ValueError:
            role = Role.USER
        tool_calls = [
            ToolCall(
                id=str(tc.get("id", "")),
                name=str(tc.get("name", "")),
                arguments=str(tc.get("arguments", "")),
            )
            for tc in d.get("tool_calls") or []
            if isinstance(tc, dict)
        ]
        seq = d.get("seq")
        return Message(
            role=role,
            content=d.get("content") or "",
            tool_calls=tool_calls,
            tool_call_id=d.get("tool_call_id"),
            name=d.get("name"),
            reasoning_content=d.get("reasoning_content") or "",
            client_msg_id=d.get("client_msg_id"),
            seq=int(seq) if seq is not None else None,
        )

    def _row_to_dict(self, row: sqlite3.Row) -> dict:
        """messages 行 → 消息 dict。

        全量重放与快照尾部重放共用同一个解码器，保证「快照 + 尾部」与
        全量重放逐字节一致（tool_calls JSON 损坏降级为 []，不拖垮整个恢复）。
        """
        tool_calls: list = []
        if row["tool_calls"]:
            try:
                parsed = json.loads(row["tool_calls"])
                tool_calls = parsed if isinstance(parsed, list) else []
            except (json.JSONDecodeError, TypeError):
                logger.warning("corrupt tool_calls JSON in messages row; using []")
                tool_calls = []
        return {
            "role": row["role"],
            "content": row["content"],
            "tool_calls": tool_calls,
            "tool_call_id": row["tool_call_id"],
            "name": row["tool_name"],
            "reasoning_content": row["reasoning_content"],
            "client_msg_id": row["client_msg_id"],
            "seq": row["seq"],
        }

    _MSG_COLUMNS = (
        "role, content, tool_call_id, tool_calls, tool_name, reasoning_content, "
        "client_msg_id, seq"
    )

    # ── SessionRepository Protocol ───────────────────────────────────────

    def create_session(self, session_id: str | None = None, title: str | None = None) -> Session:
        import uuid

        sid = session_id or uuid.uuid4().hex

        def _do(conn):
            conn.execute(
                "INSERT OR IGNORE INTO sessions (id, source, title, created_at) "
                "VALUES (?, ?, ?, ?)",
                (sid, self._source, title, time.time()),
            )

        self._execute_write(_do)
        return Session(id=sid, title=title)

    def get_session(self, session_id: str) -> Session | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, title, created_at FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return Session(id=row["id"], title=row["title"], created_at=row["created_at"])

    def append_message(self, session_id: str, message: Message) -> Message:
        """幂等追加一条消息。

        client_msg_id 命中 UNIQUE(session_id, client_msg_id) 时静默跳过（重复
        flush / 重试 / 多写者都只落一行），并返回既有消息；计数只在真插入时
        累加（与 INSERT 同事务，要么一起 commit 要么一起回滚）。seq 在事务内
        现算（MAX(seq)+1），并发下不重号。
        """
        # 进事务前预序列化，缩短持锁临界区。
        tool_calls_json = (
            json.dumps(
                [{"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                 for tc in message.tool_calls],
                ensure_ascii=False,
            )
            if message.tool_calls
            else None
        )
        now = time.time()

        def _do(conn):
            if conn.execute(
                "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
            ).fetchone() is None:
                raise SessionNotFoundError(f"session not found: {session_id}")

            next_seq = conn.execute(
                "SELECT COALESCE(MAX(seq), -1) + 1 FROM messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0]

            cursor = conn.execute(
                """INSERT INTO messages (session_id, role, content, tool_call_id,
                   tool_calls, tool_name, reasoning_content, timestamp,
                   client_msg_id, seq)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(session_id, client_msg_id)
                   WHERE client_msg_id IS NOT NULL DO NOTHING""",
                (
                    session_id,
                    message.role.value,
                    message.content,
                    message.tool_call_id,
                    tool_calls_json,
                    message.name,
                    message.reasoning_content or None,
                    now,
                    message.client_msg_id,
                    next_seq,
                ),
            )

            if cursor.rowcount < 1:
                # 幂等冲突：返回既有行（调用方拿到稳定的已存记录）
                existing = conn.execute(
                    f"SELECT {self._MSG_COLUMNS} FROM messages "
                    "WHERE session_id = ? AND client_msg_id = ?",
                    (session_id, message.client_msg_id),
                ).fetchone()
                if existing is not None:
                    return self._message_from_dict(self._row_to_dict(existing))
                return message  # 理论不可达；兜底返回入参

            conn.execute(
                "UPDATE sessions SET message_count = message_count + 1 WHERE id = ?",
                (session_id,),
            )
            message.seq = next_seq
            return message

        return self._execute_write(_do)

    def list_messages(self, session_id: str) -> list[Message]:
        """按 seq 顺序返回会话消息。内部走快照快路径（有效快照 + 尾部增量）。"""
        with self._lock:
            exists = self._conn.execute(
                "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
            ).fetchone() is not None
        if not exists:
            raise SessionNotFoundError(f"session not found: {session_id}")
        return self.resume_messages(session_id)

    def message_count(self, session_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id = ? AND active = 1",
                (session_id,),
            ).fetchone()
        return int(row[0]) if row else 0

    # ── 快照（checkpoint）+ 尾部增量重放（PORT）──────────────────────────

    def get_max_active_seq(self, session_id: str) -> int:
        """会话当前最大 active seq（无消息返回 -1）——快照生成器的「历史游标」。"""
        if not session_id:
            return -1
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq), -1) FROM messages "
                "WHERE session_id = ? AND active = 1",
                (session_id,),
            ).fetchone()
        return int(row[0]) if row and row[0] is not None else -1

    def get_history_version(self, session_id: str) -> int:
        """读会话的 history_version（会话不存在返回 0）。"""
        if not session_id:
            return 0
        with self._lock:
            row = self._conn.execute(
                "SELECT history_version FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return int(row["history_version"] or 0) if row else 0

    def bump_history_version(self, session_id: str) -> int:
        """递增 history_version，使旧快照自动失效（历史改写路径调用）。返回新版本。"""
        if not session_id:
            return 0

        def _do(conn):
            conn.execute(
                "UPDATE sessions SET history_version = history_version + 1 WHERE id = ?",
                (session_id,),
            )
            row = conn.execute(
                "SELECT history_version FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            return int(row["history_version"] or 0) if row else 0

        return self._execute_write(_do)

    def write_snapshot(self, session_id: str, keep: int = 3) -> int | None:
        """把当前完整历史写成一个恢复快照。返回快照行 id，失败返回 None（绝不抛出）。

        快照内容从**已提交的 DB 行**经与全量重放相同的解码器生成，因此
        「快照 + 尾部」与全量重放逐字节一致；last_seq 取同一批行的 MAX(seq)，
        恢复时只重放 seq > last_seq 的尾部。每个会话只保留最近 keep 条快照。
        """
        if not session_id:
            return None

        def _do(conn):
            rows = conn.execute(
                f"SELECT {self._MSG_COLUMNS} FROM messages "
                "WHERE session_id = ? AND active = 1 ORDER BY id",
                (session_id,),
            ).fetchall()
            if not rows:
                return None
            dicts = [self._row_to_dict(r) for r in rows]
            last_seq = max((int(d["seq"]) for d in dicts if d["seq"] is not None), default=-1)
            # 直接在同一事务里读 history_version（不能调 get_history_version：
            # 它会重入 self._lock，而 _execute_write 已持有 → 死锁）。
            hv_row = conn.execute(
                "SELECT history_version FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            history_version = int(hv_row["history_version"] or 0) if hv_row else 0
            snapshot_json = json.dumps(dicts, ensure_ascii=False)
            checksum = f"{zlib.crc32(snapshot_json.encode('utf-8')) & 0xFFFFFFFF:08x}"
            # zlib level 1：长会话历史是高度重复的文本，压缩省 5-10x 存储与读回页面。
            blob = zlib.compress(snapshot_json.encode("utf-8"), 1)
            cursor = conn.execute(
                "INSERT INTO session_snapshots "
                "(session_id, last_seq, history_version, snapshot_json, checksum, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    last_seq,
                    history_version,
                    blob,
                    checksum,
                    time.time(),
                ),
            )
            new_id = cursor.lastrowid
            conn.execute(
                "DELETE FROM session_snapshots WHERE session_id = ? AND id NOT IN "
                "(SELECT id FROM session_snapshots WHERE session_id = ? "
                "ORDER BY id DESC LIMIT ?)",
                (session_id, session_id, max(1, keep)),
            )
            return new_id

        try:
            return self._execute_write(_do)
        except Exception as exc:  # noqa: BLE001 — 快照失败绝不能打断主流程
            logger.warning("write_snapshot failed for %s: %s", session_id, exc)
            return None

    def list_sessions(self) -> list[dict]:
        """Return all sessions with message counts, newest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, source, title, message_count, created_at FROM sessions "
                "ORDER BY created_at DESC"
            ).fetchall()
        return [
            {
                "id": r["id"],
                "source": r["source"],
                "title": r["title"],
                "message_count": r["message_count"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    def load_latest_snapshot(
        self, session_id: str, expected_history_version: int
    ) -> dict | None:
        """加载最新有效快照；无快照 / 版本不符 / 校验失败 / 解析失败 → None（降级全量）。

        成功返回 {"last_seq": int, "messages": [dict, ...]}。
        """
        if not session_id:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT last_seq, history_version, snapshot_json, checksum "
                "FROM session_snapshots WHERE session_id = ? ORDER BY id DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        if row is None:
            return None

        if int(row["history_version"] or 0) != int(expected_history_version or 0):
            logger.debug("snapshot for %s stale (version mismatch); full replay", session_id)
            return None

        raw = row["snapshot_json"]
        try:
            snapshot_json = raw if isinstance(raw, str) else zlib.decompress(raw).decode("utf-8")
        except (zlib.error, TypeError, ValueError) as exc:
            logger.warning("snapshot for %s failed to decompress (%s); full replay", session_id, exc)
            return None
        # CRC32（而非 sha256）：威胁模型是意外损坏（撕裂写/位衰减），CRC32 可靠且
        # 在每次恢复都跑的路径上快约 10x；对抗性篡改不在范围内（能改库的人直接改
        # messages 表即可）。
        actual = f"{zlib.crc32(snapshot_json.encode('utf-8')) & 0xFFFFFFFF:08x}"
        if actual != row["checksum"]:
            logger.warning("snapshot for %s failed checksum; falling back to full replay", session_id)
            return None
        try:
            dicts = json.loads(snapshot_json)
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning("snapshot for %s failed to parse (%s); full replay", session_id, exc)
            return None
        if not isinstance(dicts, list):
            return None
        return {"last_seq": int(row["last_seq"]), "messages": dicts}

    def get_messages_after_seq(self, session_id: str, last_seq: int) -> list[dict]:
        """取尾部（seq > last_seq）消息 dict——与全量重放同一解码器，按 id 排序。"""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {self._MSG_COLUMNS} FROM messages "
                "WHERE session_id = ? AND seq > ? AND active = 1 ORDER BY id",
                (session_id, last_seq),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def resume_messages(self, session_id: str) -> list[Message]:
        """恢复会话历史：快路径 = 最新有效快照 + 尾部增量；否则全量重放。

        全量重放本身永远正确，所以快路径绝不牺牲正确性——只跳过已快照前缀
        的反序列化。任何一步异常都降级全量。
        """
        try:
            snap = self.load_latest_snapshot(session_id, self.get_history_version(session_id))
        except Exception as exc:  # noqa: BLE001 — 快照查询失败降级全量
            logger.debug("snapshot lookup failed (%s); full replay", exc)
            snap = None

        if snap is None:
            return self._full_replay(session_id)

        try:
            tail = self.get_messages_after_seq(session_id, snap["last_seq"])
            return [self._message_from_dict(d) for d in list(snap["messages"]) + tail]
        except Exception as exc:  # noqa: BLE001 — 尾部重放失败降级全量
            logger.warning("snapshot tail replay failed for %s (%s); full replay", session_id, exc)
            return self._full_replay(session_id)

    def _full_replay(self, session_id: str) -> list[Message]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {self._MSG_COLUMNS} FROM messages "
                "WHERE session_id = ? AND active = 1 ORDER BY id",
                (session_id,),
            ).fetchall()
        return [self._message_from_dict(self._row_to_dict(row)) for row in rows]

    def maybe_write_snapshot(
        self, session_id: str, *, every_n: int, keep: int = 3, force: bool = False
    ) -> int | None:
        """按消息数节奏写快照（每 every_n 条新消息一次；force 无视节奏）。

        调用方（CLI）在每轮结束后调用；内部用 per-session 游标去重。
        """
        if every_n <= 0:
            return None
        current_seq = self.get_max_active_seq(session_id)
        if current_seq < 0:
            return None
        last = self._last_snapshot_seq.get(session_id, -1)
        if not force and (current_seq - last) < every_n:
            return None
        new_id = self.write_snapshot(session_id, keep=keep)
        if new_id is not None:
            self._last_snapshot_seq[session_id] = current_seq
        return new_id

    # ── 会话租约表方法（PORT；由 sessions/lease.py 的 SQLite 后端调用）────

    def try_acquire_session_lease(
        self,
        session_id: str,
        owner_token: str,
        ttl_seconds: float = 30.0,
        pid: int | None = None,
        hostname: str | None = None,
    ) -> bool:
        """原子获取租约（仅当空闲或已过期）。单事务 删过期 + INSERT OR IGNORE + 查属主。"""
        if not session_id or not owner_token:
            return False
        now = time.time()
        expires_at = now + ttl_seconds

        def _do(conn):
            conn.execute(
                "DELETE FROM session_leases WHERE session_id = ? AND expires_at < ?",
                (session_id, now),
            )
            conn.execute(
                "INSERT OR IGNORE INTO session_leases "
                "(session_id, owner_token, pid, hostname, acquired_at, heartbeat_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (session_id, owner_token, pid, hostname, now, now, expires_at),
            )
            row = conn.execute(
                "SELECT owner_token FROM session_leases WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            return row is not None and row["owner_token"] == owner_token

        try:
            return bool(self._execute_write(_do))
        except sqlite3.Error as exc:
            logger.warning("try_acquire_session_lease(%s) failed: %s", session_id, exc)
            # 关闭失败：拿不到租约的调用方绝不能写。
            return False

    def renew_session_lease(
        self, session_id: str, owner_token: str, ttl_seconds: float = 30.0
    ) -> bool:
        """仅当我们仍是属主时续期。False = 已丢失（过期被回收）或后端不可达。"""
        if not session_id or not owner_token:
            return False
        now = time.time()

        def _do(conn):
            cur = conn.execute(
                "UPDATE session_leases SET heartbeat_at = ?, expires_at = ? "
                "WHERE session_id = ? AND owner_token = ? AND expires_at >= ?",
                (now, now + ttl_seconds, session_id, owner_token, now),
            )
            return cur.rowcount == 1

        try:
            return bool(self._execute_write(_do))
        except sqlite3.Error as exc:
            logger.warning("renew_session_lease(%s) failed: %s", session_id, exc)
            # 关闭失败：不可续期必须触发调用方的熔断，而不是冒险双写。
            return False

    def release_session_lease(self, session_id: str, owner_token: str) -> None:
        """仅当我们是属主时释放。幂等。"""
        if not session_id or not owner_token:
            return

        def _do(conn):
            conn.execute(
                "DELETE FROM session_leases WHERE session_id = ? AND owner_token = ?",
                (session_id, owner_token),
            )

        try:
            self._execute_write(_do)
        except sqlite3.Error as exc:
            logger.warning("release_session_lease(%s) failed: %s", session_id, exc)

    def is_session_lease_owner(self, session_id: str, owner_token: str) -> bool:
        """owner_token 是否持有未过期的租约。"""
        if not session_id or not owner_token:
            return False
        now = time.time()
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT 1 FROM session_leases "
                    "WHERE session_id = ? AND owner_token = ? AND expires_at >= ?",
                    (session_id, owner_token, now),
                ).fetchone()
            return row is not None
        except sqlite3.Error as exc:
            logger.warning("is_session_lease_owner(%s) failed: %s", session_id, exc)
            return False

    def get_session_lease_info(self, session_id: str) -> dict[str, Any] | None:
        """返回当前租约行（诊断用），无则 None。"""
        if not session_id:
            return None
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT session_id, owner_token, pid, hostname, acquired_at, "
                    "heartbeat_at, expires_at FROM session_leases WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
        except sqlite3.Error:
            return None
        return dict(row) if row is not None else None


__all__ = ["DEFAULT_DB_PATH", "SQLiteSessionRepository"]
