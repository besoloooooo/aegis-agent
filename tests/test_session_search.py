"""Tests for the FTS5-backed ``session_search`` tool (adapted from Hermes'
``tests/tools/test_session_search.py``).

Four calling shapes:
  1. DISCOVERY — pass query → FTS5 + anchored window + bookends per hit
  2. SCROLL    — pass session_id + around_message_id → just the window
  3. READ      — pass session_id only → dump the whole session
  4. BROWSE    — no args → recent sessions chronologically

All run zero LLM calls.  The first class also exercises the FTS5 index
lifecycle (init idempotency, backfill, reopen persistence) on the repository.
"""

from __future__ import annotations

import json
import time

import pytest

from aegis_agent.models.base import Message, Role
from aegis_agent.sessions.sqlite_store import SQLiteSessionRepository
from aegis_agent.tools.builtin.session_search import (
    _HIDDEN_SESSION_SOURCES,
    SessionSearchTool,
    _format_timestamp,
    session_search,
)
from aegis_agent.tools.schemas import SESSION_SEARCH


@pytest.fixture
def db(tmp_path):
    repo = SQLiteSessionRepository(tmp_path / "state.db", source="cli")
    yield repo
    repo.close()


@pytest.fixture
def tool(db):
    return SessionSearchTool(db)


def _append(db, sid: str, role: Role, content: str, uid: str, **kwargs) -> None:
    db.append_message(sid, Message(role=role, content=content, client_msg_id=uid, **kwargs))


def _seed_modpack_sessions(db) -> None:
    """Create three sessions about a modpack so FTS5 has hits to dedupe."""
    now = int(time.time())

    db.create_session("s_oldest")
    db._conn.execute(
        "UPDATE sessions SET created_at = ?, title = ? WHERE id = ?",
        (now - 30000, "Building the Modpack", "s_oldest"),
    )
    _append(db, "s_oldest", Role.USER, "Let's build a Minecraft modpack", "o1")
    _append(db, "s_oldest", Role.ASSISTANT, "Great. Let me scaffold the modpack repo.", "o2")
    _append(db, "s_oldest", Role.USER, "Use NeoForge 1.21.1", "o3")
    _append(db, "s_oldest", Role.ASSISTANT, "Done. Modpack repo created with NeoForge 1.21.1.", "o4")
    _append(db, "s_oldest", Role.ASSISTANT, "Tier-0 mods installed; modpack smoke test passes.", "o5")
    db._conn.execute("UPDATE messages SET timestamp = ? WHERE session_id = ?", (now - 30000, "s_oldest"))

    db.create_session("s_middle")
    db._conn.execute(
        "UPDATE sessions SET created_at = ?, title = ? WHERE id = ?",
        (now - 15000, "Modpack Quest Coverage", "s_middle"),
    )
    _append(db, "s_middle", Role.USER, "Deep-dive every modpack reference quest guide", "m1")
    _append(db, "s_middle", Role.ASSISTANT, "Surveying ATM10 questbook for modpack inspiration.", "m2")
    _append(db, "s_middle", Role.USER, "Update the modpack version too", "m3")
    _append(db, "s_middle", Role.ASSISTANT, "Modpack version bumped 0.4 → 0.8.5; quest coverage page added.", "m4")
    db._conn.execute("UPDATE messages SET timestamp = ? WHERE session_id = ?", (now - 15000, "s_middle"))

    db.create_session("s_newest")
    db._conn.execute(
        "UPDATE sessions SET created_at = ?, title = ? WHERE id = ?",
        (now - 1000, "Modpack Mob Spawn Fix", "s_newest"),
    )
    _append(db, "s_newest", Role.USER, "Fix the modpack mob spawning", "n1")
    _append(db, "s_newest", Role.ASSISTANT, "Investigating elite mob gating in the modpack KubeJS.", "n2")
    _append(db, "s_newest", Role.ASSISTANT, "Shipped commit b850442. Modpack alternator nerfed too.", "n3")
    db._conn.execute("UPDATE messages SET timestamp = ? WHERE session_id = ?", (now - 1000, "s_newest"))


# =========================================================================
# Schema invariants
# =========================================================================

class TestSchema:
    def test_schema_has_required_params(self):
        params = SESSION_SEARCH.parameters["properties"]
        assert "query" in params
        assert "limit" in params
        assert "sort" in params
        assert "session_id" in params
        assert "around_message_id" in params
        assert "window" in params
        assert "role_filter" in params

    def test_no_mode_parameter(self):
        params = SESSION_SEARCH.parameters["properties"]
        assert "mode" not in params

    def test_no_profile_parameter(self):
        # Aegis has no profile subsystem — the cross-profile param is dropped.
        params = SESSION_SEARCH.parameters["properties"]
        assert "profile" not in params

    def test_sort_enum(self):
        params = SESSION_SEARCH.parameters["properties"]
        assert params["sort"]["enum"] == ["newest", "oldest"]

    def test_schema_description_teaches_scroll(self):
        desc = SESSION_SEARCH.description
        assert "SCROLL" in desc
        assert "DISCOVERY" in desc
        assert "BROWSE" in desc
        assert "scroll FORWARD" in desc or "messages[-1]" in desc

    def test_no_llm_promise_in_description(self):
        assert "no llm" in SESSION_SEARCH.description.lower()


