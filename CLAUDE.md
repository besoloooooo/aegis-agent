# Aegis Agent Development Rules

## 1. Repository boundaries

There are three repositories in the current VS Code workspace:

Target repository:

```text
/home/nacha/aegis-agent
```

Reference repositories:

```text
/home/nacha/hermes-agent
/home/nacha/Claude-Code
```

The Hermes and Claude Code repositories are read-only reference source code.

Never modify, rename, delete, format, move, commit, or generate files under:

```text
/home/nacha/hermes-agent
/home/nacha/Claude-Code
```

All new source code, tests, scripts, configuration, and documentation must be created under:

```text
/home/nacha/aegis-agent
```

Before editing any file, verify that its absolute path belongs to the target repository.

Do not copy an entire reference repository by default.

Before migrating or adapting a feature, first inspect the relevant Hermes and/or Claude Code implementation and decide whether the lowest-risk and lowest-cost option is to:

* port a complete file, module, or cohesive directory;
* adapt selected code;
* combine relevant design ideas from the reference implementations;
* or reimplement the behavior in Aegis.

Whole-unit migration is allowed when it is the smallest coherent implementation unit, most of its dependency closure is relevant to the current milestone, and splitting it first would add unnecessary work or risk.

Do not migrate unrelated product features or large unrelated dependency trees.

## 2. Project identity

Project name:

```text
Aegis Agent
```

Repository name:

```text
aegis-agent
```

Python package:

```text
aegis_agent
```

CLI command:

```text
aegis
```

Project description:

Aegis Agent is a lightweight, recoverable, and extensible Agent Runtime built by extracting, simplifying, adapting, and modularizing useful runtime behavior and design patterns from Hermes and Claude Code while maintaining its own Python architecture.

## 3. Python environment

This project uses uv exclusively.

Allowed commands:

```text
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

## 4. Excluded scope

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
* every Hermes or Claude Code tool;
* every Hermes or Claude Code model provider;
* Hermes or Claude Code installation and distribution machinery;
* Hermes or Claude Code branding and product-specific UI;
* other product-specific features that are not required by the current Aegis milestone.

## 5. Architecture rules

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

* `ModelProvider`;
* `SessionRepository`;
* `LeaseBackend`;
* `ToolExecutor`;
* `ContextManager`;
* `SkillRouter`.

Original messages are the source of truth.

The following are derived structures and must never replace or overwrite the original message log:

* compressed model context;
* summaries;
* checkpoints;
* cached prompt views;
* tool-result previews.

Checkpoint corruption or incompatibility must fall back to full message replay.

Context compression must only affect the context sent to the model.

## 6. Migration policy

Before implementing a feature:

1. inspect the corresponding Hermes and/or Claude Code implementation when relevant;
2. identify its observable behavior;
3. identify its direct dependency closure;
4. identify which parts are generic runtime behavior and which parts are product-specific;
5. decide whether to port the complete implementation unit, adapt selected code, combine relevant ideas from multiple references, rewrite it, or drop it;
6. choose the option with the lowest total migration cost and risk while preserving the required Aegis architecture and current milestone boundaries;
7. implement only the behavior required by the current milestone;
8. write tests for that behavior.

Do not default to rewriting everything, and do not default to copying everything.

A complete Hermes or Claude Code file, module, or cohesive directory may be migrated when it is already a suitable bounded unit and most of its dependencies are needed.

When whole-unit migration is chosen:

* remove or isolate excluded product-specific dependencies only as required;
* keep the change within the current milestone;
* document why whole-unit migration was preferred;
* preserve applicable license and copyright notices.

Large coupled entry files may be migrated only when they are the smallest practical unit for the milestone.

Do not perform a broad architectural split unless it is required for correctness, testing, or the interfaces defined in this document.

Copied or substantially derived code must retain applicable license and copyright notices.

Record meaningful source relationships and migration decisions in:

```text
docs/source-map.md
```

The source map should distinguish whether behavior or code was derived from:

* Hermes;
* Claude Code;
* both reference repositories;
* or implemented independently in Aegis.

Maintain third-party attribution in:

```text
THIRD_PARTY_NOTICES.md
```

Do not describe copied or adapted Hermes or Claude Code code as completely original.

When only behavior or architectural ideas are referenced and the implementation is independently written for Aegis, document that distinction clearly.

## 7. Development workflow

Work on only one milestone at a time.

Small changes and incremental enhancements that belong to the current milestone do not count as a new milestone: they must not increment the milestone list in `README.md`.

If you believe a change warrants a new milestone number, ask the user for their opinion first, before assigning one.

Do not combine unrelated refactoring with feature implementation.

Do not perform broad cleanup outside the current task.

Do not automatically commit, push, rebase, reset, or rewrite Git history.

After every task:

* list all changed files;
* explain the implemented behavior;
* distinguish copied, adapted, and newly written code;
* identify whether Hermes, Claude Code, both, or neither were used as references;
* list commands executed;
* report test results;
* report unresolved risks and TODOs;
* update the development report described in Section 9;
* update `README.md` including the milestone list and any affected feature sections, not just `docs/source-map.md` and `docs/development-log.md`;
* whenever `README.md` changes, apply the same changes to the Chinese version `README.zh-CN.md` so both stay in sync;
* verify that neither reference repository was modified.

Use the smallest relevant test first, then run:

```text
uv run pytest -q
uv run ruff check .
```

when practical.

## 8. Testing principles

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

## 9. Development report

Maintain one cumulative interview-oriented development report at:

```text
docs/development-log.md
```

After every completed task or milestone, append a new section containing:

* task goal and the original problem;
* relevant Hermes and/or Claude Code behavior and source locations;
* why each reference implementation was relevant;
* migration decision: whole-unit port, adapted port, combined adaptation, rewrite, or new implementation;
* Aegis design, main data flow, and key interfaces;
* important files, classes, functions, tables, and fields;
* reliability invariants, edge cases, and failure handling;
* tests, fault injection or concurrency validation, and measured results;
* trade-offs, remaining limitations, and TODOs;
* a concise interview-ready explanation of what was done, why it was needed, how the reference implementations informed the design, and how the result was verified.

The report should explain meaningful technical decisions and behavior.

It does not need to be a line-by-line code changelog.

The milestone must also be reflected in `README.md`, including:

* the "Milestones delivered" list;
* the project layout when affected;
* relevant user-facing feature sections;
* relevant command sections.

Do not update only:

```text
docs/source-map.md
docs/development-log.md
README.zh-CN.md
```

## 10. Completion report format

Every completed milestone must end with:

### Changed files

### Implemented behavior

### Source relationship

Clearly identify whether the implementation is related to:

* Hermes;
* Claude Code;
* both;
* or neither.

### Tests executed

### Test results

### Development report

### README update

### Remaining risks

### Suggested next milestone

Do not begin the next milestone automatically.
