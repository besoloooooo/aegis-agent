"""Minimal ``.env`` loader — no external dependency.

Loads ``KEY=VALUE`` pairs from a ``.env`` file into ``os.environ``.  Variables
already present in the real environment are NOT overridden (real env wins), so
an exported ``AEGIS_API_KEY`` always beats the file.  Supports blank lines,
``#`` comments, an optional ``export `` prefix, and single/double-quoted
values.  This is a deliberately tiny subset of ``python-dotenv`` — enough for
local CLI configuration without adding a dependency.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

DEFAULT_FILENAME = ".env"


def load_dotenv(path: str | os.PathLike[str] | None = None, *, override: bool = False) -> bool:
    """Load a ``.env`` file into ``os.environ``.

    When ``path`` is None, searches for ``.env`` in the current directory and
    then each parent directory (so running from a project subdirectory still
    finds the project-root file).  Returns True when a file was found and
    loaded, False otherwise.
    """
    target = _find(path)
    if target is None:
        return False
    for key, value in _parse(target):
        if override or key not in os.environ:
            os.environ[key] = value
    return True


def _find(path: str | os.PathLike[str] | None) -> Path | None:
    if path is not None:
        candidate = Path(path)
        return candidate if candidate.is_file() else None
    cwd = Path.cwd()
    for directory in (cwd, *cwd.parents):
        candidate = directory / DEFAULT_FILENAME
        if candidate.is_file():
            return candidate
    return None


def _parse(path: Path) -> Iterator[tuple[str, str]]:
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        yield key, value


__all__ = ["DEFAULT_FILENAME", "load_dotenv"]