class TestHiddenSources:
    def test_tool_source_hidden(self):
        assert "tool" in _HIDDEN_SESSION_SOURCES


class TestFormatTimestamp:
    def test_unix_timestamp(self):
        assert "2023" in _format_timestamp(1700000000)

    def test_none(self):
        assert _format_timestamp(None) == "unknown"

    def test_iso_string_passthrough(self):
        assert _format_timestamp("not-a-number-string") == "not-a-number-string"


# =========================================================================
# FTS5 index lifecycle (repository level)
# =========================================================================

class TestFtsRepository:
    def test_fts_tables_created(self, db):
        assert db._fts_enabled is True
        row = db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='messages_fts'"
        ).fetchone()
        assert row is not None
        row = db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='messages_fts_trigram'"
        ).fetchone()
        assert row is not None

    def test_new_message_is_searchable(self, db):
        db.create_session("s1")
        _append(db, "s1", Role.USER, "let us discuss the aegis migration", "m1")
        hits = db.search_messages("aegis")
        assert [h["session_id"] for h in hits] == ["s1"]

    def test_backfill_existing_messages(self, tmp_path):
        path = tmp_path / "legacy.db"
        repo = SQLiteSessionRepository(path, source="cli")
        repo.create_session("s1")
        _append(repo, "s1", Role.USER, "pre-existing modpack history", "m1")
        # Simulate a legacy DB that predates FTS: drop the virtual tables and
        # their triggers, then reopen — init must backfill from messages.
        repo._conn.execute("DROP TABLE messages_fts")
        repo._conn.execute("DROP TABLE messages_fts_trigram")
        repo._drop_fts_triggers(repo._conn.cursor())
        repo.close()

        repo2 = SQLiteSessionRepository(path, source="cli")
        hits = repo2.search_messages("modpack")
        assert [h["session_id"] for h in hits] == ["s1"]
        repo2.close()

    def test_reopen_preserves_index(self, tmp_path):
        path = tmp_path / "s.db"
        repo = SQLiteSessionRepository(path, source="cli")
        repo.create_session("s1")
        _append(repo, "s1", Role.USER, "docker networking deep-dive", "m1")
        repo.close()

        repo2 = SQLiteSessionRepository(path, source="cli")
        assert repo2.search_messages("docker")
        repo2.close()

    def test_special_characters_do_not_error(self, db):
        db.create_session("s1")
        _append(db, "s1", Role.USER, "some normal content here", "m1")
        # Sanitised FTS5 queries must never raise sqlite3.OperationalError.
        for q in ["a+b(c){d}***", 'unmatched "quote', "AND OR NOT", "foo OR OR bar", "deploy*"]:
            result = db.search_messages(q)
            assert isinstance(result, list)

    def test_empty_result_returns_empty_list(self, db):
        db.create_session("s1")
        _append(db, "s1", Role.USER, "hello", "m1")
        assert db.search_messages("zzz_no_such_term_zzz") == []


# =========================================================================
# Browse shape (no args)
# =========================================================================

class TestBrowseShape:
    def test_no_args_returns_recent_sessions(self, tool, db):
        _seed_modpack_sessions(db)
        result = json.loads(tool.run({}).content)
        assert result["success"] is True
        assert result["mode"] == "browse"
        assert result["count"] >= 3

    def test_browse_excludes_current_session(self, tool, db):
        _seed_modpack_sessions(db)
        result = json.loads(session_search(db=db, current_session_id="s_newest"))
        sids = [r["session_id"] for r in result["results"]]
        assert "s_newest" not in sids

    def test_browse_returns_titles(self, tool, db):
        _seed_modpack_sessions(db)
        result = json.loads(tool.run({}).content)
        titles = [r.get("title") for r in result["results"]]
        assert any("Modpack" in (t or "") for t in titles)


# =========================================================================
# Discovery shape (with query)
# =========================================================================

