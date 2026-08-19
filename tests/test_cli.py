"""CLI minimal-startup tests using Typer's CliRunner."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from aegis_agent.cli import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Keep the real ~/.aegis/.env (and its API keys) out of CLI tests.

    The CLI loads the user-level dotenv at startup; without this isolation a
    developer machine's keys (e.g. TAVILY_API_KEY) leak into ``os.environ``
    for the whole pytest process and flip later tests (web backends) onto
    live-network code paths.
    """
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def test_cli_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "aegis" in result.output


def test_cli_repl_processes_input_and_exits(tmp_path, monkeypatch):
    # Feed one message then the exit command; the fake echoes the message.
    monkeypatch.setenv("AEGIS_DB_PATH", str(tmp_path / "state.db"))
    result = runner.invoke(app, ["--model-backend", "fake", "--no-mcp"], input="hello aegis\nexit\n")
    assert result.exit_code == 0
    assert "Echo: hello aegis" in result.output
    assert "bye." in result.output


def test_cli_repl_tool_command(tmp_path, monkeypatch):
    # 'list' triggers the list_directory tool via the rule-based fake.
    monkeypatch.setenv("AEGIS_DB_PATH", str(tmp_path / "state.db"))
    result = runner.invoke(app, ["--model-backend", "fake", "--no-mcp"], input="list .\nexit\n")
    assert result.exit_code == 0
    # After the tool runs, the fake summarises the tool result.
    assert "list_directory" in result.output


def test_cli_repl_eof_exits_cleanly(tmp_path, monkeypatch):
    # No exit command — EOF on stdin must still terminate the loop.
    monkeypatch.setenv("AEGIS_DB_PATH", str(tmp_path / "state.db"))
    result = runner.invoke(app, ["--model-backend", "fake", "--no-mcp"], input="")
    assert result.exit_code == 0


def test_cli_resume_restores_session(tmp_path, monkeypatch):
    """First run persists a turn; --resume picks the same session up."""
    db = str(tmp_path / "state.db")
    monkeypatch.setenv("AEGIS_DB_PATH", db)
    first = runner.invoke(
        app, ["--model-backend", "fake", "--session", "my-session"], input="hello aegis\nexit\n"
    )
    assert first.exit_code == 0

    resumed = runner.invoke(
        app, ["--model-backend", "fake", "--resume", "my-session"], input="again\nexit\n"
    )
    assert resumed.exit_code == 0
    assert "Resumed session my-session (2 messages)." in resumed.output
    assert "Echo: again" in resumed.output


def test_cli_resume_unknown_session_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("AEGIS_DB_PATH", str(tmp_path / "state.db"))
    result = runner.invoke(app, ["--model-backend", "fake", "--resume", "nope"], input="")
    assert result.exit_code == 1
    assert "session not found" in result.output


def test_cli_empty_session_is_resumable(tmp_path, monkeypatch):
    """Exiting without sending a message still persists the session row, so the
    printed "Resume: aegis --resume <id>" hint actually works (regression: the
    row used to be created lazily on the first turn, and --resume failed with
    "session not found" for empty sessions)."""
    monkeypatch.setenv("AEGIS_DB_PATH", str(tmp_path / "state.db"))
    first = runner.invoke(
        app, ["--model-backend", "fake", "--session", "empty-one"], input="exit\n"
    )
    assert first.exit_code == 0
    assert "Resume: aegis --resume empty-one" in first.output

    resumed = runner.invoke(
        app, ["--model-backend", "fake", "--resume", "empty-one"], input="exit\n"
    )
    assert resumed.exit_code == 0
    assert "Resumed session empty-one (0 messages)." in resumed.output


def test_cli_ephemeral_skips_lease(tmp_path, monkeypatch) -> None:
    """--ephemeral: no cross-process state exists, so the lease is skipped
    (and no default-path SQLite lock file is touched)."""
    monkeypatch.setenv("AEGIS_DB_PATH", str(tmp_path / "state.db"))
    monkeypatch.delenv("AEGIS_SESSION_LEASE_BACKEND", raising=False)
    result = runner.invoke(
        app, ["--model-backend", "fake", "--ephemeral"], input="exit\n"
    )
    assert result.exit_code == 0
    assert "session lease skipped" in result.output
    assert not (tmp_path / "state.db").exists()


def test_cli_lease_blocks_second_owner(tmp_path, monkeypatch):
    """A live lease on the session makes a second CLI process refuse to start."""
    from aegis_agent.sessions.lease import SessionLeaseManager, get_lease_backend
    from aegis_agent.sessions.sqlite_store import SQLiteSessionRepository

    db = str(tmp_path / "state.db")
    monkeypatch.setenv("AEGIS_DB_PATH", db)
    holder_repo = SQLiteSessionRepository(db)
    holder_repo.create_session("held-session")
    holder = SessionLeaseManager(get_lease_backend(holder_repo), ttl_s=30.0)
    assert holder.acquire("held-session")
    try:
        result = runner.invoke(
            app, ["--model-backend", "fake", "--session", "held-session"], input="exit\n"
        )
        assert result.exit_code == 1
        assert "owned by another process" in result.output
    finally:
        holder.stop()
        holder_repo.close()

    # After the holder releases, the session is free again.
    result = runner.invoke(
        app, ["--model-backend", "fake", "--session", "held-session"], input="exit\n"
    )
    assert result.exit_code == 0


