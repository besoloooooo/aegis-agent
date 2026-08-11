"""Builtin tool tests: read_file, list_directory."""

from __future__ import annotations

import json

from aegis_agent.tools.builtin import ListDirectoryTool, ReadFileTool
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
