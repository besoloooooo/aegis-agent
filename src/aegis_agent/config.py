"""Application configuration from ``~/.aegis/config.yaml``.

The same YAML file that holds ``mcp_servers:`` (loaded by
:mod:`aegis_agent.mcp.config`) also accepts a set of top-level keys that drive
the CLI's defaults — so an operator can persist preferences like
``memory.recall: true`` instead of passing ``--memory-recall`` every run.

Resolution precedence (highest wins):

1. an explicit CLI flag (the flag's tri-state value is not ``None``);
2. a value set in this config file;
3. the built-in default coded in :mod:`aegis_agent.cli`.

Only the keys an operator wants to override need to be present; everything
else keeps its built-in default.  The file is optional — a missing or
unparseable file is treated as an empty config and never raises.

Example::

    mcp_servers: { ... }          # consumed by the MCP loader

    memory:
      enabled: true               # --no-memory flips this off
      recall: true                # default ON
      extract: true               # default ON
      project: null               # --project
    context:
      max_tokens: 120000          # --context-max-tokens
      compress: true             # --no-compress flips this off
    iterations:
      max: 10                     # --max-iterations
    session:
      db_path: null              # --db
      snapshot_every_n: 20        # --snapshot-every-n
      lease: true                 # --no-lease flips this off
    skills:
      enabled: true               # --no-skills flips this off
      dir: null                  # --skills-dir
    mcp:
      enabled: true               # --no-mcp flips this off
    shell:
      allow_dangerous: false      # --allow-dangerous-shell
    model:
      backend: auto               # --model-backend
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path.home() / ".aegis" / "config.yaml"


def load_app_config(path: str | Path | None = None) -> dict[str, Any]:
    """Return the raw parsed config (the whole YAML mapping, not just ``mcp_servers``).

    ``path`` defaults to ``~/.aegis/config.yaml``.  A missing or unparseable
    file returns ``{}`` — never raises.  ``${ENV}`` placeholders are NOT
    interpolated here (that is the MCP loader's concern); app-config values are
    used verbatim.
    """
    target = Path(path) if path else DEFAULT_CONFIG_PATH
    if not target.exists():
        return {}
    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, OSError) as exc:
        logger.warning("Failed to parse app config %s: %s", target, exc)
        return {}
    if not isinstance(raw, dict):
        logger.warning("App config %s is not a mapping — ignoring", target)
        return {}
    return raw


def get(cfg: dict[str, Any], section: str, key: str, default: Any = None) -> Any:
    """Read ``cfg[section][key]`` with a fallback; tolerates missing sections."""
    section_map = cfg.get(section)
    if not isinstance(section_map, dict):
        return default
    value = section_map.get(key)
    return default if value is None else value


def resolve_enabled(
    disable_flag: bool | None, cfg: dict[str, Any], section: str, key: str, *, default: bool = True
) -> bool:
    """Resolve an *enable* boolean for a ``--no-x`` tri-state flag.

    * ``disable_flag is True``  → the user passed ``--no-x`` → disabled.
    * ``disable_flag is False`` → the user passed ``--x`` (the positive form) → enabled.
    * ``disable_flag is None``  → not specified → fall to ``cfg[section][key]``, then ``default``.
    """
    if disable_flag is True:
        return False
    if disable_flag is False:
        return True
    cfg_val = get(cfg, section, key, None)
    if cfg_val is not None:
        return bool(cfg_val)
    return default


def resolve_flag(
    flag: bool | None, cfg: dict[str, Any], section: str, key: str, *, default: bool = False
) -> bool:
    """Resolve a positive tri-state flag (``--x`` enables, ``--no-x`` disables).

    * ``flag is not None`` → the user was explicit; use it.
    * else ``cfg[section][key]`` if present, then ``default``.
    """
    if flag is not None:
        return flag
    cfg_val = get(cfg, section, key, None)
    if cfg_val is not None:
        return bool(cfg_val)
    return default


def resolve_value(value: Any, cfg: dict[str, Any], section: str, key: str, default: Any) -> Any:
    """Resolve a scalar/path flag: explicit value → config → default."""
    if value is not None:
        return value
    cfg_val = get(cfg, section, key, None)
    return default if cfg_val is None else cfg_val


__all__ = ["DEFAULT_CONFIG_PATH", "get", "load_app_config", "resolve_enabled", "resolve_flag", "resolve_value"]
