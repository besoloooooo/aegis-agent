"""Tests for ~/.aegis/config.yaml app-config loading and flag resolution.

Precedence being exercised: explicit CLI flag > config file > built-in default.
"""

from __future__ import annotations

from aegis_agent.config import (
    get,
    load_app_config,
    resolve_enabled,
    resolve_flag,
    resolve_value,
)


def test_load_missing_file_returns_empty(tmp_path):
    assert load_app_config(tmp_path / "nope.yaml") == {}


def test_load_parses_top_level_keys(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "mcp_servers:\n"
        "  x: {command: echo}\n"
        "memory:\n"
        "  recall: false\n"
        "  extract: false\n",
        encoding="utf-8",
    )
    cfg = load_app_config(cfg_file)
    assert "mcp_servers" in cfg
    assert get(cfg, "memory", "recall") is False
    assert get(cfg, "memory", "extract") is False


def test_load_bad_yaml_returns_empty(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("memory: [unclosed", encoding="utf-8")
    assert load_app_config(cfg_file) == {}


def test_get_defaults_when_section_missing():
    assert get({}, "memory", "recall", None) is None
    assert get({"memory": {}}, "memory", "recall", None) is None


def test_resolve_flag_explicit_wins():
    cfg = {"memory": {"recall": False}}
    assert resolve_flag(True, cfg, "memory", "recall", default=True) is True
    assert resolve_flag(False, cfg, "memory", "recall", default=True) is False


def test_resolve_flag_falls_to_config_then_default():
    cfg = {"memory": {"recall": False}}
    assert resolve_flag(None, cfg, "memory", "recall", default=True) is False
    assert resolve_flag(None, {}, "memory", "recall", default=True) is True


def test_resolve_enabled_no_flag_semantics():
    cfg = {"memory": {"enabled": False}}
    # --no-memory passed → disabled regardless of config.
    assert resolve_enabled(True, cfg, "memory", "enabled", default=True) is False
    # --memory passed → enabled even if config says off.
    assert resolve_enabled(False, cfg, "memory", "enabled", default=True) is True
    # Not specified → config value.
    assert resolve_enabled(None, cfg, "memory", "enabled", default=True) is False
    # Not specified, no config → default.
    assert resolve_enabled(None, {}, "memory", "enabled", default=True) is True


def test_resolve_value_precedence():
    cfg = {"iterations": {"max": 5}}
    assert resolve_value(3, cfg, "iterations", "max", 10) == 3
    assert resolve_value(None, cfg, "iterations", "max", 10) == 5
    assert resolve_value(None, {}, "iterations", "max", 10) == 10


def test_recall_extract_default_on():
    # With no flags and no config, both default to True.
    assert resolve_flag(None, {}, "memory", "recall", default=True) is True
    assert resolve_flag(None, {}, "memory", "extract", default=True) is True


def test_cli_runner_respects_config_recall_off(tmp_path, monkeypatch):
    """A config file turning recall/extract off still starts cleanly."""
    from typer.testing import CliRunner

    from aegis_agent.cli import app

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AEGIS_DB_PATH", str(tmp_path / "state.db"))
    cfg = tmp_path / "config.yaml"
    cfg.write_text("memory:\n  recall: false\n  extract: false\n", encoding="utf-8")

    result = CliRunner().invoke(
        app,
        ["--model-backend", "fake", "--mcp-config", str(cfg), "--no-mcp", "--no-memory-recall"],
        input="exit\n",
    )
    assert result.exit_code == 0
    assert "Memory:" in result.output


def test_cli_runner_memory_recall_default_on_panel(tmp_path, monkeypatch):
    """Default (no flags) boots cleanly; with no USER.md/MEMORY.md the panel
    reports 'Memory: none' (index empty), not an error."""
    from typer.testing import CliRunner

    from aegis_agent.cli import app

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AEGIS_DB_PATH", str(tmp_path / "state.db"))

    result = CliRunner().invoke(
        app, ["--model-backend", "fake", "--no-mcp"], input="exit\n"
    )
    assert result.exit_code == 0
    assert "Memory: none" in result.output
