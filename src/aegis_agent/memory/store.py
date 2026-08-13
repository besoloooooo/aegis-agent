# Behavioural reference (adapted and simplified):
#   * Claude Code Auto Memory / ``src/memdir/memdir.ts:truncateEntrypointContent``
#     — the ``MEMORY.md`` index is loaded into context under a dual cap (200
#     lines + 25 KB) with a warning appended when truncated so the model knows
#     the index may be incomplete.  See ``Claude-Code/docs/08-memory.md``.
#   * ``src/memdir/memoryScan.ts:scanMemoryFiles`` — reading only the leading
#     frontmatter of each memory file.  Aegis reuses the skills frontmatter
#     parser for that.
#   * ``src/memdir/memdir.ts`` (directory-creation guarantee) + the index
#     maintenance implied by ``extractMemories`` writing memory files and the
#     ``MEMORY.md`` index staying in sync — Stage-2/3 add the *write* side:
#     rendering a memory file, rebuilding the index, all confined to the memory
#     directory.
"""Read *and write* personal memory on disk.

Read paths (Stage 1):

* :func:`load_user_profile` — the whole ``USER.md`` (a stable, hand-maintained
  profile), truncated under the same dual cap as the index.
* :func:`load_memory_index` — the ``MEMORY.md`` auto-memory index, truncated
  under a 200-line / 25 KB cap with a warning when it overflows.
* :func:`parse_memory_file` — parse one memory ``*.md`` into a
  :class:`MemoryEntry` (frontmatter + body).

Write paths (Stage 3, used by the background extractor):

* :func:`render_memory_file` — serialise a memory into ``frontmatter + body``.
* :func:`write_memory_file` — atomically create/overwrite one memory file,
  **only inside the memory directory** (path-escape is refused here as a
  defence-in-depth layer on top of the extractor's filename validation).
* :func:`rebuild_index` — regenerate ``MEMORY.md`` from the current memory
  files: one ``- [name](file.md) — description`` line each, deduplicated and
  sorted, so the index is idempotent w.r.t. the directory contents.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from aegis_agent.memory.types import MemoryType
from aegis_agent.skills.frontmatter import parse_frontmatter
from aegis_agent.tools.fsutil import atomic_write

logger = logging.getLogger(__name__)

#: Dual truncation cap for entrypoint content (mirrors Claude Code).
MAX_ENTRYPOINT_LINES = 200
MAX_ENTRYPOINT_BYTES = 25 * 1024  # 25 KB

_TRUNCATION_NOTICE = (
    "\n\n[... truncated: this file exceeds the {limit} limit; the view above "
    "may be incomplete. Consider trimming it so the whole index stays visible.]"
)


@dataclass(frozen=True)
class MemoryEntry:
    """A parsed memory file: frontmatter fields + body.

    ``memory_type`` is ``None`` when the frontmatter ``type`` is missing or not
    one of the four known kinds (the file is still usable, just untyped).
    """

    name: str
    description: str
    memory_type: MemoryType | None
    body: str
    path: Path


def truncate_entrypoint_content(
    content: str,
    *,
    max_lines: int = MAX_ENTRYPOINT_LINES,
    max_bytes: int = MAX_ENTRYPOINT_BYTES,
) -> str:
    """Truncate ``content`` to the line/byte caps, appending a notice if cut.

    Line cap is applied first, then the byte cap on the (possibly already
    line-truncated) text.  Whichever cap fired is named in the appended notice
    so the model knows the view may be incomplete.  Byte counting uses UTF-8 so
    multibyte memory content is measured the way it lands on disk.
    """
    reasons: list[str] = []

    lines = content.splitlines()
    if len(lines) > max_lines:
        content = "\n".join(lines[:max_lines])
        reasons.append(f"{max_lines}-line")

    encoded = content.encode("utf-8")
    if len(encoded) > max_bytes:
        # Cut on a byte boundary, then drop any trailing partial UTF-8 sequence.
        content = encoded[:max_bytes].decode("utf-8", errors="ignore")
        reasons.append(f"{max_bytes // 1024} KB")

    if reasons:
        content = content.rstrip() + _TRUNCATION_NOTICE.format(limit=" / ".join(reasons))
    return content


def _read_text(path: Path) -> str | None:
    """Read a UTF-8 file, returning ``None`` when it is absent or unreadable.

    A missing file is the normal "no memory yet" case and must never raise —
    memory is strictly additive to startup.
    """
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return None


# ── mtime staleness check (cross-process, best-effort) ──────────────────────
#
# Behavioural reference: Claude Code ``FileWriteTool.ts`` — before writing, it
# compares the file's current mtime against the time this process last read it
# (``readFileState``); if the file changed since, it raises
# ``FILE_UNEXPECTEDLY_MODIFIED_ERROR`` instead of silently overwriting.  Aegis
# reproduces that optimistic-concurrency check for memory writes: a write whose
# target was touched by another process since we last read it is refused.  New
# files (never read → no record) are not checked, matching Claude's
# ``meta === null`` skip.  This is NOT a lock — it only detects conflicts, it
# does not serialise writers.

#: Resolved path → mtime this process last observed (read or wrote) the file.
_read_state: dict[Path, float] = {}


def record_read(path: str | Path, mtime: float) -> None:
    """Record that this process last observed ``path`` at ``mtime``."""
    try:
        _read_state[Path(path).resolve()] = mtime
    except OSError:
        pass


def get_last_read(path: str | Path) -> float | None:
    """Return the mtime this process last recorded for ``path``, or ``None``."""
    try:
        return _read_state.get(Path(path).resolve())
    except OSError:
        return None


def _record_read_of(path: Path) -> None:
    """Record this process's current observation of ``path`` (best-effort)."""
    try:
        _read_state[path.resolve()] = path.stat().st_mtime
    except OSError:
        pass


