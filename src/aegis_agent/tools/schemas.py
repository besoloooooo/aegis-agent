"""Tool schemas (JSON Schema) for Aegis Agent's builtin tools.

These are the *minimal* schemas defined in ``docs/extraction-plan.md`` §4.
They are expressed as :class:`~aegis_agent.models.base.ToolDefinition` objects
(imported from the models layer) so the registry can advertise them to the
model without this module depending on the registry — keeping the dependency
direction one-way (tools → models.base, never the reverse).
"""

from __future__ import annotations

from aegis_agent.models.base import ToolDefinition

READ_FILE = ToolDefinition(
    name="read_file",
    description=(
        "Read a text file with line numbers and pagination. Output format is "
        "'LINE_NUM|CONTENT' (1-indexed). Use offset/limit for large files."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file to read (absolute, relative, or ~/path)."},
            "offset": {
                "type": "integer",
                "description": "Line number to start reading from (1-indexed).",
                "default": 1,
                "minimum": 1,
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of lines to read.",
                "default": 500,
                "maximum": 2000,
            },
        },
        "required": ["path"],
    },
)

LIST_DIRECTORY = ToolDefinition(
    name="list_directory",
    description=(
        "List the entries of a directory. Returns each entry's name, type "
        "(file/dir/other) and size in bytes (size is null for directories)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Directory to list (default: current working directory).", "default": "."},
        },
        "required": [],
    },
)

WRITE_FILE = ToolDefinition(
    name="write_file",
    description=(
        "Write text content to a file, creating it (and any missing parent "
        "directories) or overwriting it entirely. The write is atomic and "
        "preserves an existing file's BOM and line-ending style. Use 'patch' "
        "for targeted edits to a large file."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to write (absolute, relative, or ~/path)."},
            "content": {"type": "string", "description": "The complete file content to write."},
        },
        "required": ["path", "content"],
    },
)

PATCH = ToolDefinition(
    name="patch",
    description=(
        "Replace an exact snippet of text in a file. Provide old_string (the "
        "text to find, with enough surrounding context to be unique) and "
        "new_string (its replacement). Matching tolerates whitespace/indentation "
        "differences. Set replace_all=true to replace every occurrence; an empty "
        "new_string deletes the matched text."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file to edit."},
            "old_string": {"type": "string", "description": "Exact text to find (unique unless replace_all)."},
            "new_string": {"type": "string", "description": "Replacement text (empty string deletes)."},
            "replace_all": {
                "type": "boolean",
                "description": "Replace all occurrences instead of requiring a unique match.",
                "default": False,
            },
        },
        "required": ["path", "old_string", "new_string"],
    },
)

SEARCH_FILES = ToolDefinition(
    name="search_files",
    description=(
        "Search a directory tree. With target='content' (default), search file "
        "CONTENTS by regex; with target='files', find files by name glob "
        "(e.g. '*.py'). Uses ripgrep when available, otherwise a pure-Python "
        "fallback. Hidden and VCS directories are skipped."
    ),
    parameters={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regex (content search) or glob (file search)."},
            "target": {
                "type": "string",
                "enum": ["content", "files"],
                "description": "Search file contents or file names.",
                "default": "content",
            },
            "path": {"type": "string", "description": "Directory to search (default: current directory).", "default": "."},
            "file_glob": {"type": "string", "description": "In content mode, only search files matching this glob (e.g. '*.py')."},
            "limit": {"type": "integer", "description": "Maximum results to return.", "default": 50, "minimum": 1},
            "offset": {"type": "integer", "description": "Skip this many results (pagination).", "default": 0, "minimum": 0},
            "output_mode": {
                "type": "string",
                "enum": ["content", "files_only", "count"],
                "description": "Return matching lines, just file paths, or per-file counts.",
                "default": "content",
            },
            "context": {"type": "integer", "description": "Context lines around each match (content mode).", "default": 0, "minimum": 0},
        },
        "required": ["pattern"],
    },
)

