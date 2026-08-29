# Codex Instructions for Aegis Agent

## Canonical project rules

For every task in this repository, read `CLAUDE.md` in full before doing
repository work and follow it as the canonical source of project rules.

Do not duplicate the contents of `CLAUDE.md` here. This keeps the project rules
used by Claude Code and Codex synchronized. If these instructions and
`CLAUDE.md` ever conflict, use this file only for Codex-specific delegation
behavior; otherwise, `CLAUDE.md` governs the project.

## Codex delegation strategy

This section is the compact Codex-facing form of
`/mnt/c/Users/nacha/.claude/rules/model-delegation.md`. Do not reread or copy
that global rule into every task unless it changes; this summary exists to
avoid repeatedly spending controller context on the same instructions.

The current Codex agent is the controller. Use a worker or subagent when the
task is context-heavy, relatively self-contained, and needs little user
interaction. Keep work in the controller when it requires difficult reasoning,
architectural judgment, frequent clarification, or a final high-confidence
decision.

Choose based on all three factors rather than task difficulty alone:

1. reasoning difficulty;
2. context volume;
3. expected interaction frequency.

When delegating:

- Give the worker a bounded, self-contained objective, repository path, known
  facts, constraints, expected output, and questions to answer.
- Send only the context needed for the task; do not copy the full conversation
  unless necessary.
- Ask for a compressed handoff containing conclusion, key evidence with file
  and line references, changed files, verification, risks or uncertainty, and
  any controller decision needed.
- Keep user-facing discussion and consequential architectural decisions in the
  controller.
- Escalate back to the controller when evidence conflicts, confidence is low,
  two attempts fail, or multiple high-impact solutions remain.
- Never pass secrets, tokens, credentials, or `.env` contents between agents.

There are two different worker choices; do not treat them as cost-equivalent:

- **WSL Claude CLI worker:** this is the token-saving route described by the
  global rule. Prefer it for large file/log/document inspection, broad search,
  mechanical work, test execution, and verification when the user has
  explicitly authorized external Claude delegation. Return only the compressed
  handoff above to the Codex controller.
- **Codex-native subagent:** use it when the user requests a Codex subagent or
  external Claude delegation is not authorized. It can reduce controller
  context, but it still consumes Codex capacity and is not the same cost-saving
  mechanism as the WSL worker.

Do not invoke an external model CLI or paid provider without explicit user
authorization. When no suitable authorized worker is available, or delegation
would not reduce context or latency, continue in the controller.
