# Aegis Agent

A lightweight, recoverable, and extensible **Agent Runtime**, built by
extracting, simplifying, and modularizing the core runtime behaviour of
[Hermes](https://github.com/NousResearch/hermes-agent) (© 2025 Nous Research,
MIT — see `THIRD_PARTY_NOTICES.md`).

## Status: Stage 12 — all milestones delivered ✓

Twelve milestones delivered; remaining items tracked in
`docs/extraction-plan.md` §7.

| Stage | Theme | Highlights |
|---|---|---|---|
| 1 | Minimal Agent Runtime skeleton | Fake provider, in-memory sessions, 3 builtin tools, Agent Loop |
| 2 | OpenAI-compatible provider & streaming | Real provider, tool-call fragment assembly, dangerous-command guardrail, message sanitization |
| 3 | Live terminal UI | prompt_toolkit input, rich output, pyfiglet banner, kaomoji spinner |
| 4 | Skills subsystem | SKILL.md discovery/loading/routing, `skills_list`/`skill_view` tools, slash commands, dynamic prompt injection |
| 5 | Lightweight MCP client | stdio + HTTP transports, schema normalization, MCPToolWrapper, optional `mcp` SDK |
| 6 | File-editing tools | write_file / patch (fuzzy find-replace chain, atomic writes, BOM/CRLF preservation) / search_files (rg + Python fallback) |
| 7 | Terminal & process tools | Foreground terminal (timeout, output truncation, dangerous-command guardrail) + background process registry |
| 8 | Web tools | web_search (ddgs / Tavily / Exa) + web_extract with SSRF gate; optional `ddgs`/`trafilatura` deps |
| 9 | Skill management | skill_manage tool: install / uninstall / update / list with lock-file provenance |
| 10 | Context compression pipeline | Three-phase pipeline: oversized-tool-result offload → local micro-compact → per-round LLM summary + single-round overflow fallback; original history never mutated |
| 11 | Compression loop wiring + reasoning | run_turn compresses derived context before model calls; reasoning_content captured (never echoed to wire); cross-turn ContentReplacementState for prompt-cache stability |
| 12 | SQLite session persistence + leases + resume | Message-level idempotent persistence, snapshot fast-resume (zlib+CRC32), SQLite/Redis cross-process session leases; `--resume` / `--db` / `--ephemeral` |

Still planned: concurrent tool execution, guardrails circuit breaker, MCP reconnect & circuit breaker, history_version bump callers.

## What's here

| Area | Module | Notes |
|---|---|---|
| Data structures | `aegis_agent.models.base` | `Message`, `ToolCall`, `ToolResult`, `ToolDefinition`, `ChatResponse` |
| Streaming events | `aegis_agent.events` | `ModelEvent`, `collect_response` (stream → uniform response, cancel-aware) |
| Stream assembly | `aegis_agent.models.stream` | `StreamAssembler` — tool-call name/argument fragment assembly |
| Provider abstraction | `aegis_agent.models.base.ModelProvider` | `Protocol`; runtime depends only on it |
| Fake provider | `aegis_agent.models.fake.FakeModelProvider` | deterministic, scriptable, streaming |
| OpenAI provider | `aegis_agent.models.openai_compat.OpenAICompatibleProvider` | env-configured, streaming + one-shot; temperature pinning |
| Tool registry | `aegis_agent.tools.registry.ToolRegistry` | explicit injection, no global singleton |
| Tool executor | `aegis_agent.tools.executor.ToolExecutor` | dispatch + `{"error": ...}` fallback |
| Builtin tools | `read_file`, `list_directory`, `terminal`, `process`, `write_file`, `patch`, `search_files`, `web_search`, `web_extract` | full set |
| Dangerous-command guardrail | `aegis_agent.tools.danger` | terminal blocks destructive commands by default; operator-only override |
| Context builder | `aegis_agent.context.builder.ContextBuilder` | derived view; source messages never mutated |
| Dynamic prompt | `aegis_agent.context.system_prompt` | `SystemPromptBuilder` + `PromptContributor` seam |
| Context compression | `aegis_agent.context.compress.*` | three-phase pipeline; oversized-tool-result offload; per-round LLM summary |
| Sessions | `aegis_agent.sessions` | `SessionRepository` Protocol + in-memory + SQLite store; snapshot fast-resume |
| Session leases | `aegis_agent.sessions.lease` | SQLite/Redis cross-process leases; heartbeat + circuit breaker |
| Agent loop | `aegis_agent.runtime.AgentRuntime` | guard → context → compress → model → tools → loop; budget, interrupt, timeout, error |
| Skills | `aegis_agent.skills` | SKILL.md discovery, loading, routing, progressive-disclosure tools, prompt index injection; skill management |
| MCP client | `aegis_agent.mcp` | stdio + Streamable HTTP, schema adapter, Tool Protocol wrappers, optional `mcp` SDK |
| CLI | `aegis_agent.cli` | interactive REPL, `aegis` command; `--resume` / `--db` / `--ephemeral` / `--no-lease` / `--context-max-tokens` / `--no-compress` / `--snapshot-every-n` |