TERMINAL = ToolDefinition(
    name="terminal",
    description=(
        "Run a shell command and capture its combined output and exit code. "
        "By default it runs in the foreground and returns when the command "
        "finishes (a timeout is always enforced). Set background=true to launch "
        "a long-running command (dev server, watcher, training job) and get a "
        "session_id back immediately, then manage it with the 'process' tool."
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The shell command to execute."},
            "timeout": {
                "type": "integer",
                "description": "Max seconds to wait before killing the command (foreground only).",
                "default": 60,
                "minimum": 1,
            },
            "workdir": {
                "type": "string",
                "description": "Working directory for the command (default: session cwd).",
            },
            "background": {
                "type": "boolean",
                "description": "Launch in the background and return a session_id immediately.",
                "default": False,
            },
            "pty": {
                "type": "boolean",
                "description": "Run under a pseudo-terminal (for interactive CLIs; POSIX only).",
                "default": False,
            },
        },
        "required": ["command"],
    },
)

PROCESS = ToolDefinition(
    name="process",
    description=(
        "Manage background processes started with terminal(background=true). "
        "Actions: list (all processes), poll (status + output preview), log "
        "(full buffered output), wait (block until exit/timeout), kill, "
        "write (send to stdin, no newline), submit (stdin + Enter), close "
        "(send EOF on stdin)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "poll", "log", "wait", "kill", "write", "submit", "close"],
                "description": "The process-management action to perform.",
            },
            "session_id": {"type": "string", "description": "Process id (proc_...) — required for all actions except list."},
            "data": {"type": "string", "description": "Data for write/submit."},
            "timeout": {"type": "integer", "description": "Max seconds to block for wait.", "minimum": 1},
            "offset": {"type": "integer", "description": "Line offset for log (default 0 = tail).", "default": 0, "minimum": 0},
            "limit": {"type": "integer", "description": "Max lines for log.", "default": 200, "minimum": 1},
        },
        "required": ["action"],
    },
)

WEB_SEARCH = ToolDefinition(
    name="web_search",
    description=(
        "Search the web and return a ranked list of results (title, url, "
        "description). Uses DuckDuckGo by default (no API key); a Tavily or Exa "
        "backend is used automatically when its API key is set in the environment. "
        "Use web_extract to read the full content of a result."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The search query."},
            "limit": {
                "type": "integer",
                "description": "Maximum number of results to return.",
                "default": 5,
                "minimum": 1,
                "maximum": 100,
            },
        },
        "required": ["query"],
    },
)

WEB_EXTRACT = ToolDefinition(
    name="web_extract",
    description=(
        "Fetch one or more web pages (up to 5) and extract their readable "
        "content as markdown (title + body text). URLs targeting private/"
        "internal addresses are blocked (SSRF protection)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of URLs to fetch and extract (max 5).",
                "maxItems": 5,
            },
        },
        "required": ["urls"],
    },
)