class TestDiscoveryShape:
    def test_query_returns_anchored_windows(self, tool, db):
        _seed_modpack_sessions(db)
        result = json.loads(tool.run({"query": "modpack"}).content)
        assert result["success"] is True
        assert result["mode"] == "discover"
        assert result["count"] >= 1

    def test_discovery_result_has_bookends_and_window(self, tool, db):
        _seed_modpack_sessions(db)
        result = json.loads(tool.run({"query": "modpack", "limit": 3}).content)
        for hit in result["results"]:
            assert "bookend_start" in hit
            assert "messages" in hit
            assert "bookend_end" in hit
            assert "match_message_id" in hit
            assert "snippet" in hit
            assert "messages_before" in hit
            assert "messages_after" in hit

    def test_match_message_id_is_anchor_in_window(self, tool, db):
        _seed_modpack_sessions(db)
        result = json.loads(tool.run({"query": "modpack", "limit": 3}).content)
        for hit in result["results"]:
            anchor_id = hit["match_message_id"]
            window_ids = [m["id"] for m in hit["messages"]]
            assert anchor_id in window_ids

    def test_no_results_returns_empty_list(self, tool, db):
        _seed_modpack_sessions(db)
        result = json.loads(tool.run({"query": "zzz_no_such_term_zzz"}).content)
        assert result["success"] is True
        assert result["results"] == []
        assert result["count"] == 0

    def test_limit_clamped_to_max_10(self, tool, db):
        _seed_modpack_sessions(db)
        result = json.loads(tool.run({"query": "modpack", "limit": 999}).content)
        assert result["count"] <= 10

    def test_non_int_limit_falls_back(self, tool, db):
        _seed_modpack_sessions(db)
        result = json.loads(tool.run({"query": "modpack", "limit": "bogus"}).content)
        assert result["success"] is True

    def test_current_session_filtered_out(self, tool, db):
        _seed_modpack_sessions(db)
        result = json.loads(session_search(query="modpack", db=db, current_session_id="s_newest"))
        sids = [r["session_id"] for r in result["results"]]
        assert "s_newest" not in sids


class TestDiscoverySort:
    def test_sort_newest_orders_by_recency(self, tool, db):
        _seed_modpack_sessions(db)
        result = json.loads(tool.run({"query": "modpack", "limit": 3, "sort": "newest"}).content)
        assert result["results"][0]["session_id"] == "s_newest"

    def test_sort_oldest_orders_by_age(self, tool, db):
        _seed_modpack_sessions(db)
        result = json.loads(tool.run({"query": "modpack", "limit": 3, "sort": "oldest"}).content)
        assert result["results"][0]["session_id"] == "s_oldest"

    def test_invalid_sort_silently_ignored(self, tool, db):
        _seed_modpack_sessions(db)
        result = json.loads(tool.run({"query": "modpack", "sort": "bogus"}).content)
        assert result["success"] is True


class TestRoleFilter:
    def test_default_excludes_tool_role(self, db):
        db.create_session("s1")
        _append(db, "s1", Role.USER, "modpack question", "m1")
        _append(db, "s1", Role.TOOL, "modpack tool output", "m2", name="x")
        result = json.loads(session_search(query="modpack", db=db))
        if result["count"] > 0:
            assert result["results"][0]["matched_role"] in ("user", "assistant")

    def test_explicit_tool_role_includes_tool(self, db):
        db.create_session("s1")
        _append(db, "s1", Role.TOOL, "modpack tool output", "m1", name="x")
        result = json.loads(session_search(query="modpack", role_filter="tool", db=db))
        if result["count"] > 0:
            assert result["results"][0]["matched_role"] == "tool"


# =========================================================================
# Scroll shape (session_id + around_message_id)
# =========================================================================

class TestScrollShape:
    def _anchor(self, db):
        disc = json.loads(session_search(query="modpack", limit=1, db=db))
        return disc["results"][0]["session_id"], disc["results"][0]["match_message_id"]

    def test_scroll_returns_window_without_bookends(self, db):
        _seed_modpack_sessions(db)
        sid, mid = self._anchor(db)
        result = json.loads(session_search(session_id=sid, around_message_id=mid, window=2, db=db))
        assert result["success"] is True
        assert result["mode"] == "scroll"
        assert "messages" in result
        assert "bookend_start" not in result
        assert "bookend_end" not in result

    def test_scroll_window_clamped_to_20(self, db):
        _seed_modpack_sessions(db)
        sid, mid = self._anchor(db)
        result = json.loads(session_search(session_id=sid, around_message_id=mid, window=999, db=db))
        assert result["window"] == 20

    def test_scroll_window_floor_to_1(self, db):
        _seed_modpack_sessions(db)
        sid, mid = self._anchor(db)
        result = json.loads(session_search(session_id=sid, around_message_id=mid, window=-5, db=db))
        assert result["window"] == 1

    def test_scroll_returns_messages_before_after_counts(self, db):
        _seed_modpack_sessions(db)
        sid, mid = self._anchor(db)
        result = json.loads(session_search(session_id=sid, around_message_id=mid, window=3, db=db))
        assert "messages_before" in result
        assert "messages_after" in result

    def test_scroll_anchor_in_window(self, db):
        _seed_modpack_sessions(db)
        sid, mid = self._anchor(db)
        result = json.loads(session_search(session_id=sid, around_message_id=mid, window=2, db=db))
        anchor_in_window = [m for m in result["messages"] if m["id"] == mid]
        assert len(anchor_in_window) == 1
        assert anchor_in_window[0].get("anchor") is True

    def test_scroll_missing_anchor_errors(self, db):
        _seed_modpack_sessions(db)
        result = json.loads(session_search(session_id="s_oldest", around_message_id=999999, db=db))
        assert result["success"] is False
        assert "not in" in result.get("error", "")

    def test_scroll_missing_session_errors(self, db):
        result = json.loads(session_search(session_id="nonexistent", around_message_id=1, db=db))
        assert result["success"] is False

    def test_scroll_rejects_current_session(self, db):
        _seed_modpack_sessions(db)
        sid, mid = self._anchor(db)
        result = json.loads(session_search(
            session_id=sid, around_message_id=mid, db=db, current_session_id=sid
        ))
        assert result["success"] is False
        assert "current session" in result.get("error", "").lower()

    def test_scroll_invalid_around_message_id_errors(self, db):
        _seed_modpack_sessions(db)
        result = json.loads(session_search(session_id="s_oldest", around_message_id="not-an-int", db=db))
        assert result["success"] is False


