# Aegis Agent

A lightweight, recoverable, and extensible **Agent Runtime**, built by extracting, simplifying, and evolving useful runtime behavior from [Hermes](https://github.com/NousResearch/hermes-agent) and Claude Code reference implementations.

> Built for reliable long-running agents with session recovery, context compression, memory, tool use, subagents, teams, inter-agent messaging, and extensible runtime components.

## 🏁 Milestones delivered

Nineteen milestones, from a minimal skeleton to the full runtime:

**Core runtime**
1. Minimal Agent Runtime — fake provider, in-memory sessions, Agent Loop
2. OpenAI-compatible provider & streaming tool calls
3. Live terminal UI

**Tools**
4. Skills subsystem — `SKILL.md` discovery / loading / routing
5. Lightweight MCP client — stdio + Streamable HTTP
6. File-editing tools — write_file / patch / search_files
7. Terminal & background-process tools
8. Web tools — web_search / web_extract with SSRF gate
9. Skill management — `skill_manage`

**Reliability & sessions**
10. Context compression pipeline — offload → micro-compact → LLM summary
11. Compression wired into the agent loop + reasoning_content
12. SQLite persistence + snapshot fast-resume + cross-process leases

**Prompt / memory / search**
13. Dynamic system-prompt sections
14. Personal long-term memory (Auto Memory) — `USER.md` + `MEMORY.md` index
15. Memory recall + background extraction
16. Session history search — FTS5 `session_search`
17. Project-scoped long-term memory — `--project [PATH]`

**Interactive UX**
18. Slash-command suite — `/save` `/new` `/history` `/undo` `/retry` `/title` …
    plus a full-screen TTY layout with scrollable chat history, fixed bottom
    composer, Markdown replies, and highlighted input tokens.

**Multi-agent orchestration**
19. Multi-agent orchestration — `Agent`, `team_create`, `send_message`, `/agents`

**Agent quality foundation**
- Quality Stage 0 — optional Langfuse end-to-end traces for Agent runs, model
  calls, tool calls, subagents, and final results

---

## 🏗 Project layout

```text
src/aegis_agent/
├── cli.py          # Typer CLI / REPL entry point
├── tui.py          # terminal UI (prompt_toolkit + rich)
├── slash_commands.py  # interactive /command registry + dispatcher
├── runtime.py      # AgentRuntime — the agent loop
├── events.py       # model event stream
├── agents/         # subagents, teams, inter-agent messaging
├── observability/  # fail-open tracing API, sanitization, Langfuse adapter
├── models/         # ModelProvider protocol, fake / OpenAI providers, Message / ToolCall
├── tools/          # tool registry, executor, builtin tools
├── context/        # context builder + compression
├── sessions/       # session repository (in-memory / SQLite) + leases
├── memory/         # Auto Memory (long-term memory, personal + project scopes)
├── skills/         # SKILL.md loading / routing
└── mcp/            # MCP client
```

Original conversation messages are preserved. Context compression only modifies the derived view sent to the model.

---

## 🚀 Quick Start

Prerequisites: [uv](https://docs.astral.sh/uv/) — it installs the Python 3.11 toolchain automatically.

```bash
# 1. Install uv (once)
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. Sync dependencies — uv reads .python-version and prepares Python 3.11 + all deps
uv sync

# 3. Run
uv run aegis
```

Configure an OpenAI-compatible model:

```bash
export AEGIS_API_KEY=...
export AEGIS_BASE_URL=http://localhost:1234/v1
export AEGIS_MODEL=gpt-4o-mini

uv run aegis
```

Without model configuration, Aegis can run with its deterministic fake provider.

---

## 💾 Session Recovery

Messages are persisted to a SQLite store. The store is **scoped**: the personal
scope uses `~/.aegis/state.db`, while a project scope stores its sessions beside
its memory in `~/.aegis/projects/<project-id>/state.db` (mirroring Claude Code,
so a session always belongs to the scope that created it).

```text
~/.aegis/state.db                          # personal scope
~/.aegis/projects/<project-id>/state.db    # project scope
```

Aegis uses:

* SQLite WAL persistence
* idempotent message writes
* periodic snapshots
* SQLite / Redis session leases
* snapshot + tail replay for recovery

Resume a previous session — pass the same `--project` it was started with, since
a project session lives in its project's store and is not visible to personal scope:

```bash
uv run aegis --resume my-session
uv run aegis --project /path/to/repo --resume my-session
```

Run without persistence:

```bash
uv run aegis --ephemeral
```

---

## ⌨️ Slash Commands

The interactive REPL understands `/commands` (type `/help` inside the REPL):

```text
/new [name]     start a new session (alias: /reset)
/clear          clear screen + new session
/history        show the conversation (tool messages collapsed)
/save           dump debug snapshots (local / wire / system prompt)
                to ./aegis-chat-logs/  (alias: /chatlog)
/retry          resend the last message
/undo [N]       back up N user turns (rows are soft-deleted, kept on
                disk for audit) and prefill the composer for editing
/title [name]   set or show the session title
/sessions       list recorded sessions
/agents         list subagent tasks and their status
/exit           quit (alias: /quit)
```

A `/token` matching no command falls through to skill routing, then to the
model unchanged. In an interactive TTY, the composer highlights known slash
commands, quoted strings, path-like tokens, mentions, and tags, and shows a
small command hint while typing.

---

## 🖥 Interactive TTY rendering

The live terminal UI uses Rich and prompt_toolkit. In an interactive TTY, the
current assistant segment is re-rendered as Rich Markdown while tokens stream,
then committed to history at a tool boundary or turn end. Headings, lists,
emphasis, inline code, fenced code blocks, and tables therefore keep their
terminal styling during generation as well as afterward. Tool calls still
appear as compact status lines between response segments.

Interactive TTY sessions use a prompt_toolkit full-screen layout: the chat
history lives in a scrollable output pane and the composer stays fixed at the
bottom. Mouse wheel events are routed to the history pane, and PageUp/PageDown
scroll it from the keyboard, while the input row remains visible. New output
follows the tail only while the user is already
at the bottom, so streaming does not pull a manually scrolled history view away.
The composer keeps prompt_toolkit history, cursor editing, and token highlighting
in one clean row. User messages, assistant Markdown, tool status, and errors all
share the history pane. Non-TTY input/output keeps the simpler plain streaming
path so pipes, logs, and tests remain stable.

---

## Multi-agent orchestration

Aegis includes built-in orchestration tools for splitting work without adding a
second agent loop. Subagents and teammates reuse `AgentRuntime` with their own
session repositories, tool sets, and transcripts.

### `Agent`

The `Agent` tool starts a one-shot subagent for a self-contained task:

```text
Agent
- prompt: task instructions for the subagent
- subagent_type: explore | general-purpose  (optional)
- run_in_background: true | false
```

Typed subagents are **fresh** by default: they receive the requested task and
their own private transcript, not the main session history. The built-in
`explore` agent is read-only and is intended for broad code/documentation
inspection. `general-purpose` has the normal built-in tool set but does not get
`Agent` by default, avoiding recursive fan-out.

If `subagent_type` is omitted, Aegis creates a fork subagent seeded from the
main session history. This is useful for review, counterexamples, or a second
opinion over the current context. In all modes, subagent intermediate turns stay
private; the main session receives only the final result or a background
completion notification.

With `run_in_background: true`, `Agent` returns a task id immediately. The
background task runs on a daemon thread and Aegis injects its completion notice
between REPL turns, so the model does not need to poll. The startup panel shows
how many subagents are currently running (`Subagents: N running`); the REPL
refreshes the number between turns, so it drops back to 0 once background tasks
finish.

### `team_create` and `send_message`

`team_create` creates an in-process team with named, long-lived teammates. Each
teammate has a stable name, its own session repository, and continuous context
for the current Aegis process. After handling a message it becomes idle, then
wakes when a new team message arrives.

```text
team_create
- description: what the team is for
- members:
  - name: researcher
    agent_type: explore
    task: initial task
```

`send_message` routes messages inside the active team:

```text
send_message
- recipient: teammate-name | team-lead | *
- message: text to deliver
```

The team lead can message a teammate, teammates can message each other or the
lead, and `*` broadcasts to the other teammates. Delivery is team-scoped: a
teammate cannot send across team boundaries.

Use `/agents` inside the REPL to inspect subagent task ids, types, status,
background flag, and descriptions. It is task introspection, not a full team
roster or team administration UI.

Current limits: teams and teammate mailboxes are in-process only; team state and
teammate transcripts are not durable across process restarts; `/agents` does not
yet list complete team membership; nested subagent creation is intentionally
limited to prevent runaway recursion.

---

## 🔭 Langfuse Observability (Quality Stage 0)

Langfuse tracing is an optional, fail-open side channel. Install the extra and
provide both credentials to enable it:

```bash
uv sync --extra observability

export LANGFUSE_PUBLIC_KEY=pk-lf-...
export LANGFUSE_SECRET_KEY=sk-lf-...
export LANGFUSE_BASE_URL=https://cloud.langfuse.com  # optional

uv run aegis
```

Each `AgentRuntime.run_turn` creates one top-level trace. Model calls, centralized
tool execution, subagent runs, and the final result appear as nested observations:

```text
Aegis Run
├── Model Call
├── Tool Call: list_directory
├── Tool Call: Agent
│   └── Subagent Run: explore
│       ├── Model Call
│       ├── Tool Call: read_file
│       └── Final Result
├── Model Call
└── Final Result
```

If the SDK is absent, credentials are incomplete, or Langfuse reporting fails,
Aegis automatically uses a no-op backend and preserves the original runtime
result and error handling. Trace payloads are recursively redacted for common
credential fields and secret patterns; long strings are capped at 20,000
characters with their original length retained as metadata.

The current Aegis model-event contract does not expose token usage, cache tokens,
or cost, so those fields are intentionally left unset instead of being estimated.

---

## 📦 Context Compression

Long-running sessions are compressed before model calls when they exceed the configured context budget.

The pipeline contains three stages:

```text
Oversized Tool Result Offload
          ↓
     Local Micro-Compact
          ↓
    Round-level LLM Summary
```

Large tool outputs are moved to:

```text
~/.aegis/tool-result-cache/
```

The original session history is never modified.

Configure the context budget with:

```bash
uv run aegis --context-max-tokens 80000
```

or:

```bash
export AEGIS_CONTEXT_MAX_TOKENS=80000
```

---

## 🧠 Memory

Aegis separates **long-term memory** from **raw session history**.

```text
~/.aegis/
├── state.db                    # personal session store
├── USER.md                     # global user profile (both scopes)
├── memory/                     # personal scope
│   ├── MEMORY.md
│   └── *.md
└── projects/
    └── <project-id>/           # project scope (isolated per project)
        ├── state.db            # project session store
        └── memory/
            ├── MEMORY.md
            └── *.md
```

Long-term memory supports:

* memory index injection
* relevance-based recall — **on by default**
* post-turn memory extraction — **on by default**
* **personal scope** (default) and **project scope** — `USER.md` is global, memory is scoped

Disable either dynamic channel with:

```bash
uv run aegis --no-memory-recall
uv run aegis --no-memory-extract
```

Use project-scoped memory with `--project` (a bare `--project` uses the current directory):

```bash
uv run aegis --project /path/to/repo
```

---

## 🧩 Configuration

Persistent settings live in `~/.aegis/config.yaml` (the same file as `mcp_servers`;
see `config.example.yaml` for the full key reference).  Only the keys you want to
override are needed.  Precedence: **CLI flag > config file > built-in default**.

```yaml
# ~/.aegis/config.yaml
memory:
  recall: true
  extract: true
context:
  max_tokens: 120000
iterations:
  max: 10
session:
  snapshot_every_n: 20
mcp_servers: { ... }
```

Configurable from the file: memory (`enabled` / `recall` / `extract` / `project`),
context (`compress` / `max_tokens`), iterations (`max`), session (`db_path` /
`snapshot_every_n` / `lease`), skills (`enabled` / `dir`), mcp (`enabled`), shell
(`allow_dangerous`), model (`backend`).

---

## 🔍 Session History Search

Aegis can search historical conversations directly from SQLite without calling an LLM.

The search layer uses:

```text
SQLite FTS5
   +
BM25 Ranking
   +
CJK Trigram Matching
```

The `session_search` tool supports:

* searching historical messages
* browsing recent sessions
* reading a complete session
* inspecting messages around a specific point

---

## 🛠 Built-in Tools

Aegis currently includes:

```text
read_file
list_directory
write_file
patch
search_files

terminal
process

web_search
web_extract

session_search

Agent
team_create
send_message

skills_list
skill_view
skill_manage
```

Additional tools can be exposed through MCP.

`terminal` foreground timeouts return `exit_code: 124` and preserve any stdout/stderr captured before the process is killed, so agents can recover with alternate commands instead of losing partial diagnostics.

---

## 🧩 Skills & MCP

### Skills

Aegis supports `SKILL.md` based extensions with:

* discovery
* loading
* routing
* slash commands
* progressive disclosure
* dynamic prompt injection

### MCP

The MCP client supports:

```text
stdio
Streamable HTTP
```

with schema normalization and runtime tool wrappers. Individual MCP tool calls still obey the server's configured `timeout`; a slow upstream operation is returned to the model as an MCP error result rather than crashing the agent loop.

---

## ⚙️ Useful Commands

```bash
uv run aegis

uv run aegis --resume my-session
uv run aegis --db ./custom.db
uv run aegis --ephemeral

uv run aegis --no-lease
uv run aegis --no-compress
uv run aegis --no-memory

# recall/extract are on by default; turn them off with:
uv run aegis --no-memory-recall
uv run aegis --no-memory-extract

uv run aegis --project /path/to/repo
uv run aegis --project

uv run aegis --version
```

Inside the REPL, use `/agents` to inspect subagent tasks.

Optional dependencies:

```bash
uv sync --extra web
uv sync --extra redis
```

---

## 🧪 Development

```bash
uv run pytest -q
uv run ruff check .
```

Default tests do not require a paid model API.

---

## 🗺 Roadmap

Planned improvements include:

* concurrent tool execution
* guardrail circuit breaker
* MCP reconnect and circuit breaker
* remaining history versioning integration

---

## 📚 Documentation

More implementation details are available in:

```text
docs/extraction-plan.md
docs/development-log.md
docs/source-map.md
```

* `extraction-plan.md` — runtime extraction and development plan
* `development-log.md` — implementation notes and engineering decisions
* `source-map.md` — mapping between Aegis modules and their Hermes / Claude Code reference origins

---

## 📄 Provenance

Aegis extracts, adapts, and reimplements selected runtime behavior from Hermes
and Claude Code reference sources where documented. Claude Code is used as a
behavioral and architectural reference for memory and multi-agent/team features
where noted in `docs/source-map.md`.

Adapted source files retain attribution where required. See:

```text
THIRD_PARTY_NOTICES.md
docs/source-map.md
```

for detailed provenance and licensing information.

Hermes © 2025 Nous Research, licensed under MIT.
