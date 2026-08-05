"""MCP config loader tests."""

from __future__ import annotations

import pytest

from aegis_agent.mcp.config import load_mcp_config


class TestLoadMCPConfig:
    def test_empty_when_file_missing(self, tmp_path):
        result = load_mcp_config(tmp_path / "nonexistent.yaml")
        assert result == {}

    def test_empty_when_no_mcp_servers_key(self, tmp_path):
        f = tmp_path / "config.yaml"
        f.write_text("other: data\n", encoding="utf-8")
        result = load_mcp_config(f)
        assert result == {}

    def test_empty_when_mcp_servers_not_dict(self, tmp_path):
        f = tmp_path / "config.yaml"
        f.write_text("mcp_servers: [1, 2, 3]\n", encoding="utf-8")
        result = load_mcp_config(f)
        assert result == {}

    def test_loads_server_entries(self, tmp_path):
        f = tmp_path / "config.yaml"
        f.write_text(
            "mcp_servers:\n  filesystem:\n    command: npx\n    args: [-y, fs]\n",
            encoding="utf-8",
        )
        result = load_mcp_config(f)
        assert "filesystem" in result
        assert result["filesystem"]["command"] == "npx"
        assert result["filesystem"]["args"] == ["-y", "fs"]

    def test_skips_non_dict_entry(self, tmp_path):
        f = tmp_path / "config.yaml"
        f.write_text(
            "mcp_servers:\n  bad: just a string\n  good:\n    url: http://x\n",
            encoding="utf-8",
        )
        result = load_mcp_config(f)
        assert "bad" not in result
        assert "good" in result

    def test_merges_defaults(self, tmp_path):
        f = tmp_path / "config.yaml"
        f.write_text("mcp_servers:\n  srv:\n    command: echo\n", encoding="utf-8")
        result = load_mcp_config(f)
        assert result["srv"]["timeout"] == 120
        assert result["srv"]["connect_timeout"] == 60
        assert result["srv"]["enabled"] is True
        assert result["srv"]["headers"] == {}

    def test_interpolates_env_vars(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MY_SECRET", "abc123")
        f = tmp_path / "config.yaml"
        f.write_text(
            "mcp_servers:\n  srv:\n    url: http://host\n    headers:\n      Authorization: Bearer ${MY_SECRET}\n",
            encoding="utf-8",
        )
        result = load_mcp_config(f)
        assert result["srv"]["headers"]["Authorization"] == "Bearer abc123"

    def test_unmatched_env_var_kept_as_is(self, tmp_path):
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.delenv("UNKNOWN_VAR", raising=False)
        f = tmp_path / "config.yaml"
        f.write_text("mcp_servers:\n  srv:\n    url: ${UNKNOWN_VAR}/api\n", encoding="utf-8")
        result = load_mcp_config(f)
        assert "${UNKNOWN_VAR}" in result["srv"]["url"]

    def test_interpolates_in_lists(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DIR", "/data")
        f = tmp_path / "config.yaml"
        f.write_text(
            'mcp_servers:\n  srv:\n    command: npx\n    args: ["-y", "${DIR}", extra]\n',
            encoding="utf-8",
        )
        result = load_mcp_config(f)
        assert result["srv"]["args"] == ["-y", "/data", "extra"]
