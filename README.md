# Aegis Agent

A lightweight, recoverable, and extensible **Agent Runtime**, built by extracting, simplifying, and evolving useful runtime behavior from [Hermes](https://github.com/NousResearch/hermes-agent) and Claude Code reference implementations.

> Built for reliable long-running agents with session recovery, context compression, memory, tool use, subagents, teams, inter-agent messaging, and extensible runtime components.

## 🏁 Milestones delivered

Nineteen milestones, from a minimal skeleton to the full runtime:

**Core runtime**
1. Minimal Agent Runtime — fake provider, in-memory sessions, Agent Loop
   with a configurable 50-iteration default per turn
2. OpenAI-compatible and native Anthropic providers & streaming tool calls
3. Live terminal UI

**Tools**
4. Skills subsystem — `SKILL.md` discovery / loading / routing
5. Lightweight MCP client — stdio + Streamable HTTP
6. File-editing tools — write_file / patch / search_files
7. Terminal & background-process tools — strict non-zero status and POSIX
   pipeline-failure preservation
8. Web tools — web_search / web_extract with SSRF gate
9. Skill management — `skill_manage`

**Reliability & sessions**
10. Context compression pipeline — offload → micro-compact → LLM summary
11. Compression wired into the agent loop + reasoning_content
12. SQLite persistence + snapshot fast-resume + cross-process leases

**Prompt / memory / search**
13. Dynamic system-prompt sections — acceptance gates and container-aware
    environment hints
14. Personal long-term memory (Auto Memory) — `USER.md` + `MEMORY.md` index
15. Memory recall + background extraction
16. Session history search — FTS5 `session_search`
17. Project-scoped long-term memory — `--project [PATH]`

**Interactive UX**
18. Slash-command suite — `/save` `/new` `/history` `/undo` `/retry` `/title` …
    plus a full-screen TTY layout with scrollable chat history, fixed bottom
    composer, Markdown replies, highlighted input tokens, and `Ctrl+End` to
    jump back to the live tail.

**Multi-agent orchestration**
19. Multi-agent orchestration — `Agent`, `team_create`, `send_message`, `/agents`

**Agent quality foundation**
- Harbor setup reliability — bounded Docker Hub recovery, strict APT refresh with one package-404 recovery, and isolated compatible Python provisioning.
- Quality Stage 0 — optional Langfuse end-to-end traces for Agent runs, model
  calls, tool calls, subagents, and final results
- Quality Phase 1–2 — Harbor custom-agent integration plus a provider-neutral
  `ExecutionRecord` joining runtime steps, usage, Harbor verifier results, and artifacts,
  with explicit setup/runtime/verifier proxies, an opt-in Docker host alias,
  and configurable model request timeouts
- Quality Phase 3 — rule-first, explainable Process Evaluation with an optional
  failure-recovery LLM Judge, opt-in live/historical conversation records, CLI
  batch evaluation, and trace-addressable Viewer issues
- Local Trace Viewer with one-click Harbor sync, cursor-paginated Langfuse roots,
  separate Conversation/Evaluation/All Runs views, and explicit failed-run/error-observation metrics

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
├── quality/        # ExecutionRecord, Process Evaluation, local store, Harbor adapter
├── integrations/   # optional external orchestrator adapters (Harbor)
├── models/         # provider-neutral protocol, fake / OpenAI / Anthropic adapters
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

Or configure the native Anthropic Messages API provider:

```bash
export ANTHROPIC_API_KEY=...
export ANTHROPIC_MODEL=claude-sonnet-4-6
# export ANTHROPIC_BASE_URL=https://api.anthropic.com  # optional override

uv run aegis --model-backend anthropic
```

`--model-backend auto` keeps OpenAI-compatible configuration precedence, then
selects Anthropic when both `ANTHROPIC_API_KEY` and `ANTHROPIC_MODEL` are set.
Use `--model-backend fake` to force the deterministic provider.

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
scroll it from the keyboard, while the input row remains visible. Each wheel
event moves one history row for precise native-terminal scrolling. Press
`Ctrl+End` at any time to jump directly to the bottom and resume following live
output. The clipped pane intentionally has no scrollbar because a slice-local
thumb cannot accurately represent or drag through the complete history. New
output follows the tail only while the user is already at the bottom, so
streaming does not pull a manually scrolled history view away.
The composer keeps prompt_toolkit history, cursor editing, and token highlighting
in one clean row. User messages, assistant Markdown, tool status, and errors all
share the history pane. Non-TTY input/output keeps the simpler plain streaming
path so pipes, logs, and tests remain stable.

The history pane uses **viewport clipping** to maintain scrolling performance
even with long conversations. Only the visible lines plus a buffer zone are
rendered. A separate absolute history position is translated into the clipped
slice's local scroll position, so manual scrolling remains stable while live
output keeps extending the conversation.

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

OpenAI-compatible and Anthropic responses propagate provider-reported input,
output, cache-read, and cache-write token usage into each Langfuse Model Call.
OpenAI streaming collects its final usage-only chunk. Anthropic streaming
merges input/cache buckets from `message_start` with output tokens from the
final `message_delta`, preserving separate cache reads and cache creations.
Anthropic's API does not return total tokens or direct cost, so Aegis leaves
those fields unset rather than estimating them. Compatible gateways that
directly return a total or cost are passed through; Aegis has no price table.

---

## 🧪 Harbor Offline Evaluation (Quality Phase 1–2)

Aegis exposes a non-interactive runner and a Harbor custom installed agent.
Harbor remains responsible for tasks, environments, trials, retry/concurrency,
verifiers, and rewards. Proxy-enabled setup uses the sibling Harbor checkout's
`ensure_system_dependencies(..., env=...)` extension added with this integration.

Run one Aegis task directly (this uses the selected real provider unless
`--model-backend fake` is explicitly requested):

```bash
uv run aegis run \
  --instruction "inspect the project and complete the task" \
  --model-backend openai \
  --cwd /path/to/worktree
```

Run Aegis from the sibling Harbor checkout (Docker must be available to Harbor):

```bash
cd ../harbor
PYTHONPATH=../aegis-agent/src uv run harbor run \
  -p /path/to/task-or-dataset \
  --agent aegis_agent.integrations.harbor:Aegis \
  --model openai/qwen-model \
  --ae AEGIS_API_KEY="$AEGIS_API_KEY" \
  --ae AEGIS_BASE_URL="$AEGIS_BASE_URL" \
  --ae AEGIS_MODEL_TIMEOUT=300 \
  --ae LANGFUSE_PUBLIC_KEY="$LANGFUSE_PUBLIC_KEY" \
  --ae LANGFUSE_SECRET_KEY="$LANGFUSE_SECRET_KEY" \
  --ae LANGFUSE_BASE_URL="$LANGFUSE_BASE_URL"
```

Host proxy variables are not copied into the task container implicitly. A
loopback proxy such as `127.0.0.1:10808` would refer to the container itself and
is rejected when supplied explicitly. If setup or the model endpoint requires a
proxy, expose it on an address the container can reach and pass `HTTP_PROXY` /
`HTTPS_PROXY` with `--ae`. On native Linux/WSL Docker, prefer a proxy URL using
`host.docker.internal`; when that hostname is explicitly requested, the adapter
maps it to the task container's current Docker gateway. This remains stable if
the WSL LAN address changes during a trial. Explicit proxies now cover system-package setup,
Aegis wheel installation, and runtime. Explicit `PIP_INDEX_URL`,
`PIP_DEFAULT_TIMEOUT`, `PIP_RETRIES`, and related pip settings also apply during
wheel installation. The local `scripts/aegis-eval.sh` wrapper maps
`AEGIS_HARBOR_PROXY` to upper/lowercase HTTP(S) proxy variables for both agent
and verifier, plus `AEGIS_HARBOR_NO_PROXY` to their bypass settings. The proxy
must listen on an interface reachable from Docker; the hostname mapping does
not start a proxy or expose a localhost-only listener.

Docker image pulls happen before Agent setup. The companion Harbor checkout
prepares `task.toml` prebuilt images explicitly: reuse a local image, try the
configured registry for up to 90 seconds, then retry the same Docker Hub
repository/tag/digest at its canonical endpoint for up to 300 seconds. Custom
registries and Podman are not redirected. Compose uses the prepared image;
progress survives cancellation and subprocesses are reaped. The existing
outer environment deadline still applies. Dockerfile base images and explicit
custom Compose overrides are outside this recovery path.

APT setup now requires a successful index refresh, bypasses HTTP caches, and
bounds transport retries/timeouts. A package-download 404 gets one fresh-index
retry; other installation failures remain errors. Aegis provisions Python 3.11
under `/opt/aegis-python` and a venv under `/opt/aegis-venv`, leaving the task's
system Python untouched. The pinned uv bootstrap is checksum-verified on the
host and transferred into the task, so container GitHub access is not required.
Explicit pip index/certificate settings are translated for uv, and explicit
UV download settings are forwarded. The local wrapper supplies the managed
Python download mirror for Agent setup as well as the verifier.

For an inexpensive setup-only check (no model request or task scoring):

```bash
aegis-eval terminal-bench/qemu-startup --install-only
```

A setup-only pass confirms environment and Agent installation, not task success.
External registry/package outages can still exhaust the bounded attempts; logs
identify the failing phase rather than silently reporting success. Use
`--environment-build-timeout-multiplier` only when image progress justifies a
larger budget; Agent setup/model timeout options do not affect image pulls.

Model requests default to a 60-second inactivity timeout. Long-reasoning models
can pause between streamed chunks for longer than that, so Harbor evaluations
should set `AEGIS_MODEL_TIMEOUT` explicitly; the local wrapper defaults it to
300 seconds. This changes the model transport timeout only and does not hide
setup failures or extend Harbor's independent phase deadlines.

The [sample-based evaluator review](docs/process-evaluator-review-20260918.md)
documents observed false positives, empty-trace scoring, infrastructure failures,
and the proposed order of scoring improvements.

The adapter installs the current Aegis wheel inside the Harbor task environment
and runs tools in `/app`. Container markers take precedence over an inherited
WSL kernel signature, so the system prompt describes the Linux container and
does not advertise host-only `/mnt/c` paths. The Harbor trial UUID becomes Aegis `execution_id`;
the Langfuse trace id is deterministically derived from it, so no timestamp
matching is needed. A runtime record is written under the trial's `agent/` logs.
After Harbor finishes its verifier, finalize one trial or an entire job:

```bash
uv run aegis quality import-harbor ../harbor/jobs/<job-directory>
```

Final records are stored in `~/.aegis/quality/executions` (override with
`AEGIS_EXECUTION_RECORDS_DIR`) and beside each Harbor `result.json` as
`execution-record.json`. Runtime success and verifier pass/fail are separate;
missing usage, cost, or verifier fields stay `null` rather than being guessed.
Langfuse remains optional and fail-open.

Open the local Trace Viewer:

```bash
uv run aegis quality view
```

Click **Sync Harbor** to import every new or changed completed trial from
`~/harbor/jobs`; records already synchronized are left untouched. Set another
location with `--harbor-jobs-dir PATH` or `AEGIS_HARBOR_JOBS_DIR`. The manual
`quality import-harbor` command remains available for one specific result or
job directory.

The viewer opens `http://127.0.0.1:8765`, shows local ExecutionRecords
immediately, and cursor-paginates Langfuse root observations in the background
through the SDK's v4 Observations API (up to a 1,000-root safety limit, which is
shown explicitly if reached). Root observation IDs remain distinct even when an
external execution ID was reused and several roots share one trace. The run-type
tabs keep normal chat analysis separate from benchmark results:

