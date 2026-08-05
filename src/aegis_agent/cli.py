"""Interactive command-line interface for Aegis Agent.

A minimal Typer app.  Running ``aegis`` starts an interactive REPL backed by
the fake model provider (Stage 1) and the builtin tools.  The CLI is a thin
shell: it parses arguments, feeds user input to :class:`AgentRuntime`, and
prints results.  All agent logic lives in the runtime; nothing here is
imported by the runtime (one-way dependency cli → runtime).
"""

from __future__ import annotations

import os

import typer

from aegis_agent import __version__
from aegis_agent.env import load_dotenv
from aegis_agent.exceptions import AegisError
from aegis_agent.runtime import DEFAULT_MAX_ITERATIONS, AgentRuntime
from aegis_agent.tui import Tui

app = typer.Typer(add_completion=False, help="Aegis Agent — minimal interactive agent runtime.")

_EXIT_COMMANDS = {"exit", "quit", "/exit", "/quit", ":q"}


@app.callback(invoke_without_command=True)
def _main(
    ctx: typer.Context,
    session_id: str = typer.Option("default", "--session", "-s", help="Session id for this run."),
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
        help="Operator-only: allow run_shell to execute commands matching the dangerous list. Use with care.",
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
    version: bool = typer.Option(False, "--version", "-V", help="Show version and exit."),
) -> None:
    """Start the interactive Aegis Agent REPL (default action)."""
    if version:
        typer.echo(f"aegis {__version__}")
        raise typer.Exit()
    if ctx.invoked_subcommand is not None:
        return
    # Load a project-local .env (if any) before backend auto-detection, so
    # AEGIS_* saved there is picked up without exporting it each run.
    load_dotenv()
    try:
        provider, label = _select_provider(model_flag)
    except AegisError as exc:
        typer.echo(f"[error] {exc}")
        raise typer.Exit(code=1) from exc
    runtime = AgentRuntime.with_defaults(
        provider=provider,
        max_iterations=max_iterations,
        allow_dangerous_shell=allow_dangerous_shell,
        enable_skills=not no_skills,
        skills_dir=skills_dir,
        enable_mcp=not no_mcp,
        mcp_config_path=mcp_config,
    )
    tui = Tui()
    tui.banner(label=label, session_id=session_id, startup_info=runtime.startup_info)
    _repl(runtime, session_id, tui)


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


def _repl(runtime: AgentRuntime, session_id: str, tui: Tui) -> None:
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
            runtime.run_turn(session_id, turn_input, on_event=tui.on_event_factory(state))
        except AegisError as exc:
            tui.say(f"[error] {exc}")
            continue
    tui.bye()


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
