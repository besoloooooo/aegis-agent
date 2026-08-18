Aegis Agent Development Rules

1. Repository boundaries

There are two repositories in the current VS Code workspace:

Target repository:

/home/administrator/projects/aegis-agent

Reference repository:

/home/administrator/projects/hermes-agent

The Hermes repository is read-only reference source code.

Never modify, rename, delete, format, move, commit, or generate files under:

/home/administrator/projects/hermes-agent

All new source code, tests, scripts, configuration, and documentation must be created under:

/home/administrator/projects/aegis-agent

Before editing any file, verify that its absolute path belongs to the target repository.

Do not copy the entire Hermes repository by default. Before migrating a feature, first decide whether the lowest-risk and lowest-cost option is to port a complete file, module, or cohesive directory, adapt selected code, or reimplement it. Whole-unit migration is allowed when it is the smallest coherent implementation unit, most of its dependency closure is relevant to the current milestone, and splitting it first would add unnecessary work or risk. Do not migrate unrelated product features or large unrelated dependency trees.

2. Project identity

Project name:

Aegis Agent

Repository name:

aegis-agent

Python package:

aegis_agent

CLI command:

aegis

Project description:

Aegis Agent is a lightweight, recoverable, and extensible Agent Runtime built by extracting, simplifying, and modularizing the core runtime behavior of Hermes.

3. Python environment

This project uses uv exclusively.

Allowed commands:

uv add <dependency>
uv add --dev <dependency>
uv remove <dependency>
uv sync
uv run python ...
uv run pytest -q
uv run ruff check .
uv run mypy src

Do not:

run python -m venv;

run pip install directly;

create requirements.txt;

maintain dependencies outside pyproject.toml and uv.lock;

manually edit uv.lock.

Keep pyproject.toml and uv.lock synchronized.

4. Project scope

Aegis Agent should eventually contain:

interactive CLI;

Agent Runtime;

Agent Loop;

model provider abstraction;

OpenAI-compatible provider;

fake model provider;

tool registry;

tool executor;

built-in tools;

context builder;

hierarchical context compression;

oversized tool-result storage;

loop detection and circuit breaking;

session repository abstraction;

SQLite session storage;

message-level idempotent persistence;

checkpoint plus tail recovery;

SQLite and Redis session leases;

lightweight Skill loading and routing;

reliability and concurrency tests.

5. Excluded scope

Do not migrate unless explicitly requested:

Telegram;

Discord;

messaging gateways;

desktop pet;

Web UI;

voice;

browser automation;

scheduled tasks;

external messaging integrations;

every Hermes tool;

every Hermes model provider;

Hermes installation scripts;

Hermes branding and product-specific UI.

6. Architecture rules

Keep the following modules separate:

models;

tools;

context;

sessions;

skills;

runtime;

CLI.

The Agent Loop must not directly depend on:

Typer;

SQLite SQL statements;

Redis commands;

a concrete model provider;

global CLI state.

Use interfaces or protocols for:

ModelProvider;

SessionRepository;

LeaseBackend;

ToolExecutor;

ContextManager;

SkillRouter.

Original messages are the source of truth.

The following are derived structures and must never replace or overwrite the original message log:

compressed model context;

summaries;

checkpoints;

cached prompt views;

tool-result previews.

Checkpoint corruption or incompatibility must fall back to full message replay.

Context compression must only affect the context sent to the model.

7. Migration policy

Before implementing a feature:

inspect the corresponding Hermes implementation;

identify its observable behavior;

identify its direct dependency closure;

decide whether to port the complete implementation unit, adapt selected code, rewrite it, or drop it;

choose the option with the lowest total migration cost and risk while preserving the required architecture and current milestone boundaries;

implement only the behavior required by the current milestone;

write tests for that behavior.

Do not default to rewriting everything, and do not default to copying everything. A complete Hermes file, module, or cohesive directory may be migrated when it is already a suitable bounded unit and most of its dependencies are needed. When whole-unit migration is chosen, remove or isolate excluded product-specific dependencies only as required, keep the change within the current milestone, and document why whole-unit migration was preferred.

Large coupled entry files may be migrated only when they are the smallest practical unit for the milestone. Do not perform a broad architectural split unless it is required for correctness, testing, or the interfaces defined in this document.

Copied or substantially derived code must retain applicable license and copyright notices.

Record meaningful source relationships and migration decisions in:

docs/source-map.md

Maintain third-party attribution in:

THIRD_PARTY_NOTICES.md

Do not describe copied or adapted Hermes code as completely original.

8. Development workflow

Work on only one milestone at a time.

Do not combine unrelated refactoring with feature implementation.

Do not perform broad cleanup outside the current task.

Do not automatically commit, push, rebase, reset, or rewrite Git history.

After every task:

list all changed files;

explain the implemented behavior;

distinguish copied, adapted, and newly written code;

list commands executed;

report test results;

report unresolved risks and TODOs;

update the development report described in Section 10;

update README.md (the milestone list and any affected feature sections — not just docs/source-map.md and docs/development-log.md);

verify that Hermes was not modified.

Use the smallest relevant test first, then run:

uv run pytest -q
uv run ruff check .

when practical.

9. Testing principles

Tests must not require a real paid model API unless explicitly marked as optional integration tests.

Use deterministic fake providers for core Agent Loop tests.

Reliability tests should verify observable invariants rather than private implementation details.

Important invariants include:

one persisted logical message per client message ID;

monotonically ordered messages within a session;

no duplicated model request after successful completion;

no duplicated tool result;

no cross-session history;

only one lease owner for the same session;

checkpoint recovery equals full replay;

corrupted checkpoints fall back safely;

original messages remain unchanged after context compression.

10. Development report

Maintain one cumulative interview-oriented development report at:

docs/development-log.md

After every completed task or milestone, append a new section containing:

task goal and the original problem;

relevant Hermes behavior and source locations;

migration decision: whole-unit port, adapted port, rewrite, or new implementation;

Aegis design, main data flow, and key interfaces;

important files, classes, functions, tables, and fields;

reliability invariants, edge cases, and failure handling;

tests, fault injection or concurrency validation, and measured results;

trade-offs, remaining limitations, and TODOs;

a concise interview-ready explanation of what was done, why it was needed, and how it was verified.

The report should explain meaningful technical decisions and behavior. It does not need to be a line-by-line code changelog.

The milestone must also be reflected in README.md (the "Milestones delivered" list, the project layout, and any user-facing feature / command sections), not only in docs/source-map.md and docs/development-log.md.

11. Completion report format

Every completed milestone must end with:

Changed files

Implemented behavior

Source relationship

Tests executed

Test results

Development report

README update

Remaining risks

Suggested next milestone

Do not begin the next milestone automatically.