- **Conversations** groups interactive traces as Session → Turn.
- **Evaluations** groups imported Harbor records as Job → Trial and surfaces
  task identity, reward, verifier pass/fail, runtime errors, and usage.
- **All Runs** provides a combined operational view and also includes ordinary
  non-interactive `aegis run` tasks.

Selecting any run continues into Model/Tool/Final detail. Each level shows the
relevant call counts, duration, input/output tokens, cache read/write and
cache hit rate, cost, and error state. Cache hit rate is Cache Read divided by
the provider-reported total input. For Alibaba Cloud Model Studio's OpenAI-compatible
implicit cache, that is `prompt_tokens_details.cached_tokens / prompt_tokens`;
the viewer reads `prompt_tokens` directly when available, or uses
`total_tokens - output_tokens` as the same exact denominator when cache-write usage
is omitted. A real zero is displayed as zero (for example `$0.00`), while
provider-omitted usage or cost stays unknown (`—`). Mixed aggregates with some
unknown contributions are marked as lower bounds (`≥`). Consecutive Model Calls
in one Turn also show adjacent input-token growth.

Selecting a turn shows the complete message context sent to its last model call
(`system`, `user`, `assistant`, and `tool`) plus the final model output, followed
by the Agent/Model/Tool/Final tree. Model and Tool nodes surface their important
fields directly; the detail pane presents status, usage, request, and response
before a collapsed Raw Payload. Error observations are visually emphasized and
roll up to their Turn and Session. Summary cards report failed runs separately
from error observations, so one multi-node failure is not presented as several
failed runs. The Python response boundary applies
sanitization again so SDK-added metadata or historical local fields containing
credentials do not reach the browser. Historical messages do not carry
their own timestamps, so timing belongs to the executable observations rather
than being invented. Turns sharing a `session_id` appear in a collapsible
Session group; the group preserves chronological turn order and aggregates its
status. Langfuse credentials remain in the Python process and are never sent to
browser JavaScript. Trace GET endpoints remain read-only; the only mutation is
the explicit Harbor sync POST, scoped to the configured jobs directory and
protected by a per-server same-origin token. There is no CORS, and the server
binds to loopback by default; use `--no-open`, `--port`, `--records-dir`, or
`--harbor-jobs-dir` when needed.

