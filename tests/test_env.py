"""Tests for the minimal .env loader."""

from __future__ import annotations

import os

from aegis_agent.env import load_dotenv


def _write(path, text):
    path.write_text(text, encoding="utf-8")
    return path


def test_loads_key_value(tmp_path, monkeypatch):
    env = _write(tmp_path / ".env", "AEGIS_MODEL=qwen-plus\nAEGIS_BASE_URL=https://x/v1\n")
    monkeypatch.delenv("AEGIS_MODEL", raising=False)
    monkeypatch.delenv("AEGIS_BASE_URL", raising=False)
    assert load_dotenv(env) is True
    assert os.environ["AEGIS_MODEL"] == "qwen-plus"
    assert os.environ["AEGIS_BASE_URL"] == "https://x/v1"


def test_comments_blank_lines_and_export_prefix(tmp_path, monkeypatch):
    env = _write(
        tmp_path / ".env",
        "# comment\n\nexport AEGIS_MODEL=qwen-turbo\n   \nAEGIS_MODEL2 = spaced \n",
    )
    monkeypatch.delenv("AEGIS_MODEL", raising=False)
    load_dotenv(env)
    assert os.environ["AEGIS_MODEL"] == "qwen-turbo"
    assert os.environ["AEGIS_MODEL2"] == "spaced"


def test_quoted_values_unwrapped(tmp_path, monkeypatch):
    env = _write(tmp_path / ".env", 'AEGIS_API_KEY="sk-abc123"\nAEGIS_OTHER=\'single\'\n')
    monkeypatch.delenv("AEGIS_API_KEY", raising=False)
    monkeypatch.delenv("AEGIS_OTHER", raising=False)
    load_dotenv(env)
    assert os.environ["AEGIS_API_KEY"] == "sk-abc123"
    assert os.environ["AEGIS_OTHER"] == "single"


def test_real_env_not_overridden_by_default(tmp_path, monkeypatch):
    env = _write(tmp_path / ".env", "AEGIS_MODEL=from-file\n")
    monkeypatch.setenv("AEGIS_MODEL", "from-env")
    load_dotenv(env)
    assert os.environ["AEGIS_MODEL"] == "from-env"  # real env wins


def test_override_flag_replaces(tmp_path, monkeypatch):
    env = _write(tmp_path / ".env", "AEGIS_MODEL=from-file\n")
    monkeypatch.setenv("AEGIS_MODEL", "from-env")
    load_dotenv(env, override=True)
    assert os.environ["AEGIS_MODEL"] == "from-file"


def test_missing_file_returns_false(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert load_dotenv(tmp_path / "nope.env") is False
    assert load_dotenv() is False  # no .env anywhere up from tmp_path's parents is fine


def test_searches_parent_directories(tmp_path, monkeypatch):
    _write(tmp_path / ".env", "AEGIS_MODEL=qwen-max\n")
    subdir = tmp_path / "a" / "b"
    subdir.mkdir(parents=True)
    monkeypatch.chdir(subdir)
    monkeypatch.delenv("AEGIS_MODEL", raising=False)
    assert load_dotenv() is True
    assert os.environ["AEGIS_MODEL"] == "qwen-max"
