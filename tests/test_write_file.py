"""write_file builtin tool tests."""

from __future__ import annotations

import json
import sys

import pytest

from aegis_agent.tools.builtin import WriteFileTool
from aegis_agent.tools.registry import ToolContext


def _ctx(tmp_path) -> ToolContext:
    return ToolContext(cwd=str(tmp_path))


def test_write_new_file(tmp_path):
    result = WriteFileTool().run({"path": "hello.txt", "content": "hello aegis\n"}, _ctx(tmp_path))
    assert not result.is_error
    payload = json.loads(result.content)
    assert payload["created"] is True
    assert payload["dirs_created"] is False
    assert (tmp_path / "hello.txt").read_text(encoding="utf-8") == "hello aegis\n"
    assert payload["bytes_written"] == len(b"hello aegis\n")


def test_write_creates_parent_dirs(tmp_path):
    result = WriteFileTool().run({"path": "a/b/c.txt", "content": "deep"}, _ctx(tmp_path))
    assert not result.is_error
    payload = json.loads(result.content)
    assert payload["dirs_created"] is True
    assert (tmp_path / "a" / "b" / "c.txt").read_text(encoding="utf-8") == "deep"


def test_write_overwrites_existing(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("old content here\n", encoding="utf-8")
    result = WriteFileTool().run({"path": "f.txt", "content": "new\n"}, _ctx(tmp_path))
    payload = json.loads(result.content)
    assert payload["created"] is False
    assert f.read_text(encoding="utf-8") == "new\n"


def test_write_preserves_crlf_line_endings(tmp_path):
    f = tmp_path / "win.txt"
    f.write_bytes(b"line1\r\nline2\r\n")
    WriteFileTool().run({"path": "win.txt", "content": "a\nb\n"}, _ctx(tmp_path))
    assert f.read_bytes() == b"a\r\nb\r\n"


def test_write_refuses_system_path(tmp_path):
    if sys.platform == "win32":
        pytest.skip("POSIX system-path guard")
    result = WriteFileTool().run({"path": "/etc/hostname-aegis", "content": "x"}, _ctx(tmp_path))
    assert result.is_error
    assert "denied" in json.loads(result.content)["error"].lower()


def test_write_requires_fields(tmp_path):
    assert WriteFileTool().run({}, _ctx(tmp_path)).is_error
    assert WriteFileTool().run({"path": "x.txt"}, _ctx(tmp_path)).is_error


def test_write_rejects_directory(tmp_path):
    result = WriteFileTool().run({"path": ".", "content": "x"}, _ctx(tmp_path))
    assert result.is_error
    assert "directory" in json.loads(result.content)["error"].lower()
