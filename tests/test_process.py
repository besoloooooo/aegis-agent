"""process builtin tool tests (background process lifecycle)."""

from __future__ import annotations

import json

from aegis_agent.tools.builtin import ProcessTool, TerminalTool
from aegis_agent.tools.process_registry import ProcessRegistry
from aegis_agent.tools.registry import ToolContext


def _make(tmp_path):
    registry = ProcessRegistry()
    terminal = TerminalTool(registry)
    process = ProcessTool(registry)
    ctx = ToolContext(cwd=str(tmp_path))
    return terminal, process, ctx


def _spawn(terminal, ctx, command="echo hi && sleep 0.1"):
    result = terminal.run({"command": command, "background": True}, ctx)
    return json.loads(result.content)["session_id"]


def test_process_list_shows_spawned(tmp_path):
    terminal, process, ctx = _make(tmp_path)
    sid = _spawn(terminal, ctx)
    payload = json.loads(process.run({"action": "list"}, ctx).content)
    ids = {p["session_id"] for p in payload["processes"]}
    assert sid in ids


def test_process_poll_and_wait(tmp_path):
    terminal, process, ctx = _make(tmp_path)
    sid = _spawn(terminal, ctx, command="echo done-marker")
    wait = json.loads(process.run({"action": "wait", "session_id": sid, "timeout": 10}, ctx).content)
    assert wait["status"] == "exited"
    assert wait["exit_code"] == 0
    assert "done-marker" in wait["output"]

    poll = json.loads(process.run({"action": "poll", "session_id": sid}, ctx).content)
    assert poll["status"] == "exited"
    assert poll["exit_code"] == 0


def test_process_log(tmp_path):
    terminal, process, ctx = _make(tmp_path)
    sid = _spawn(terminal, ctx, command="printf 'l1\\nl2\\nl3\\n'")
    process.run({"action": "wait", "session_id": sid, "timeout": 10}, ctx)
    log = json.loads(process.run({"action": "log", "session_id": sid}, ctx).content)
    assert "l1" in log["output"] and "l3" in log["output"]
    assert log["total_lines"] >= 3


def test_process_kill(tmp_path):
    terminal, process, ctx = _make(tmp_path)
    sid = _spawn(terminal, ctx, command="sleep 60")
    kill = json.loads(process.run({"action": "kill", "session_id": sid}, ctx).content)
    assert kill["status"] == "killed"
    # Killing again reports already exited.
    again = json.loads(process.run({"action": "kill", "session_id": sid}, ctx).content)
    assert again["status"] == "already_exited"


def test_process_stdin_write_submit(tmp_path):
    terminal, process, ctx = _make(tmp_path)
    # `cat` echoes stdin back to stdout.
    sid = _spawn(terminal, ctx, command="cat")
    write = json.loads(process.run({"action": "submit", "session_id": sid, "data": "ping"}, ctx).content)
    assert write["status"] == "ok"
    process.run({"action": "close", "session_id": sid}, ctx)
    wait = json.loads(process.run({"action": "wait", "session_id": sid, "timeout": 10}, ctx).content)
    assert "ping" in wait["output"]


def test_process_not_found(tmp_path):
    _, process, ctx = _make(tmp_path)
    for action in ("poll", "log", "wait", "kill", "write", "submit", "close"):
        payload = json.loads(process.run({"action": action, "session_id": "proc_nope"}, ctx).content)
        assert payload["status"] == "not_found"


def test_process_requires_session_id(tmp_path):
    _, process, ctx = _make(tmp_path)
    result = process.run({"action": "poll"}, ctx)
    assert result.is_error


def test_process_invalid_action(tmp_path):
    _, process, ctx = _make(tmp_path)
    result = process.run({"action": "explode"}, ctx)
    assert result.is_error
