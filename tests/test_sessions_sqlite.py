"""Tests for the SQLite session store: idempotent persistence, crash
durability, and snapshot+tail fast resume (ported from Hermes' session
commits 5a51f55 / 181e078).

Invariants under test (CLAUDE.md §9):
  * one persisted logical message per client message ID;
  * monotonically ordered messages within a session;
  * no cross-session history;
  * checkpoint recovery equals full replay;
  * corrupted checkpoints fall back safely;
  * resume-then-continue produces no duplicates.
"""

from __future__ import annotations

import sqlite3

import pytest

from aegis_agent.exceptions import SessionNotFoundError
from aegis_agent.models.base import Message, Role, ToolCall
from aegis_agent.sessions.sqlite_store import SQLiteSessionRepository


def _msg(role: Role, content: str, uid: str, **kwargs) -> Message:
    return Message(role=role, content=content, client_msg_id=uid, **kwargs)


def _seed(repo: SQLiteSessionRepository, session: str, count: int) -> list[Message]:
    out = []
    for i in range(count):
        out.append(repo.append_message(session, _msg(Role.USER, f"u{i}", f"u-{i}")))
        out.append(repo.append_message(session, _msg(Role.ASSISTANT, f"a{i}", f"a-{i}")))
    return out


# ---------------------------------------------------------------------------
# protocol conformance & idempotency
# ---------------------------------------------------------------------------


def test_append_and_list_roundtrip(tmp_path) -> None:
    repo = SQLiteSessionRepository(tmp_path / "s.db")
    repo.create_session("s")
    repo.append_message("s", _msg(Role.USER, "hello", "m1"))
    repo.append_message("s", Message(
        role=Role.ASSISTANT,
        content="working",
        tool_calls=[ToolCall(id="c1", name="terminal", arguments='{"command": "ls"}')],
        reasoning_content="thinking",
        client_msg_id="m2",
    ))
    repo.append_message("s", _msg(Role.TOOL, "out", "m3", tool_call_id="c1", name="terminal"))

    msgs = repo.list_messages("s")
    assert [m.content for m in msgs] == ["hello", "working", "out"]
    assert [m.seq for m in msgs] == [0, 1, 2]
    assert msgs[1].tool_calls == [ToolCall(id="c1", name="terminal", arguments='{"command": "ls"}')]
    assert msgs[1].reasoning_content == "thinking"
    assert msgs[2].tool_call_id == "c1" and msgs[2].name == "terminal"
    repo.close()


def test_idempotent_append_same_client_msg_id(tmp_path) -> None:
    repo = SQLiteSessionRepository(tmp_path / "s.db")
    repo.create_session("s")
    first = repo.append_message("s", _msg(Role.USER, "hello", "dup"))
    again = repo.append_message("s", _msg(Role.USER, "hello", "dup"))
    assert repo.message_count("s") == 1
    assert again.seq == first.seq  # returns the existing record
    # session counter stays consistent with the real row count
    row = repo._conn.execute("SELECT message_count FROM sessions WHERE id='s'").fetchone()
    assert row["message_count"] == 1
    repo.close()


def test_sessions_are_isolated(tmp_path) -> None:
    repo = SQLiteSessionRepository(tmp_path / "s.db")
    repo.create_session("a")
    repo.create_session("b")
    repo.append_message("a", _msg(Role.USER, "for-a", "a1"))
    repo.append_message("b", _msg(Role.USER, "for-b", "b1"))
    assert [m.content for m in repo.list_messages("a")] == ["for-a"]
    assert [m.content for m in repo.list_messages("b")] == ["for-b"]
    repo.close()


def test_append_to_missing_session_raises(tmp_path) -> None:
    repo = SQLiteSessionRepository(tmp_path / "s.db")
    with pytest.raises(SessionNotFoundError):
        repo.append_message("ghost", _msg(Role.USER, "x", "g1"))
    with pytest.raises(SessionNotFoundError):
        repo.list_messages("ghost")
    repo.close()


def test_persistence_across_instances(tmp_path) -> None:
    """Committed rows are visible to a brand-new connection (crash durability)."""
    db = tmp_path / "s.db"
    repo1 = SQLiteSessionRepository(db)
    repo1.create_session("s")
    repo1.append_message("s", _msg(Role.USER, "durable", "d1"))
    # Do NOT close repo1 — a second connection must still see committed rows.
    repo2 = SQLiteSessionRepository(db)
    assert [m.content for m in repo2.list_messages("s")] == ["durable"]
    repo1.close()
    repo2.close()


