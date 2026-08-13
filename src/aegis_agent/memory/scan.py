# Behavioural reference (adapted and simplified):
#   * Claude Code Auto Memory / ``src/memdir/memoryScan.ts:scanMemoryFiles`` —
#     scans the memory directory reading only each file's leading frontmatter
#     (name / description / type / mtime), caps the scan at ~200 files, and
#     skips the ``MEMORY.md`` index itself.  See ``Claude-Code/docs/08-memory.md``.
#   * ``formatMemoryManifest`` — renders the scan into a compact list the side
#     query LLM ranks for relevance (never the bodies).
#
# Aegis reuses the Stage-1 frontmatter parser but adds a *metadata-only* read
# path so building the candidate manifest never loads memory bodies.
"""Scan the personal memory directory into a lightweight manifest.

The manifest is the input to relevance recall (:mod:`aegis_agent.memory.retriever`)
and to the extractor's "what already exists" check
(:mod:`aegis_agent.memory.extractor`).  It carries only the metadata needed to
*rank* memories — filename, name, description, type, mtime — never the bodies,
which are read on demand only for the handful the side query selects.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from aegis_agent.memory.paths import memory_dir
from aegis_agent.memory.store import _read_text
from aegis_agent.memory.types import MemoryType
from aegis_agent.skills.frontmatter import parse_frontmatter

logger = logging.getLogger(__name__)

#: Upper bound on files considered per scan (mirrors Claude Code's cap).
MAX_SCAN_FILES = 200

#: Only the frontmatter is needed; read at most this many leading lines so a
#: huge memory body never has to be loaded just to rank it.
_FRONTMATTER_MAX_LINES = 40

#: The index file is never itself a memory candidate.
_INDEX_FILENAME = "MEMORY.md"


@dataclass(frozen=True)
class MemoryCandidate:
    """One memory file's ranking metadata (no body)."""

    filename: str          # basename, e.g. "prefer-uv.md"
    name: str
    description: str
    memory_type: MemoryType | None
    mtime: float
    path: Path


def _read_frontmatter_head(path: Path) -> str | None:
    """Read only the leading frontmatter block of a file, or ``None``.

    Reads the whole file but is called only on ``*.md`` memory files, which are
    small by design; to stay cheap on a pathologically large body we truncate
    to the first :data:`_FRONTMATTER_MAX_LINES` lines before parsing (the
    closing ``---`` fence is always near the top of a well-formed memory file).
    """
    content = _read_text(path)
    if content is None:
        return None
    lines = content.splitlines()
    if len(lines) > _FRONTMATTER_MAX_LINES:
        content = "\n".join(lines[:_FRONTMATTER_MAX_LINES])
    return content


def scan_memory_files(home: str | Path | None = None) -> list[MemoryCandidate]:
    """Scan ``<home>/memory/*.md`` into ranking candidates.

    Excludes ``MEMORY.md``; caps the result at :data:`MAX_SCAN_FILES` (newest
    first by mtime, matching Claude Code's recency bias).  A missing directory
    yields ``[]``; a single unreadable / malformed / vanished file is skipped
    with a debug log and never aborts the scan — recall must never break the
    main turn.
    """
    directory = memory_dir(home)
    if not directory.is_dir():
        return []

    candidates: list[MemoryCandidate] = []
    try:
        entries = sorted(directory.glob("*.md"))
    except OSError:
        return []

    for path in entries:
        if path.name == _INDEX_FILENAME:
            continue
        try:
            if not path.is_file():
                continue
            mtime = path.stat().st_mtime
            head = _read_frontmatter_head(path)
            if head is None:
                continue
            frontmatter, _ = parse_frontmatter(head)
        except OSError:
            logger.debug("memory scan: skipping unreadable %s", path, exc_info=True)
            continue
        name = str(frontmatter.get("name", "")).strip() or path.stem
        description = str(frontmatter.get("description", "")).strip()
        memory_type = MemoryType.parse(frontmatter.get("type"))
        candidates.append(
            MemoryCandidate(
                filename=path.name,
                name=name,
                description=description,
                memory_type=memory_type,
                mtime=mtime,
                path=path,
            )
        )

    # Newest first, then cap.
    candidates.sort(key=lambda c: c.mtime, reverse=True)
    if len(candidates) > MAX_SCAN_FILES:
        logger.debug(
            "memory scan: %d files exceeds cap %d; using newest %d",
            len(candidates),
            MAX_SCAN_FILES,
            MAX_SCAN_FILES,
        )
        candidates = candidates[:MAX_SCAN_FILES]
    return candidates


def format_manifest(candidates: list[MemoryCandidate]) -> str:
    """Render candidates into the compact list the side query LLM ranks.

    One block per memory: filename, type, description.  Bodies are never
    included — the manifest exists only to answer "which of these is relevant?".
    """
    blocks: list[str] = []
    for c in candidates:
        type_label = c.memory_type.value if c.memory_type else "unknown"
        desc = c.description or "(no description)"
        blocks.append(f"- {c.filename}\n  type: {type_label}\n  description: {desc}")
    return "\n\n".join(blocks)


__all__ = [
    "MAX_SCAN_FILES",
    "MemoryCandidate",
    "format_manifest",
    "scan_memory_files",
]