## Model configuration

The provider is configured purely from the environment (no secrets in code):

```bash
export AEGIS_API_KEY=...                 # required for the real provider
export AEGIS_BASE_URL=http://localhost:1234/v1   # optional; any OpenAI-compatible endpoint
export AEGIS_MODEL=gpt-4o-mini           # required for the real provider
# Context compression budget (default 120_000):
export AEGIS_CONTEXT_MAX_TOKENS=100000   # optional; lower = aggressive, off = very large number
```

The CLI picks the backend with `--model-backend auto|fake|openai` (default
`auto`): real provider when `AEGIS_API_KEY` and `AEGIS_MODEL` are set,
otherwise the deterministic fake.

## Install & run

This project uses [uv](https://docs.astral.sh/uv/) exclusively.

```bash
uv sync
uv run aegis                    # start the interactive REPL
uv run aegis --model-backend fake     # force the fake provider
uv run aegis --resume my-session      # resume a previous session
uv run aegis --db ./custom.db         # custom session store path
uv run aegis --ephemeral              # in-memory only (nothing persisted)
uv run aegis --no-lease               # disable cross-process lease
uv run aegis --no-compress            # disable context compression
uv run aegis --context-max-tokens 80000  # tighter compression budget
uv run aegis --version
uv run python -m aegis_agent
```

Optional extras:

```bash
uv sync --extra mcp    # MCP server support
uv sync --extra web    # web_search + web_extract (ddgs / trafilatura)
uv sync --extra redis  # Redis session-lease backend
```

In the REPL:

```
you> hello
aegis> Echo: hello
you> list .                   # triggers the list_directory tool
you> read README.md           # triggers the read_file tool
you> terminal echo hi         # triggers the terminal tool
you> /skill-name do X         # activates a skill
you> exit
```

## Session persistence & resume

By default Aegis persists every message to `~/.aegis/state.db` (SQLite, WAL
mode) as it arrives — crash-durable, idempotent.  Snapshots are recorded every
20 messages for fast resume.  A cross-process lease (SQLite by default,
Redis via `AEGIS_SESSION_LEASE_BACKEND=redis`) guarantees only one process
runs a given session at a time.

```bash
# First session — messages are persisted as they land:
uv run aegis --session my-task
you> list ~/projects/src
you> terminal npm test
you> exit

# Resume it later (or after a crash) — full history restored:
uv run aegis --resume my-task
# Resumed session my-task (6 messages).
you> continue working ...
```

## Context compression

When the derived context exceeds `--context-max-tokens` (default 120 000,
env `AEGIS_CONTEXT_MAX_TOKENS`), the runtime compresses it before each model
call.  The three-phase pipeline:

1. **Oversized tool offload** — tool results > 20,000 chars are persisted to
   `~/.aegis/tool-result-cache/` and replaced with a preview + file path.
2. **Micro-compact** — deduplicates / summarises old tool results, truncates
   tool-call arguments, clears historical reasoning — all locally, no LLM call.
3. **Round-level LLM summary** — oldest complete rounds are summarised via a
   dedicated provider (temperature=0 when using the OpenAI-compatible backend).

Original messages are **never** mutated; compression only affects the derived
view sent to the model.

## Test & lint

```bash
uv run pytest -q
uv run ruff check .
```

Default tests never touch a real paid API.  Opt-in tests:

```bash
# Real model endpoint:
AEGIS_RUN_INTEGRATION=1 AEGIS_API_KEY=... AEGIS_MODEL=... uv run pytest -m integration

# Real Redis lease backend:
docker compose -f tests/docker-compose.redis.yml up -d
AEGIS_TEST_REDIS_URL=redis://localhost:6379/0 uv run pytest -m integration
docker compose -f tests/docker-compose.redis.yml down
```

## Provenance

See `docs/extraction-plan.md` (the phased plan), `docs/development-log.md`
(interview-oriented technical log), and `docs/source-map.md` (which Aegis
modules derive from which Hermes sources, and how).  No Hermes code is
described as wholly original; adapted files carry an attribution header and
retain the Hermes MIT copyright.
