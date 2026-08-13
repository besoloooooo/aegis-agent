# Behavioural reference (adapted and simplified):
#   * Claude Code Auto Memory / ``src/services/extractMemories/extractMemories.ts``
#     — after the main agent's final (tool-free) reply, a background extractor
#     reviews only messages *after a cursor*, proposes memory writes confined to
#     the memory directory, and skips the round if the main agent already wrote
#     memory itself.  See ``Claude-Code/docs/08-memory.md``.
#   * ``src/services/extractMemories/prompts.ts`` — the extractor's instructions:
#     what is worth storing, what is not, create-vs-update against the existing
#     manifest.
#   * ``src/query/stopHooks.ts`` — the fire-after-final-reply trigger.
#
# Aegis differences (documented in the development log):
#   * No forked sub-agent / prompt-cache sharing: extraction is a single
#     structured side query returning MemoryActions, applied by the store.  This
#     avoids granting the extractor a general Write tool (§8 "权限安全").
#   * Personal scope only: ``project``-scoped facts are intentionally dropped
#     this milestone even though the enum exists.
"""Background memory extraction: conversation slice → MemoryActions.

The extractor is best-effort and runs *after* the main agent's final reply.  It
reviews only the messages added since the last cursor, asks a side-query model
whether anything is worth keeping long-term, and returns a list of
:class:`MemoryAction` (``create`` / ``update`` / ``noop``).  Applying the
actions (writing files, refreshing ``MEMORY.md``) is done by
:func:`apply_actions`, which routes every write through the path-safe store — the
model never names an absolute path or touches anything outside the memory dir.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field

from aegis_agent.memory.scan import format_manifest, scan_memory_files
from aegis_agent.memory.sidequery import run_side_query
from aegis_agent.memory.store import (
    is_valid_memory_filename,
    rebuild_index,
    render_memory_file,
    write_memory_file,
)
from aegis_agent.memory.types import MemoryType
from aegis_agent.models.base import Message, ModelProvider, Role

logger = logging.getLogger(__name__)

#: How many trailing conversation messages to include as extraction context
#: when the cursor is unknown/invalid (safe fallback, never "review nothing").
DEFAULT_REVIEW_FALLBACK = 12

_EXTRACT_SYSTEM = (
    "You maintain a user's PERSONAL long-term memory across sessions. You are "
    "given the recent conversation and a manifest of existing memories. Decide "
    "whether anything in the NEW conversation is worth storing long-term.\n\n"
    "STORE only durable, personal facts: who the user is (`user`), preferences "
    "and corrections/approaches the user confirmed (`feedback`), or reusable "
    "external pointers (`reference`). For `feedback`, include a **Why:** and a "
    "**How to apply:** line.\n\n"
    "DO NOT store: temporary task details, one-off search/web results, ordinary "
    "debugging, current code-implementation details, git history, transient "
    "paths, one-time errors, your own speculation, fast-changing facts, or "
    "anything already covered by an existing memory. Information that is only "
    "true for one specific project, repo, or task is NOT personal memory — skip "
    "it this milestone.\n\n"
    "Prefer UPDATING an existing memory (by its exact filename from the "
    "manifest) over creating a near-duplicate; especially when the user "
    "corrected a previously stored rule. If nothing qualifies, return an empty "
    "actions list.\n\n"
    "Return STRICT JSON only:\n"
    '{"actions": [{"action": "create|update|noop", "filename": "kebab-name.md", '
    '"type": "user|feedback|reference", "name": "Short title", "description": '
    '"one-line summary", "content": "the memory body"}]}\n'
    "Filenames must be bare kebab-case *.md names (no paths, no MEMORY.md). "
    "Return only the JSON object."
)

_ALLOWED_TYPES = {MemoryType.USER, MemoryType.FEEDBACK, MemoryType.REFERENCE}


@dataclass(frozen=True)
class MemoryAction:
    """One proposed memory write from the extractor."""

    action: str  # "create" | "update" | "noop"
    filename: str = ""
    memory_type: MemoryType | None = None
    name: str = ""
    description: str = ""
    content: str = ""


@dataclass
class ExtractionResult:
    """Outcome of one extraction pass (for telemetry + tests)."""

    actions: list[MemoryAction] = field(default_factory=list)
    applied: list[str] = field(default_factory=list)  # filenames created/updated
    reviewed_messages: int = 0
    failure_reason: str | None = None


def messages_since_cursor(
    messages: Sequence[Message],
    cursor: str | None,
) -> list[Message]:
    """Return the messages added after ``cursor`` (a ``client_msg_id``).

    When ``cursor`` is ``None`` or not found (compressed away / lost), falls
    back to the last :data:`DEFAULT_REVIEW_FALLBACK` messages — so a missing
    cursor degrades to "review recent context" rather than "review nothing" or
    "review everything".
    """
    if cursor is not None:
        for i, m in enumerate(messages):
            if m.client_msg_id == cursor:
                return list(messages[i + 1 :])
    # Unknown cursor → safe fallback window.
    return list(messages[-DEFAULT_REVIEW_FALLBACK:])


def _render_conversation(messages: Sequence[Message]) -> str:
    """Render messages into a compact transcript for the extractor prompt."""
    lines: list[str] = []
    for m in messages:
        if m.role is Role.SYSTEM:
            continue
        if m.role is Role.TOOL:
            preview = (m.content or "").strip().replace("\n", " ")
            if len(preview) > 300:
                preview = preview[:300] + "…"
            lines.append(f"[tool:{m.name}] {preview}")
            continue
        role = m.role.value
        text = (m.content or "").strip()
        if text:
            lines.append(f"{role}: {text}")
    return "\n".join(lines)


def _coerce_action(raw: object) -> MemoryAction | None:
    """Validate one raw action dict into a :class:`MemoryAction`, or ``None``."""
    if not isinstance(raw, dict):
        return None
    action = str(raw.get("action", "")).strip().lower()
    if action not in ("create", "update", "noop"):
        return None
    if action == "noop":
        return MemoryAction(action="noop")

    filename = str(raw.get("filename", "")).strip()
    if not is_valid_memory_filename(filename):
        return None
    memory_type = MemoryType.parse(raw.get("type"))
    if memory_type not in _ALLOWED_TYPES:
        # Personal scope: reject project (and unknown) types this milestone.
        return None
    name = str(raw.get("name", "")).strip()
    description = str(raw.get("description", "")).strip()
    content = str(raw.get("content", "")).strip()
    if not content:
        return None
    return MemoryAction(
        action=action,
        filename=filename,
        memory_type=memory_type,
        name=name or filename[:-3],
        description=description,
        content=content,
    )


def extract_memories(
    provider: ModelProvider,
    messages: Sequence[Message],
    cursor: str | None,
    home: str | None = None,
) -> ExtractionResult:
    """Propose memory actions from the messages after ``cursor``; never raises.

    Runs the side query and validates every returned action; does NOT write
    anything (that is :func:`apply_actions`).  Returns an
    :class:`ExtractionResult` with the (possibly empty) validated action list.
    """
    review = messages_since_cursor(messages, cursor)
    substantive = [m for m in review if m.role in (Role.USER, Role.ASSISTANT, Role.TOOL)]
    if not substantive:
        return ExtractionResult(reviewed_messages=0)

    transcript = _render_conversation(review)
    if not transcript.strip():
        return ExtractionResult(reviewed_messages=len(substantive))

    manifest = format_manifest(scan_memory_files(home)) or "(no existing memories)"
    user_prompt = (
        f"Recent conversation:\n{transcript}\n\n"
        f"Existing memories:\n{manifest}"
    )
    parsed = run_side_query(provider, _EXTRACT_SYSTEM, user_prompt)
    if parsed is None:
        return ExtractionResult(
            reviewed_messages=len(substantive), failure_reason="side_query_failed"
        )

    raw_actions = parsed.get("actions")
    actions: list[MemoryAction] = []
    if isinstance(raw_actions, list):
        for raw in raw_actions:
            act = _coerce_action(raw)
            if act is not None and act.action != "noop":
                actions.append(act)
    return ExtractionResult(actions=actions, reviewed_messages=len(substantive))


def apply_actions(
    actions: Sequence[MemoryAction],
    home: str | None = None,
    *,
    memory_directory: str | None = None,
) -> list[str]:
    """Apply create/update actions to disk and rebuild the index; return files.

    Every write goes through :func:`write_memory_file`, which refuses any path
    outside the memory directory.  A single failing action is logged and
    skipped.  The ``MEMORY.md`` index is rebuilt once at the end (idempotent).
    """
    applied: list[str] = []
    for act in actions:
        if act.action not in ("create", "update"):
            continue
        try:
            content = render_memory_file(
                name=act.name,
                description=act.description,
                memory_type=act.memory_type,
                body=act.content,
            )
            write_memory_file(home, act.filename, content, memory_directory=memory_directory)
            applied.append(act.filename)
        except (ValueError, OSError):
            logger.warning("failed to apply memory action for %s", act.filename, exc_info=True)
            continue
    if applied:
        rebuild_index(home, memory_directory=memory_directory)
    return applied


__all__ = [
    "DEFAULT_REVIEW_FALLBACK",
    "ExtractionResult",
    "MemoryAction",
    "apply_actions",
    "extract_memories",
    "messages_since_cursor",
]
