"""patch builtin tool tests (replace mode)."""

from __future__ import annotations

import json

from aegis_agent.tools.builtin import PatchTool
from aegis_agent.tools.registry import ToolContext


def _ctx(tmp_path) -> ToolContext:
    return ToolContext(cwd=str(tmp_path))


def test_patch_exact_replace(tmp_path):
    f = tmp_path / "code.py"
    f.write_text("def foo():\n    return 1\n", encoding="utf-8")
    result = PatchTool().run(
        {"path": "code.py", "old_string": "    return 1", "new_string": "    return 2"},
        _ctx(tmp_path),
    )
    assert not result.is_error
    payload = json.loads(result.content)
    assert payload["success"] is True
    assert payload["replaced"] == 1
    assert payload["strategy"] == "exact"
    assert "return 2" in f.read_text(encoding="utf-8")
    assert "-    return 1" in payload["diff"]
    assert "+    return 2" in payload["diff"]


def test_patch_fuzzy_whitespace_match(tmp_path):
    f = tmp_path / "code.py"
    f.write_text("def foo():\n        return 1\n", encoding="utf-8")  # 8-space indent
    # old_string uses 4-space indent — fuzzy indentation-flexible match.
    result = PatchTool().run(
        {"path": "code.py", "old_string": "    return 1", "new_string": "    return 9"},
        _ctx(tmp_path),
    )
    payload = json.loads(result.content)
    assert payload["success"] is True
    text = f.read_text(encoding="utf-8")
    assert "return 9" in text


def test_patch_delete_with_empty_new_string(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("keep\ndelete-me\nkeep2\n", encoding="utf-8")
    result = PatchTool().run(
        {"path": "f.txt", "old_string": "delete-me", "new_string": ""},
        _ctx(tmp_path),
    )
    assert json.loads(result.content)["success"] is True
    assert "delete-me" not in f.read_text(encoding="utf-8")


def test_patch_ambiguous_requires_replace_all(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("x\nx\nx\n", encoding="utf-8")
    result = PatchTool().run({"path": "f.txt", "old_string": "x", "new_string": "y"}, _ctx(tmp_path))
    assert result.is_error
    assert "matches" in json.loads(result.content)["error"].lower()


def test_patch_replace_all(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("x\nx\nx\n", encoding="utf-8")
    result = PatchTool().run(
        {"path": "f.txt", "old_string": "x", "new_string": "y", "replace_all": True},
        _ctx(tmp_path),
    )
    payload = json.loads(result.content)
    assert payload["replaced"] == 3
    assert f.read_text(encoding="utf-8") == "y\ny\ny\n"


def test_patch_no_match_gives_hint(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("alpha beta gamma\n", encoding="utf-8")
    result = PatchTool().run({"path": "f.txt", "old_string": "beta gamme", "new_string": "z"}, _ctx(tmp_path))
    assert result.is_error
    error = json.loads(result.content)["error"]
    assert "Could not find" in error
    assert "Did you mean" in error  # closest-lines hint appended


def test_patch_missing_file(tmp_path):
    result = PatchTool().run({"path": "nope.txt", "old_string": "a", "new_string": "b"}, _ctx(tmp_path))
    assert result.is_error
    assert "not found" in json.loads(result.content)["error"].lower()


def test_patch_requires_fields(tmp_path):
    assert PatchTool().run({}, _ctx(tmp_path)).is_error
    assert PatchTool().run({"path": "x"}, _ctx(tmp_path)).is_error
    assert PatchTool().run({"path": "x", "old_string": "a"}, _ctx(tmp_path)).is_error


def test_patch_preserves_crlf(tmp_path):
    f = tmp_path / "win.txt"
    f.write_bytes(b"foo = 1\r\nbar = 2\r\n")
    PatchTool().run({"path": "win.txt", "old_string": "foo = 1", "new_string": "foo = 99"}, _ctx(tmp_path))
    data = f.read_bytes()
    assert b"foo = 99" in data
    assert b"\r\n" in data  # CRLF preserved, not converted to LF