def load_user_profile(path: str | Path) -> str | None:
    """Return the truncated ``USER.md`` text, or ``None`` if absent/empty."""
    content = _read_text(Path(path))
    if content is None or not content.strip():
        return None
    return truncate_entrypoint_content(content)


def load_memory_index(path: str | Path) -> str | None:
    """Return the truncated ``MEMORY.md`` index text, or ``None`` if absent/empty."""
    content = _read_text(Path(path))
    if content is None or not content.strip():
        return None
    return truncate_entrypoint_content(content)


def parse_memory_file(path: str | Path) -> MemoryEntry | None:
    """Parse one memory ``*.md`` file into a :class:`MemoryEntry`.

    Returns ``None`` when the file cannot be read.  A missing ``name`` falls
    back to the filename stem so the entry is still identifiable; ``type`` that
    is absent/unknown yields ``memory_type=None``.
    """
    p = Path(path)
    content = _read_text(p)
    if content is None:
        return None
    _record_read_of(p)
    frontmatter, body = parse_frontmatter(content)
    name = str(frontmatter.get("name", "")).strip() or p.stem
    description = str(frontmatter.get("description", "")).strip()
    memory_type = MemoryType.parse(frontmatter.get("type"))
    return MemoryEntry(
        name=name,
        description=description,
        memory_type=memory_type,
        body=body,
        path=p,
    )


# ── write side (Stage 3) ─────────────────────────────────────────────────────

#: Filenames refused as memory targets (the index is managed separately).
_INDEX_FILENAME = "MEMORY.md"


