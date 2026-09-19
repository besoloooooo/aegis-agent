"""terminal builtin tool tests (foreground + dangerous-command guardrail)."""

from __future__ import annotations

import json
import os
import shutil

import pytest

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
    assert result.is_error
    payload = json.loads(result.content)
    assert payload["exit_code"] == 3
    assert payload["error"] == "Command exited with status 3."


def test_terminal_python_exit_one_is_error(tmp_path):
    tool, _, ctx = _make(tmp_path)
    result = tool.run({"command": 'python -c "raise SystemExit(1)"'}, ctx)
    payload = json.loads(result.content)
    assert result.is_error
    assert payload["exit_code"] == 1
    assert "non-zero" in payload["exit_code_meaning"]


def test_terminal_grep_explanation_does_not_override_error(tmp_path):
    tool, _, ctx = _make(tmp_path)
    result = tool.run({"command": "printf found | grep missing"}, ctx)
    payload = json.loads(result.content)
    assert result.is_error
    assert payload["exit_code"] == 1
    assert "no lines were selected" in payload["exit_code_meaning"]


def test_terminal_pytest_exit_one_is_error(tmp_path):
    (tmp_path / "test_failing.py").write_text(
        "def test_failing():\n    assert False\n",
        encoding="utf-8",
    )
    tool, _, ctx = _make(tmp_path)
    result = tool.run({"command": "python -m pytest -q test_failing.py"}, ctx)
    payload = json.loads(result.content)
    assert result.is_error
    assert payload["exit_code"] == 1
    assert "1 failed" in payload["output"]


def test_terminal_git_exit_one_is_error(tmp_path):
    tool, _, ctx = _make(tmp_path)
    result = tool.run(
        {
            "command": (
                "git init -q && git config user.email test@example.invalid "
                "&& git config user.name Test && git commit -m empty"
            )
        },
        ctx,
    )
    payload = json.loads(result.content)
    assert result.is_error
    assert payload["exit_code"] == 1
    assert payload["error"] == "Command exited with status 1."


def test_terminal_server_path_argument_does_not_trigger_hint(tmp_path):
    tool, _, ctx = _make(tmp_path)
    result = tool.run({"command": "printf '%s\\n' /git/server.git"}, ctx)
    payload = json.loads(result.content)
    assert "hint" not in payload


@pytest.mark.parametrize(
    "command",
    ["python -m http.server --help", "watch --help"],
)
def test_terminal_real_server_and_watch_commands_trigger_hint(tmp_path, command):
    tool, _, ctx = _make(tmp_path)
    result = tool.run({"command": command}, ctx)
    payload = json.loads(result.content)
    assert "long-running server/watch command" in payload["hint"]


@pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None,
    reason="POSIX Bash pipefail is unavailable",
)
def test_terminal_pipeline_reports_upstream_failure(tmp_path):
    tool, _, ctx = _make(tmp_path)
    result = tool.run(
        {"command": 'python -c "raise SystemExit(7)" 2>&1 | head -100'},
        ctx,
    )
    payload = json.loads(result.content)
    assert result.is_error
    assert payload["exit_code"] == 7


def test_terminal_timeout(tmp_path):
    tool, _, ctx = _make(tmp_path)
    result = tool.run({"command": "sleep 5", "timeout": 1}, ctx)
    assert result.is_error
    payload = json.loads(result.content)
    assert payload["exit_code"] == 124
    assert "timed out" in payload["error"].lower()


def test_terminal_handles_partial_output_on_timeout(tmp_path):
    tool, _, ctx = _make(tmp_path)
    result = tool.run({"command": "printf partial; sleep 5", "timeout": 1}, ctx)
    assert result.is_error
    payload = json.loads(result.content)
    assert payload["exit_code"] == 124
    assert payload["output"] == "partial"


def test_terminal_handles_partial_stderr_on_timeout(tmp_path):
    tool, _, ctx = _make(tmp_path)
    result = tool.run({"command": "printf partial >&2; sleep 5", "timeout": 1}, ctx)
    assert result.is_error
    payload = json.loads(result.content)
    assert payload["exit_code"] == 124
    assert payload["output"] == "partial"


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


def test_terminal_foreground_cancel_raises(tmp_path):
    """A cooperative cancel aborts the foreground command (raises, no result)."""
    from aegis_agent.exceptions import OperationCancelled

    tool, _, _ = _make(tmp_path)
    ctx = ToolContext(cwd=str(tmp_path), is_cancelled=lambda: True)
    with pytest.raises(OperationCancelled):
        # The cancel is checked before the child is even waited on, so this
        # returns immediately instead of sleeping.
        tool.run({"command": "sleep 5", "timeout": 30}, ctx)
