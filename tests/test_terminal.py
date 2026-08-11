"""terminal builtin tool tests (foreground + dangerous-command guardrail)."""

from __future__ import annotations

import json

from aegis_agent.tools.builtin import TerminalTool
from aegis_agent.tools.process_registry import ProcessRegistry
from aegis_agent.tools.registry import ToolContext


def _make(tmp_path, allow_dangerous=False):
    registry = ProcessRegistry()
    tool = TerminalTool(registry)
    ctx = ToolContext(cwd=str(tmp_path), allow_dangerous_shell=allow_dangerous)
    return tool, registry, ctx


def test_terminal_foreground_captures_output_and_exit_code(tmp_path):
    tool, _, ctx = _make(tmp_path)
    result = tool.run({"command": "echo hello-aegis"}, ctx)
    assert not result.is_error
    payload = json.loads(result.content)
    assert "hello-aegis" in payload["output"]
    assert payload["exit_code"] == 0
    assert payload["error"] is None


def test_terminal_nonzero_exit(tmp_path):
    tool, _, ctx = _make(tmp_path)
    result = tool.run({"command": "exit 3"}, ctx)
    payload = json.loads(result.content)
    assert payload["exit_code"] == 3


def test_terminal_timeout(tmp_path):
    tool, _, ctx = _make(tmp_path)
    result = tool.run({"command": "sleep 5", "timeout": 1}, ctx)
    assert result.is_error
    payload = json.loads(result.content)
    assert payload["exit_code"] == 124
    assert "timed out" in payload["error"].lower()


def test_terminal_requires_command(tmp_path):
    tool, _, ctx = _make(tmp_path)
    assert tool.run({}, ctx).is_error


def test_terminal_uses_workdir(tmp_path):
    sub = tmp_path / "wd"
    sub.mkdir()
    (sub / "marker.txt").write_text("here", encoding="utf-8")
    tool, _, ctx = _make(tmp_path)
    result = tool.run({"command": "ls", "workdir": "wd"}, ctx)
    payload = json.loads(result.content)
    assert "marker.txt" in payload["output"]


def test_terminal_background_returns_session_id(tmp_path):
    tool, registry, ctx = _make(tmp_path)
    result = tool.run({"command": "echo bg-out", "background": True}, ctx)
    assert not result.is_error
    payload = json.loads(result.content)
    assert payload["session_id"].startswith("proc_")
    assert payload["exit_code"] == 0
    # The process is tracked in the shared registry.
    assert registry.get(payload["session_id"]) is not None
    registry.kill_process(payload["session_id"])


# -- dangerous-command guardrail (ported from Hermes) ------------------------


def test_dangerous_command_blocked_by_default(tmp_path):
    tool, _, ctx = _make(tmp_path)
    result = tool.run({"command": "rm -rf /"}, ctx)
    assert result.is_error
    assert "Blocked dangerous command" in json.loads(result.content)["error"]


def test_dangerous_command_git_reset_hard_blocked(tmp_path):
    tool, _, ctx = _make(tmp_path)
    result = tool.run({"command": "git reset --hard"}, ctx)
    assert result.is_error
    assert "git reset --hard" in json.loads(result.content)["error"]


def test_safe_command_not_blocked(tmp_path):
    tool, _, ctx = _make(tmp_path)
    assert not tool.run({"command": "echo safe"}, ctx).is_error


def test_dangerous_command_allowed_with_operator_override(tmp_path):
    tool, _, ctx = _make(tmp_path, allow_dangerous=True)
    result = tool.run({"command": "git branch -D somebranch 2>&1 || true"}, ctx)
    # Executes (exit may be non-zero) but is NOT the guardrail's blocked-error.
    if result.is_error:
        assert "Blocked dangerous command" not in json.loads(result.content)["error"]


def test_model_cannot_enable_dangerous_via_arguments(tmp_path):
    tool, _, ctx = _make(tmp_path)
    result = tool.run({"command": "rm -rf /", "allow_dangerous": True, "force": True}, ctx)
    assert result.is_error
    assert "Blocked dangerous command" in json.loads(result.content)["error"]


def test_detect_dangerous_command_subset():
    from aegis_agent.tools.danger import detect_dangerous_command

    assert detect_dangerous_command("rm -rf /tmp/x") is not None
    assert detect_dangerous_command("ls -la") is None
    assert detect_dangerous_command("echo hello") is None
    assert detect_dangerous_command("") is None