## 🔎 Process Evaluation (Quality Phase 3)

Process Evaluation consumes an existing `ExecutionRecord`; it does not require
Langfuse or Harbor and does not alter the Agent Loop. The failure-recovery LLM
Judge is enabled by default with provider `auto`; it makes one side query only
when the record contains a Failure Episode. If no real model is configured or
the Judge fails, evaluation safely keeps the deterministic rule result. Run it
for one JSON record/path or every record in the local store:

```bash
uv run aegis quality evaluate <execution-id-or-record.json>
uv run aegis quality evaluate --all
uv run aegis quality evaluate --all --json
uv run aegis quality evaluate <record> --failure-recovery-judge openai
```

Interactive conversations remain opt-in. `--record-conversations` writes one
independent `run_kind=conversation` record per Turn, while
`--process-evaluate-conversations` also evaluates each completed Turn. Turns
share the conversation `session_id` but have distinct execution/trace IDs:

```bash
uv run aegis --record-conversations
uv run aegis --process-evaluate-conversations
```

An existing SQLite conversation can be reconstructed later. The command uses a
deterministic ID per user Turn, so rerunning it replaces the same records rather
than creating duplicates:

```bash
uv run aegis quality record-session <session-id>
uv run aegis quality record-session <session-id> --evaluate
```

