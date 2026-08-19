"""Interactive command-line interface for Aegis Agent.

A minimal Typer app.  Running ``aegis`` starts an interactive REPL backed by
the fake model provider (Stage 1) and the builtin tools.  The CLI is a thin
shell: it parses arguments, feeds user input to :class:`AgentRuntime`, and
prints results.  All agent logic lives in the runtime; nothing here is
imported by the runtime (one-way dependency cli → runtime).
"""

from __future__ import annotations

import os
import signal
import sys
import threading
from pathlib import Path

import typer

from aegis_agent import __version__
from aegis_agent.env import load_dotenv
from aegis_agent.exceptions import AegisError
from aegis_agent.models.base import Message, Role
from aegis_agent.runtime import DEFAULT_MAX_ITERATIONS, AgentRuntime
from aegis_agent.slash_commands import (
    SlashHandler,
    SlashKind,
    WireCaptureProvider,
    format_session_table,
)
from aegis_agent.tui import Tui

app = typer.Typer(add_completion=False, help="Aegis Agent — minimal interactive agent runtime.")

_EXIT_COMMANDS = {"exit", "quit", "/exit", "/quit", ":q"}

#: Default token budget for the derived model context (compression target).
DEFAULT_CONTEXT_MAX_TOKENS = 120_000

#: True while a turn is executing.  A SIGINT during this window sets the
#: interrupt event (a graceful cancel) instead of raising KeyboardInterrupt,
#: which would otherwise tear down the whole process mid-turn.
_turn_active = False
_interrupt_event: threading.Event | None = None


def _sigint_handler(signum, frame):
    """Route Ctrl+C to the cooperative interrupt while a turn is running.

    When idle (at the prompt) the default behaviour is preserved: raise
    ``KeyboardInterrupt`` so ``Tui.prompt`` returns and the REPL exits.  While
    a turn is running, the first Ctrl+C sets the interrupt event (a graceful
    cancel); a second Ctrl+C raises ``KeyboardInterrupt`` as a force-quit
    escape hatch if the turn is stuck in a non-pollable wait.
    """
    if not _turn_active or _interrupt_event is None:
        raise KeyboardInterrupt
    if _interrupt_event.is_set():
        raise KeyboardInterrupt
    _interrupt_event.set()


def _install_sigint_handler(interrupt: threading.Event) -> None:
    """Install the Ctrl+C handler; a no-op when not on the main thread."""
    global _interrupt_event
    _interrupt_event = interrupt
    try:
        signal.signal(signal.SIGINT, _sigint_handler)
    except (ValueError, OSError):
        # Not the main thread (embedding host / test): leave the default
        # handler in place — graceful cancel is simply not wired up here.
        pass