# ── project-scoped session storage ──────────────────────────────────────────


def test_scoped_db_path():
    """An explicit db path wins; otherwise project scope derives a per-project
    store and personal scope keeps the shared default."""
    from aegis_agent.cli import _scoped_db_path
    from aegis_agent.memory.paths import project_home

    assert _scoped_db_path("/x/explicit.db", "/proj") == "/x/explicit.db"
    assert _scoped_db_path("/x/explicit.db", None) == "/x/explicit.db"
    assert _scoped_db_path(None, None) is None
    assert _scoped_db_path(None, "/proj") == str(project_home("/proj") / "state.db")


def test_cli_project_session_stored_in_project_home(tmp_path, monkeypatch):
    """In project scope the session store defaults to <project home>/state.db,
    not the shared personal store."""
    from aegis_agent.memory.paths import project_home

    monkeypatch.delenv("AEGIS_DB_PATH", raising=False)
    proj = tmp_path / "proj"
    proj.mkdir()
    scoped_db = project_home(proj) / "state.db"

    result = runner.invoke(
        app,
        ["--model-backend", "fake", "--no-mcp", "--project", str(proj), "--session", "p1"],
        input="hello aegis\nexit\n",
    )
    assert result.exit_code == 0, result.output
    assert scoped_db.is_file()
    # The resume hint carries the project flag so the session can be found again.
    assert f"Resume: aegis --project {proj} --resume p1" in result.output


def test_cli_project_session_resumes_within_project(tmp_path, monkeypatch):
    monkeypatch.delenv("AEGIS_DB_PATH", raising=False)
    proj = tmp_path / "proj"
    proj.mkdir()

    first = runner.invoke(
        app,
        ["--model-backend", "fake", "--no-mcp", "--project", str(proj), "--session", "p2"],
        input="hello aegis\nexit\n",
    )
    assert first.exit_code == 0, first.output

    resumed = runner.invoke(
        app,
        ["--model-backend", "fake", "--no-mcp", "--project", str(proj), "--resume", "p2"],
        input="again\nexit\n",
    )
    assert resumed.exit_code == 0, resumed.output
    assert "Resumed session p2 (2 messages)." in resumed.output


def test_cli_project_session_not_visible_to_personal(tmp_path, monkeypatch):
    """A project session must not be resumable from personal scope; the error
    points the user back to the project scope."""
    import aegis_agent.sessions.sqlite_store as store_mod

    monkeypatch.delenv("AEGIS_DB_PATH", raising=False)
    personal_db = tmp_path / "personal-state.db"
    monkeypatch.setattr(store_mod, "DEFAULT_DB_PATH", personal_db)
    proj = tmp_path / "proj"
    proj.mkdir()

    first = runner.invoke(
        app,
        ["--model-backend", "fake", "--no-mcp", "--project", str(proj), "--session", "p3"],
        input="hello aegis\nexit\n",
    )
    assert first.exit_code == 0, first.output

    # Personal scope (no --project) cannot see the project session.
    lost = runner.invoke(
        app, ["--model-backend", "fake", "--no-mcp", "--resume", "p3"], input="exit\n"
    )
    assert lost.exit_code == 1
    assert "session not found" in lost.output
    assert "lives in a project scope" in lost.output


def test_cli_personal_session_not_visible_to_project(tmp_path, monkeypatch):
    """The reverse direction: a personal session is not resumable from a
    project scope; the hint says to drop --project."""
    import aegis_agent.sessions.sqlite_store as store_mod

    monkeypatch.delenv("AEGIS_DB_PATH", raising=False)
    personal_db = tmp_path / "personal-state.db"
    monkeypatch.setattr(store_mod, "DEFAULT_DB_PATH", personal_db)
    proj = tmp_path / "proj"
    proj.mkdir()

    first = runner.invoke(
        app,
        ["--model-backend", "fake", "--no-mcp", "--session", "pers1"],
        input="hello aegis\nexit\n",
    )
    assert first.exit_code == 0, first.output

    lost = runner.invoke(
        app,
        ["--model-backend", "fake", "--no-mcp", "--project", str(proj), "--resume", "pers1"],
        input="exit\n",
    )
    assert lost.exit_code == 1
    assert "session not found" in lost.output
    assert "lives in personal scope" in lost.output


def test_cli_explicit_db_overrides_project_scope(tmp_path, monkeypatch):
    """An explicit --db / AEGIS_DB_PATH still wins over the scoped default."""
    from aegis_agent.memory.paths import project_home

    explicit_db = tmp_path / "explicit.db"
    monkeypatch.setenv("AEGIS_DB_PATH", str(explicit_db))
    proj = tmp_path / "proj"
    proj.mkdir()

    result = runner.invoke(
        app,
        ["--model-backend", "fake", "--no-mcp", "--project", str(proj), "--session", "p4"],
        input="hello aegis\nexit\n",
    )
    assert result.exit_code == 0, result.output
    assert explicit_db.is_file()
    assert not (project_home(proj) / "state.db").exists()
