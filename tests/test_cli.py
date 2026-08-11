"""CLI minimal-startup tests using Typer's CliRunner."""

from __future__ import annotations

from typer.testing import CliRunner

from aegis_agent.cli import app

runner = CliRunner()


def test_cli_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "aegis" in result.output


def test_cli_repl_processes_input_and_exits(tmp_path, monkeypatch):
    # Feed one message then the exit command; the fake echoes the message.
    monkeypatch.setenv("AEGIS_DB_PATH", str(tmp_path / "state.db"))
    result = runner.invoke(app, ["--model-backend", "fake"], input="hello aegis\nexit\n")
    assert result.exit_code == 0
    assert "Echo: hello aegis" in result.output
    assert "bye." in result.output


def test_cli_repl_tool_command(tmp_path, monkeypatch):
    # 'list' triggers the list_directory tool via the rule-based fake.
    monkeypatch.setenv("AEGIS_DB_PATH", str(tmp_path / "state.db"))
    result = runner.invoke(app, ["--model-backend", "fake"], input="list .\nexit\n")
    assert result.exit_code == 0
    # After the tool runs, the fake summarises the tool result.
    assert "list_directory" in result.output


def test_cli_repl_eof_exits_cleanly(tmp_path, monkeypatch):
    # No exit command — EOF on stdin must still terminate the loop.
    monkeypatch.setenv("AEGIS_DB_PATH", str(tmp_path / "state.db"))
    result = runner.invoke(app, ["--model-backend", "fake"], input="")
    assert result.exit_code == 0


def test_cli_resume_restores_session(tmp_path, monkeypatch):
    """First run persists a turn; --resume picks the same session up."""
    db = str(tmp_path / "state.db")
    monkeypatch.setenv("AEGIS_DB_PATH", db)
    first = runner.invoke(app, ["--model-backend", "fake"], input="hello aegis\nexit\n")
    assert first.exit_code == 0

    resumed = runner.invoke(
        app, ["--model-backend", "fake", "--resume", "default"], input="again\nexit\n"
    )
    assert resumed.exit_code == 0
    assert "Resumed session default (2 messages)." in resumed.output
    assert "Echo: again" in resumed.output


def test_cli_resume_unknown_session_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("AEGIS_DB_PATH", str(tmp_path / "state.db"))
    result = runner.invoke(app, ["--model-backend", "fake", "--resume", "nope"], input="")
    assert result.exit_code == 1
    assert "session not found" in result.output


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
    holder_repo.create_session("default")
    holder = SessionLeaseManager(get_lease_backend(holder_repo), ttl_s=30.0)
    assert holder.acquire("default")
    try:
        result = runner.invoke(app, ["--model-backend", "fake"], input="exit\n")
        assert result.exit_code == 1
        assert "owned by another process" in result.output
    finally:
        holder.stop()
        holder_repo.close()

    # After the holder releases, the session is resumable again.
    result = runner.invoke(app, ["--model-backend", "fake"], input="exit\n")
    assert result.exit_code == 0
