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
import re
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

# ── FTS5 全文检索（移植自 hermes_state.SessionDB）─────────────────────────
# messages 写入时经触发器同步到两张 FTS5 虚拟表；行 id 即 FTS rowid，正文 =
# content || tool_name || tool_calls。主表 messages_fts 用 unicode61 分词器
# （BM25 排序），messages_fts_trigram 用 trigram 分词器（CJK/子串检索）。
# 触发器把「写 messages」与「更新 FTS 索引」绑在同一事务里，保证不会出现
# 「messages 有数据、FTS 没数据」的分裂。
_FTS_TRIGGERS = (
    "messages_fts_insert",
    "messages_fts_delete",
    "messages_fts_update",
    "messages_fts_trigram_insert",
    "messages_fts_trigram_delete",
    "messages_fts_trigram_update",
)

FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content
);

CREATE TRIGGER IF NOT EXISTS messages_fts_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (
        new.id,
        COALESCE(new.content, '') || ' ' || COALESCE(new.tool_name, '') || ' ' || COALESCE(new.tool_calls, '')
    );
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_delete AFTER DELETE ON messages BEGIN
    DELETE FROM messages_fts WHERE rowid = old.id;
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_update AFTER UPDATE ON messages BEGIN
    DELETE FROM messages_fts WHERE rowid = old.id;
    INSERT INTO messages_fts(rowid, content) VALUES (
        new.id,
        COALESCE(new.content, '') || ' ' || COALESCE(new.tool_name, '') || ' ' || COALESCE(new.tool_calls, '')
    );
