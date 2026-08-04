"""Builtin tool tests: read_file, list_directory, run_shell."""

from __future__ import annotations

import json

from aegis_agent.tools.builtin import ListDirectoryTool, ReadFileTool, RunShellTool
from aegis_agent.tools.registry import ToolContext


def _ctx(tmp_path) -> ToolContext:
    return ToolContext(cwd=str(tmp_path))


# -- read_file ---------------------------------------------------------------


def test_read_file_basic(tmp_path):
    f = tmp_path / "hello.txt"
    f.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    result = ReadFileTool().run({"path": "hello.txt"}, _ctx(tmp_path))

    assert not result.is_error
    payload = json.loads(result.content)
    assert payload["total_lines"] == 3
    assert payload["truncated"] is False
    # LINE_NUM|CONTENT format, 1-indexed
    assert payload["content"].splitlines()[0] == "1|alpha"
    assert payload["content"].splitlines()[2] == "3|gamma"


def test_read_file_pagination(tmp_path):
    f = tmp_path / "big.txt"
    f.write_text("\n".join(f"line{i}" for i in range(1, 11)), encoding="utf-8")
    result = ReadFileTool().run({"path": "big.txt", "offset": 4, "limit": 3}, _ctx(tmp_path))

    payload = json.loads(result.content)
    assert payload["truncated"] is True
    lines = payload["content"].splitlines()
    assert lines == ["4|line4", "5|line5", "6|line6"]


def test_read_file_missing(tmp_path):
    result = ReadFileTool().run({"path": "nope.txt"}, _ctx(tmp_path))
    assert result.is_error
    assert "not found" in json.loads(result.content)["error"].lower()


def test_read_file_requires_path(tmp_path):
    result = ReadFileTool().run({}, _ctx(tmp_path))
    assert result.is_error
    assert "path" in json.loads(result.content)["error"]


def test_read_file_rejects_directory(tmp_path):
    result = ReadFileTool().run({"path": "."}, _ctx(tmp_path))
    assert result.is_error
    assert "directory" in json.loads(result.content)["error"].lower()


# -- list_directory ----------------------------------------------------------


def test_list_directory(tmp_path):
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    (tmp_path / "subdir").mkdir()
    result = ListDirectoryTool().run({"path": "."}, _ctx(tmp_path))

    assert not result.is_error
    payload = json.loads(result.content)
    names = {e["name"]: e for e in payload["entries"]}
    assert names["a.txt"]["type"] == "file"
    assert names["a.txt"]["size"] == 1
    assert names["subdir"]["type"] == "dir"
    assert names["subdir"]["size"] is None
    assert payload["count"] == 2


def test_list_directory_missing(tmp_path):
    result = ListDirectoryTool().run({"path": "does-not-exist"}, _ctx(tmp_path))
    assert result.is_error


# -- run_shell ---------------------------------------------------------------


def test_run_shell_captures_output_and_exit_code(tmp_path):
    result = RunShellTool().run({"command": "echo hello-aegis"}, _ctx(tmp_path))
    assert not result.is_error
    payload = json.loads(result.content)
    assert "hello-aegis" in payload["output"]
    assert payload["exit_code"] == 0


def test_run_shell_nonzero_exit(tmp_path):
    result = RunShellTool().run({"command": "exit 3"}, _ctx(tmp_path))
    payload = json.loads(result.content)
    assert payload["exit_code"] == 3


def test_run_shell_timeout(tmp_path):
    result = RunShellTool().run({"command": "sleep 5", "timeout": 1}, _ctx(tmp_path))
    assert result.is_error
    assert "timed out" in json.loads(result.content)["error"].lower()


def test_run_shell_requires_command(tmp_path):
    result = RunShellTool().run({}, _ctx(tmp_path))
    assert result.is_error


def test_run_shell_uses_workdir(tmp_path):
    sub = tmp_path / "wd"
    sub.mkdir()
    (sub / "marker.txt").write_text("here", encoding="utf-8")
    result = RunShellTool().run({"command": "ls", "workdir": "wd"}, _ctx(tmp_path))
    payload = json.loads(result.content)
    assert "marker.txt" in payload["output"]


# -- dangerous-command guardrail (ported from Hermes) ------------------------


def test_dangerous_command_blocked_by_default(tmp_path):
    result = RunShellTool().run({"command": "rm -rf /"}, _ctx(tmp_path))
    assert result.is_error
    error = json.loads(result.content)["error"]
    assert "Blocked dangerous command" in error


def test_dangerous_command_git_reset_hard_blocked(tmp_path):
    result = RunShellTool().run({"command": "git reset --hard"}, _ctx(tmp_path))
    assert result.is_error
    assert "git reset --hard" in json.loads(result.content)["error"]


def test_safe_command_not_blocked(tmp_path):
    result = RunShellTool().run({"command": "echo safe"}, _ctx(tmp_path))
    assert not result.is_error


def test_dangerous_command_allowed_with_operator_override(tmp_path):
    ctx = ToolContext(cwd=str(tmp_path), allow_dangerous_shell=True)
    # Same destructive pattern as above, but the operator opted in.
    result = RunShellTool().run({"command": "git branch -D somebranch 2>&1 || true"}, ctx)
    # It executes (exit code may be non-zero because there's no such branch),
    # but it is NOT the guardrail's blocked-error.
    if result.is_error:
        assert "Blocked dangerous command" not in json.loads(result.content)["error"]


def test_model_cannot_enable_dangerous_via_arguments(tmp_path):
    # The tool has no 'force'/'allow' argument — passing one changes nothing.
    result = RunShellTool().run({"command": "rm -rf /", "allow_dangerous": True, "force": True}, _ctx(tmp_path))
    assert result.is_error
    assert "Blocked dangerous command" in json.loads(result.content)["error"]


def test_detect_dangerous_command_subset():
    from aegis_agent.tools.danger import detect_dangerous_command

    assert detect_dangerous_command("rm -rf /tmp/x") is not None
    assert detect_dangerous_command("ls -la") is None
    assert detect_dangerous_command("echo hello") is None
    assert detect_dangerous_command("") is None

