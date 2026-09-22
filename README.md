# Aegis Agent

A lightweight, recoverable, and extensible **Agent Runtime**, built by extracting,
simplifying, and evolving useful runtime behavior from [Hermes](https://github.com/NousResearch/hermes-agent)
and Claude Code reference implementations.

Aegis is designed for long-running agents with session recovery, context compression,
memory, tool use, subagents, teams, and extensible runtime components.

## What is included

- **Core runtime:** Agent Loop, fake/OpenAI-compatible/native Anthropic providers,
  streaming tool calls, context compression, and a live terminal UI.
- **Tools and extensions:** file and shell tools, web tools with SSRF protection,
  `SKILL.md` skills, MCP (stdio and Streamable HTTP), and session search.
- **Sessions and memory:** SQLite persistence, snapshots, leases, personal/project
  memory, asynchronous recall, extraction, and resumable conversations. Recalled
  bodies use cache-friendly transient context and never become persisted or orphaned
  tool messages.
- **Orchestration:** one-shot/background subagents, in-process teams, and messaging.
- **Quality:** optional Langfuse tracing, Harbor integration, `ExecutionRecord`,
  Process Evaluation, and a local Trace Viewer.

Detailed implementation history and design decisions are documented in
[`docs/development-log.md`](docs/development-log.md).

## Project layout

```text
src/aegis_agent/
├── cli.py          # CLI / REPL entry point
├── runtime.py      # AgentRuntime and Agent Loop
├── models/         # provider protocols and model adapters
├── tools/          # registry, executor, and built-in tools
├── context/        # context construction and compression
├── sessions/       # session repositories and leases
├── memory/         # personal and project memory
├── skills/         # SKILL.md loading and routing
├── agents/         # subagents, teams, and messaging
├── observability/  # optional tracing and sanitization
├── quality/        # records, evaluation, storage, and Viewer
├── integrations/   # Harbor adapter
└── mcp/            # MCP client
```

Original conversation messages are preserved; context compression only changes the
view sent to the model.

## Quick start

Prerequisite: [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run aegis
```

Without model configuration, Aegis uses its deterministic fake provider. For an
OpenAI-compatible endpoint:

```bash
export AEGIS_API_KEY=...
export AEGIS_BASE_URL=http://localhost:1234/v1
export AEGIS_MODEL=gpt-4o-mini
uv run aegis
```

For the native Anthropic provider:

```bash
export ANTHROPIC_API_KEY=...
export ANTHROPIC_MODEL=claude-sonnet-4-6
uv run aegis --model-backend anthropic
```

Use `--model-backend fake` to force the deterministic provider. `auto` prefers the
OpenAI-compatible configuration and otherwise uses Anthropic when configured.

## Sessions and projects

Interactive sessions are stored in SQLite. Personal sessions use
`~/.aegis/state.db`; project sessions use a separate store under
`~/.aegis/projects/<project-id>/`.

```bash
uv run aegis --resume my-session
uv run aegis --project /path/to/repo --resume my-session
uv run aegis --ephemeral       # do not persist a session
```

Use `--project` to scope memory and sessions to the current repository (a bare
`--project` uses the current directory).

## Interactive commands

Inside the REPL, type `/help` for the complete list. Common commands include:

```text
/new [name]    start a session (alias: /reset)
/clear         clear the screen and start a session
/history       show the conversation
/save          write a local debug snapshot (alias: /chatlog)
/retry         resend the last user message
/undo [N]      undo user turns while retaining audit history
/title [name]  set or show the session title
/sessions      list recorded sessions
/agents        inspect subagent tasks
/exit          quit (alias: /quit)
```

The interactive UI supports Markdown responses, scrolling history, and slash-command
completion. Non-TTY input/output remains suitable for pipes and logs.

## Harbor and quality evaluation

Run a non-interactive task directly:

```bash
uv run aegis run \
  --instruction "inspect the project and complete the task" \
  --model-backend openai \
  --cwd /path/to/worktree
```

Harbor runs are started from the sibling Harbor checkout and require Docker:

```bash
cd ../harbor
PYTHONPATH=../aegis-agent/src uv run harbor run \
  -p /path/to/task-or-dataset \
  --agent aegis_agent.integrations.harbor:Aegis \
  --model openai/qwen-model
```

After Harbor verification, import a result or job:

```bash
uv run aegis quality import-harbor ../harbor/jobs/<job-directory>
```

Process Evaluation works offline from an `ExecutionRecord` and does not alter the
Agent Loop:

```bash
uv run aegis quality evaluate <execution-id-or-record.json>
uv run aegis quality evaluate --all
uv run aegis quality evaluate --all --json
```

Evaluation is evidence-aware: incomplete traces remain `insufficient_data` rather
than being scored as successful or failed. Execution Efficiency is `NOT_SCORED` until
at least five successful executions for the same `task_id` provide a median baseline;
that state does not affect the overall PASS/FAIL result. Optional grounded Judges can
review ambiguous recovery and verification evidence; keep credentials in environment
variables, not YAML. The full grading behavior is described in the
[development log](docs/development-log.md) and the [review report](docs/process-evaluator-review-20260918.md).

## Trace Viewer and observability

Enable optional Langfuse tracing:

```bash
uv sync --extra observability
export LANGFUSE_PUBLIC_KEY=pk-lf-...
export LANGFUSE_SECRET_KEY=sk-lf-...
uv run aegis
```

Tracing is fail-open and optional. Start the local Viewer with:

```bash
uv run aegis quality view
```

The Viewer reads local records, can synchronize completed Harbor trials, and keeps
conversation, evaluation, and combined run views separate. It binds to loopback by
default; use `--no-open`, `--port`, `--records-dir`, or `--harbor-jobs-dir` as needed.
Run status reflects the final execution result, while recovered step errors remain
visible on their individual observations and in the separate error-observation count.

## Configuration

Persistent settings live in `~/.aegis/config.yaml`; precedence is
**CLI flag > config file > built-in default**. See [`config.example.yaml`](config.example.yaml)
for the complete key reference.

```yaml
memory:
  recall: true
  extract: true
context:
  max_tokens: 120000
iterations:
  max: 50
session:
  snapshot_every_n: 20
```

Useful options include `--max-iterations`, `--context-max-tokens`, `--no-compress`,
`--no-memory`, `--no-memory-recall`, `--no-memory-extract`, `--no-lease`, and
`--project`.

Optional dependency groups:

```bash
uv sync --extra web
uv sync --extra redis
uv sync --extra observability
```

## Development

```bash
uv run pytest -q
uv run ruff check .
```

Tests use deterministic providers by default and do not require a paid model API.

## Documentation and provenance

- [`docs/development-log.md`](docs/development-log.md) — implementation history and engineering decisions
- [`docs/source-map.md`](docs/source-map.md) — mapping to Hermes and Claude Code references
- [`docs/extraction-plan.md`](docs/extraction-plan.md) — extraction and development plan
- [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) — third-party attribution and licenses

Aegis independently implements or adapts selected runtime behaviors from Hermes and
Claude Code where documented. It does not include their unrelated product features.