Reconstructed records preserve persisted messages, tool names/arguments/results,
correlation IDs, and available timestamps. They are marked
`reconstructed_from_session`; historical provider/model, usage, cost, exact
request context, and call latency remain unknown instead of being invented.

The command safely replaces the derived result at
`quality.process_evaluation` while preserving `schema_version: "1.0"`, original
steps, runtime outcome, and Harbor verifier data. Each result records its
evaluation time, evaluator version, actual weights, overall score/status, and
six versioned, explainable grades:

- repeated tool calls: same/highly similar arguments and identical results,
  without an intervening successful mutation;
- repeated failures: an unchanged tool operation reaches the configurable
  consecutive-failure threshold;
- failure recovery: extracts a complete episode for each failed tool/model/runtime
  step (diagnosis, tool/argument/path changes, mutations, related retries, and
  results); successful reads/searches/status checks and unrelated successes are
  never treated as recovery by themselves;
- final verification: after a detected material modification, looks for a
  successful task-appropriate verification tool or command;
- loop detection: finds contiguous `A×N` and periodic `(A,B)×N` tool patterns;
- execution efficiency: reports step/model/tool/failure/repeat/token/cost/
  latency metrics and only applies absolute or relative limits when configured.

Every non-pass grade includes severity, evidence, and affected `step_id` values.
The optional `FailureRecoveryLLMGrader` refines only the existing
`failure_recovery` grade. It makes one side query only when at least one failure
episode exists and judges effective diagnosis, targeted adjustment, and actual
recovery of the original failed target. Select `auto`, `openai`, or `anthropic`
with `--failure-recovery-judge` (or `AEGIS_FAILURE_RECOVERY_JUDGE`). Missing model
configuration, provider errors, malformed JSON, mismatched episode IDs, and
ungrounded recovery steps all preserve the rule score/status. The other five
graders remain deterministic.

Persistent Quality preferences live in `~/.aegis/config.yaml` (or the file
passed with `--config` / `--mcp-config`):

