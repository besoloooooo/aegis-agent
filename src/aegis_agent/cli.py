"""Interactive command-line interface for Aegis Agent.

A minimal Typer app.  Running ``aegis`` starts an interactive REPL backed by
the fake model provider (Stage 1) and the builtin tools.  The CLI is a thin
shell: it parses arguments, feeds user input to :class:`AgentRuntime`, and
prints results.  All agent logic lives in the runtime; nothing here is
imported by the runtime (one-way dependency cli → runtime).
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import typer

from aegis_agent import __version__
from aegis_agent.env import load_dotenv
from aegis_agent.exceptions import AegisError
from aegis_agent.models.base import Message, Role
from aegis_agent.runtime import DEFAULT_MAX_ITERATIONS, AgentRuntime
from aegis_agent.tui import Tui

app = typer.Typer(add_completion=False, help="Aegis Agent — minimal interactive agent runtime.")

_EXIT_COMMANDS = {"exit", "quit", "/exit", "/quit", ":q"}

#: Default token budget for the derived model context (compression target).
DEFAULT_CONTEXT_MAX_TOKENS = 120_000


@app.callback(invoke_without_command=True)
def _main(
    ctx: typer.Context,
    session_id: str | None = typer.Option(
        None,
        "--session",
        "-s",
        help="Session id for this run (default: auto-generated timestamp+hex).",
    ),
    max_iterations: int = typer.Option(
        DEFAULT_MAX_ITERATIONS, "--max-iterations", "-n", help="Max model/tool iterations per turn."
    ),
    model_flag: str = typer.Option(
        "auto",
        "--model-backend",
        help="Which model backend to use: 'auto' (real if AEGIS_* is set, else fake), 'fake', or 'openai'.",
    ),
    allow_dangerous_shell: bool = typer.Option(
        False,
        "--allow-dangerous-shell",
        help="Operator-only: allow terminal to execute commands matching the dangerous list. Use with care.",
    ),
    skills_dir: str = typer.Option(
        None,
        "--skills-dir",
        help="Directory to load skills from (default: $AEGIS_SKILLS_DIR or ~/.aegis/skills).",
    ),
    no_skills: bool = typer.Option(False, "--no-skills", help="Disable skill loading and routing."),
    mcp_config: str = typer.Option(
        None,
        "--mcp-config",
        help="Path to MCP server config (default: ~/.aegis/config.yaml).",
    ),
    no_mcp: bool = typer.Option(False, "--no-mcp", help="Disable MCP server discovery."),
    no_memory: bool = typer.Option(
        False,
        "--no-memory",
        help="Disable personal long-term memory (USER.md / MEMORY.md injection).",
    ),
    memory_recall: bool = typer.Option(
        False,
        "--memory-recall",
        help="Enable relevance recall: surface relevant memories per turn via a side-query model.",
    ),
    memory_extract: bool = typer.Option(
        False,
        "--memory-extract",
        help="Enable background memory extraction after each final reply (writes to ~/.aegis/memory).",
    ),
    context_max_tokens: int | None = typer.Option(
        None,
        "--context-max-tokens",
        envvar="AEGIS_CONTEXT_MAX_TOKENS",
        help=f"Token budget for the model context; the derived context is compressed before each model call "
        f"(default: {DEFAULT_CONTEXT_MAX_TOKENS}).",
    ),
    no_compress: bool = typer.Option(
        False, "--no-compress", help="Disable context compression entirely."
    ),
    db_path: str | None = typer.Option(
        None,
        "--db",
        envvar="AEGIS_DB_PATH",
        help="SQLite session store path (default: ~/.aegis/state.db).",
    ),
    ephemeral: bool = typer.Option(
        False, "--ephemeral", help="Use the in-memory session store (nothing is persisted)."
    ),
    resume: str | None = typer.Option(
        None, "--resume", "-r", help="Resume an existing session id from the session store."
    ),
    no_lease: bool = typer.Option(
        False,
        "--no-lease",
        help="Disable the cross-process session lease (not recommended: two processes "
        "running the same session duplicate model requests and interleave history).",
    ),
    snapshot_every_n: int = typer.Option(
        20, "--snapshot-every-n", help="Write a fast-resume snapshot every N messages (0=off)."
    ),
    list_sessions: bool = typer.Option(
        False, "--list", "-l", help="List all recorded sessions and exit."
    ),
    version: bool = typer.Option(False, "--version", "-V", help="Show version and exit."),
) -> None:
    """Start the interactive Aegis Agent REPL (default action)."""
    if version:
        typer.echo(f"aegis {__version__}")
        raise typer.Exit()
    if ctx.invoked_subcommand is not None:
        return
    # Load project-local .env first, then the user-level ~/.aegis/.env
    # (mirrors Hermes' ~/.hermes/.env pattern for secrets like TAVILY_API_KEY).
    load_dotenv()
    load_dotenv(Path.home() / ".aegis" / ".env")
    try:
        provider, label = _select_provider(model_flag)
    except AegisError as exc:
        typer.echo(f"[error] {exc}")
        raise typer.Exit(code=1) from exc
    context_budget: int | None = None
    summary_provider = None
    if not no_compress:
        context_budget = context_max_tokens or DEFAULT_CONTEXT_MAX_TOKENS
        summary_provider = _build_summary_provider(provider)

    # ---- session store (persistence + resume) ---------------------------
    repository = _build_repository(db_path, ephemeral)
    if list_sessions:
        _print_session_list(repository)
        raise typer.Exit()
    session_id = resume or session_id or _new_session_id()
    if resume and repository.get_session(resume) is None:
        typer.echo(f"[error] session not found: {resume}")
        raise typer.Exit(code=1)

    # ---- cross-process session lease ------------------------------------
    # With the in-memory store there is no cross-process shared state, so a
    # lease has nothing to protect — skip it rather than falling back to the
    # default-path SQLite lock namespace.  An operator who explicitly sets
    # AEGIS_SESSION_LEASE_BACKEND still gets a lease (explicit intent wins).
    lease_manager = None
    lease_lost = threading.Event()
    lease_backend_env_set = bool(os.environ.get("AEGIS_SESSION_LEASE_BACKEND"))
    skip_lease = no_lease or (ephemeral and not lease_backend_env_set)
    if ephemeral and not no_lease and not lease_backend_env_set:
        typer.echo("[note] ephemeral store: session lease skipped "
                   "(nothing is shared across processes).")
    if not skip_lease:
        lease_manager = _start_lease(repository, session_id, lease_lost)
        if lease_manager is None:
            typer.echo(
                f"[error] session '{session_id}' is currently owned by another process. "
                "Wait for its lease to expire or use a different --session."
            )
            raise typer.Exit(code=1)

    runtime: AgentRuntime | None = None
    try:
        runtime = AgentRuntime.with_defaults(
            provider=provider,
            repository=repository,
            max_iterations=max_iterations,
            allow_dangerous_shell=allow_dangerous_shell,
            enable_skills=not no_skills,
            skills_dir=skills_dir,
            enable_mcp=not no_mcp,
            mcp_config_path=mcp_config,
            enable_memory=not no_memory,
            enable_memory_recall=memory_recall and not no_memory,
            enable_memory_extract=memory_extract and not no_memory,
            context_token_budget=context_budget,
            summary_provider=summary_provider,
        )
        tui = Tui()
        tui.banner(label=label, session_id=session_id, startup_info=runtime.startup_info)
        if resume:
            tui.say(f"Resumed session {session_id} "
                    f"({repository.message_count(session_id)} messages).")
            _print_resume_preview(repository, session_id)
        _repl(
            runtime,
            session_id,
            tui,
            interrupt=lease_lost if lease_manager is not None else None,
            snapshot_every_n=snapshot_every_n,
        )
    finally:
        # Wait for any in-flight background memory work (recall/extract) before
        # the process exits, mirroring Claude Code's drain-before-exit.
        if runtime is not None:
            runtime.shutdown()
        if lease_manager is not None:
            lease_manager.stop()
        # Snapshot final state before closing the store.
        show_resume = not ephemeral and not list_sessions
        msg_count = 0
        if show_resume:
            try:
                msg_count = repository.message_count(session_id)
            except Exception:  # noqa: BLE001 — session may not exist yet (empty REPL)
                show_resume = False
        close = getattr(repository, "close", None)
        if callable(close):
            close()
        if show_resume:
            typer.echo(
                f"\nSession {session_id} — {msg_count} messages.\n"
                f"Resume: aegis --resume {session_id}"
            )


def _select_provider(model_flag: str):
    """Resolve the model backend from the flag + environment.

    'openai' forces the OpenAI-compatible provider (errors if unconfigured);
    'fake' forces the deterministic fake; 'auto' picks the real provider when
    ``AEGIS_API_KEY`` and ``AEGIS_MODEL`` are set, otherwise the fake.  The
    fake is built with ``chunk_text=True`` so the streaming path is visible in
    the interactive demo (text arrives one character at a time).
    """
    from aegis_agent.models.openai_compat import ENV_API_KEY, ENV_MODEL

    want_real = model_flag == "openai" or (
        model_flag == "auto" and os.environ.get(ENV_API_KEY) and os.environ.get(ENV_MODEL)
    )
    if want_real:
        from aegis_agent.models.openai_compat import OpenAICompatibleProvider

        provider = OpenAICompatibleProvider.from_env()
        return provider, f"openai-compatible model '{provider.model}'"
    from aegis_agent.models.fake import FakeModelProvider

    return FakeModelProvider(chunk_text=True), "fake model"


def _build_summary_provider(provider):
    """Build the deterministic provider used for context-compression summaries.

    Returns ``None`` (fall back to the main provider) unless the main provider
    is OpenAI-compatible — in that case a sibling provider is built with
    ``temperature=0`` and the summary token budget pinned, mirroring the Hermes
    prototype's ``temperature=0.0, max_tokens=SUMMARY_MAX_TOKENS`` summary call.
    Construction failure (e.g. missing env) falls back to ``None`` so the CLI
    never fails to start over a summariser.
    """
    from aegis_agent.models.openai_compat import OpenAICompatibleProvider

    if not isinstance(provider, OpenAICompatibleProvider):
        return None
    from aegis_agent.context.compress_config import SUMMARY_MAX_TOKENS

    try:
        return OpenAICompatibleProvider.from_env(
            stream=False, temperature=0.0, max_tokens=SUMMARY_MAX_TOKENS
        )
    except AegisError:
        return None


def _new_session_id() -> str:
    """Auto-generate a session id (timestamp + random hex, like Hermes)."""
    import datetime
    import uuid

    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6]


def _build_repository(db_path: str | None, ephemeral: bool):
    """Resolve the session store: in-memory (--ephemeral) or SQLite (default)."""
    if ephemeral:
        from aegis_agent.sessions.memory_store import InMemorySessionRepository

        return InMemorySessionRepository()
    from aegis_agent.sessions.sqlite_store import SQLiteSessionRepository

    return SQLiteSessionRepository(db_path)


def _print_resume_preview(repository, session_id: str, exchanges: int = 4) -> None:
    """Print the last few exchanges so the user can see what was discussed.

    Mirrors Hermes' resumed-session preview: last N user messages and
    the corresponding assistant first line, showing enough to recognise
    the conversation without scrolling.
    """
    try:
        msgs = repository.list_messages(session_id)
    except Exception:  # noqa: BLE001 — preview failure must not block resume
        return
    # Build pairs of (user_msg, assistant_msg_or_none).
    pairs: list[tuple[Message, Message | None]] = []
    current_user = None
    current_assistant = None
    for m in msgs:
        if m.role is Role.USER:
            if current_user is not None:
                pairs.append((current_user, current_assistant))
            current_user = m
            current_assistant = None
        elif m.role is Role.ASSISTANT and current_assistant is None and m.content.strip():
            current_assistant = m
    if current_user is not None:
        pairs.append((current_user, current_assistant))
    # Show the last N exchanges.
    to_show = pairs[-exchanges:] if len(pairs) > exchanges else pairs
    if not to_show:
        return

    typer.echo("─" * 70)
    for user, assistant in to_show:
        u_text = user.content.strip()
        # Collapse to one line, truncate.
        u_line = u_text.replace("\n", " ")[:120]
        typer.echo(f"  you> {u_line}{'…' if len(u_text) > 120 else ''}")
        if assistant is not None:
            a_text = assistant.content.strip()
            a_line = a_text.replace("\n", " ")[:120]
            typer.echo(f"aegis> {a_line}{'…' if len(a_text) > 120 else ''}")
        typer.echo()
    typer.echo("─" * 70)
    """Print a human-readable session table and exit."""
    import datetime

    if not hasattr(repository, "list_sessions"):
        typer.echo("(this session store does not support listing)")
        return
    sessions = repository.list_sessions()
    if not sessions:
        typer.echo("(no sessions recorded)")
        return
    typer.echo(f"{'SESSION ID':<32} {'TITLE':<20} {'MSGS':>5}  {'CREATED'}")
    typer.echo("-" * 80)
    for s in sessions:
        sid = s["id"][:32]
        title = (s.get("title") or "")[:20]
        count = s.get("message_count", 0)
        ts = s.get("created_at")
        created = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "-"
        typer.echo(f"{sid:<32} {title:<20} {count:>5}  {created}")


def _start_lease(repository, session_id: str, lease_lost: threading.Event):
    """Acquire the cross-process session lease; None when it is already held.

    The lease backend comes from ``AEGIS_SESSION_LEASE_BACKEND`` (default
    sqlite, sharing the session store DB; redis when configured).  When the
    configured backend is unreachable the error is surfaced (never a silent
    fallback).  Callers using the in-memory store are expected to skip this
    entirely (see ``_main``) — an in-memory session has no cross-process
    shared state to protect.

    ``on_lost`` sets ``lease_lost`` — the event is passed to
    ``run_turn(interrupt=...)``, so a lost lease stops the agent loop at the
    next guard instead of risking dual writers.
    """
    from aegis_agent.sessions.lease import (
        SessionLeaseManager,
        SessionLeaseUnavailableError,
        get_lease_backend,
    )
    from aegis_agent.sessions.sqlite_store import SQLiteSessionRepository

    repo_for_lease = repository if isinstance(repository, SQLiteSessionRepository) else None
    try:
        backend = get_lease_backend(repo_for_lease)
    except SessionLeaseUnavailableError as exc:
        typer.echo(f"[error] {exc}")
        raise typer.Exit(code=1) from exc

    def _on_lost(lost_session: str) -> None:
        lease_lost.set()
        typer.echo(
            f"\n[warning] session lease for '{lost_session}' was lost — "
            "another process took over; stopping to avoid duplicate writes."
        )

    manager = SessionLeaseManager(backend, on_lost=_on_lost)
    if not manager.acquire(session_id):
        return None
    return manager


def _repl(
    runtime: AgentRuntime,
    session_id: str,
    tui: Tui,
    *,
    interrupt: threading.Event | None = None,
    snapshot_every_n: int = 20,
) -> None:
    """Read user lines, run turns, stream replies until an exit command / EOF."""
    while True:
        line = tui.prompt()
        if line is None:  # EOF / Ctrl-C
            break
        if not line:
            continue
        if line.lower() in _EXIT_COMMANDS:
            break
        turn_input = _maybe_route_skill(runtime, line, tui)
        try:
            state = tui.begin_turn()
            runtime.run_turn(
                session_id, turn_input, interrupt=interrupt, on_event=tui.on_event_factory(state)
            )
        except AegisError as exc:
            tui.say(f"[error] {exc}")
            continue
        _maybe_snapshot(runtime, session_id, snapshot_every_n)
    tui.bye()


def _maybe_snapshot(runtime: AgentRuntime, session_id: str, every_n: int) -> None:
    """Write a fast-resume snapshot when the store supports it (SQLite repo).

    Best-effort and cadence-gated (every N new messages); the full-replay
    resume path is always correct on its own, snapshots only skip re-decoding
    the snapshotted prefix.
    """
    if every_n <= 0:
        return
    maybe = getattr(runtime.repository, "maybe_write_snapshot", None)
    if callable(maybe):
        maybe(session_id, every_n=every_n)


def _maybe_route_skill(runtime: AgentRuntime, line: str, tui: Tui) -> str:
    """Expand a ``/skill-name [instruction]`` line into the skill's activation message.

    A leading ``/`` whose first token resolves to a known skill is replaced by
    the router's invocation message (activation note + skill body).  Anything
    else — including a ``/token`` that matches no skill — is passed through
    unchanged so the model sees exactly what the user typed.
    """
    router = runtime.skill_router
    if router is None or not line.startswith("/"):
        return line
    token, _, instruction = line[1:].partition(" ")
    skill = router.resolve(token)
    if skill is None:
        return line
    tui.say(f"[skill] activating '{skill.name}'")
    return router.invocation_message(skill, instruction)


def main() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
