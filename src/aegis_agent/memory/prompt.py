# Behavioural reference (adapted and simplified):
#   * Claude Code Auto Memory / ``src/memdir/memdir.ts:loadMemoryPrompt`` and
#     ``src/memdir/memoryTypes.ts`` — the ``memory`` system-prompt section that
#     tells the model what memory is, what to store under each kind, what NOT to
#     store, and how to treat memory as (possibly stale) history.  See
#     ``Claude-Code/docs/08-memory.md``.
#   * ``src/utils/claudemd.ts`` (:979) — the ``MEMORY.md`` index injected as an
#     ``AutoMem`` context entry, and the user-profile file injected alongside.
#
# Aegis reproduces the *behaviour rules* and the *index/profile injection* only.
# Recall (findRelevantMemories / Top-K), background extraction and auto-writing
# are explicitly out of scope for this milestone.
"""System-prompt contributors for personal long-term memory.

Three :class:`~aegis_agent.context.system_prompt.PromptContributor` sections,
each re-rendered on every prompt build so edits to the files show up next turn:

* :class:`MemoryBehaviorContributor` — the static behaviour rules (what memory
  is, what to store / not store, treat as stale history).  Renders whenever
  memory is enabled, independent of whether any files exist yet.
* :class:`UserProfileContributor` — the ``USER.md`` stable profile, wrapped so
  its semantics are unmistakably distinct from the memory index.
* :class:`MemoryIndexContributor` — the ``MEMORY.md`` auto-memory *index*
  (directory page), wrapped and explicitly labelled as an index, not bodies.

Missing files simply render nothing; they never block startup.
"""

from __future__ import annotations

from pathlib import Path

from aegis_agent.memory.paths import memory_index_path, user_profile_path
from aegis_agent.memory.store import load_memory_index, load_user_profile

# ── Behaviour rules (adapted from Claude Code's ``memory`` section) ──────────

MEMORY_BEHAVIOR_GUIDANCE = (
    "# Memory\n"
    "You have a personal, cross-session long-term memory on disk. Use it to "
    "retain information that is genuinely worth keeping across conversations — "
    "who the user is, durable preferences, corrections they've given you, and "
    "background on ongoing work.\n"
    "\n"
    "How memory is organised:\n"
    "- `USER.md` is a stable user profile (role, expertise, long-term "
    "preferences). It is NOT an auto-memory file.\n"
    "- The memory index (`MEMORY.md`) is an INDEX, not the memories themselves: "
    "one line per memory, `- [Title](file.md) — one-line summary`. The bodies "
    "live in separate files next to it.\n"
    "- When an index entry is clearly relevant to the current task, read that "
    "specific memory file on demand with your normal file tools (Read/Grep). "
    "Do not assume a memory's contents from its one-line summary.\n"
    "\n"
    "Memory kinds: `user` (who the user is), `feedback` (a correction or "
    "approach the user confirmed — record the rule plus **Why:** and **How to "
    "apply:**), `project` (background on in-progress work), `reference` "
    "(pointers to external systems).\n"
    "\n"
    "What NOT to store: temporary task details; anything easily re-derived from "
    "the code or repository; ordinary git history; one-off debugging steps and "
    "transient errors; and anything already captured in a stable config file. "
    "If asked to remember something like that, ask what was non-obvious about "
    "it and store that instead.\n"
    "\n"
    "Memory is history, not current truth. It reflects what was true when it "
    "was written and may now be stale. If a memory names a specific file, "
    "function, path, or project state, verify that it still holds before you "
    "rely on it. An explicit current instruction from the user always "
    "overrides memory; if the user asks you to ignore memory, treat the index "
    "as empty for that turn and do not act on it."
)

MEMORY_BEHAVIOR_GUIDANCE_PROJECT = (
    "# Memory (project scope)\n"
    "You are working in PROJECT memory scope: the memory index below is the "
    "current project's long-term memory, kept separate from your personal "
    "memory and from every other project's memory. `USER.md` (your stable user "
    "profile) is still loaded and shared across all scopes.\n"
    "\n"
    "How memory is organised:\n"
    "- `USER.md` is a stable, GLOBAL user profile (role, expertise, long-term "
    "preferences). It is NOT an auto-memory file and is the same in every scope.\n"
    "- The project memory index (`MEMORY.md`) is an INDEX, not the memories "
    "themselves: one line per memory, `- [Title](file.md) — one-line summary`. "
    "The bodies live in separate files next to it.\n"
    "- When an index entry is clearly relevant to the current task, read that "
    "specific memory file on demand with your normal file tools (Read/Grep). "
    "Do not assume a memory's contents from its one-line summary.\n"
    "\n"
    "What belongs in PROJECT memory: the project's long-term goals, architecture "
    "decisions, technical constraints, project rules the user confirmed, and "
    "long-lived gotchas or pointers. For rules and project facts, record the "
    "rule/fact plus **Why:** and **How to apply:**.\n"
    "\n"
    "What NOT to store here: temporary debugging, one-off errors, ordinary git "
    "history, anything easily re-derived from the code or repository, and plain "
    "user profile facts (those belong in `USER.md`, not project memory).\n"
    "\n"
    "Memory is history, not current truth. It reflects what was true when it "
    "was written and may now be stale. If a memory names a specific file, "
    "function, path, or project state, verify that it still holds before you "
    "rely on it. An explicit current instruction from the user always "
    "overrides memory; if the user asks you to ignore memory, treat the index "
    "as empty for that turn and do not act on it."
)

