"""CLI minimal-startup tests using Typer's CliRunner."""

from __future__ import annotations

from typer.testing import CliRunner

from aegis_agent.cli import app

runner = CliRunner()


def test_cli_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "aegis" in result.output


def test_cli_repl_processes_input_and_exits():
    # Feed one message then the exit command; the fake echoes the message.
    result = runner.invoke(app, ["--model-backend", "fake"], input="hello aegis\nexit\n")
    assert result.exit_code == 0
    assert "Echo: hello aegis" in result.output
    assert "bye." in result.output


def test_cli_repl_tool_command():
    # 'list' triggers the list_directory tool via the rule-based fake.
    result = runner.invoke(app, ["--model-backend", "fake"], input="list .\nexit\n")
    assert result.exit_code == 0
    # After the tool runs, the fake summarises the tool result.
    assert "list_directory" in result.output


def test_cli_repl_eof_exits_cleanly():
    # No exit command — EOF on stdin must still terminate the loop.
    result = runner.invoke(app, ["--model-backend", "fake"], input="")
    assert result.exit_code == 0