```yaml
quality:
  conversations:
    record: false
    evaluate: false
  failure_recovery_judge:
    enabled: true          # default; set false for rule-only evaluation
    provider: auto         # auto, openai, or anthropic
    model: null            # optional Judge-specific model override
    base_url: null         # optional compatible endpoint
```

Keep API keys out of YAML. OpenAI-compatible Judges read `AEGIS_API_KEY` and,
when not overridden above, `AEGIS_MODEL` / `AEGIS_BASE_URL`; Anthropic Judges
read `ANTHROPIC_API_KEY` and optionally `ANTHROPIC_MODEL` /
`ANTHROPIC_BASE_URL`. Explicit CLI options temporarily override the configured
provider. With no Quality config, Judge resolution still defaults to `enabled:
true` and `provider: auto`; `enabled: false` keeps all Process Evaluation
rule-only.

Missing fields produce `insufficient_data` where a reliable judgment cannot be
made. Per-task overrides can be supplied in
`record.metadata.process_evaluation_config` (verification patterns/requirement,
similarity and loop thresholds, efficiency thresholds or baselines, and grader
weights). `failure_recovery_llm_enabled=false` disables the optional judge for a
record. A process FAIL is analysis output, not a Quality Gate, so the command only
exits non-zero for evaluation/load/save errors.

The Viewer keeps Harbor **Outcome** and **Process** status separate, allowing
`Outcome PASS` with `Process FAIL`. Run cards show the process score/status; the
ExecutionRecord detail lists issues with grader, severity, message, and affected
steps. Selecting an issue focuses its first affected local Step when available.
Harbor re-import preserves an existing process result and never modifies the
original `result.json`.

### Trigger and record lifecycle

The CLI loads the project `.env` first and then `~/.aegis/.env`. Langfuse is
enabled only when both `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` are
available; `LANGFUSE_BASE_URL` selects the Cloud region or a self-hosted
backend. A missing SDK, incomplete credentials, initialization error, or upload
failure automatically degrades to no-op tracing.

| Action | What triggers | Result |
|---|---|---|
| `uv run aegis --session <id>` | Every submitted user turn enters `AgentRuntime.run_turn()` | Writes normal session history and, when Langfuse is enabled, one cloud trace per turn marked `conversation`. ExecutionRecord capture remains off unless `--record-conversations`, `--process-evaluate-conversations`, or its persistent config is enabled. |
| `uv run aegis quality record-session <session-id>` | Explicit reconstruction of an existing SQLite session | Writes one deterministic, idempotent `conversation` ExecutionRecord per persisted user Turn; `--evaluate` also runs Process Evaluation. Unavailable historical telemetry stays unknown and is disclosed in record metadata. |
| `uv run aegis run ...` | One non-interactive task | Always writes a local ExecutionRecord marked `task` and also sends a Langfuse trace when enabled. Missing `execution_id` is generated as a UUID; `session_id` defaults to it; `trace_id` is deterministically derived from it. `--run-kind evaluation` is reserved for evaluation adapters. |
| `uv run harbor run ... --agent aegis_agent.integrations.harbor:Aegis` | One or more Harbor trials | Harbor starts each Docker task environment, installs Aegis, uses the trial UUID as `execution_id`, and marks the run `evaluation`. Aegis writes the runtime record, then Harbor runs the verifier and writes `result.json`. |
| `uv run aegis quality import-harbor <result-or-job>` | Explicit post-verifier import | Merges Harbor identity, verifier rewards, exceptions, aggregate usage, and artifacts into the final ExecutionRecord. The argument may be one `result.json` or an entire job directory. |
| `uv run aegis quality evaluate <record>` / `--all` | Explicit offline process evaluation | Runs five deterministic graders plus rule-first failure recovery. An explicitly configured Judge refines only failure episodes; safe fallback preserves the rule result. The command atomically updates `quality.process_evaluation`; outcome/verifier fields and source steps remain unchanged. |
| `uv run aegis quality view` | Starts the local viewer | Reads local JSON first, then cursor-paginates optional Langfuse roots up to an explicit safety limit. **Sync Harbor** incrementally imports new or changed completed TrialResults from the configured jobs directory; it never changes Langfuse trace data. Unit tests force Langfuse credentials empty so a developer `.env` cannot upload pytest traces. This isolation prevents future uploads but does not automatically mutate existing Langfuse history. |

Langfuse upload is asynchronous. A normal CLI exit calls Runtime
`shutdown()` so queued observations are flushed. Harbor finalization is a
separate explicit step: without `import-harbor` or **Sync Harbor**, the trial
still has Aegis's runtime record and Harbor's raw `result.json`, but the central
record has not yet been enriched with verifier output.

