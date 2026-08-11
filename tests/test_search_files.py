"""search_files builtin tool tests."""

from __future__ import annotations

import json

from aegis_agent.tools.builtin import SearchFilesTool
from aegis_agent.tools.registry import ToolContext


def _ctx(tmp_path) -> ToolContext:
    return ToolContext(cwd=str(tmp_path))


def _seed(tmp_path):
    (tmp_path / "a.py").write_text("import os\ndef main():\n    print('hello')\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("def helper():\n    return 42\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("nothing here\n", encoding="utf-8")
    sub = tmp_path / "pkg"
    sub.mkdir()
    (sub / "c.py").write_text("def main():  # another main\n    pass\n", encoding="utf-8")


# -- content search --------------------------------------------------------


def test_search_content_matches(tmp_path):
    _seed(tmp_path)
    result = SearchFilesTool().run({"pattern": "def main"}, _ctx(tmp_path))
    assert not result.is_error
    payload = json.loads(result.content)
    paths = {m["path"].split("/")[-1].split("\\")[-1] for m in payload["matches"]}
    assert "a.py" in paths and "c.py" in paths
    assert payload["total_count"] >= 2
    first = payload["matches"][0]
    assert "line" in first and "content" in first


def test_search_content_file_glob_filter(tmp_path):
    _seed(tmp_path)
    result = SearchFilesTool().run(
        {"pattern": "return 42", "file_glob": "*.py"}, _ctx(tmp_path)
    )
    payload = json.loads(result.content)
    assert all(m["path"].endswith(".py") for m in payload["matches"])
    assert payload["total_count"] == 1


def test_search_content_no_matches(tmp_path):
    _seed(tmp_path)
    result = SearchFilesTool().run({"pattern": "zzz_not_present"}, _ctx(tmp_path))
    payload = json.loads(result.content)
    assert payload["total_count"] == 0


def test_search_files_only_mode(tmp_path):
    _seed(tmp_path)
    result = SearchFilesTool().run(
        {"pattern": "def main", "output_mode": "files_only"}, _ctx(tmp_path)
    )
    payload = json.loads(result.content)
    assert "files" in payload
    assert all(p.endswith(".py") for p in payload["files"])


def test_search_count_mode(tmp_path):
    _seed(tmp_path)
    result = SearchFilesTool().run(
        {"pattern": "def main", "output_mode": "count"}, _ctx(tmp_path)
    )
    payload = json.loads(result.content)
    assert "counts" in payload
    assert sum(payload["counts"].values()) == payload["total_count"]


def test_search_invalid_regex(tmp_path):
    _seed(tmp_path)
    result = SearchFilesTool().run({"pattern": "([unclosed"}, _ctx(tmp_path))
    # Either our regex validation or rg surfaces an error.
    assert result.is_error


def test_search_limit_and_offset(tmp_path):
    _seed(tmp_path)
    limited = SearchFilesTool().run({"pattern": "def", "limit": 1}, _ctx(tmp_path))
    payload = json.loads(limited.content)
    assert len(payload["matches"]) == 1
    assert payload["truncated"] is True
    # offset shifts the window.
    rest = SearchFilesTool().run({"pattern": "def", "limit": 1, "offset": 1}, _ctx(tmp_path))
    assert json.loads(rest.content)["matches"] != payload["matches"]


# -- file (name) search -----------------------------------------------------


def test_search_target_files_by_glob(tmp_path):
    _seed(tmp_path)
    result = SearchFilesTool().run({"pattern": "*.py", "target": "files"}, _ctx(tmp_path))
    payload = json.loads(result.content)
    names = {p.split("/")[-1].split("\\")[-1] for p in payload["files"]}
    assert names == {"a.py", "b.py", "c.py"}


def test_search_target_files_bare_pattern(tmp_path):
    _seed(tmp_path)
    result = SearchFilesTool().run({"pattern": "notes", "target": "files"}, _ctx(tmp_path))
    payload = json.loads(result.content)
    assert any(p.endswith("notes.txt") for p in payload["files"])


def test_search_path_not_found(tmp_path):
    result = SearchFilesTool().run({"pattern": "x", "path": "no-such-dir"}, _ctx(tmp_path))
    assert result.is_error
    assert "not found" in json.loads(result.content)["error"].lower()


def test_search_requires_pattern(tmp_path):
    assert SearchFilesTool().run({}, _ctx(tmp_path)).is_error


def test_search_excludes_hidden_and_vcs(tmp_path):
    _seed(tmp_path)
    git = tmp_path / ".git"
    git.mkdir()
    (git / "hidden.py").write_text("def main(): pass\n", encoding="utf-8")
    result = SearchFilesTool().run({"pattern": "*.py", "target": "files"}, _ctx(tmp_path))
    payload = json.loads(result.content)
    assert not any("hidden.py" in p for p in payload["files"])