def test_concurrent_writers_all_rows_unique_seqs(tmp_path) -> None:
    """Two writer instances interleaving appends: no lost rows, unique seqs."""
    import threading

    db = tmp_path / "s.db"
    repo_a = SQLiteSessionRepository(db)
    repo_a.create_session("s")
    repo_b = SQLiteSessionRepository(db)

    def writer(repo, prefix):
        for i in range(10):
            repo.append_message("s", _msg(Role.USER, f"{prefix}{i}", f"{prefix}-{i}"))

    threads = [threading.Thread(target=writer, args=(repo_a, "a")),
               threading.Thread(target=writer, args=(repo_b, "b"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    msgs = repo_a.list_messages("s")
    assert len(msgs) == 20
    assert sorted(m.seq for m in msgs) == list(range(20))
    repo_a.close()
    repo_b.close()


# ---------------------------------------------------------------------------
# snapshot + tail fast resume
# ---------------------------------------------------------------------------


def test_snapshot_resume_reconstructs_identically(tmp_path) -> None:
    repo = SQLiteSessionRepository(tmp_path / "s.db")
    repo.create_session("s")
    for i in range(6):
        repo.append_message("s", _msg(Role.USER, f"u{i}", f"u-{i}"))
        repo.append_message("s", Message(
            role=Role.ASSISTANT, content=f"a{i}", client_msg_id=f"a-{i}",
            reasoning_content=f"r{i}",
        ))
    snap_id = repo.write_snapshot("s")
    assert snap_id is not None
    # tail after the snapshot
    repo.append_message("s", _msg(Role.USER, "tail-q", "t-1"))
    repo.append_message("s", _msg(Role.ASSISTANT, "tail-a", "t-2"))

    fast = repo.resume_messages("s")
    full = repo._full_replay("s")
    assert fast == full
    assert len(fast) == 14
    assert fast[-2].content == "tail-q" and fast[-2].seq == 12
    assert fast[1].reasoning_content == "r0"
    repo.close()


def test_no_snapshot_falls_back_to_full_replay(tmp_path) -> None:
    repo = SQLiteSessionRepository(tmp_path / "s.db")
    repo.create_session("s")
    _seed(repo, "s", 3)
    assert repo.resume_messages("s") == repo._full_replay("s")
    repo.close()


def test_corrupted_blob_falls_back_safely(tmp_path) -> None:
    repo = SQLiteSessionRepository(tmp_path / "s.db")
    repo.create_session("s")
    _seed(repo, "s", 3)
    repo.write_snapshot("s")
    # 撕裂写模拟：把快照 blob 改成无法解压的字节
    repo._conn.execute(
        "UPDATE session_snapshots SET snapshot_json = ? WHERE session_id = 's'",
        (b"\x00\x01garbage",),
    )
    repo._conn.commit()
    assert repo.resume_messages("s") == repo._full_replay("s")
    repo.close()


def test_bad_checksum_falls_back_safely(tmp_path) -> None:
    repo = SQLiteSessionRepository(tmp_path / "s.db")
    repo.create_session("s")
    _seed(repo, "s", 3)
    repo.write_snapshot("s")
    repo._conn.execute(
        "UPDATE session_snapshots SET checksum = 'deadbeef' WHERE session_id = 's'"
    )
    repo._conn.commit()
    assert repo.resume_messages("s") == repo._full_replay("s")
    repo.close()


def test_stale_history_version_invalidates_snapshot(tmp_path) -> None:
    repo = SQLiteSessionRepository(tmp_path / "s.db")
    repo.create_session("s")
    _seed(repo, "s", 3)
    repo.write_snapshot("s")
    repo.bump_history_version("s")  # 历史被改写（/undo 等）→ 旧快照必须失效
    assert repo.load_latest_snapshot("s", repo.get_history_version("s")) is None
    assert repo.resume_messages("s") == repo._full_replay("s")
    repo.close()


def test_only_recent_snapshots_kept(tmp_path) -> None:
    repo = SQLiteSessionRepository(tmp_path / "s.db")
    repo.create_session("s")
    for round_no in range(5):
        repo.append_message("s", _msg(Role.USER, f"u{round_no}", f"u-{round_no}"))
        repo.write_snapshot("s", keep=2)
    count = repo._conn.execute(
        "SELECT COUNT(*) FROM session_snapshots WHERE session_id = 's'"
    ).fetchone()[0]
    assert count == 2
    repo.close()


def test_resume_then_continue_no_duplicates(tmp_path) -> None:
    """Simulate kill -9 + resume: history reloads, new turn appends continue
    monotonically, and re-flushing resumed messages inserts nothing."""
    db = tmp_path / "s.db"
    repo1 = SQLiteSessionRepository(db)
    repo1.create_session("s")
    _seed(repo1, "s", 3)
    repo1.write_snapshot("s")
    repo1.close()  # stand-in for process exit (committed rows survive)

    repo2 = SQLiteSessionRepository(db)
    restored = repo2.resume_messages("s")
    assert len(restored) == 6
    # 恢复后继续写：seq 接着走
    new_msg = repo2.append_message("s", _msg(Role.USER, "next", "n-1"))
    assert new_msg.seq == 6
    # 把恢复出来的消息重新 flush 一遍（带原 client_msg_id）→ 全部幂等跳过
    for m in restored:
        repo2.append_message("s", m)
    assert repo2.message_count("s") == 7
    assert [m.seq for m in repo2.list_messages("s")] == list(range(7))
    repo2.close()


def test_maybe_write_snapshot_cadence(tmp_path) -> None:
    repo = SQLiteSessionRepository(tmp_path / "s.db")
    repo.create_session("s")
    _seed(repo, "s", 1)  # 2 messages, max seq = 1
    assert repo.maybe_write_snapshot("s", every_n=5) is None   # below threshold
    _seed_more = [repo.append_message("s", _msg(Role.USER, f"x{i}", f"x-{i}")) for i in range(4)]
    assert repo.maybe_write_snapshot("s", every_n=5) is not None  # crossed
    assert repo.maybe_write_snapshot("s", every_n=5) is None      # already snapshotted here
    assert repo.maybe_write_snapshot("s", every_n=5, force=True) is not None
    repo.close()


# ---------------------------------------------------------------------------
# runtime-level resume integration
# ---------------------------------------------------------------------------


def test_runtime_resumes_history_from_sqlite(tmp_path) -> None:
    """A fresh runtime on the same DB sees the previous process's history."""
    from aegis_agent.models.fake import FakeModelProvider
    from aegis_agent.runtime import AgentRuntime

    class RecordingProvider:
        def __init__(self, inner):
            self._inner = inner
            self.seen = []

        @property
        def name(self):
            return "recording"

        def stream(self, messages, tools=None):
            self.seen.append(list(messages))
            yield from self._inner.stream(messages, tools)

    db = tmp_path / "s.db"
    repo1 = SQLiteSessionRepository(db)
    rt1 = AgentRuntime.with_defaults(
        provider=FakeModelProvider(), repository=repo1,
        enable_skills=False, enable_mcp=False,
    )
    rt1.run_turn("s", "first question")
    repo1.close()

    recording = RecordingProvider(FakeModelProvider())
    repo2 = SQLiteSessionRepository(db)
    rt2 = AgentRuntime.with_defaults(
        provider=recording, repository=repo2,
        enable_skills=False, enable_mcp=False,
    )
    rt2.run_turn("s", "second question")

    sent = recording.seen[0]
    contents = [m.content for m in sent]
    assert "first question" in contents        # previous turn restored
    assert "Echo: first question" in contents  # and its answer
    assert contents[-1] == "second question"
    repo2.close()


def test_sqlite_store_satisfies_repository_protocol(tmp_path) -> None:
    from aegis_agent.sessions.repository import SessionRepository

    repo = SQLiteSessionRepository(tmp_path / "s.db")
    assert isinstance(repo, SessionRepository)
    repo.close()


def test_db_file_is_valid_sqlite(tmp_path) -> None:
    db = tmp_path / "s.db"
    repo = SQLiteSessionRepository(db)
    repo.create_session("s")
    repo.close()
    # independent sanity check with a plain sqlite3 connection
    conn = sqlite3.connect(str(db))
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    conn.close()
