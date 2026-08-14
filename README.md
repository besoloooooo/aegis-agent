# Aegis Agent

A lightweight, recoverable, and extensible **Agent Runtime**, built by extracting, simplifying, and evolving the core runtime behavior of [Hermes](https://github.com/NousResearch/hermes-agent).

> Built for reliable long-running agents with session recovery, context compression, memory, tool use, and extensible runtime components.

## ✨ Features

* **Agent Runtime** — streaming responses, tool calling, multi-turn execution, timeout and interruption handling
* **Reliable Sessions** — SQLite persistence, idempotent writes, snapshots, and crash recovery
* **Session Lease** — SQLite / Redis cross-process concurrency control
* **Context Compression** — oversized tool offload, local compacting, and LLM summaries
* **Long-term Memory** — automatic memory extraction and relevance-based recall
* **History Search** — local FTS5 search with BM25 ranking and CJK trigram matching
* **Tools** — terminal, file editing, search, web search, web extraction, and background processes
* **Skills** — `SKILL.md` discovery, loading, routing, and dynamic prompt injection
* **MCP** — stdio and Streamable HTTP MCP clients
* **OpenAI-compatible Models** — configurable through environment variables

---

## 🏗 Architecture

```text
User
 │
 ▼
CLI / REPL
 │
 ▼
Agent Runtime
 ├── System Prompt
 ├── Context Builder
 ├── Context Compression
 ├── Memory
 ├── Session Store
 ├── Model Provider
 │
 └── Tool Runtime
      ├── Builtin Tools
      ├── Skills
      └── MCP
```

The runtime follows a simple execution loop:

```text
guard
  ↓
build context
  ↓
compress context
  ↓
call model
  ↓
execute tools
  ↓
continue loop
```

Original conversation messages are preserved. Context compression only modifies the derived view sent to the model.

---

## 🚀 Quick Start

Aegis uses [uv](https://docs.astral.sh/uv/) for dependency management.

```bash
uv sync
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
