# Behavioural reference (adapted and simplified):
#   * Claude Code Auto Memory / ``src/memdir/findRelevantMemories.ts`` — scan →
#     manifest → side query (JSON schema, ≤5 files, "unsure → don't pick") →
#     validate returned filenames against the manifest → read only the selected
#     bodies.  See ``Claude-Code/docs/08-memory.md``.
#   * ``src/utils/attachments.ts:getRelevantMemoryAttachments`` /
#     ``readMemoriesForSurfacing`` / ``collectSurfacedMemories`` — read the
#     selected bodies with per-file + total caps, tag them as
#     ``relevant_memories``, and de-duplicate against what was already surfaced
#     this session.
#
# Aegis keeps the pipeline and the caps but drops the prefetch/async attachment
# machinery: recall is a single synchronous best-effort call the manager runs
# before the turn, and injection is via a prompt contributor rather than an
# attachment message.
"""Relevance recall: pick the memories worth surfacing for a user query.

The pipeline (all best-effort — any failure yields "no recall"):

1. scan the memory dir into a manifest (metadata only);
2. ask a side-query model which files are relevant (≤5, JSON, unsure→skip);
3. keep only filenames that were in the manifest (reject anything invented,
   path-escaping, or already surfaced this session);
4. read the selected bodies under per-file and total byte caps;
5. render a ``Relevant memories`` block for injection into the derived context.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from aegis_agent.memory.scan import format_manifest, scan_memory_files
from aegis_agent.memory.sidequery import run_side_query
from aegis_agent.memory.store import parse_memory_file
from aegis_agent.models.base import ModelProvider

logger = logging.getLogger(__name__)

#: Hard cap on how many memories are surfaced in one turn (mirrors Claude Code).
MAX_RECALL_FILES = 5

#: Per-file body cap and total cap for injected memory text (bytes, UTF-8).
MAX_MEMORY_BODY_BYTES = 4 * 1024
MAX_TOTAL_RECALL_BYTES = 12 * 1024

_SIDE_QUERY_SYSTEM = (
    "You select which stored long-term memories are relevant to the user's "
    "current message. You are given a manifest of memories (filename, type, "
    "one-line description) and the user's message. Return STRICT JSON of the "
    'form {"files": ["a.md", "b.md"]} listing at most 5 filenames, copied '
    "EXACTLY from the manifest, for memories clearly relevant to the message. "
    "If none are clearly relevant, return {\"files\": []}. When unsure, leave a "
    "file out — prefer too few over too many. Do not invent filenames. Return "
    "only the JSON object, nothing else."
)


@dataclass(frozen=True)
class RecalledMemory:
    """One memory selected and read for surfacing."""

    filename: str
    name: str
    description: str
    memory_type: str
    body: str


@dataclass
class RecallResult:
    """Outcome of one recall pass (for injection + telemetry)."""

    memories: list[RecalledMemory] = field(default_factory=list)
    candidate_count: int = 0
    selected_count: int = 0
    selected_files: list[str] = field(default_factory=list)
    failure_reason: str | None = None


def _select_filenames(raw: object, valid: set[str]) -> list[str]:
    """Coerce the side-query ``files`` field into a validated filename list.

    Keeps only strings that (a) appear verbatim in the manifest ``valid`` set —
    which by construction are bare ``*.md`` basenames — and (b) are not
    duplicated.  Anything invented, path-like (``../x``), or absent is dropped.
    Truncated to :data:`MAX_RECALL_FILES`.
    """
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        name = item.strip()
        if name in valid and name not in out:
            out.append(name)
        if len(out) >= MAX_RECALL_FILES:
            break
    return out


def _truncate_bytes(text: str, limit: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", errors="ignore").rstrip() + "\n[... memory truncated]"


def recall_memories(
    provider: ModelProvider,
    user_query: str,
    home: str | None = None,
    *,
    already_surfaced: set[str] | None = None,
) -> RecallResult:
    """Run the full recall pipeline for ``user_query``; never raises.

    ``already_surfaced`` (a session-scoped set of filenames) is excluded from
    the candidate manifest so a memory surfaced earlier in the session is not
    re-injected every turn.  On any failure a :class:`RecallResult` with an
    empty ``memories`` list and a ``failure_reason`` is returned.
    """
    surfaced = already_surfaced or set()
    query = (user_query or "").strip()
    if not query:
        return RecallResult(failure_reason="empty_query")

    candidates = scan_memory_files(home)
    # Drop already-surfaced files before ranking (dedup channel #1).
    candidates = [c for c in candidates if c.filename not in surfaced]
    if not candidates:
        return RecallResult(candidate_count=0, failure_reason="no_candidates")

    manifest = format_manifest(candidates)
    user_prompt = f"User message:\n{query}\n\nMemory manifest:\n{manifest}"
    parsed = run_side_query(provider, _SIDE_QUERY_SYSTEM, user_prompt)
    if parsed is None:
        return RecallResult(candidate_count=len(candidates), failure_reason="side_query_failed")

    valid = {c.filename for c in candidates}
    selected = _select_filenames(parsed.get("files"), valid)
    if not selected:
        return RecallResult(candidate_count=len(candidates), selected_count=0)

    by_name = {c.filename: c for c in candidates}
    memories: list[RecalledMemory] = []
    total = 0
    for filename in selected:
        cand = by_name[filename]
        entry = parse_memory_file(cand.path)
        if entry is None:
            continue
        body = _truncate_bytes(entry.body.strip(), MAX_MEMORY_BODY_BYTES)
        body_bytes = len(body.encode("utf-8"))
        if total + body_bytes > MAX_TOTAL_RECALL_BYTES:
            break
        total += body_bytes
        memories.append(
            RecalledMemory(
                filename=filename,
                name=entry.name,
                description=entry.description,
                memory_type=entry.memory_type.value if entry.memory_type else "unknown",
                body=body,
            )
        )

    return RecallResult(
        memories=memories,
        candidate_count=len(candidates),
        selected_count=len(memories),
        selected_files=[m.filename for m in memories],
    )


def render_recall_block(memories: list[RecalledMemory]) -> str | None:
    """Render selected memories into a labelled ``Relevant memories`` block.

    Each memory keeps its filename / type / description header above the body so
    the model can tell recalled bodies apart from ``USER.md`` and the
    ``MEMORY.md`` index.  Returns ``None`` for an empty list.
    """
    if not memories:
        return None
    parts = [
        "## Relevant memories",
        (
            "The following long-term memories were automatically retrieved as "
            "possibly relevant to the current message. They are history and may be "
            "stale — verify before relying on specifics."
        ),
    ]
    for m in memories:
        parts.append(
            f"\n<memory file=\"{m.filename}\" type=\"{m.memory_type}\">\n"
            f"description: {m.description}\n\n{m.body}\n</memory>"
        )
    return "\n".join(parts)


__all__ = [
    "MAX_MEMORY_BODY_BYTES",
    "MAX_RECALL_FILES",
    "MAX_TOTAL_RECALL_BYTES",
    "RecallResult",
    "RecalledMemory",
    "recall_memories",
    "render_recall_block",
]
