# Portions adapted from Hermes (hermes-agent), © 2025 Nous Research.
# Licensed under the MIT License. See THIRD_PARTY_NOTICES.md.
#
# Behavioural source (adapted and simplified):
#   * ``tools/mcp_tool.py:_load_mcp_config`` (line 2537, MIT) — reads a YAML
#     config file, extracts the ``mcp_servers`` key, and recursively interpolates
#     ``${ENV_VAR}`` placeholders.  Aegis drops the Hermes-specific config
#     backend (``hermes_cli.config.load_config``) and the dotenv side-load.
"""Load MCP server configuration from a YAML file.

Configuration lives under the ``mcp_servers:`` key of ``~/.aegis/config.yaml``
(or a custom path).  Each server entry is a mapping; :func:`load_mcp_config`
returns the raw dict with ``${ENV}`` placeholders resolved.  Validation happens
at connection time, not at load time.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)

#: Default values applied to every server entry that omits the key.
DEFAULT_MCP_SERVER_CONFIG: dict[str, Any] = {
    "timeout": 120,
    "connect_timeout": 60,
    "enabled": True,
    "transport": "http",
    "args": [],
    "headers": {},
    "env": {},
    "tools": {},
}

_ENV_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}")


def _default_config_path() -> Path:
    return Path.home() / ".aegis" / "config.yaml"


def load_mcp_config(path: str | Path | None = None) -> dict[str, dict]:
    """Return the ``mcp_servers`` map from the config file.

    ``path`` defaults to ``~/.aegis/config.yaml``.  Returns an empty dict when
    the file is missing or has no ``mcp_servers`` key — never raises.
    ``${ENV_VAR}`` placeholders are recursively resolved from ``os.environ``.

    Before interpolation, ``~/.aegis/.env`` (if present) is loaded into
    ``os.environ`` so secrets stored there are available for ``${VAR}``
    substitution without manual export.
    """
    _load_dotenv_if_present()
    target = Path(path) if path else _default_config_path()
    if not target.exists():
        logger.debug("MCP config file not found: %s", target)
        return {}

    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, OSError) as exc:
        logger.warning("Failed to parse MCP config %s: %s", target, exc)
        return {}

    servers = raw.get("mcp_servers")
    if not servers or not isinstance(servers, dict):
        return {}

    result: dict[str, dict] = {}
    for name, cfg in servers.items():
        if not isinstance(cfg, dict):
            logger.warning("MCP server %r entry is not a mapping — skipping", name)
            continue
        resolved = _interpolate_env_vars(cfg)
        # Merge defaults for missing keys.
        merged = dict(DEFAULT_MCP_SERVER_CONFIG, **resolved)
        result[name] = merged
    return result


def _interpolate_env_vars(value: Any) -> Any:
    """Recursively resolve ``${VAR}`` placeholders from ``os.environ``."""
    if isinstance(value, str):
        def _replace(m: re.Match) -> str:
            return os.environ.get(m.group(1), m.group(0))
        return _ENV_VAR_PATTERN.sub(_replace, value)
    if isinstance(value, dict):
        return {k: _interpolate_env_vars(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate_env_vars(v) for v in value]
    return value


def _load_dotenv_if_present() -> None:
    """Load ``~/.aegis/.env`` into ``os.environ`` if it exists.

    This allows users to store MCP API keys in a file without exporting them
    in every shell session, mirroring Hermes' ``.env`` pattern.
    """
    dotenv_path = Path.home() / ".aegis" / ".env"
    if not dotenv_path.exists():
        return
    try:
        for line in dotenv_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("'\"")
            if key and key not in os.environ:
                os.environ[key] = value
    except OSError:
        pass


__all__ = ["DEFAULT_MCP_SERVER_CONFIG", "load_mcp_config"]
