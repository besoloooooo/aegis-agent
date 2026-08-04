# Aegis Agent Development Rules

## 1. Repository boundaries

There are two repositories in the current VS Code workspace:

Target repository:

`/home/administrator/projects/aegis-agent`

Reference repository:

`/home/administrator/projects/hermes-agent`

The Hermes repository is read-only reference source code.

Never modify, rename, delete, format, move, commit, or generate files under:

`/home/administrator/projects/hermes-agent`

All new source code, tests, scripts, configuration, and documentation must be created under:

`/home/administrator/projects/aegis-agent`

Before editing any file, verify that its absolute path belongs to the target repository.

Do not copy the entire Hermes repository, an entire Hermes directory, or a large monolithic entry file into Aegis Agent.

## 2. Project identity

Project name:

`Aegis Agent`

Repository name:

`aegis-agent`

Python package:

`aegis_agent`

CLI command:

`aegis`

Project description:

Aegis Agent is a lightweight, recoverable, and extensible Agent Runtime built by extracting, simplifying, and modularizing the core runtime behavior of Hermes.

## 3. Python environment

This project uses uv exclusively.

Allowed commands:

```bash
uv add <dependency>
uv add --dev <dependency>
uv remove <dependency>
uv sync
uv run python ...
uv run pytest -q
uv run ruff check .
uv run mypy src
```

Do not:

* run `python -m venv`;
* run `pip install` directly;
* create `requirements.txt`;
* maintain dependencies outside `pyproject.toml` and `uv.lock`;
* manually edit `uv.lock`.

Keep `pyproject.toml` and `uv.lock` synchronized.

## 4. Project scope

Aegis Agent should eventually contain:

* interactive CLI;
* Agent Runtime;
* Agent Loop;
* model provider abstraction;
* OpenAI-compatible provider;
* fake model provider;
* tool registry;
* tool executor;
* built-in tools;
* context builder;
* hierarchical context compression;
* oversized tool-result storage;
* loop detection and circuit breaking;
* session repository abstraction;
* SQLite session storage;
* message-level idempotent persistence;
* checkpoint plus tail recovery;
* SQLite and Redis session leases;
* lightweight Skill loading and routing;
* reliability and concurrency tests.

## 5. Excluded scope

Do not migrate unless explicitly requested:

* Telegram;
* Discord;
* messaging gateways;
* desktop pet;
* Web UI;
* voice;
* browser automation;
* scheduled tasks;
* external messaging integrations;
* every Hermes tool;
* every Hermes model provider;
* Hermes installation scripts;
* Hermes branding and product-specific UI.

## 6. Architecture rules

Keep the following modules separate:

* models;
* tools;
* context;
* sessions;
* skills;
* runtime;
* CLI.

The Agent Loop must not directly depend on:

* Typer;
* SQLite SQL statements;
* Redis commands;
* a concrete model provider;
* global CLI state.

Use interfaces or protocols for:

* ModelProvider;
* SessionRepository;
* LeaseBackend;
* ToolExecutor;
* ContextManager;
* SkillRouter.

Original messages are the source of truth.

The following are derived structures and must never replace or overwrite the original message log:

* compressed model context;
* summaries;
* checkpoints;
* cached prompt views;
* tool-result previews.

Checkpoint corruption or incompatibility must fall back to full message replay.

Context compression must only affect the context sent to the model.

## 7. Migration policy

Before implementing a feature:

1. inspect the corresponding Hermes implementation;
2. identify its observable behavior;
3. identify its direct dependency closure;
4. decide whether to port, adapt, rewrite, or drop it;
5. implement only the minimum behavior required by the current milestone;
6. write tests for that behavior.

Prefer clean reimplementation around small interfaces over copying large coupled files.

Copied or substantially derived code must retain applicable license and copyright notices.

Record meaningful source relationships in:

`docs/source-map.md`

Maintain third-party attribution in:

`THIRD_PARTY_NOTICES.md`

Do not describe copied or adapted Hermes code as completely original.

## 8. Development workflow

Work on only one milestone at a time.

Do not combine unrelated refactoring with feature implementation.

Do not perform broad cleanup outside the current task.

Do not automatically commit, push, rebase, reset, or rewrite Git history.

After every task:

1. list all changed files;
2. explain the implemented behavior;
3. distinguish copied, adapted, and newly written code;
4. list commands executed;
5. report test results;
6. report unresolved risks and TODOs;
7. verify that Hermes was not modified.

Use the smallest relevant test first, then run:

```bash
uv run pytest -q
uv run ruff check .
```

when practical.

## 9. Testing principles

Tests must not require a real paid model API unless explicitly marked as optional integration tests.

Use deterministic fake providers for core Agent Loop tests.

Reliability tests should verify observable invariants rather than private implementation details.

Important invariants include:

* one persisted logical message per client message ID;
* monotonically ordered messages within a session;
* no duplicated model request after successful completion;
* no duplicated tool result;
* no cross-session history;
* only one lease owner for the same session;
* checkpoint recovery equals full replay;
* corrupted checkpoints fall back safely;
* original messages remain unchanged after context compression.

## 10. Completion report format

Every completed milestone must end with:

### Changed files

### Implemented behavior

### Source relationship

### Tests executed

### Test results

### Remaining risks

### Suggested next milestone

Do not begin the next milestone automatically.