@app.callback(invoke_without_command=True)
def _main(
    ctx: typer.Context,
    session_id: str | None = typer.Option(
        None,
        "--session",
        "-s",
        help="Session id for this run (default: auto-generated timestamp+hex).",
    ),
    max_iterations: int | None = typer.Option(
        None, "--max-iterations", "-n", help="Max model/tool iterations per turn."
    ),
    model_flag: str | None = typer.Option(
        None,
        "--model-backend",
        help="Which model backend to use: 'auto' (real if AEGIS_* is set, else fake), 'fake', or 'openai'.",
    ),
    allow_dangerous_shell: bool | None = typer.Option(
        None,
        "--allow-dangerous-shell/--no-allow-dangerous-shell",
        help="Operator-only: allow terminal to execute commands matching the dangerous list. Use with care.",
    ),
    skills_dir: str | None = typer.Option(
        None,
        "--skills-dir",
        help="Directory to load skills from (default: $AEGIS_SKILLS_DIR or ~/.aegis/skills).",
    ),
    no_skills: bool | None = typer.Option(
        None, "--no-skills/--skills", help="Disable skill loading and routing."
    ),
    mcp_config: str | None = typer.Option(
        None,
        "--mcp-config",
        help="Path to the config file (default: ~/.aegis/config.yaml). Holds both MCP servers and app settings.",
    ),
    no_mcp: bool | None = typer.Option(
        None, "--no-mcp/--mcp", help="Disable MCP server discovery."
    ),
    no_memory: bool | None = typer.Option(
        None,
        "--no-memory/--memory",
        help="Disable personal long-term memory (USER.md / MEMORY.md injection).",
    ),
    memory_recall: bool | None = typer.Option(
        None,
        "--memory-recall/--no-memory-recall",
        help="Surface relevant memories per turn via a side-query model (default: on).",
    ),
    memory_extract: bool | None = typer.Option(
        None,
        "--memory-extract/--no-memory-extract",
        help="Background-extract memories after each final reply (default: on).",
    ),
    project: str | None = typer.Option(
        None,
        "--project",
        help="Use project-scoped memory for the given project root (a bare '--project' uses the "
        "current directory). Absent → personal memory scope.",
    ),
    context_max_tokens: int | None = typer.Option(
        None,
        "--context-max-tokens",
        envvar="AEGIS_CONTEXT_MAX_TOKENS",
        help=f"Token budget for the model context; the derived context is compressed before each model call "
        f"(default: {DEFAULT_CONTEXT_MAX_TOKENS}).",
    ),
    no_compress: bool | None = typer.Option(
        None, "--no-compress/--compress", help="Disable context compression entirely."
    ),
    db_path: str | None = typer.Option(
        None,
        "--db",
        envvar="AEGIS_DB_PATH",
        help="SQLite session store path (default: ~/.aegis/state.db; in project scope: "
        "<project home>/state.db).",
    ),
    ephemeral: bool = typer.Option(
        False, "--ephemeral", help="Use the in-memory session store (nothing is persisted)."
    ),
    resume: str | None = typer.Option(
        None, "--resume", "-r", help="Resume an existing session id from the session store."
    ),
    no_lease: bool | None = typer.Option(
        None,
        "--no-lease/--lease",
        help="Disable the cross-process session lease (not recommended: two processes "
        "running the same session duplicate model requests and interleave history).",
    ),
    snapshot_every_n: int | None = typer.Option(
        None, "--snapshot-every-n", help="Write a fast-resume snapshot every N messages (0=off)."
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

    # ---- config file: ~/.aegis/config.yaml (same file as mcp_servers) --
    # Precedence: explicit CLI flag > config file > built-in default.
    from aegis_agent.config import (
        load_app_config,
        resolve_enabled,
        resolve_flag,
        resolve_value,
    )

    cfg = load_app_config(mcp_config)
    model_backend = resolve_value(model_flag, cfg, "model", "backend", "auto")
    allow_dangerous = resolve_flag(
        allow_dangerous_shell, cfg, "shell", "allow_dangerous", default=False
    )
    enable_skills = resolve_enabled(no_skills, cfg, "skills", "enabled", default=True)
    skills_dir_resolved = resolve_value(skills_dir, cfg, "skills", "dir", None)
    enable_mcp = resolve_enabled(no_mcp, cfg, "mcp", "enabled", default=True)
    enable_memory = resolve_enabled(no_memory, cfg, "memory", "enabled", default=True)
    # Recall / extract default ON; recall/extract are also suppressed when the
    # whole memory subsystem is disabled.
    enable_recall = resolve_flag(memory_recall, cfg, "memory", "recall", default=True) and enable_memory
    enable_extract = resolve_flag(memory_extract, cfg, "memory", "extract", default=True) and enable_memory
    project_resolved = resolve_value(project, cfg, "memory", "project", None)
    enable_compress = resolve_enabled(no_compress, cfg, "context", "compress", default=True)
    max_iter_resolved = resolve_value(max_iterations, cfg, "iterations", "max", DEFAULT_MAX_ITERATIONS)
    snapshot_n = resolve_value(snapshot_every_n, cfg, "session", "snapshot_every_n", 20)
    db_path_resolved = resolve_value(db_path, cfg, "session", "db_path", None)

    try:
        provider, label = _select_provider(model_backend)
    except AegisError as exc:
        typer.echo(f"[error] {exc}")
        raise typer.Exit(code=1) from exc
    context_budget: int | None = None
    summary_provider = None
    if enable_compress:
        context_budget = context_max_tokens or DEFAULT_CONTEXT_MAX_TOKENS
        summary_provider = _build_summary_provider(provider)

    # Wrap the provider so /chatlog can dump the exact derived message list
    # sent to the model on the most recent call (post system-prompt assembly,
    # post compression).  A single shallow list copy per call — cheap.  The
    # unwrapped provider is passed as the memory side-query backend so
    # recall/extract calls don't overwrite the conversation wire capture.
    wire = WireCaptureProvider(provider)
    inner_provider = provider
    provider = wire

    # ---- session store (persistence + resume) ---------------------------
    # Mirrors Claude Code: a session belongs to the scope it was started in.  In
    # project scope the default store lives next to the project memory
    # (<project home>/state.db) instead of the shared personal store, so
    # --resume / --list / session_search naturally bind to that project and a
    # project session can never leak into (or be resumed from) personal scope.
    # An explicit --db / AEGIS_DB_PATH / session.db_path always wins.
    db_path_resolved = _scoped_db_path(db_path_resolved, project_resolved)
    repository = _build_repository(db_path_resolved, ephemeral)
    if list_sessions:
        _print_session_list(repository)
        raise typer.Exit()
    session_id = resume or session_id or _new_session_id()
    if resume and repository.get_session(resume) is None:
        typer.echo(f"[error] session not found: {resume}")
        hint = _session_scope_hint(resume, project_resolved)
        if hint:
            typer.echo(hint)
        raise typer.Exit(code=1)
    if not ephemeral:
        # Persist the session row up front.  The row is otherwise created
        # lazily by the first run_turn, so exiting without sending a message
        # left nothing on disk and the printed "Resume: aegis --resume <id>"
        # hint failed with "session not found".  Idempotent (INSERT OR
        # IGNORE), so a resumed session keeps its original title/created_at.
        repository.create_session(session_id)

    # ---- cooperative interrupt (Ctrl+C / lease loss) --------------------
    # ``interrupt`` is the transient cancel signal: the SIGINT handler sets it
    # while a turn is running (instead of the default KeyboardInterrupt, which
    # would kill the process) and the REPL clears it before the next turn.
    # ``lease_lost`` is the *terminal* signal set when another process takes
    # the session lease — it also sets ``interrupt`` (to stop the in-flight
    # turn) but is never cleared, so subsequent turns keep stopping instead of
    # risking dual writers.  The runtime polls ``interrupt`` between model
    # calls and — via ToolContext.is_cancelled — inside long-running tools.
    interrupt = threading.Event()
    lease_lost = threading.Event()
    _install_sigint_handler(interrupt)

    # ---- cross-process session lease ------------------------------------
    # With the in-memory store there is no cross-process shared state, so a
    # lease has nothing to protect — skip it rather than falling back to the
    # default-path SQLite lock namespace.  An operator who explicitly sets
    # AEGIS_SESSION_LEASE_BACKEND still gets a lease (explicit intent wins).
    enable_lease = resolve_enabled(no_lease, cfg, "session", "lease", default=True)
    lease_manager = None
    lease_backend_env_set = bool(os.environ.get("AEGIS_SESSION_LEASE_BACKEND"))
    skip_lease = not enable_lease or (ephemeral and not lease_backend_env_set)
    if ephemeral and enable_lease and not lease_backend_env_set:
        typer.echo("[note] ephemeral store: session lease skipped "
                   "(nothing is shared across processes).")
    if not skip_lease:
        lease_manager = _start_lease(repository, session_id, interrupt, lease_lost)
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
            max_iterations=max_iter_resolved,
            allow_dangerous_shell=allow_dangerous,
            enable_skills=enable_skills,
            skills_dir=skills_dir_resolved,
            enable_mcp=enable_mcp,
            mcp_config_path=mcp_config,
            enable_memory=enable_memory,
            enable_memory_recall=enable_recall,
            enable_memory_extract=enable_extract,
            memory_side_provider=inner_provider,
            memory_project=project_resolved,
            context_token_budget=context_budget,
            summary_provider=summary_provider,
        )
        tui = Tui()
        tui.banner(label=label, session_id=session_id, startup_info=runtime.startup_info)
        if resume:
            tui.say(f"Resumed session {session_id} "
                    f"({repository.message_count(session_id)} messages).")
            _print_resume_preview(repository, session_id)

        # ---- slash commands ----------------------------------------------
        # Session rotation (/new, /clear): create the fresh session row, then
        # migrate the lease with switch_session (acquire-new-then-release-old,
        # so a failed acquire keeps the old lease).  A successful rotation
        # clears the terminal lease_lost marker — the new session is ours.
        def _rotate_session(title: str | None) -> str | None:
            nonlocal session_id
            new_id = _new_session_id()
            repository.create_session(new_id, title=title)
            if lease_manager is not None and not lease_manager.switch_session(new_id):
                return None
            session_id = new_id
            lease_lost.clear()
            interrupt.clear()
            return new_id

        slash = SlashHandler(
            runtime=runtime,
            repository=repository,
            emit=tui.out,
            session_id=session_id,
            wire=wire,
            rotate_session=_rotate_session,
            clear_screen=tui.clear_screen,
        )
        _repl(
            runtime,
            slash,
            tui,
            interrupt=interrupt,
            lease_lost=lease_lost,
            snapshot_every_n=snapshot_n,
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
            # The session lives in the scope it was started in, so the resume
            # command must carry the same --project to find it again.
            project_flag = f" --project {project_resolved}" if project_resolved else ""
            typer.echo(
                f"\nSession {session_id} — {msg_count} messages.\n"
                f"Resume: aegis{project_flag} --resume {session_id}"
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


def _scoped_db_path(db_path: str | None, project: str | None) -> str | None:
    """Derive the session store path, scoping it to the project when active.

    An explicit ``db_path`` (``--db`` / ``AEGIS_DB_PATH`` / ``session.db_path``)
    always wins.  Otherwise, in project scope the store defaults to
    ``<project home>/state.db`` (mirroring Claude Code's per-project session
    storage) so a project's sessions live beside its memory; with no project it
    stays ``None`` → the shared personal store default.
    """
    if db_path is not None:
        return db_path
    if project is None:
        return None
    from aegis_agent.memory.paths import project_home

    return str(project_home(project) / "state.db")


def _session_scope_hint(session_id: str, project: str | None) -> str | None:
    """Best-effort hint when a resumed session is absent from the active scope.

    Sessions live in the store of the scope that created them, so a project
    session is invisible to personal scope and vice-versa.  This looks for the
    session in the *other* scope's store(s) (read-only) and, if found, tells the
    user which scope to resume it from.  Returns ``None`` when it cannot be
    located (the plain "session not found" error stands alone).
    """
    from aegis_agent.sessions.sqlite_store import DEFAULT_DB_PATH

    def _contains(db_path: Path) -> bool:
        import sqlite3

        if not db_path.is_file():
            return False
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        except sqlite3.Error:
            return False
        try:
            row = conn.execute(
                "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            return row is not None
        except sqlite3.Error:
            return False
        finally:
            conn.close()

    if project is not None:
        # Active scope is a project; the session may live in the personal store.
        if _contains(DEFAULT_DB_PATH):
            return (
                f"[hint] session '{session_id}' lives in personal scope — "
                "resume it without --project."
            )
        return None

    # Active scope is personal; the session may live in some project store.
    from aegis_agent.memory.paths import projects_dir

    base = projects_dir()
    if not base.is_dir():
        return None
    for proj_dir in sorted(base.iterdir()):
        candidate = proj_dir / "state.db"
        if _contains(candidate):
            return (
                f"[hint] session '{session_id}' lives in a project scope — "
                f"resume it with --project (project id '{proj_dir.name}')."
            )
    return None


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


def _print_session_list(repository) -> None:
    """Print a human-readable session table and exit."""
    if not hasattr(repository, "list_sessions"):
        typer.echo("(this session store does not support listing)")
        return
    typer.echo("\n".join(format_session_table(repository.list_sessions())))


def _start_lease(
    repository,
    session_id: str,
    interrupt: threading.Event,
    lease_lost: threading.Event,
):
    """Acquire the cross-process session lease; None when it is already held.

    The lease backend comes from ``AEGIS_SESSION_LEASE_BACKEND`` (default
    sqlite, sharing the session store DB; redis when configured).  When the
    configured backend is unreachable the error is surfaced (never a silent
    fallback).  Callers using the in-memory store are expected to skip this
    entirely (see ``_main``) — an in-memory session has no cross-process
    shared state to protect.

    ``on_lost`` sets both ``interrupt`` (to stop the in-flight turn at the
    next guard) and ``lease_lost`` (a terminal marker the REPL never clears),
    so a lost lease stops the agent loop and keeps it stopped instead of
    risking dual writers.
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
        interrupt.set()
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
    slash: SlashHandler,
    tui: Tui,
    *,
    interrupt: threading.Event | None = None,
    lease_lost: threading.Event | None = None,
    snapshot_every_n: int = 20,
) -> None:
    """Read user lines, run turns, stream replies until an exit command / EOF.

    A line starting with ``/`` is first offered to the slash-command handler
    (``/save``, ``/chatlog``, ``/new``, …).  Unrecognised ``/tokens`` fall
    through to skill routing and then to the model unchanged.  ``/retry``
    re-queues the last user message as a fresh turn; ``/undo`` prefills the
    next composer with the backed-up text.
    """
    global _turn_active
    prefill = ""
    while True:
        line = tui.prompt(default=prefill)
        prefill = ""
        if line is None:  # EOF / Ctrl-C
            break
        if not line:
            continue
        if line.lower() in _EXIT_COMMANDS:
            break
        if line.startswith("/"):
            result = slash.handle(line)
            if result is not None:
                if result.kind is SlashKind.EXIT:
                    break
                if result.kind is SlashKind.HANDLED:
                    prefill = result.prefill
                    continue
                turn_input = result.text  # REQUEUE (/retry)
                if not turn_input:
                    continue
            else:
                turn_input = _maybe_route_skill(runtime, line, tui)
        else:
            turn_input = _maybe_route_skill(runtime, line, tui)
        session_id = slash.session_id
        try:
            state = tui.begin_turn()
            # Clear a transient Ctrl+C from a previous turn so it doesn't
            # instantly interrupt the next message.  A lost lease is terminal:
            # its event stays set (and keeps ``interrupt`` set) so we never
            # resume writing to a session another process now owns.
            if interrupt is not None and not (
                lease_lost is not None and lease_lost.is_set()
            ):
                interrupt.clear()
            # Mark the turn active so a SIGINT here cancels cooperatively
            # (via the interrupt event) rather than raising KeyboardInterrupt.
            _turn_active = True
            try:
                runtime.run_turn(
                    session_id, turn_input, interrupt=interrupt, on_event=tui.on_event_factory(state)
                )
            finally:
                _turn_active = False
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


def _normalize_project_flag(argv: list[str] | None = None) -> list[str]:
    """Rewrite a bare ``--project`` (no value) into ``--project <cwd>``.

    Typer/Click cannot express an option with an *optional* value (its
    ``flag_value`` path is dropped by Typer), so ``--project`` is a normal
    required-value option.  To keep the ergonomic "``--project`` with no value
    means the current directory" behaviour, a leading bare ``--project`` is
    expanded here before the Typer app parses ``argv``.  ``--project PATH`` and
    an absent ``--project`` are left untouched.  Returns the (possibly
    unchanged) argument list; ``main`` assigns the result back to ``sys.argv``.
    """
    args = list(sys.argv if argv is None else argv)
    for i, token in enumerate(args):
        if token != "--project":
            continue
        next_is_value = i + 1 < len(args) and not args[i + 1].startswith("-")
        if next_is_value:
            continue
        args.insert(i + 1, os.getcwd())
        return args
    return args


def main() -> None:
    """Console-script entry point."""
    sys.argv = _normalize_project_flag()
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
