# Portions adapted from Hermes (hermes-agent), © 2025 Nous Research.
# Licensed under the MIT License. See THIRD_PARTY_NOTICES.md.
#
#   * The ``CommandDef`` registry pattern and ``resolve_command`` are ADAPTED
#     from ``hermes_cli/commands.py`` (central slash-command registry).
#     Hermes' registry also feeds gateway/Telegram/Slack dispatch; Aegis keeps
#     only the interactive-CLI subset (gateway commands are out of scope).
#   * ``/save`` behaviour is ADAPTED from ``cli.py:HermesCLI.dump_chatlog``
#     (NN-prefixed local/wire/system debug dumps; ``/chatlog`` is kept as an
#     alias).  Hermes captures wire messages by monkey-patching the agent;
#     Aegis uses an explicit ``WireCaptureProvider`` wrapper instead.
#   * ``/history`` rendering is ADAPTED from ``cli.py:HermesCLI.show_history``
#     (tool messages collapsed into a summary line, 400-char previews).
#   * ``/retry`` / ``/undo`` are ADAPTED from ``cli.py:HermesCLI.retry_last`` /
#     ``undo_last`` (walk back to the Nth-from-last user message, truncate,
#     soft-delete on disk).  Hermes' memory-provider notify and agent-surgery
#     steps have no Aegis counterpart yet and are intentionally omitted.
"""Interactive slash commands for the Aegis CLI.

This module owns everything about ``/command`` lines typed at the REPL:

* :data:`COMMAND_REGISTRY` — the single source of truth for which commands
  exist, their aliases, and their one-line descriptions (``/help`` renders
  straight from it).
* :func:`resolve_command` — name/alias lookup.
* :class:`WireCaptureProvider` — a ``ModelProvider`` wrapper that records the
  exact derived message list sent to the model on the most recent call, so
  ``/save`` can dump what actually went over the wire.
* :class:`SlashHandler` — the dispatcher.  It is deliberately UI-agnostic: all
  output goes through an ``emit`` callable and session rotation through a
  ``rotate_session`` callback, so the handler is testable without Typer, Rich,
  or a TTY.  The runtime never imports this module (one-way cli → runtime).

Out of scope (Hermes product features, see CLAUDE.md §5): gateway/messaging
commands (``/platforms``, ``/handoff``, ``/approve``…), ``/model``, ``/cron``,
``/kanban``, browser/voice/image commands, skins, and auto-update.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

from aegis_agent.models.base import Message, ModelProvider, Role, ToolDefinition


def _local_now() -> datetime:
    """Timezone-aware 'now' in local time (ruff DTZ-safe)."""
    return datetime.now(UTC).astimezone()


def _local_fromtimestamp(ts: float) -> datetime:
    """Timezone-aware local datetime from a POSIX timestamp."""
    return datetime.fromtimestamp(ts, tz=UTC).astimezone()

# ---------------------------------------------------------------------------
# Command registry (adapted from hermes_cli/commands.py)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommandDef:
    """Definition of a single slash command."""

    name: str                          # canonical name without slash: "save"
    description: str                   # human-readable description
    category: str                      # "Session", "Debug", etc.
    aliases: tuple[str, ...] = ()      # alternative names: ("reset",)
    args_hint: str = ""                # argument placeholder: "[name]"


COMMAND_REGISTRY: list[CommandDef] = [
    # Session
    CommandDef("new", "Start a new session (fresh session ID + history)", "Session",
               aliases=("reset",), args_hint="[name]"),
    CommandDef("clear", "Clear screen and start a new session", "Session"),
    CommandDef("history", "Show conversation history", "Session"),
    CommandDef("save", "Dump conversation snapshots (local/wire/system) to aegis-chat-logs/", "Session",
               aliases=("chatlog",)),
    CommandDef("retry", "Retry the last message (resend to the agent)", "Session"),
    CommandDef("undo", "Back up N user turns and prefill the composer (default 1)", "Session",
               args_hint="[N]"),
    CommandDef("title", "Set or show the current session title", "Session",
               args_hint="[name]"),
    CommandDef("sessions", "List recorded sessions", "Session"),
    # Meta
    CommandDef("help", "Show this command list", "Meta"),
    CommandDef("exit", "Quit the REPL", "Meta", aliases=("quit",)),
]

_COMMAND_LOOKUP: dict[str, CommandDef] = {}
for _cmd in COMMAND_REGISTRY:
    _COMMAND_LOOKUP[_cmd.name] = _cmd
    for _alias in _cmd.aliases:
        _COMMAND_LOOKUP[_alias] = _cmd


def resolve_command(name: str) -> CommandDef | None:
    """Resolve a command name or alias to its :class:`CommandDef`.

    Accepts names with or without the leading slash.
    """
    return _COMMAND_LOOKUP.get(name.lower().lstrip("/"))


def help_lines() -> list[str]:
    """Render the registry grouped by category (drives ``/help``)."""
    lines: list[str] = []
    seen: set[str] = set()
    for cmd in COMMAND_REGISTRY:
        if cmd.category not in seen:
            seen.add(cmd.category)
            if lines:
                lines.append("")
            lines.append(f"{cmd.category}:")
        usage = f"/{cmd.name}" + (f" {cmd.args_hint}" if cmd.args_hint else "")
        alias_note = f" (aliases: {', '.join('/' + a for a in cmd.aliases)})" if cmd.aliases else ""
        lines.append(f"  {usage:<22} {cmd.description}{alias_note}")
    return lines


# ---------------------------------------------------------------------------
# Title sanitisation (adapted from hermes_state.SessionDB.sanitize_title)
# ---------------------------------------------------------------------------

MAX_TITLE_LENGTH = 60


def sanitize_title(raw: str) -> str:
    """Normalise a user-supplied session title; ``""`` means invalid.

    Strips control/non-printable characters, collapses whitespace, and caps
    the length so titles stay one-line and safe to echo in lists.
    """
    cleaned = "".join(ch for ch in raw if ch.isprintable())
    cleaned = " ".join(cleaned.split())
    return cleaned[:MAX_TITLE_LENGTH]


# ---------------------------------------------------------------------------
# Wire capture (replaces Hermes' monkey-patch on agent._build_api_kwargs)
# ---------------------------------------------------------------------------


class WireCaptureProvider:
    """``ModelProvider`` wrapper recording the most recent outbound messages.

    Every ``stream()`` call snapshots the *derived* message list it receives
    (a shallow list copy — the same cost profile as Hermes' capture, which
    deliberately avoids deepcopy on the hot path) plus a timestamp.  ``/chatlog``
    reads :attr:`last_messages` to dump exactly what went to the provider,
    post system-prompt assembly and post compression.
    """

    def __init__(self, inner: ModelProvider) -> None:
        self._inner = inner
        self.last_messages: list[Message] | None = None
        self.captured_at: float | None = None

    @property
    def name(self) -> str:
        return self._inner.name

    def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition] | None = None,
    ) -> Iterator:
        self.last_messages = list(messages)
        self.captured_at = time.time()
        return self._inner.stream(messages, tools=tools)


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


class SlashKind(str, Enum):
    """What the REPL should do after a command runs."""

    HANDLED = "handled"    # command fully handled; back to the prompt
    REQUEUE = "requeue"    # run ``text`` as a user turn (e.g. /retry)
    EXIT = "exit"          # leave the REPL


@dataclass
class SlashResult:
    kind: SlashKind
    text: str = ""         # REQUEUE payload: the user message to re-run
    prefill: str = ""      # composer prefill for the next prompt (/undo)


def default_chatlog_dir() -> Path:
    return Path.cwd() / "aegis-chat-logs"


def message_to_dict(m: Message) -> dict:
    """Serialise a message for export (``/save`` and ``/chatlog`` payloads)."""
    d: dict = {"role": m.role.value, "content": m.content}
    if m.tool_calls:
        d["tool_calls"] = [
            {"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in m.tool_calls
        ]
    if m.tool_call_id is not None:
        d["tool_call_id"] = m.tool_call_id
    if m.name is not None:
        d["name"] = m.name
    if m.reasoning_content:
        d["reasoning_content"] = m.reasoning_content
    if m.seq is not None:
        d["seq"] = m.seq
    if m.client_msg_id is not None:
        d["client_msg_id"] = m.client_msg_id
    return d


def format_session_table(sessions: list[dict]) -> list[str]:
    """Render the session list shared by ``/sessions`` and ``aegis --list``."""
    if not sessions:
        return ["(no sessions recorded)"]
    lines = [f"{'SESSION ID':<32} {'TITLE':<20} {'MSGS':>5}  CREATED", "-" * 80]
    for s in sessions:
        sid = str(s["id"])[:32]
        title = str(s.get("title") or "")[:20]
        count = s.get("message_count", 0)
        ts = s.get("created_at")
        created = _local_fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "-"
        lines.append(f"{sid:<32} {title:<20} {count:>5}  {created}")
    return lines


class SlashHandler:
    """Dispatch ``/command`` lines against the live session.

    All interaction with the outside world is injected: ``emit`` for output,
    ``rotate_session`` for ``/new``/``/clear`` (the CLI owns lease rotation and
    returns the fresh session id), ``clear_screen`` for ``/clear``.  The
    handler mutates only its own ``session_id`` plus whatever the repository
    methods do — the runtime is only *read* (history, system prompt, provider
    name) except through persistence-agnostic repository calls.
    """

    def __init__(
        self,
        *,
        runtime,
        repository,
        emit: Callable[[str], None],
        session_id: str,
        wire: WireCaptureProvider | None = None,
        rotate_session: Callable[[str | None], str | None] | None = None,
        clear_screen: Callable[[], None] | None = None,
        chatlog_dir: Path | None = None,
    ) -> None:
        self._runtime = runtime
        self._repository = repository
        self._emit = emit
        self.session_id = session_id
        self._wire = wire
        self._rotate_session = rotate_session
        self._clear_screen = clear_screen
        self._chatlog_dir = chatlog_dir or default_chatlog_dir()

    # -- entry point -------------------------------------------------------

    def handle(self, line: str) -> SlashResult | None:
        """Handle a ``/`` line; ``None`` when it is not a registered command.

        Unknown ``/tokens`` return ``None`` so the caller can fall through to
        skill routing and then to the model, exactly as if no command layer
        existed.
        """
        token, _, rest = line[1:].partition(" ")
        cmd = resolve_command(token)
        if cmd is None:
            return None
        arg = rest.strip()
        handler = getattr(self, f"_cmd_{cmd.name}")
        return handler(arg)

    # -- helpers -----------------------------------------------------------

    def _messages(self) -> list[Message]:
        try:
            return self._repository.list_messages(self.session_id)
        except Exception:  # noqa: BLE001 — a missing session reads as empty
            return []

    def _new_session(self, title: str | None) -> None:
        if self._rotate_session is None:
            self._emit("(session rotation is not available in this context)")
            return
        new_id = self._rotate_session(title)
        if new_id is None:
            self._emit("[error] could not acquire the lease for the new session; "
                       "staying on the current one.")
            return
        self.session_id = new_id
        self._emit(f"✨ Fresh start! New session: {new_id}")

    # -- Session commands --------------------------------------------------

    def _cmd_new(self, arg: str) -> SlashResult:
        self._new_session(arg or None)
        return SlashResult(SlashKind.HANDLED)

    def _cmd_clear(self, arg: str) -> SlashResult:
        if self._clear_screen is not None:
            self._clear_screen()
        self._new_session(None)
        return SlashResult(SlashKind.HANDLED)

    def _cmd_history(self, arg: str) -> SlashResult:
        messages = self._messages()
        if not messages:
            self._emit("(._.) No conversation history yet.")
            return SlashResult(SlashKind.HANDLED)

        preview_limit = 400
        visible_index = 0
        hidden_tool_messages = 0

        def flush_tool_summary(out: list[str]) -> None:
            nonlocal hidden_tool_messages
            if hidden_tool_messages:
                noun = "message" if hidden_tool_messages == 1 else "messages"
                out.append("\n  [Tools]")
                out.append(f"    ({hidden_tool_messages} tool {noun} hidden)")
                hidden_tool_messages = 0

        out = ["", "+" + "-" * 50 + "+",
               "|" + " " * 13 + "Conversation History" + " " * 13 + "|",
               "+" + "-" * 50 + "+"]
        for msg in messages:
            if msg.role is Role.TOOL:
                hidden_tool_messages += 1
                continue
            if msg.role not in (Role.USER, Role.ASSISTANT):
                continue
            flush_tool_summary(out)
            visible_index += 1
            content = msg.content or ""
            if msg.role is Role.USER:
                out.append(f"\n  [You #{visible_index}]")
                out.append(f"    {content[:preview_limit]}"
                           f"{'...' if len(content) > preview_limit else ''}")
                continue
            out.append(f"\n  [Aegis #{visible_index}]")
            if content:
                preview = content[:preview_limit]
                suffix = "..." if len(content) > preview_limit else ""
            elif msg.tool_calls:
                count = len(msg.tool_calls)
                noun = "call" if count == 1 else "calls"
                preview, suffix = f"(requested {count} tool {noun})", ""
            else:
                preview, suffix = "(no text response)", ""
            out.append(f"    {preview}{suffix}")
        flush_tool_summary(out)
        out.append("")
        self._emit("\n".join(out))
        return SlashResult(SlashKind.HANDLED)

    def _cmd_save(self, arg: str) -> SlashResult:
        """``/save`` is Hermes' ``/chatlog`` behaviour (``/chatlog`` aliases it)."""
        return self._dump_chatlog()

    def _cmd_retry(self, arg: str) -> SlashResult:
        messages = self._messages()
        if not messages:
            self._emit("(._.) No messages to retry.")
            return SlashResult(SlashKind.HANDLED)
        last_user = next((m for m in reversed(messages) if m.role is Role.USER), None)
        if last_user is None:
            self._emit("(._.) No user message found to retry.")
            return SlashResult(SlashKind.HANDLED)
        if not self._rewind(last_user):
            return SlashResult(SlashKind.HANDLED)
        text = last_user.content or ""
        self._emit(f"(^_^)b Retrying: \"{text[:60]}{'...' if len(text) > 60 else ''}\"")
        return SlashResult(SlashKind.REQUEUE, text=text)

    def _cmd_undo(self, arg: str) -> SlashResult:
        n = 1
        if arg:
            try:
                n = int(arg)
            except ValueError:
                self._emit(f"(._.) Invalid count {arg!r} — use /undo or /undo N.")
                return SlashResult(SlashKind.HANDLED)
        n = max(n, 1)
        messages = self._messages()
        if not messages:
            self._emit("(._.) No messages to undo.")
            return SlashResult(SlashKind.HANDLED)
        # Walk backwards collecting the last N user messages; the oldest of
        # them is the truncation point (its assistant reply and tool messages
        # go with it).
        user_msgs = [m for m in messages if m.role is Role.USER]
        if not user_msgs:
            self._emit("(._.) No user message found to undo.")
            return SlashResult(SlashKind.HANDLED)
        target = user_msgs[-min(n, len(user_msgs))]
        removed = len(messages) - messages.index(target)
        text = target.content or ""
        if not self._rewind(target):
            return SlashResult(SlashKind.HANDLED)
        turns = min(n, len(user_msgs))
        noun = "turn" if turns == 1 else "turns"
        self._emit(f"(^_^)b Undid {turns} {noun} ({removed} messages removed; "
                   "soft-deleted on disk for audit).")
        return SlashResult(SlashKind.HANDLED, prefill=text)

    def _rewind(self, target: Message) -> bool:
        """Soft-delete ``target`` and everything after it.  False on failure."""
        rewind = getattr(self._repository, "rewind_from_seq", None)
        if not callable(rewind) or target.seq is None:
            self._emit("(this session store does not support undo)")
            return False
        rewind(self.session_id, target.seq)
        return True

    def _cmd_title(self, arg: str) -> SlashResult:
        if not arg:
            session = self._repository.get_session(self.session_id)
            title = session.title if session else None
            self._emit(f"  Session ID: {self.session_id}")
            self._emit(f"  Title: {title}" if title else
                       "  No title set. Usage: /title <your session title>")
            return SlashResult(SlashKind.HANDLED)
        title = sanitize_title(arg)
        if not title:
            self._emit("  Title is empty after cleanup. Please use printable characters.")
            return SlashResult(SlashKind.HANDLED)
        set_title = getattr(self._repository, "set_session_title", None)
        if not callable(set_title):
            self._emit("(this session store does not support titles)")
            return SlashResult(SlashKind.HANDLED)
        if self._repository.get_session(self.session_id) is None:
            # Session not created yet (no messages sent) — create it with the
            # title so nothing is lost.
            self._repository.create_session(self.session_id, title=title)
            self._emit(f"  Session title set: {title}")
        elif set_title(self.session_id, title):
            self._emit(f"  Session title set: {title}")
        else:
            self._emit("  Session not found in the store.")
        return SlashResult(SlashKind.HANDLED)

    def _cmd_sessions(self, arg: str) -> SlashResult:
        list_sessions = getattr(self._repository, "list_sessions", None)
        if not callable(list_sessions):
            self._emit("(this session store does not support listing)")
            return SlashResult(SlashKind.HANDLED)
        self._emit("\n".join(format_session_table(list_sessions())))
        return SlashResult(SlashKind.HANDLED)

    # -- Debug commands ----------------------------------------------------

    def _dump_chatlog(self) -> SlashResult:
        """Dump local/wire/system snapshots with a shared numeric prefix.

        Ported behaviour (Hermes ``dump_chatlog``): ``NN-local.json`` is the
        local history; ``NN-wire.json`` is the most recent outbound message
        list (post system-prompt assembly, post compression) captured by
        :class:`WireCaptureProvider`; ``NN-system.txt`` is the current system
        prompt.  The prefix scans existing ``*-local.json`` files so repeated
        dumps stay short and human-orderable.
        """
        messages = self._messages()
        if not messages:
            self._emit("(;_;) No conversation to dump.")
            return SlashResult(SlashKind.HANDLED)
        try:
            self._chatlog_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._emit(f"(x_x) Failed to create dump directory: {exc}")
            return SlashResult(SlashKind.HANDLED)

        next_n = 1
        try:
            existing = []
            for p in self._chatlog_dir.glob("*-local.json"):
                num_part = p.stem.split("-", 1)[0]
                if num_part.isdigit():
                    existing.append(int(num_part))
            if existing:
                next_n = max(existing) + 1
        except OSError:
            pass
        prefix = f"{next_n:02d}" if next_n < 100 else str(next_n)

        local_path = self._chatlog_dir / f"{prefix}-local.json"
        wire_path = self._chatlog_dir / f"{prefix}-wire.json"
        sys_path = self._chatlog_dir / f"{prefix}-system.txt"

        session = self._repository.get_session(self.session_id)
        processed_at = (
            _local_fromtimestamp(session.created_at).isoformat()
            if session and session.created_at
            else _local_now().isoformat()
        )
        model = self._provider_name()

        # (1) Local history snapshot.
        try:
            local_path.write_text(
                json.dumps(
                    {
                        "processed_at": processed_at,
                        "session_id": self.session_id,
                        "model": model,
                        "messages": [message_to_dict(m) for m in messages],
                    },
                    indent=2, ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            self._emit(f"(x_x) Failed to write {local_path.name}: {exc}")
            return SlashResult(SlashKind.HANDLED)

        # (2) Wire snapshot — empty when /chatlog runs before the first turn
        # (e.g. immediately after resume), mirroring Hermes' fallback.
        wire_messages = self._wire.last_messages if self._wire else None
        wire_list = [message_to_dict(m) for m in wire_messages] if wire_messages else []
        captured_at = (
            _local_fromtimestamp(self._wire.captured_at).isoformat()
            if self._wire and self._wire.captured_at
            else None
        )
        try:
            wire_path.write_text(
                json.dumps(
                    {
                        "processed_at": processed_at,
                        "session_id": self.session_id,
                        "model": model,
                        "captured_at": captured_at,
                        "messages": wire_list,
                    },
                    indent=2, ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            self._emit(f"(x_x) Failed to write {wire_path.name}: {exc}")
            return SlashResult(SlashKind.HANDLED)

        # (3) System prompt: prefer the captured wire copy (what the model
        # actually saw); fall back to a fresh render so /chatlog stays useful
        # pre-first-turn.
        system_prompt = ""
        if wire_messages and wire_messages[0].role is Role.SYSTEM:
            system_prompt = wire_messages[0].content
        if not system_prompt:
            try:
                system_prompt = self._runtime.system_prompt or ""
            except Exception:  # noqa: BLE001 — debug aid must never crash the REPL
                system_prompt = ""
        try:
            sys_path.write_text(system_prompt, encoding="utf-8")
        except OSError as exc:
            self._emit(f"(x_x) Failed to write {sys_path.name}: {exc}")
            return SlashResult(SlashKind.HANDLED)

        self._emit(f"(^_^)v Dumped chatlog #{prefix} to: {self._chatlog_dir}")
        self._emit(f"       - {local_path.name}  ({len(messages)} messages)")
        self._emit(f"       - {wire_path.name}   ({len(wire_list)} wire messages)")
        self._emit(f"       - {sys_path.name}    ({len(system_prompt)} chars)")
        return SlashResult(SlashKind.HANDLED)

    # -- Meta commands -----------------------------------------------------

    def _cmd_help(self, arg: str) -> SlashResult:
        self._emit("\n".join(help_lines()))
        return SlashResult(SlashKind.HANDLED)

    def _cmd_exit(self, arg: str) -> SlashResult:
        return SlashResult(SlashKind.EXIT)

    # -- internals -----------------------------------------------------------

    def _provider_name(self) -> str:
        try:
            return self._runtime.provider_name
        except Exception:  # noqa: BLE001 — cosmetic field; never block the command
            return "unknown"


__all__ = [
    "COMMAND_REGISTRY",
    "CommandDef",
    "SlashHandler",
    "SlashKind",
    "SlashResult",
    "WireCaptureProvider",
    "default_chatlog_dir",
    "format_session_table",
    "help_lines",
    "message_to_dict",
    "resolve_command",
    "sanitize_title",
]