def _yaml_escape(value: str) -> str:
    """Escape a scalar for a single-line double-quoted YAML value."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").strip()


def render_memory_file(
    *,
    name: str,
    description: str,
    memory_type: MemoryType | str | None,
    body: str,
) -> str:
    """Serialise a memory into ``--- frontmatter --- \\n body`` text.

    ``name`` / ``description`` are single-line (newlines collapsed); the body is
    kept verbatim.  ``memory_type`` accepts an enum, a string, or ``None``
    (omitted).  The output round-trips through :func:`parse_memory_file`.
    """
    mt = MemoryType.parse(memory_type) if memory_type is not None else None
    lines = ["---", f'name: "{_yaml_escape(name)}"', f'description: "{_yaml_escape(description)}"']
    if mt is not None:
        lines.append(f"type: {mt.value}")
    lines.append("---")
    text = "\n".join(lines) + "\n\n" + body.strip() + "\n"
    return text


def is_valid_memory_filename(filename: str) -> bool:
    """Return True when ``filename`` is a safe, bare ``*.md`` memory filename.

    Rejects anything with a path separator, a ``..`` component, an absolute
    path, or the reserved index name — the write layer's defence-in-depth check
    so a memory can never be created outside the memory directory.
    """
    if not filename or filename != Path(filename).name:
        return False
    if filename == _INDEX_FILENAME or filename.startswith("."):
        return False
    if not filename.endswith(".md"):
        return False
    return ".." not in filename


def write_memory_file(
    home: str | Path | None,
    filename: str,
    content: str,
    *,
    memory_directory: str | Path | None = None,
) -> Path:
    """Atomically write ``content`` to ``<memory_dir>/<filename>``.

    ``filename`` must satisfy :func:`is_valid_memory_filename`; the resolved
    path is additionally verified to stay inside the memory directory (a
    symlink / normalisation escape is refused).  Raises :class:`ValueError` on
    any violation — the caller treats that as "skip this action".
    """
    from aegis_agent.memory.paths import memory_dir as _memory_dir

    if not is_valid_memory_filename(filename):
        raise ValueError(f"unsafe memory filename: {filename!r}")

    directory = Path(memory_directory) if memory_directory is not None else _memory_dir(home)
    directory.mkdir(parents=True, exist_ok=True)
    target = (directory / filename).resolve()
    # Defence in depth: the resolved target must live directly in the dir.
    if target.parent != directory.resolve():
        raise ValueError(f"memory path escapes the memory directory: {filename!r}")

    # mtime staleness check (cross-process, best-effort): refuse to overwrite a
    # file that changed since this process last read it — another process wrote
    # it, so the write is skipped rather than silently clobbering the newer
    # content.  New files (no prior read) are written unconditionally.
    if target.exists():
        try:
            current = target.stat().st_mtime
        except OSError:
            current = 0.0
        last = get_last_read(target)
        if last is not None and current > last:
            raise ValueError(
                f"memory file {filename!r} changed since it was last read; skipping write"
            )

    atomic_write(target, content)
    _record_read_of(target)
    return target


def _iter_memory_entries(directory: Path) -> list[MemoryEntry]:
    """Parse every non-index ``*.md`` in ``directory`` into entries (sorted)."""
    entries: list[MemoryEntry] = []
    try:
        paths = sorted(directory.glob("*.md"))
    except OSError:
        return []
    for path in paths:
        if path.name == _INDEX_FILENAME:
            continue
        entry = parse_memory_file(path)
        if entry is not None:
            entries.append(entry)
    return entries


def rebuild_index(
    home: str | Path | None = None,
    *,
    memory_directory: str | Path | None = None,
) -> str:
    """Regenerate ``MEMORY.md`` from the current memory files; return its text.

    Idempotent: the index is derived purely from the directory contents, so the
    same files always produce the same index (one ``- [name](file.md) — desc``
    line per memory, sorted by filename, no duplicates).  When the directory has
    no memories the index file is removed (an empty index is noise).  Errors are
    logged and swallowed — index maintenance must never crash extraction.
    """
    from aegis_agent.memory.paths import memory_dir as _memory_dir
    from aegis_agent.memory.paths import memory_index_path as _index_path

    directory = Path(memory_directory) if memory_directory is not None else _memory_dir(home)
    index_file = (
        directory / _INDEX_FILENAME
        if memory_directory is not None
        else _index_path(home)
    )
    if not directory.is_dir():
        return ""

    entries = _iter_memory_entries(directory)
    if not entries:
        try:
            if index_file.exists():
                index_file.unlink()
        except OSError:
            logger.debug("failed to remove empty index %s", index_file, exc_info=True)
        return ""

    lines = ["# Memory", ""]
    seen: set[str] = set()
    for entry in entries:
        fname = entry.path.name
        if fname in seen:
            continue
        seen.add(fname)
        title = entry.name or entry.path.stem
        desc = entry.description or "(no description)"
        lines.append(f"- [{title}]({fname}) — {desc}")
    text = "\n".join(lines) + "\n"

    try:
        directory.mkdir(parents=True, exist_ok=True)
        atomic_write(index_file, text)
    except OSError:
        logger.warning("failed to write memory index %s", index_file, exc_info=True)
    return text


__all__ = [
    "MAX_ENTRYPOINT_BYTES",
    "MAX_ENTRYPOINT_LINES",
    "MemoryEntry",
    "is_valid_memory_filename",
    "load_memory_index",
    "load_user_profile",
    "parse_memory_file",
    "rebuild_index",
    "render_memory_file",
    "truncate_entrypoint_content",
    "write_memory_file",
]