END;
"""

# trigram 分词器：默认 unicode61 会把 CJK 拆成单字，破坏短语匹配；trigram 生成
# 重叠的三字节序列，让任意文字的子串查询原生可用（CJK、泰文等）。
FTS_TRIGRAM_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts_trigram USING fts5(
    content,
    tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS messages_fts_trigram_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts_trigram(rowid, content) VALUES (
        new.id,
        COALESCE(new.content, '') || ' ' || COALESCE(new.tool_name, '') || ' ' || COALESCE(new.tool_calls, '')
    );
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_trigram_delete AFTER DELETE ON messages BEGIN
    DELETE FROM messages_fts_trigram WHERE rowid = old.id;
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_trigram_update AFTER UPDATE ON messages BEGIN
    DELETE FROM messages_fts_trigram WHERE rowid = old.id;
    INSERT INTO messages_fts_trigram(rowid, content) VALUES (
        new.id,
        COALESCE(new.content, '') || ' ' || COALESCE(new.tool_name, '') || ' ' || COALESCE(new.tool_calls, '')
    );
END;
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
        self._fts_enabled = False
        self._fts_unavailable_warned = False

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
            self._init_fts(self._conn.cursor())
            self._conn.commit()

    # ── FTS5 初始化与同步（PORT）──────────────────────────────────────────

    @staticmethod
    def _is_fts5_unavailable_error(exc: sqlite3.OperationalError) -> bool:
        err = str(exc).lower()
        return "no such module" in err and "fts5" in err

    def _warn_fts5_unavailable(self, exc: sqlite3.OperationalError) -> None:
        self._fts_enabled = False
        if self._fts_unavailable_warned:
            return
        self._fts_unavailable_warned = True
        logger.warning(
            "SQLite FTS5 unavailable for %s; full-text session search disabled "
            "(underlying error: %s)",
            self.db_path,
            exc,
        )

    def _sqlite_supports_fts5(self, cursor: sqlite3.Cursor) -> bool:
        try:
            cursor.execute("CREATE VIRTUAL TABLE temp._aegis_fts5_probe USING fts5(x)")
            cursor.execute("DROP TABLE temp._aegis_fts5_probe")
            return True
        except sqlite3.OperationalError as exc:
            if not self._is_fts5_unavailable_error(exc):
                raise
            self._warn_fts5_unavailable(exc)
            return False

    @staticmethod
    def _drop_fts_triggers(cursor: sqlite3.Cursor) -> None:
        for trigger in _FTS_TRIGGERS:
            try:
                cursor.execute(f"DROP TRIGGER IF EXISTS {trigger}")
            except sqlite3.OperationalError:
                pass

    @staticmethod
    def _fts_trigger_count(cursor: sqlite3.Cursor) -> int:
        placeholders = ",".join("?" for _ in _FTS_TRIGGERS)
        row = cursor.execute(
            f"SELECT COUNT(*) FROM sqlite_master "
            f"WHERE type = 'trigger' AND name IN ({placeholders})",
            _FTS_TRIGGERS,
        ).fetchone()
        return int(row[0])

    @staticmethod
    def _rebuild_fts_indexes(cursor: sqlite3.Cursor) -> None:
        for table_name in ("messages_fts", "messages_fts_trigram"):
            cursor.execute(f"DELETE FROM {table_name}")
        cursor.execute(
            "INSERT INTO messages_fts(rowid, content) "
            "SELECT id, "
            "COALESCE(content, '') || ' ' || "
            "COALESCE(tool_name, '') || ' ' || "
            "COALESCE(tool_calls, '') "
            "FROM messages"
        )
        cursor.execute(
            "INSERT INTO messages_fts_trigram(rowid, content) "
            "SELECT id, "
            "COALESCE(content, '') || ' ' || "
            "COALESCE(tool_name, '') || ' ' || "
            "COALESCE(tool_calls, '') "
            "FROM messages"
        )

    def _fts_table_probe(self, cursor: sqlite3.Cursor, table_name: str) -> bool | None:
        try:
            cursor.execute(f"SELECT * FROM {table_name} LIMIT 0")
            return True
        except sqlite3.OperationalError as exc:
            if self._is_fts5_unavailable_error(exc):
                self._warn_fts5_unavailable(exc)
                return None
            if "no such table" in str(exc).lower():
                return False
            raise

    def _ensure_fts_schema(self, cursor: sqlite3.Cursor, table_name: str, ddl: str) -> bool:
        status = self._fts_table_probe(cursor, table_name)
        if status is None:
            return False
        try:
            # 即使虚拟表已存在也重跑 DDL，以便此前「无 FTS5 的运行」删掉的触发器被
            # CREATE TRIGGER IF NOT EXISTS 重建。
            cursor.executescript(ddl)
            return True
        except sqlite3.OperationalError as exc:
            if not self._is_fts5_unavailable_error(exc):
                raise
            self._warn_fts5_unavailable(exc)
            return False

    def _init_fts(self, cursor: sqlite3.Cursor) -> None:
        """幂等地建 FTS5 虚拟表 + 同步触发器，并在缺失时 backfill 历史消息。

        关键不变式：messages 有数据 ⟹ FTS 有数据。旧库首次启用 FTS（表不存在）、
        或触发器曾被无 FTS5 的运行删掉（triggers_need_repair）时，从 messages
        全量重建索引，使旧消息也能被检索。
        """
        self._fts_enabled = self._sqlite_supports_fts5(cursor)
        if not self._fts_enabled:
            # 当前 sqlite 运行时不支持 FTS5：删掉已存在的触发器，保证核心持久化
            # （INSERT/UPDATE）不被无法读写的虚拟表触发器拖垮。
            self._drop_fts_triggers(cursor)
            return

        triggers_need_repair = self._fts_trigger_count(cursor) < len(_FTS_TRIGGERS)
        messages_fts_existed = self._fts_table_probe(cursor, "messages_fts")

        self._fts_enabled = self._ensure_fts_schema(cursor, "messages_fts", FTS_SQL)
        if not self._fts_enabled:
            return
        trigram_enabled = self._ensure_fts_schema(cursor, "messages_fts_trigram", FTS_TRIGRAM_SQL)

        if trigram_enabled and (triggers_need_repair or messages_fts_existed is False):
            self._rebuild_fts_indexes(cursor)

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

    # ── 历史会话检索（FTS5；移植自 hermes_state.SessionDB）────────────────
    # 这些方法直接返回「DB 行 dict」（含 id / timestamp 等 Message 数据类不携带
    # 的字段），供 session_search 工具做 FTS5 命中、锚点窗口、bookends 和浏览。
    # 它们不参与恢复 / 快照 / 幂等主链路，只读，绝不改写原始消息。

    @staticmethod
    def _search_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        """messages 行 → session_search 用的扁平 dict（tool_calls 反序列化）。"""
        tool_calls: list = []
        if row["tool_calls"]:
            try:
                parsed = json.loads(row["tool_calls"])
                tool_calls = parsed if isinstance(parsed, list) else []
            except (json.JSONDecodeError, TypeError):
                tool_calls = []
        return {
            "id": row["id"],
            "session_id": row["session_id"],
            "role": row["role"],
            "content": row["content"],
            "timestamp": row["timestamp"],
            "tool_name": row["tool_name"],
            "tool_call_id": row["tool_call_id"],
            "tool_calls": tool_calls,
        }

    @staticmethod
    def _sanitize_fts5_query(query: str) -> str:
        """清洗用户输入，使其可安全用于 FTS5 MATCH 查询。

        FTS5 里 ``"`` ``(`` ``)`` ``+`` ``*`` ``{`` ``}`` 及裸布尔算子（AND/OR/NOT）
        有特殊含义，直接把原始输入喂给 MATCH 会抛 sqlite3.OperationalError。

        策略：
        - 保留成对的引号短语（``"exact phrase"``）
        - 去掉未配对的 FTS5 特殊字符
        - 把带连字符/点号的裸词用引号包起来，使其作为精确短语匹配（如
          ``chat-send`` / ``P2.2``），而不是被分词器在连字符/点号处拆开
        """
        quoted_parts: list = []

        def _preserve_quoted(m: re.Match) -> str:
            quoted_parts.append(m.group(0))
            return f"\x00Q{len(quoted_parts) - 1}\x00"

        sanitized = re.sub(r'"[^"]*"', _preserve_quoted, query)
        sanitized = re.sub(r'[+{}()\"^]', " ", sanitized)
        sanitized = re.sub(r"\*+", "*", sanitized)
        sanitized = re.sub(r"(^|\s)\*", r"\1", sanitized)
        sanitized = re.sub(r"(?i)^(AND|OR|NOT)\b\s*", "", sanitized.strip())
        sanitized = re.sub(r"(?i)\s+(AND|OR|NOT)\s*$", "", sanitized.strip())
        sanitized = re.sub(r"\b(\w+(?:[._-]\w+)+)\b", r'"\1"', sanitized)
        for i, quoted in enumerate(quoted_parts):
            sanitized = sanitized.replace(f"\x00Q{i}\x00", quoted)
        return sanitized.strip()

    @staticmethod
    def _is_cjk_codepoint(cp: int) -> bool:
        return (
            0x4E00 <= cp <= 0x9FFF or    # CJK 统一表意文字
            0x3400 <= cp <= 0x4DBF or    # CJK 扩展 A
            0x20000 <= cp <= 0x2A6DF or  # CJK 扩展 B
            0x3000 <= cp <= 0x303F or    # CJK 符号
            0x3040 <= cp <= 0x309F or    # 平假名
            0x30A0 <= cp <= 0x30FF or    # 片假名
            0xAC00 <= cp <= 0xD7AF       # 谚文音节
        )

    @staticmethod
    def _contains_cjk(text: str) -> bool:
        for ch in text:
            if SQLiteSessionRepository._is_cjk_codepoint(ord(ch)):
                return True
        return False

    @staticmethod
    def _count_cjk(text: str) -> int:
        return sum(1 for ch in text if SQLiteSessionRepository._is_cjk_codepoint(ord(ch)))

    def search_messages(
        self,
        query: str,
        source_filter: list[str] | None = None,
        exclude_sources: list[str] | None = None,
        role_filter: list[str] | None = None,
        limit: int = 20,
        offset: int = 0,
        sort: str | None = None,
        include_inactive: bool = False,
    ) -> list[dict[str, Any]]:
        """跨会话全文检索，使用 FTS5（BM25 排序）。

        支持 FTS5 查询语法：简单关键词、``"精确短语"``、布尔（``docker OR kubernetes``、
        ``python NOT java``）、前缀（``deploy*``）。

        返回命中消息 + 会话元信息 + 片段 + 命中前后各 1 条上下文。``sort``：
        ``None``（默认）仅按 BM25 相关性；``"newest"``/``"oldest"`` 按时间戳
        排序、rank 作 tiebreaker。默认排除软删除（active=0）行。
        """
        if not self._fts_enabled:
            return []
        if not query or not query.strip():
            return []

        query = self._sanitize_fts5_query(query)
        if not query:
            return []

        sort_norm: str | None = None
        if isinstance(sort, str):
            candidate = sort.strip().lower()
            if candidate in ("newest", "oldest"):
                sort_norm = candidate

        if sort_norm == "newest":
            order_by_sql = "ORDER BY m.timestamp DESC, rank"
        elif sort_norm == "oldest":
            order_by_sql = "ORDER BY m.timestamp ASC, rank"
        else:
            order_by_sql = "ORDER BY rank"

        where_clauses = ["messages_fts MATCH ?"]
        params: list = [query]
        if not include_inactive:
            where_clauses.append("m.active = 1")
        if source_filter is not None:
            where_clauses.append(f"s.source IN ({','.join('?' for _ in source_filter)})")
            params.extend(source_filter)
        if exclude_sources is not None:
            where_clauses.append(f"s.source NOT IN ({','.join('?' for _ in exclude_sources)})")
            params.extend(exclude_sources)
        if role_filter:
            where_clauses.append(f"m.role IN ({','.join('?' for _ in role_filter)})")
            params.extend(role_filter)

        where_sql = " AND ".join(where_clauses)
        params.extend([limit, offset])

        sql = f"""
            SELECT
                m.id,
                m.session_id,
                m.role,
                snippet(messages_fts, 0, '>>>', '<<<', '...', 40) AS snippet,
                m.content,
                m.timestamp,
                m.tool_name,
                s.source,
                s.created_at AS session_started
            FROM messages_fts
            JOIN messages m ON m.id = messages_fts.rowid
            JOIN sessions s ON s.id = m.session_id
            WHERE {where_sql}
            {order_by_sql}
            LIMIT ? OFFSET ?
        """

        is_cjk = self._contains_cjk(query)
        if is_cjk:
            raw_query = query.strip('"').strip()
            cjk_count = self._count_cjk(raw_query)
            _tokens_for_check = [
                t for t in raw_query.split()
                if t.upper() not in {"AND", "OR", "NOT"} and self._contains_cjk(t)
            ]
            _any_short_cjk = any(self._count_cjk(t) < 3 for t in _tokens_for_check)

            if cjk_count >= 3 and not _any_short_cjk:
                tokens = raw_query.split()
                parts = []
                for tok in tokens:
                    if tok.upper() in {"AND", "OR", "NOT"}:
                        parts.append(tok)
                    else:
                        parts.append('"' + tok.replace('"', '""') + '"')
                trigram_query = " ".join(parts)
                tri_where = ["messages_fts_trigram MATCH ?"]
                tri_params: list = [trigram_query]
                if not include_inactive:
                    tri_where.append("m.active = 1")
                if source_filter is not None:
                    tri_where.append(f"s.source IN ({','.join('?' for _ in source_filter)})")
                    tri_params.extend(source_filter)
                if exclude_sources is not None:
                    tri_where.append(f"s.source NOT IN ({','.join('?' for _ in exclude_sources)})")
                    tri_params.extend(exclude_sources)
                if role_filter:
                    tri_where.append(f"m.role IN ({','.join('?' for _ in role_filter)})")
                    tri_params.extend(role_filter)
                tri_sql = f"""
                    SELECT
                        m.id,
                        m.session_id,
                        m.role,
                        snippet(messages_fts_trigram, 0, '>>>', '<<<', '...', 40) AS snippet,
                        m.content,
                        m.timestamp,
                        m.tool_name,
                        s.source,
                        s.created_at AS session_started
                    FROM messages_fts_trigram
                    JOIN messages m ON m.id = messages_fts_trigram.rowid
                    JOIN sessions s ON s.id = m.session_id
                    WHERE {' AND '.join(tri_where)}
                    {order_by_sql}
                    LIMIT ? OFFSET ?
                """
                tri_params.extend([limit, offset])
                with self._lock:
                    try:
                        tri_cursor = self._conn.execute(tri_sql, tri_params)
                    except sqlite3.OperationalError:
                        matches = []
                    else:
                        matches = [dict(row) for row in tri_cursor.fetchall()]
            else:
                non_op_tokens = [
                    t for t in raw_query.split()
                    if t.upper() not in {"AND", "OR", "NOT"}
                ] or [raw_query]
                token_clauses = []
                like_params: list = []
                for tok in non_op_tokens:
                    esc = tok.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                    token_clauses.append(
                        "(m.content LIKE ? ESCAPE '\\' OR m.tool_name LIKE ? ESCAPE '\\' OR m.tool_calls LIKE ? ESCAPE '\\')"
                    )
                    like_params += [f"%{esc}%", f"%{esc}%", f"%{esc}%"]
                like_where = [f"({' OR '.join(token_clauses)})"]
                if source_filter is not None:
                    like_where.append(f"s.source IN ({','.join('?' for _ in source_filter)})")
                    like_params.extend(source_filter)
                if exclude_sources is not None:
                    like_where.append(f"s.source NOT IN ({','.join('?' for _ in exclude_sources)})")
                    like_params.extend(exclude_sources)
                if role_filter:
                    like_where.append(f"m.role IN ({','.join('?' for _ in role_filter)})")
                    like_params.extend(role_filter)
                like_sql = f"""
                    SELECT m.id, m.session_id, m.role,
                           substr(m.content,
                                  max(1, instr(m.content, ?) - 40),
                                  120) AS snippet,
                           m.content, m.timestamp, m.tool_name,
                           s.source, s.created_at AS session_started
                    FROM messages m
                    JOIN sessions s ON s.id = m.session_id
                    WHERE {' AND '.join(like_where)}
                    ORDER BY m.timestamp DESC
                    LIMIT ? OFFSET ?
                """
                like_params.extend([limit, offset])
                like_params = [non_op_tokens[0]] + like_params
                with self._lock:
                    like_cursor = self._conn.execute(like_sql, like_params)
                    matches = [dict(row) for row in like_cursor.fetchall()]
        else:
            with self._lock:
                try:
                    cursor = self._conn.execute(sql, params)
                except sqlite3.OperationalError:
                    return []
                else:
                    matches = [dict(row) for row in cursor.fetchall()]

        # 命中前后各 1 条上下文（锁外执行，避免跨 N 次查询持锁）。
        for match in matches:
            try:
                with self._lock:
                    ctx_cursor = self._conn.execute(
                        """WITH target AS (
                               SELECT session_id, timestamp, id FROM messages WHERE id = ?
                           )
                           SELECT role, content FROM (
                               SELECT m.id, m.timestamp, m.role, m.content
                               FROM messages m
                               JOIN target t ON t.session_id = m.session_id
                               WHERE m.active = 1 AND ((m.timestamp < t.timestamp)
                                  OR (m.timestamp = t.timestamp AND m.id < t.id))
                               ORDER BY m.timestamp DESC, m.id DESC LIMIT 1
                           )
                           UNION ALL
                           SELECT role, content FROM messages WHERE id = ?
                           UNION ALL
                           SELECT role, content FROM (
                               SELECT m.id, m.timestamp, m.role, m.content
                               FROM messages m
                               JOIN target t ON t.session_id = m.session_id
                               WHERE m.active = 1 AND ((m.timestamp > t.timestamp)
                                  OR (m.timestamp = t.timestamp AND m.id > t.id))
                               ORDER BY m.timestamp ASC, m.id ASC LIMIT 1
                           )""",
                        (match["id"], match["id"]),
                    )
                    context_msgs = [
                        {"role": r["role"], "content": (r["content"] or "")[:200]}
                        for r in ctx_cursor.fetchall()
                    ]
                match["context"] = context_msgs
            except Exception:  # noqa: BLE001 — 单条命中的上下文缺失不应拖垮整个搜索
                match["context"] = []

        # 去掉完整正文（snippet 足够，省 token）。
        for match in matches:
            match.pop("content", None)

        return matches

    def get_messages_around(
        self,
        session_id: str,
        around_message_id: int,
        window: int = 5,
    ) -> dict[str, Any]:
        """加载锚定在某条消息 id 上的窗口（±window，含锚点，按 id 升序）。

        返回 ``{"window", "messages_before", "messages_after"}``。当
        ``around_message_id`` 不在 ``session_id`` 里时返回空窗口。
        """
        window = max(0, window)
        with self._lock:
            anchor_exists = self._conn.execute(
                "SELECT 1 FROM messages WHERE id = ? AND session_id = ? AND active = 1 LIMIT 1",
                (around_message_id, session_id),
            ).fetchone()
            if not anchor_exists:
                return {"window": [], "messages_before": 0, "messages_after": 0}

            before_rows = self._conn.execute(
                "SELECT * FROM messages "
                "WHERE session_id = ? AND id <= ? AND active = 1 "
                "ORDER BY id DESC LIMIT ?",
                (session_id, around_message_id, window + 1),
            ).fetchall()
            after_rows = self._conn.execute(
                "SELECT * FROM messages "
                "WHERE session_id = ? AND id > ? AND active = 1 "
                "ORDER BY id ASC LIMIT ?",
                (session_id, around_message_id, window),
            ).fetchall()

        rows = list(reversed(before_rows)) + list(after_rows)
        result = [self._search_row_to_dict(row) for row in rows]
        return {
            "window": result,
            "messages_before": max(0, len(before_rows) - 1),
            "messages_after": len(after_rows),
        }

    def get_anchored_view(
        self,
        session_id: str,
        around_message_id: int,
        window: int = 5,
        bookend: int = 3,
        keep_roles: tuple[str, ...] | None = ("user", "assistant"),
    ) -> dict[str, Any]:
        """返回锚点窗口 + 会话 bookends（开头/结尾各 ``bookend`` 条 user/assistant）。

        三块：``window``（锚点附近，按 ``keep_roles`` 过滤、但锚点本身永保留）、
        ``bookend_start``（窗口之前最早几条 user/assistant）、``bookend_end``
        （窗口之后最后几条）。让 FTS5 命中长会话任意位置时，一次调用即可拿到
        「目标（开头）→ 命中 → 结论（结尾）」而无需加载整段转录。
        """
        bookend = max(0, bookend)

        primitive = self.get_messages_around(session_id, around_message_id, window=window)
        window_rows = primitive["window"]
        if not window_rows:
            return {
                "window": [],
                "messages_before": 0,
                "messages_after": 0,
                "bookend_start": [],
                "bookend_end": [],
            }

        if keep_roles is not None:
            keep_set = set(keep_roles)
            filtered_window = [
                m for m in window_rows
                if m.get("id") == around_message_id or m.get("role") in keep_set
            ]
        else:
            filtered_window = window_rows

        window_min_id = window_rows[0]["id"]
        window_max_id = window_rows[-1]["id"]

        bookend_start_rows: list = []
        bookend_end_rows: list = []
        if bookend > 0:
            with self._lock:
                role_clause = ""
                role_params: list = []
                if keep_roles is not None:
                    role_clause = f" AND role IN ({','.join('?' for _ in keep_roles)})"
                    role_params = list(keep_roles)

                bookend_start_rows = self._conn.execute(
                    f"SELECT * FROM messages "
                    f"WHERE session_id = ? AND id < ? AND active = 1{role_clause} "
                    f"AND length(content) > 0 "
                    f"ORDER BY id ASC LIMIT ?",
                    (session_id, window_min_id, *role_params, bookend),
                ).fetchall()

                bookend_end_rows = self._conn.execute(
                    f"SELECT * FROM messages "
                    f"WHERE session_id = ? AND id > ? AND active = 1{role_clause} "
                    f"AND length(content) > 0 "
                    f"ORDER BY id DESC LIMIT ?",
                    (session_id, window_max_id, *role_params, bookend),
                ).fetchall()
                bookend_end_rows = list(reversed(bookend_end_rows))

        return {
            "window": filtered_window,
            "messages_before": primitive["messages_before"],
            "messages_after": primitive["messages_after"],
            "bookend_start": [self._search_row_to_dict(r) for r in bookend_start_rows],
            "bookend_end": [self._search_row_to_dict(r) for r in bookend_end_rows],
        }

    def get_messages_dict(self, session_id: str) -> list[dict[str, Any]]:
        """按 id 顺序返回会话全部 active 消息的扁平 dict（读 shape 用）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM messages WHERE session_id = ? AND active = 1 ORDER BY id",
                (session_id,),
            ).fetchall()
        return [self._search_row_to_dict(row) for row in rows]

    def get_session_dict(self, session_id: str) -> dict[str, Any] | None:
        """返回会话元信息原始 dict（session_search 用）。``started_at`` 别名为
        ``created_at``（Aegis 无 started_at 列），``model`` 恒为 None（Aegis 不记录）。
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["started_at"] = d.get("created_at")
        d.setdefault("model", None)
        return d

    def list_sessions_rich(
        self,
        limit: int = 20,
        offset: int = 0,
        exclude_sources: list[str] | None = None,
        order_by_last_active: bool = False,
    ) -> list[dict[str, Any]]:
        """列出会话 + 预览（首条 user 消息前 60 字符）+ 最近活跃时间。

        Aegis 无压缩链（parent_session_id 未迁移），故这里是 Hermes
        ``list_sessions_rich`` 的简化版：去掉递归 CTE 的压缩链投影与 child
        过滤，保留单查询 + 相关子查询（预览 / last_active）。
        """
        where_clauses: list[str] = []
        params: list = []
        if exclude_sources:
            where_clauses.append(f"s.source NOT IN ({','.join('?' for _ in exclude_sources)})")
            params.extend(exclude_sources)
        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

        if order_by_last_active:
            order_sql = "ORDER BY last_active DESC, s.created_at DESC, s.id DESC"
        else:
            order_sql = "ORDER BY s.created_at DESC"

        query = f"""
            SELECT s.*,
                COALESCE(
                    (SELECT SUBSTR(REPLACE(REPLACE(m.content, X'0A', ' '), X'0D', ' '), 1, 63)
                     FROM messages m
                     WHERE m.session_id = s.id AND m.role = 'user' AND m.content IS NOT NULL
                     ORDER BY m.timestamp, m.id LIMIT 1),
                    ''
                ) AS _preview_raw,
                COALESCE(
                    (SELECT MAX(m2.timestamp) FROM messages m2 WHERE m2.session_id = s.id),
                    s.created_at
                ) AS last_active
            FROM sessions s
            {where_sql}
            {order_sql}
            LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()

        sessions: list[dict[str, Any]] = []
        for row in rows:
            s = dict(row)
            raw = s.pop("_preview_raw", "").strip()
            s["preview"] = (raw[:60] + ("..." if len(raw) > 60 else "")) if raw else ""
            s["started_at"] = s.get("created_at")
            sessions.append(s)
        return sessions


__all__ = ["DEFAULT_DB_PATH", "SQLiteSessionRepository"]
