"""Personal long-term memory.

A file-system + prompt-driven cross-session memory, modelled on Claude Code's
Auto Memory (see ``Claude-Code/docs/08-memory.md``), personal scope only.

Stage 1 (storage + injection + behaviour):

1. **Storage format** — Markdown + YAML frontmatter, four memory kinds
   (``user`` / ``feedback`` / ``project`` / ``reference``); see
   :mod:`aegis_agent.memory.types` and :mod:`aegis_agent.memory.store`.
2. **``MEMORY.md`` index injection** — the index (not the bodies) is injected
   into the system prompt under a 200-line / 25 KB cap; ``USER.md`` is injected
   as a separate, distinctly-labelled profile section.
3. **Memory behaviour rules** — a dedicated ``memory`` system-prompt section.

Stage 2/3 (recall + extraction):

4. **Relevance recall** — scan → manifest → side-query LLM (≤5 files) → read
   selected bodies → inject as ``relevant memories``; see
   :mod:`aegis_agent.memory.scan`, :mod:`aegis_agent.memory.retriever`.
5. **Background extraction** — after the final reply, review new messages and
   propose create/update memory actions, applied through the path-safe store;
   see :mod:`aegis_agent.memory.extractor`, :mod:`aegis_agent.memory.store`.
6. **Orchestration** — :class:`aegis_agent.memory.manager.MemoryManager` wires
   recall (pre-turn) and extraction (post-turn) around a runtime turn.

Deliberately **out of scope**: embeddings / vector DB, project & team memory,
autoDream, and a full memory-eval platform.
"""

from __future__ import annotations

from aegis_agent.memory.extractor import (
    ExtractionResult,
    MemoryAction,
    apply_actions,
    extract_memories,
    messages_since_cursor,
)
from aegis_agent.memory.manager import MemoryEvent, MemoryManager
from aegis_agent.memory.paths import (
    AEGIS_HOME_ENV_VAR,
    AEGIS_MEMORY_DIR_ENV_VAR,
    aegis_home,
    memory_dir,
    memory_index_path,
    user_profile_path,
)
from aegis_agent.memory.prompt import (
    MEMORY_BEHAVIOR_GUIDANCE,
    MemoryBehaviorContributor,
    MemoryIndexContributor,
    RelevantMemoriesContributor,
    UserProfileContributor,
    default_memory_index_contributor,
    default_user_profile_contributor,
)
from aegis_agent.memory.retriever import (
    RecalledMemory,
    RecallResult,
    recall_memories,
    render_recall_block,
)
from aegis_agent.memory.scan import (
    MAX_SCAN_FILES,
    MemoryCandidate,
    format_manifest,
    scan_memory_files,
)
from aegis_agent.memory.store import (
    MAX_ENTRYPOINT_BYTES,
    MAX_ENTRYPOINT_LINES,
    MemoryEntry,
    is_valid_memory_filename,
    load_memory_index,
    load_user_profile,
    parse_memory_file,
    rebuild_index,
    render_memory_file,
    truncate_entrypoint_content,
    write_memory_file,
)
from aegis_agent.memory.types import MemoryType

__all__ = [
    "AEGIS_HOME_ENV_VAR",
    "AEGIS_MEMORY_DIR_ENV_VAR",
    "MAX_ENTRYPOINT_BYTES",
    "MAX_ENTRYPOINT_LINES",
    "MAX_SCAN_FILES",
    "MEMORY_BEHAVIOR_GUIDANCE",
    "ExtractionResult",
    "MemoryAction",
    "MemoryBehaviorContributor",
    "MemoryCandidate",
    "MemoryEntry",
    "MemoryEvent",
    "MemoryIndexContributor",
    "MemoryManager",
    "MemoryType",
    "RecallResult",
    "RecalledMemory",
    "RelevantMemoriesContributor",
    "UserProfileContributor",
    "aegis_home",
    "apply_actions",
    "default_memory_index_contributor",
    "default_user_profile_contributor",
    "extract_memories",
    "format_manifest",
    "is_valid_memory_filename",
    "load_memory_index",
    "load_user_profile",
    "memory_dir",
    "memory_index_path",
    "messages_since_cursor",
    "parse_memory_file",
    "rebuild_index",
    "recall_memories",
    "render_memory_file",
    "render_recall_block",
    "scan_memory_files",
    "truncate_entrypoint_content",
    "user_profile_path",
    "write_memory_file",
]
