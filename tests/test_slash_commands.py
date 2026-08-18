"""Tests for the interactive slash-command suite (``/save``, ``/chatlog``, …).

The handler is UI-agnostic (output via an ``emit`` callable, session rotation
via a callback), so these tests drive it directly against the in-memory and
SQLite stores — no Typer runner, no TTY.  A few REPL-level integration tests
at the bottom go through Typer's CliRunner.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from aegis_agent.models.base import Message, Role
from aegis_agent.models.fake import FakeModelProvider
from aegis_agent.runtime import AgentRuntime
from aegis_agent.sessions.memory_store import InMemorySessionRepository
from aegis_agent.sessions.sqlite_store import SQLiteSessionRepository
from aegis_agent.slash_commands import (
    COMMAND_REGISTRY,
    SlashHandler,
    SlashKind,
    WireCaptureProvider,
    help_lines,
    resolve_command,
    sanitize_title,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_handler(repo, tmp_path, *, rotate=None, session_id="s1"):
    """Build a handler + runtime wired to the fake provider, capturing output."""
    wire = WireCaptureProvider(FakeModelProvider())
    runtime = AgentRuntime.with_defaults(
        provider=wire,
        repository=repo,
        enable_skills=False,
        enable_mcp=False,
        enable_memory=False,
    )
    out: list[str] = []
    handler = SlashHandler(
        runtime=runtime,
        repository=repo,
        emit=out.append,
        session_id=session_id,
        wire=wire,
        rotate_session=rotate,
        chatlog_dir=tmp_path / "chat-logs",
    )
    return handler, runtime, wire, out


def _run_turn(runtime: AgentRuntime, session_id: str, text: str) -> None:
    runtime.run_turn(session_id, text)


# ---------------------------------------------------------------------------
# Registry / resolution
# ---------------------------------------------------------------------------


def test_resolve_canonical_alias_and_slash_prefix():
    assert resolve_command("save").name == "save"
    assert resolve_command("/chatlog").name == "save"  # alias of /save
    assert resolve_command("reset").name == "new"      # alias
    assert resolve_command("/quit").name == "exit"     # alias
    assert resolve_command("nosuch") is None


def test_registry_names_unique_and_aliases_resolve():
    names = [c.name for c in COMMAND_REGISTRY]
    assert len(names) == len(set(names))
    for cmd in COMMAND_REGISTRY:
        for alias in cmd.aliases:
            assert resolve_command(alias) is cmd


def test_help_lists_every_command():
    text = "\n".join(help_lines())
    for cmd in COMMAND_REGISTRY:
        assert f"/{cmd.name}" in text


def test_sanitize_title():
    assert sanitize_title("  hello   world ") == "hello world"
    assert sanitize_title("a\x00b\x07c") == "abc"
    assert sanitize_title("   ") == ""
    assert len(sanitize_title("x" * 200)) == 60


def test_unknown_slash_token_falls_through(tmp_path):
    repo = InMemorySessionRepository()
    handler, _, _, _ = _make_handler(repo, tmp_path)
    assert handler.handle("/notacommand") is None  # caller falls to skill/model


# ---------------------------------------------------------------------------
# /save (= Hermes' /chatlog debug dump; /chatlog is an alias)
# ---------------------------------------------------------------------------


def test_save_dumps_local_wire_system(tmp_path):
    repo = InMemorySessionRepository()
    handler, runtime, wire, _out = _make_handler(repo, tmp_path)
    _run_turn(runtime, "s1", "hello chatlog")
    handler.handle("/save")

    dump = tmp_path / "chat-logs"
    local = json.loads((dump / "01-local.json").read_text(encoding="utf-8"))
    wire_dump = json.loads((dump / "01-wire.json").read_text(encoding="utf-8"))
    system = (dump / "01-system.txt").read_text(encoding="utf-8")

    # Local: exactly the persisted history (user + assistant).
    assert [m["role"] for m in local["messages"]] == ["user", "assistant"]

    # Wire: what the provider actually received — system prompt first, and the
    # captured list matches what WireCaptureProvider saw.
    assert wire.last_messages is not None
    assert wire_dump["captured_at"] is not None
    assert wire_dump["messages"][0]["role"] == "system"
    assert wire_dump["messages"][-1]["content"] == "hello chatlog"

    # System: non-empty and consistent with the wire copy.
    assert system
    assert system == wire_dump["messages"][0]["content"]


def test_chatlog_is_an_alias_of_save(tmp_path):
    repo = InMemorySessionRepository()
    handler, runtime, _, _out = _make_handler(repo, tmp_path)
    _run_turn(runtime, "s1", "one")
    result = handler.handle("/chatlog")
    assert result.kind is SlashKind.HANDLED
    assert (tmp_path / "chat-logs" / "01-local.json").exists()


def test_save_prefix_increments(tmp_path):
    repo = InMemorySessionRepository()
    handler, runtime, _, _ = _make_handler(repo, tmp_path)
    _run_turn(runtime, "s1", "one")
    handler.handle("/save")
    _run_turn(runtime, "s1", "two")
    handler.handle("/chatlog")
    dumps = sorted(p.name for p in (tmp_path / "chat-logs").glob("*-local.json"))
    assert dumps == ["01-local.json", "02-local.json"]


def test_save_before_first_turn_still_writes(tmp_path):
    # Messages exist but the provider was never called (e.g. right after
    # resume): wire snapshot falls back to an empty list, system.txt is
    # rendered fresh.  Mirrors Hermes' pre-first-turn fallback.
    repo = InMemorySessionRepository()
    handler, runtime, _, _ = _make_handler(repo, tmp_path)
    runtime.repository.create_session("s1")
    runtime.repository.append_message(
        "s1", Message(role=Role.USER, content="persisted offline"))
    handler.handle("/save")
    dump = tmp_path / "chat-logs"
    wire_dump = json.loads((dump / "01-wire.json").read_text(encoding="utf-8"))
    assert wire_dump["messages"] == []
    assert wire_dump["captured_at"] is None
    assert (dump / "01-system.txt").read_text(encoding="utf-8")


def test_save_empty_conversation(tmp_path):
    repo = InMemorySessionRepository()
    handler, _, _, out = _make_handler(repo, tmp_path)
    handler.handle("/save")
    assert any("No conversation to dump" in line for line in out)
    assert not (tmp_path / "chat-logs").exists()


# ---------------------------------------------------------------------------
# /history
# ---------------------------------------------------------------------------


def test_history_renders_user_and_assistant(tmp_path):
    repo = InMemorySessionRepository()
    handler, runtime, _, out = _make_handler(repo, tmp_path)
    _run_turn(runtime, "s1", "show me history")
    handler.handle("/history")
    text = "\n".join(out)
    assert "Conversation History" in text
    assert "[You #1]" in text
    assert "show me history" in text
    assert "[Aegis #2]" in text


def test_history_empty(tmp_path):
    repo = InMemorySessionRepository()
    handler, _, _, out = _make_handler(repo, tmp_path)
    handler.handle("/history")
    assert any("No conversation history" in line for line in out)


def test_history_collapses_tool_messages(tmp_path):
    from aegis_agent.models.base import ToolCall

    repo = InMemorySessionRepository()
    handler, _runtime, _, out = _make_handler(repo, tmp_path)
    repo.create_session("s1")
    repo.append_message("s1", Message(role=Role.USER, content="run a tool"))
    repo.append_message("s1", Message(
        role=Role.ASSISTANT, content="",
        tool_calls=[ToolCall(id="t1", name="list_directory", arguments="{}")]))
    repo.append_message("s1", Message(
        role=Role.TOOL, content='{"count": 3}', tool_call_id="t1", name="list_directory"))
    repo.append_message("s1", Message(role=Role.ASSISTANT, content="done"))
    handler.handle("/history")
    text = "\n".join(out)
    assert "(1 tool message hidden)" in text
    assert "(requested 1 tool call)" in text
    assert "list_directory" not in text  # raw tool payload is not dumped


# ---------------------------------------------------------------------------
# /new, /clear, /title, /sessions
# ---------------------------------------------------------------------------


def test_new_rotates_session_with_title(tmp_path):
    repo = InMemorySessionRepository()
    rotated: list[str | None] = []

    def rotate(title: str | None) -> str:
        rotated.append(title)
        repo.create_session("s2", title=title)
        return "s2"

    handler, runtime, _, _out = _make_handler(repo, tmp_path, rotate=rotate)
    _run_turn(runtime, "s1", "old session message")
    handler.handle("/new My Title")
    assert handler.session_id == "s2"
    assert rotated == ["My Title"]
    assert repo.get_session("s2").title == "My Title"
    # The old session's history is untouched (isolation invariant).
    assert repo.message_count("s1") == 2
    assert repo.message_count("s2") == 0


def test_new_without_rotate_callback_reports_unavailable(tmp_path):
    repo = InMemorySessionRepository()
    handler, _, _, out = _make_handler(repo, tmp_path)
    handler.handle("/new")
    assert any("not available" in line for line in out)
    assert handler.session_id == "s1"


def test_new_failed_rotation_keeps_old_session(tmp_path):
    repo = InMemorySessionRepository()
    handler, _, _, out = _make_handler(repo, tmp_path, rotate=lambda title: None)
    handler.handle("/new")
    assert handler.session_id == "s1"
    assert any("staying on the current one" in line for line in out)


def test_clear_invokes_clear_screen_and_rotates(tmp_path):
    repo = InMemorySessionRepository()
    cleared: list[bool] = []
    handler, _, _, _ = _make_handler(
        repo, tmp_path, rotate=lambda title: "s2")
    handler._clear_screen = lambda: cleared.append(True)
    handler.handle("/clear")
    assert cleared == [True]
    assert handler.session_id == "s2"


def test_title_set_and_show(tmp_path):
    repo = InMemorySessionRepository()
    handler, runtime, _, out = _make_handler(repo, tmp_path)
    _run_turn(runtime, "s1", "hi")  # materialise the session row
    handler.handle("/title  My   Session ")
    assert repo.get_session("s1").title == "My Session"
    out.clear()
    handler.handle("/title")
    assert any("Title: My Session" in line for line in out)


def test_title_before_first_message_creates_session(tmp_path):
    repo = InMemorySessionRepository()
    handler, _, _, out = _make_handler(repo, tmp_path)
    handler.handle("/title Early Bird")
    assert repo.get_session("s1").title == "Early Bird"
    assert any("title set" in line.lower() for line in out)


def test_title_rejects_control_chars(tmp_path):
    repo = InMemorySessionRepository()
    handler, runtime, _, out = _make_handler(repo, tmp_path)
    _run_turn(runtime, "s1", "hi")
    handler.handle("/title \x00\x07")
    assert any("empty after cleanup" in line for line in out)
    assert repo.get_session("s1").title is None


def test_sessions_lists_sessions(tmp_path):
    repo = InMemorySessionRepository()
    handler, runtime, _, out = _make_handler(repo, tmp_path)
    _run_turn(runtime, "s1", "first")
    handler.handle("/sessions")
    text = "\n".join(out)
    assert "SESSION ID" in text
    assert "s1" in text


# ---------------------------------------------------------------------------
# /retry and /undo (in-memory store)
# ---------------------------------------------------------------------------


def test_retry_requeues_last_user_message(tmp_path):
    repo = InMemorySessionRepository()
    handler, runtime, _, _out = _make_handler(repo, tmp_path)
    _run_turn(runtime, "s1", "first question")
    _run_turn(runtime, "s1", "second question")
    assert repo.message_count("s1") == 4
    result = handler.handle("/retry")
    assert result.kind is SlashKind.REQUEUE
    assert result.text == "second question"
    # The last exchange (user + assistant) was truncated.
    assert repo.message_count("s1") == 2
    remaining = repo.list_messages("s1")
    assert remaining[-1].content == "Echo: first question"


def test_retry_empty_history(tmp_path):
    repo = InMemorySessionRepository()
    handler, _, _, out = _make_handler(repo, tmp_path)
    result = handler.handle("/retry")
    assert result.kind is SlashKind.HANDLED
    assert any("No messages to retry" in line for line in out)


def test_undo_defaults_to_one_turn_and_prefills(tmp_path):
    repo = InMemorySessionRepository()
    handler, runtime, _, _ = _make_handler(repo, tmp_path)
    _run_turn(runtime, "s1", "keep this")
    _run_turn(runtime, "s1", "undo me")
    result = handler.handle("/undo")
    assert result.kind is SlashKind.HANDLED
    assert result.prefill == "undo me"
    assert repo.message_count("s1") == 2


def test_undo_n_turns(tmp_path):
    repo = InMemorySessionRepository()
    handler, runtime, _, _ = _make_handler(repo, tmp_path)
    for i in range(3):
        _run_turn(runtime, "s1", f"question {i}")
    result = handler.handle(f"/undo {2}")
    assert result.prefill == "question 1"
    assert repo.message_count("s1") == 2  # only question 0 + its reply survive


def test_undo_more_than_available_undoes_everything(tmp_path):
    repo = InMemorySessionRepository()
    handler, runtime, _, _ = _make_handler(repo, tmp_path)
    _run_turn(runtime, "s1", "only question")
    result = handler.handle("/undo 99")
    assert result.prefill == "only question"
    assert repo.message_count("s1") == 0


def test_undo_invalid_count(tmp_path):
    repo = InMemorySessionRepository()
    handler, runtime, _, out = _make_handler(repo, tmp_path)
    _run_turn(runtime, "s1", "still here")
    handler.handle("/undo abc")
    assert any("Invalid count" in line for line in out)
    assert repo.message_count("s1") == 2  # untouched


def test_exit_command(tmp_path):
    repo = InMemorySessionRepository()
    handler, _, _, _ = _make_handler(repo, tmp_path)
    assert handler.handle("/exit").kind is SlashKind.EXIT
    assert handler.handle("/quit").kind is SlashKind.EXIT


# ---------------------------------------------------------------------------
# SQLite rewind invariants (soft delete, snapshot invalidation)
# ---------------------------------------------------------------------------


def test_sqlite_rewind_soft_deletes_and_bumps_history_version(tmp_path):
    db = tmp_path / "state.db"
    repo = SQLiteSessionRepository(db)
    runtime = AgentRuntime.with_defaults(
        provider=FakeModelProvider(), repository=repo,
        enable_skills=False, enable_mcp=False, enable_memory=False,
    )
    runtime.run_turn("s1", "question A")
    runtime.run_turn("s1", "question B")

    # Snapshot the current (4-message) history so we can prove invalidation.
    version_before = repo.get_history_version("s1")
    assert repo.write_snapshot("s1") is not None
    assert repo.message_count("s1") == 4

    # Rewind from the second user message (seq 2).
    removed = repo.rewind_from_seq("s1", 2)
    assert removed == 2

    # Visible history is truncated; the rows survive as active=0 (audit).
    assert [m.content for m in repo.list_messages("s1")] == [
        "question A", "Echo: question A"]
    assert repo.message_count("s1") == 2
    with sqlite3.connect(db) as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = 's1'").fetchone()[0]
        inactive = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = 's1' AND active = 0"
        ).fetchone()[0]
    assert total == 4 and inactive == 2

    # history_version bumped → the pre-rewind snapshot is stale and ignored,
    # so resume equals full replay of the truncated history.
    assert repo.get_history_version("s1") > version_before
    assert repo.load_latest_snapshot("s1", repo.get_history_version("s1")) is None
    assert [m.content for m in repo.resume_messages("s1")] == [
        "question A", "Echo: question A"]
    repo.close()


def test_sqlite_rewind_is_idempotent_and_scoped(tmp_path):
    repo = SQLiteSessionRepository(tmp_path / "state.db")
    runtime = AgentRuntime.with_defaults(
        provider=FakeModelProvider(), repository=repo,
        enable_skills=False, enable_mcp=False, enable_memory=False,
    )
    runtime.run_turn("s1", "hello")
    runtime.run_turn("s2", "other session")
    # Second rewind of the same range removes nothing new.
    assert repo.rewind_from_seq("s1", 0) == 2
    assert repo.rewind_from_seq("s1", 0) == 0
    # Another session's messages are never touched (session isolation).
    assert repo.message_count("s2") == 2
    repo.close()


def test_sqlite_set_session_title(tmp_path):
    repo = SQLiteSessionRepository(tmp_path / "state.db")
    repo.create_session("s1")
    assert repo.set_session_title("s1", "titled") is True
    assert repo.get_session("s1").title == "titled"
    assert repo.set_session_title("missing", "x") is False
    repo.close()


# ---------------------------------------------------------------------------
# REPL integration (Typer CliRunner, real SQLite store)
# ---------------------------------------------------------------------------


@pytest.fixture
def _isolated_home(tmp_path, monkeypatch):
    """Keep the real ~/.aegis/.env (API keys) out of CliRunner runs.

    Same isolation as tests/test_cli.py: without it the CLI's user-level
    dotenv load leaks developer keys into ``os.environ`` for the whole pytest
    process.
    """
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def test_repl_slash_commands_end_to_end(tmp_path, monkeypatch, _isolated_home):
    from typer.testing import CliRunner

    from aegis_agent.cli import app

    monkeypatch.setenv("AEGIS_DB_PATH", str(tmp_path / "state.db"))
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        app,
        ["--model-backend", "fake", "--no-mcp", "--no-memory"],
        input="hello there\n/save\n/chatlog\n/title My Chat\n/history\n/new next\n/exit\n",
    )
    assert result.exit_code == 0
    out = result.output
    assert "Echo: hello there" in out
    # /save and /chatlog are the same command → two numbered dumps.
    assert "Dumped chatlog #01" in out
    assert "Dumped chatlog #02" in out
    assert "Session title set: My Chat" in out
    assert "Conversation History" in out
    assert "Fresh start! New session:" in out
    assert (tmp_path / "aegis-chat-logs" / "01-wire.json").exists()
    assert (tmp_path / "aegis-chat-logs" / "02-wire.json").exists()


def test_repl_retry_reruns_last_message(tmp_path, monkeypatch, _isolated_home):
    from typer.testing import CliRunner

    from aegis_agent.cli import app

    monkeypatch.setenv("AEGIS_DB_PATH", str(tmp_path / "state.db"))
    result = CliRunner().invoke(
        app,
        ["--model-backend", "fake", "--no-mcp", "--no-memory", "--ephemeral"],
        input="do it\n/retry\n/exit\n",
    )
    assert result.exit_code == 0
    # The retried message produced a second, identical echo.
    assert result.output.count("Echo: do it") == 2
    assert "Retrying:" in result.output


def test_repl_unknown_slash_goes_to_model(tmp_path, monkeypatch, _isolated_home):
    from typer.testing import CliRunner

    from aegis_agent.cli import app

    monkeypatch.setenv("AEGIS_DB_PATH", str(tmp_path / "state.db"))
    result = CliRunner().invoke(
        app,
        ["--model-backend", "fake", "--no-mcp", "--no-memory", "--ephemeral"],
        input="/notacommand hi\n/exit\n",
    )
    assert result.exit_code == 0
    # Unrecognised /tokens fall through to the model unchanged.
    assert "Echo: /notacommand hi" in result.output


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