class TestScrollPattern:
    def test_scroll_forward_from_last_id(self, db):
        db.create_session("s_long")
        for i in range(20):
            _append(db, "s_long", Role.USER if i % 2 == 0 else Role.ASSISTANT, f"long session msg {i}", f"m{i}")
        ids = [m["id"] for m in db.get_messages_dict("s_long")]

        v1 = json.loads(session_search(session_id="s_long", around_message_id=ids[5], window=3, db=db))
        last_id = v1["messages"][-1]["id"]
        v2 = json.loads(session_search(session_id="s_long", around_message_id=last_id, window=3, db=db))
        assert max(m["id"] for m in v2["messages"]) > max(m["id"] for m in v1["messages"])
        assert last_id in [m["id"] for m in v1["messages"]]
        assert last_id in [m["id"] for m in v2["messages"]]


# =========================================================================
# Shape precedence
# =========================================================================

class TestShapePrecedence:
    def test_scroll_args_beat_query(self, db):
        _seed_modpack_sessions(db)
        disc = json.loads(session_search(query="modpack", limit=1, db=db))
        sid = disc["results"][0]["session_id"]
        mid = disc["results"][0]["match_message_id"]
        result = json.loads(session_search(query="modpack", session_id=sid, around_message_id=mid, db=db))
        assert result["mode"] == "scroll"

    def test_empty_query_falls_back_to_browse(self, db):
        _seed_modpack_sessions(db)
        result = json.loads(session_search(query="   ", db=db))
        assert result["mode"] == "browse"

    def test_non_string_query_falls_back_to_browse(self, db):
        _seed_modpack_sessions(db)
        result = json.loads(session_search(query=None, db=db))  # type: ignore[arg-type]
        assert result["mode"] == "browse"

    def test_session_id_without_anchor_reads(self, db):
        _seed_modpack_sessions(db)
        result = json.loads(session_search(session_id="s_oldest", db=db))
        assert result["mode"] == "read"


# =========================================================================
# Read shape — dump a whole session by id
# =========================================================================

class TestReadShape:
    def test_read_returns_full_session(self, db):
        _seed_modpack_sessions(db)
        result = json.loads(session_search(session_id="s_oldest", db=db))
        assert result["success"] is True
        assert result["mode"] == "read"
        assert result["session_id"] == "s_oldest"
        assert result["message_count"] == 5
        assert result["truncated"] is False
        assert len(result["messages"]) == 5
        assert result["session_meta"]["title"] == "Building the Modpack"

    def test_read_unknown_session_errors(self, db):
        result = json.loads(session_search(session_id="ghost", db=db))
        assert result["success"] is False

    def test_read_truncates_large_session(self, db):
        db.create_session("s_big")
        for i in range(50):
            _append(db, "s_big", Role.USER if i % 2 == 0 else Role.ASSISTANT, f"m{i}", f"m{i}")
        result = json.loads(session_search(session_id="s_big", db=db))
        assert result["mode"] == "read"
        assert result["message_count"] == 50
        assert result["truncated"] is True
        assert len(result["messages"]) == 30  # head 20 + tail 10


# =========================================================================
# Unavailable store degrades gracefully
# =========================================================================

class TestUnavailableStore:
    def test_inmemory_store_returns_error(self):
        from aegis_agent.sessions.memory_store import InMemorySessionRepository

        tool = SessionSearchTool(InMemorySessionRepository())
        result = json.loads(tool.run({"query": "x"}).content)
        assert result["success"] is False
        assert "unavailable" in result.get("error", "")
