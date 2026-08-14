# Aegis Agent

A lightweight, recoverable, and extensible **Agent Runtime**, built by extracting, simplifying, and evolving the core runtime behavior of [Hermes](https://github.com/NousResearch/hermes-agent).

> Built for reliable long-running agents with session recovery, context compression, memory, tool use, and extensible runtime components.

## 🏁 Milestones delivered

Sixteen milestones, from a minimal skeleton to the full runtime:

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

---

## 🏗 Project layout

```text
src/aegis_agent/
├── cli.py          # Typer CLI / REPL entry point
├── tui.py          # terminal UI (prompt_toolkit + rich)
├── runtime.py      # AgentRuntime — the agent loop
├── events.py       # model event stream
├── models/         # ModelProvider protocol, fake / OpenAI providers, Message / ToolCall
├── tools/          # tool registry, executor, builtin tools
├── context/        # context builder + compression
├── sessions/       # session repository (in-memory / SQLite) + leases
├── memory/         # Auto Memory (long-term memory)
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

Messages are persisted to:

```text
~/.aegis/state.db
```

Aegis uses:

* SQLite WAL persistence
* idempotent message writes
* periodic snapshots
* SQLite / Redis session leases
* snapshot + tail replay for recovery

Resume a previous session:

```bash
uv run aegis --resume my-session
```

Run without persistence:

```bash
uv run aegis --ephemeral
```

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
├── state.db
├── USER.md
└── memory/
    ├── MEMORY.md
    └── *.md
```

Long-term memory supports:

* memory index injection
* relevance-based recall
* post-turn memory extraction
* personal memory scope

Enable memory features with:

```bash
uv run aegis --memory-recall
uv run aegis --memory-extract
```

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

skills_list
skill_view
skill_manage
```

Additional tools can be exposed through MCP.

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

with schema normalization and runtime tool wrappers.

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

uv run aegis --memory-recall
uv run aegis --memory-extract

uv run aegis --version
```

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
* `source-map.md` — mapping between Aegis modules and their Hermes origins

---

## 📄 Provenance

Aegis extracts and adapts parts of the Hermes Agent runtime.

Adapted source files retain attribution where required. See:

```text
THIRD_PARTY_NOTICES.md
docs/source-map.md
```

for detailed provenance and licensing information.

Hermes © 2025 Nous Research, licensed under MIT.