SESSION_SEARCH = ToolDefinition(
    name="session_search",
    description=(
        "Search past sessions stored in the local SQLite session DB, or scroll "
        "inside one. FTS5-backed retrieval over the message store. No LLM calls "
        "— every shape returns actual messages from the DB.\n\n"
        "FOUR CALLING SHAPES\n\n"
        "  1) DISCOVERY — pass `query`:\n"
        "     session_search(query=\"auth refactor\", limit=3)\n"
        "     Runs FTS5, dedupes hits by session, returns the top N sessions. "
        "Each result carries:\n"
        "       - session_id, title, when, source\n"
        "       - snippet: FTS5-highlighted match excerpt\n"
        "       - bookend_start: first 3 user+assistant messages of the session "
        "(the goal / kickoff)\n"
        "       - messages: ±5 messages around the FTS5 match, with the anchor "
        "message flagged (the hit in context)\n"
        "       - bookend_end: last 3 user+assistant messages of the session "
        "(the resolution / decisions)\n"
        "       - match_message_id, messages_before, messages_after\n"
        "     Bookends + window together let you reconstruct goal → match → "
        "resolution without paying for the whole transcript.\n\n"
        "  2) SCROLL — pass `session_id` + `around_message_id`:\n"
        "     session_search(session_id=\"...\", around_message_id=12345, window=10)\n"
        "     Returns a window of ±`window` messages centered on the anchor. No "
        "FTS5, no bookends — just the slice. Use after a discovery call when you "
        "need more context than the ±5 default window.\n"
        "       - To scroll FORWARD: pass messages[-1].id back as around_message_id.\n"
        "       - To scroll BACKWARD: pass messages[0].id back as around_message_id.\n"
        "       - The boundary message appears in both windows — orientation marker.\n"
        "       - When messages_before or messages_after is < window, you're at the "
        "start or end of the session.\n\n"
        "  3) READ — pass `session_id` only (no around_message_id):\n"
        "     session_search(session_id=\"...\")\n"
        "     Dumps the whole session by id (first 20 + last 10 messages when "
        "large).\n\n"
        "  4) BROWSE — no args:\n"
        "     session_search()\n"
        "     Returns recent sessions chronologically: titles, previews, timestamps. "
        "Use when the user asks \"what was I working on\" without naming a topic.\n\n"
        "FTS5 SYNTAX\n\n"
        "  AND is the default — multi-word queries require all terms. Use OR "
        "explicitly for broader recall (`alpha OR beta OR gamma`), quoted phrases "
        "for exact match (`\"docker networking\"`), boolean (`python NOT java`), or "
        "prefix wildcards (`deploy*`).\n\n"
        "WHEN TO USE\n\n"
        "  Reach for this on any \"what did we do about X\" / \"where did we leave Y\" / "
        "\"find the session where Z\" question — before web search or filesystem "
        "inspection. The session DB carries what was said when; external tools show "
        "current world state."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Search query (discovery shape). Keywords, phrases, or boolean "
                    "expressions to find in past sessions. Omit to browse recent "
                    "sessions. Ignored when session_id + around_message_id are set "
                    "(scroll shape)."
                ),
            },
            "limit": {
                "type": "integer",
                "description": (
                    "Discovery shape only. Max sessions to return (default 3, max 10). "
                    "Bump to 5–10 when the topic likely spans several sessions and you "
                    "want to pick the right one to scroll into."
                ),
                "default": 3,
            },
            "sort": {
                "type": "string",
                "enum": ["newest", "oldest"],
                "description": (
                    "Discovery shape only. Temporal bias on top of FTS5 ranking. Omit "
                    "to keep relevance-only ordering (suitable for exploratory recall — "
                    "\"what do we know about X\"). Set 'newest' for recency-shaped "
                    "questions (\"where did we leave X\"). Set 'oldest' for "
                    "origin-shaped questions (\"how did X start\"). Ignored in scroll "
                    "and browse shapes."
                ),
            },
            "session_id": {
                "type": "string",
                "description": (
                    "Scroll or read shape. Session to read inside. Use the session_id "
                    "returned from a prior discovery call. Pair with around_message_id "
                    "to scroll, or omit the anchor to read the whole session."
                ),
            },
            "around_message_id": {
                "type": "integer",
                "description": (
                    "Scroll shape. Message id to center the window on. From a discovery "
                    "result use match_message_id, or any id seen in a prior window. To "
                    "scroll forward pass the last window message's id; to scroll "
                    "backward pass the first."
                ),
            },
            "window": {
                "type": "integer",
                "description": (
                    "Scroll shape only. Messages to return on each side of the anchor "
                    "(anchor itself always included). Clamped to [1, 20]. Default 5."
                ),
                "default": 5,
            },
            "role_filter": {
                "type": "string",
                "description": (
                    "Optional. Comma-separated roles to include. Discovery defaults to "
                    "'user,assistant' (tool output is usually noise). Pass "
                    "'user,assistant,tool' to include tool output (debugging tool "
                    "behaviour) or 'tool' to search tool output only."
                ),
            },
        },
        "required": [],
    },
)

#: All builtin tool definitions, in registration order.
BUILTIN_DEFINITIONS = (
    READ_FILE,
    LIST_DIRECTORY,
    WRITE_FILE,
    PATCH,
    SEARCH_FILES,
    TERMINAL,
    PROCESS,
    WEB_SEARCH,
    WEB_EXTRACT,
    SESSION_SEARCH,
)


__all__ = [
    "BUILTIN_DEFINITIONS",
    "LIST_DIRECTORY",
    "PATCH",
    "PROCESS",
    "READ_FILE",
    "SEARCH_FILES",
    "SESSION_SEARCH",
    "TERMINAL",
    "WEB_EXTRACT",
    "WEB_SEARCH",
    "WRITE_FILE",
]