_USER_PROFILE_HEADER = (
    "## User profile (USER.md)\n"
    "The following is the stable, long-term user profile. Treat it as durable "
    "context about who the user is and their standing preferences — distinct "
    "from the auto-memory index below."
)

_MEMORY_INDEX_HEADER = (
    "## Memory index (MEMORY.md)\n"
    "The following is the INDEX of your long-term memories — a directory page, "
    "not the memory bodies. Each line points to a separate memory file you can "
    "read on demand when it is relevant."
)


class MemoryBehaviorContributor:
    """Render the static memory behaviour rules when memory is enabled.

    ``project=True`` renders the project-scope variant (project memory is the
    active index; ``USER.md`` is still the shared global profile).  When a
    ``project_root`` is given it is appended as an explicit line so the model
    knows which directory "the project" refers to.  ``memory_dir_path`` (the
    directory actually scanned/written by the pipeline) is likewise surfaced —
    without it the model cannot guess ``~/.aegis/projects/<id>/memory`` and
    ends up looking for memory bodies inside the project root.  ``enabled``
    turns the whole section off (``--no-memory``).
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        project: bool = False,
        project_root: str | Path | None = None,
        memory_dir_path: str | Path | None = None,
    ) -> None:
        self._enabled = enabled
        self._project = project
        self._project_root = Path(project_root) if project_root is not None else None
        self._memory_dir = Path(memory_dir_path) if memory_dir_path is not None else None

    def render(self) -> str | None:
        if not self._enabled:
            return None
        if self._project:
            text = MEMORY_BEHAVIOR_GUIDANCE_PROJECT
            if self._project_root is not None or self._memory_dir is not None:
                lines = ["\n\nProject scope facts:"]
                if self._project_root is not None:
                    lines.append(
                        f"- Project root: {self._project_root} — file tools "
                        "resolve relative paths against this directory by "
                        "default; treat it as the project the memory scope "
                        "belongs to."
                    )
                if self._memory_dir is not None:
                    lines.append(
                        f"- Project memory directory: {self._memory_dir} — the "
                        "MEMORY.md index and the memory bodies (*.md) live "
                        "HERE, outside the project root. Relative links in the "
                        "memory index resolve against this directory, not the "
                        "project root; read, create, or edit memory files only "
                        "at this location."
                    )
                text += "\n".join(lines)
            return text
        text = MEMORY_BEHAVIOR_GUIDANCE
        if self._memory_dir is not None:
            text += (
                f"\n\nYour personal memory directory is: {self._memory_dir} — "
                "the MEMORY.md index and the memory bodies (*.md) live in that "
                "directory; read or update memory files there."
            )
        return text


class UserProfileContributor:
    """Render the ``USER.md`` profile, wrapped with a distinct header.

    Reads the file on every build so an edited profile is reflected next turn.
    Returns ``None`` when the file is absent or empty.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def render(self) -> str | None:
        content = load_user_profile(self._path)
        if not content:
            return None
        return f"{_USER_PROFILE_HEADER}\n\n{content.strip()}"


class MemoryIndexContributor:
    """Render the ``MEMORY.md`` index, wrapped and labelled as an index.

    Reads the file on every build (truncated under the line/byte cap by the
    store).  Returns ``None`` when the index is absent or empty.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def render(self) -> str | None:
        content = load_memory_index(self._path)
        if not content:
            return None
        return f"{_MEMORY_INDEX_HEADER}\n\n{content.strip()}"


class RelevantMemoriesContributor:
    """Inject per-turn recalled memories into the system prompt.

    Unlike the other contributors this one is *stateful*: the manager runs
    recall before a turn and calls :meth:`set_block` with the rendered
    ``Relevant memories`` text (or ``None`` to clear).  Because the system
    prompt is rebuilt every model call, whatever is set here is what the current
    turn sees; clearing it before the next turn keeps recall strictly per-turn
    and never mutates the source history.
    """

    def __init__(self) -> None:
        self._block: str | None = None

    def set_block(self, block: str | None) -> None:
        self._block = block if (block and block.strip()) else None

    def clear(self) -> None:
        self._block = None

    def render(self) -> str | None:
        return self._block


def default_user_profile_contributor(
    home: str | Path | None = None,
) -> UserProfileContributor:
    """Build a :class:`UserProfileContributor` for the default ``USER.md`` path."""
    return UserProfileContributor(user_profile_path(home))


def default_memory_index_contributor(
    home: str | Path | None = None,
) -> MemoryIndexContributor:
    """Build a :class:`MemoryIndexContributor` for the default ``MEMORY.md`` path."""
    return MemoryIndexContributor(memory_index_path(home))


__all__ = [
    "MEMORY_BEHAVIOR_GUIDANCE",
    "MEMORY_BEHAVIOR_GUIDANCE_PROJECT",
    "MemoryBehaviorContributor",
    "MemoryIndexContributor",
    "RelevantMemoriesContributor",
    "UserProfileContributor",
    "default_memory_index_contributor",
    "default_user_profile_contributor",
]