### Recorded data

A Langfuse `Aegis Run` records the user task, session/agent/version metadata,
final output, success or error, stop reason, iteration/tool counts, and latency.
Its children record:

- Model calls: provider, model, input messages, output, finish reason, tool
  calls, errors, latency, reliable input/output/total tokens, separate cache
  read/write tokens, and direct upstream cost when available.
- Tool calls: tool name, sanitized arguments and result, success or error, and
  latency.
- Subagent runs: type, task, parent agent, result, error, latency, and their
  nested model/tool/final tree.
- Final result: output, success or error, and stop reason.

The local `ExecutionRecord` schema adds a stable `run_kind` (`conversation`,
`task`, or `evaluation`), execution/task/trial/job/session/trace identity,
agent configuration, execution timing and status, the ordered
parent-linked step tree, usage buckets, Harbor verifier result/rewards/pass
state, and artifact/log paths. Runtime success and verifier pass/fail remain
separate. Fields that the provider or Harbor does not supply remain `null`.

All Langfuse payloads and local step payloads pass through the same sanitizer.
Common credential fields and key patterns are replaced with `[REDACTED]`.
Strings are bounded to 20,000 characters, collections to 100 items, and nesting
to eight levels; truncation metadata retains the original size.

### Storage locations

| Data | Default location | Override / notes |
|---|---|---|
| Interactive session history | `~/.aegis/state.db` | `--db` overrides it; `--ephemeral` disables persistence. Project-scoped sessions use the project data directory. |
| Langfuse traces | The project at `LANGFUSE_BASE_URL` | Aegis does not maintain a second local Langfuse database. |
| One-shot central record | `~/.aegis/quality/executions/<execution_id>.json` | Override with `--records-dir` or `AEGIS_EXECUTION_RECORDS_DIR`. |
| Explicit one-shot copy | `--record-path` / `AEGIS_EXECUTION_RECORD_PATH` | Written alongside the central record. |
| Harbor runtime record | `<trial-dir>/agent/execution-record.runtime.json` | Written before verifier output exists. |
| Harbor agent log | `<trial-dir>/agent/aegis.txt` | Preserves agent stdout/stderr. |
| Harbor raw result | `<trial-dir>/result.json` | Written by Harbor after the verifier. |
| Harbor final record | `<trial-dir>/execution-record.json` | Written by `quality import-harbor`. |
| Imported central copy | `~/.aegis/quality/executions/<trial-uuid>.json` | Contains the merged runtime and verifier result. |
| Trace Viewer | No separate storage | Reads local records and Langfuse v4 observations; explicit Harbor sync writes finalized records to the existing central store and beside each TrialResult. |

The viewer groups conversations by `session_id` and Harbor evaluations by
`job_id`, but joins local and cloud copies of an individual run by exact
`trace_id`, not by timestamp. Cloud success comes from
explicit Aegis success metadata; older traces fall back to their level,
`stop_reason`, completion time, and output. `CLOUD` means a Langfuse-only trace,
`LOCAL` means an ExecutionRecord, and `LF` on a local entry means a matching
Langfuse trace was found. A search match in any turn keeps the complete Session
visible so follow-up context is not hidden.

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
  max: 50
session:
  snapshot_every_n: 20
mcp_servers: { ... }
```

The built-in limit is **50 model/tool iterations per turn**, not 50 individual
tool calls. Override it with `iterations.max` above or, for one launch,
`uv run aegis --max-iterations 100` (`-n 100`). Restart Aegis and resume the
session for a changed limit to take effect; an already-running session keeps
its startup limit. Built-in typed subagents (`explore` and `general-purpose`)
allow 25 iterations per turn; custom `AgentDefinition` values continue to use
their explicit `max_iterations` setting.

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

`terminal` treats every non-zero command status as an error while preserving
the numeric `exit_code`, output, and command-aware explanation for diagnosis.
On POSIX, foreground and managed background pipelines use Bash `pipefail` when
Bash is available, so a successful final filter cannot hide an upstream
failure; Windows retains `cmd /c`. Long-running hints inspect actual
command/executable positions rather than arbitrary path or argument substrings.
Foreground timeouts return `exit_code: 124` and preserve stdout/stderr captured
before the process is killed.

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
