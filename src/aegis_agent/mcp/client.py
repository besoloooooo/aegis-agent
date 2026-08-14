# Portions adapted from Hermes (hermes-agent), © 2025 Nous Research.
# Licensed under the MIT License. See THIRD_PARTY_NOTICES.md.
#
# Behavioural source (adapted and simplified):
#   * ``tools/mcp_tool.py`` (© 2025 Nous Research, MIT) — the background daemon
#     event-loop thread (``_ensure_mcp_loop`` / ``_mcp_loop``), cross-thread
#     coroutine scheduling (``_run_on_mcp_loop``), stdio + HTTP connection
#     flows (``_run_stdio`` / ``_run_http``), and the tool-call handler
#     (``_make_tool_handler`` clone, with text-block collection).  Aegis
#     drops: interrupt-aware polling, OAuth recovery, session-expiry retry,
#     circuit breaker, reconnect backoff, SSE transport, content-type preflight
#     probe, image-block caching, MCP-notification message handler, and
#     sampling support.
"""Connect to MCP servers and execute tool calls.

Single background-thread event-loop model: one daemon thread runs an asyncio
event loop; all MCP server sessions live on that thread.  Synchronous callers
schedule coroutines onto the loop via :func:`_run_on_loop` and block until the
result is ready (or the timeout fires).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import re
import threading
import time
from collections.abc import Callable
from typing import Any

from aegis_agent.exceptions import OperationCancelled

logger = logging.getLogger(__name__)

# Per-server tracking payload.
_MD = dict

# ---------------------------------------------------------------------------
# Guarded SDK import
# ---------------------------------------------------------------------------

try:
    import httpx
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.client.streamable_http import streamable_http_client
    _SDK_AVAILABLE = True
except ImportError:
    _SDK_AVAILABLE = False
    ClientSession = None  # type: ignore[assignment,misc]
    StdioServerParameters = None  # type: ignore[assignment,misc]
    stdio_client = None  # type: ignore[assignment,misc]
    streamable_http_client = None  # type: ignore[assignment,misc]
    httpx = None  # type: ignore[assignment,misc]

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_loop: asyncio.AbstractEventLoop | None = None
_thread: threading.Thread | None = None
_lock = threading.Lock()
_servers: dict[str, _MD] = {}

# Credential-pattern scrub for error text returned to the model.
_CREDENTIAL_RE = re.compile(
    r"(sk-[A-Za-z0-9_\-]+)"           # OpenAI-style keys
    r"|(ghp_[A-Za-z0-9_]+)"           # GitHub PAT classic
    r"|(github_pat_[A-Za-z0-9_]+)"    # GitHub PAT fine-grained
    r"|(\b[A-Za-z0-9_+/]{40,}\b)"     # generic long base64-ish tokens
    r"|(Bearer\s+\S+)"                 # Bearer tokens in headers
    r"|(key\s*=\s*['\"]?\S+['\"]?)",  # key=value tokens
    re.IGNORECASE,
)
_CREDENTIAL_SUB = "[REDACTED]"

# ---------------------------------------------------------------------------
# Loop management
# ---------------------------------------------------------------------------


def _ensure_loop() -> None:
    """Start the background daemon-thread event loop if not running."""
    global _loop, _thread
    with _lock:
        if _loop is not None and _loop.is_running():
            return
        _loop = asyncio.new_event_loop()
        _thread = threading.Thread(
            target=_loop.run_forever,
            name="aegis-mcp-loop",
            daemon=True,
        )
        _thread.start()


def _run_on_loop(
    coro_factory,
    timeout: float,
    is_cancelled: Callable[[], bool] | None = None,
) -> Any:
    """Schedule ``coro_factory()`` on the background loop, block, return result.

    Raises ``TimeoutError`` when the operation exceeds ``timeout`` seconds and
    :class:`~aegis_agent.exceptions.OperationCancelled` when ``is_cancelled``
    fires (the coroutine is cancelled first, so the caller can abort early
    instead of blocking to the full timeout).  Every other exception (including
    connection and tool errors) is propagated to the caller.
    """
    _ensure_loop()
    assert _loop is not None

    future: concurrent.futures.Future = asyncio.run_coroutine_threadsafe(
        coro_factory() if callable(coro_factory) else coro_factory,
        _loop,
    )

    deadline = time.monotonic() + timeout
    while True:
        if is_cancelled is not None and is_cancelled():
            future.cancel()
            raise OperationCancelled("MCP tool call cancelled by interrupt")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            future.cancel()
            raise TimeoutError(f"MCP operation timed out after {timeout:.0f}s")
        try:
            return future.result(timeout=min(0.1, remaining))
        except concurrent.futures.TimeoutError:
            continue  # poll again


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


def connect_server(name: str, config: dict) -> bool:
    """Connect to one MCP server (convenience wrapper).  Returns ``True`` on success.

    For multiple servers, prefer :func:`connect_servers_parallel` — it runs them
    concurrently so total wall-clock time is bounded by the slowest server, not
    the sum of timeouts.
    """
    if not _SDK_AVAILABLE:
        logger.warning("MCP SDK not installed — skipping server %r", name)
        return False
    if not config.get("enabled", True):
        logger.debug("MCP server %r is disabled", name)
        return False

    transport = _resolve_transport(name, config)
    if transport is None:
        return False

    logger.info("Connecting MCP server %r (%s)...", name, transport)
    connect_timeout = float(config.get("connect_timeout", 60))
    try:
        _run_on_loop(lambda: _connect_async(name, config, transport), connect_timeout)
    except Exception as exc:  # noqa: BLE001
        logger.error("MCP server %r: connection failed: %s", name, exc, exc_info=False)
        return False
    return True


def connect_servers_parallel(
    servers: dict[str, dict],
    overall_timeout: float = 120,
) -> dict[str, bool]:
    """Connect to multiple MCP servers **concurrently** via ``asyncio.gather``.

    Per-server ``connect_timeout`` deadlines are enforced inside
    :func:`_connect_async` (``asyncio.wait_for(ready.wait(), timeout=ct)``);
    ``overall_timeout`` is a generous safety ceiling.  Total wall-clock time
    is bounded by the slowest reachable server, not the sum.

    Ported from Hermes' ``register_mcp_servers`` parallel gather pattern:
    ``asyncio.gather(*coros, return_exceptions=True)``, single outer
    ``_run_on_mcp_loop(_discover_all, timeout=120)``.

    Returns a mapping of server name → connected.
    """
    if not _SDK_AVAILABLE:
        return {name: False for name in servers}
    _ensure_loop()
    assert _loop is not None

    # Collect eligible servers.
    enabled: list[tuple[str, dict, str]] = []  # (name, config, transport)
    for name, cfg in servers.items():
        if not cfg.get("enabled", True):
            continue
        transport = _resolve_transport(name, cfg)
        if transport is None:
            continue
        enabled.append((name, cfg, transport))
    if not enabled:
        return {name: False for name in servers}

    for name, _, transport in enabled:
        logger.info("Connecting MCP server %r (%s)...", name, transport)

    async def _connect_all() -> dict[str, bool]:
        names = [n for n, _, _ in enabled]
        coros = [_connect_async(name, cfg, transport) for name, cfg, transport in enabled]
        gathered = await asyncio.gather(*coros, return_exceptions=True)
        result: dict[str, bool] = {}
        for name, outcome in zip(names, gathered):
            if outcome is None:
                result[name] = True  # success (coroutine returned None)
            else:
                # Per-server deadline or connection error captured as exception.
                logger.error(
                    "MCP server %r: connection failed: %s", name, outcome, exc_info=False
                )
                result[name] = False
        return result

    try:
        return _run_on_loop(_connect_all, overall_timeout)
    except TimeoutError:
        logger.warning(
            "MCP parallel discovery: overall deadline exceeded (%ss)", overall_timeout
        )
        return {name: False for name in servers}


def _resolve_transport(name: str, config: dict) -> str | None:
    if "command" in config:
        return "stdio"
    if "url" in config:
        return "http"
    logger.warning("MCP server %r: missing 'command' or 'url' — skipping", name)
    return None


async def _connect_async(name: str, config: dict, transport: str) -> None:
    """Asyncio-side: start a long-running session task and wait for readiness.

    The session stays alive inside a background asyncio task — we never exit
    the ``async with`` blocks until shutdown, so ``call_tool`` can use the
    stored ``ClientSession`` without hitting "Connection closed".
    """
    tool_timeout = float(config.get("timeout", 120))
    connect_timeout = float(config.get("connect_timeout", 60))

    ready: asyncio.Event = asyncio.Event()
    shutdown: asyncio.Event = asyncio.Event()

    # Mutable containers so the inner _run_task closure can write to them.
    _session_holder: list[Any] = [None]
    _tools_holder: list[list] = [[]]

    async def _run_task() -> None:
        if transport == "stdio":
            await _run_stdio_forever(config, ready, shutdown, _session_holder, _tools_holder)
        else:
            await _run_http_forever(config, ready, shutdown, _session_holder, _tools_holder)

    task = asyncio.ensure_future(_run_task())

    # Block until ready (or timeout / error).
    try:
        await asyncio.wait_for(ready.wait(), timeout=connect_timeout)
    except TimeoutError:
        task.cancel()
        raise TimeoutError(f"MCP server {name!r}: connection timed out after {connect_timeout:.0f}s")
    except Exception:
        task.cancel()
        raise

    # If the task errored before setting ready, the exception is on the task.
    if task.done() and task.exception() is not None:
        raise task.exception()  # type: ignore[arg-type]

    _servers[name] = {
        "session": _session_holder[0],
        "tools": _tools_holder[0],
        "tool_timeout": tool_timeout,
        "_task": task,
        "_shutdown": shutdown,
    }
    logger.info("MCP server %r: connected, %d tools discovered", name, len(_tools_holder[0]))


async def _run_stdio_forever(
    config: dict,
    ready: asyncio.Event,
    shutdown: asyncio.Event,
    session_holder: list,
    tools_holder: list,
) -> None:
    """Connect via stdio and block until shutdown."""
    params = StdioServerParameters(
        command=str(config["command"]),
        args=[str(a) for a in config.get("args", [])],
        env=config.get("env") or None,
    )
    async with stdio_client(params, errlog=_stdio_errlog()) as (read, write):  # noqa: SIM117
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.list_tools()
            session_holder[0] = session
            tools_holder[0] = list(result.tools)
            ready.set()
            # Stay inside the context managers until shutdown.
            await shutdown.wait()


async def _run_http_forever(
    config: dict,
    ready: asyncio.Event,
    shutdown: asyncio.Event,
    session_holder: list,
    tools_holder: list,
) -> None:
    """Connect via Streamable HTTP and block until shutdown."""
    url = str(config["url"])
    headers = {str(k): str(v) for k, v in config.get("headers", {}).items()} or None
    timeout_val = float(config.get("connect_timeout", 60))
    async with httpx.AsyncClient(  # noqa: SIM117
        timeout=httpx.Timeout(timeout_val, read=float(config.get("timeout", 120))),
        headers=headers,
    ) as http_client:
        async with streamable_http_client(url, http_client=http_client) as (read, write, _sid):  # type: ignore[arg-type,misc]
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
                session_holder[0] = session
                tools_holder[0] = list(result.tools)
                ready.set()
                await shutdown.wait()


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------


def call_tool(
    server_name: str,
    tool_name: str,
    arguments: dict,
    timeout: float | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> str:
    """Execute one MCP tool call and return the JSON result string.

    ``arguments`` is the already-parsed dict of tool arguments.
    Returns a JSON object string with either ``"result"`` or ``"error"``.
    Never raises for tool/connection errors — those are returned as
    ``{"error": "..."}`` JSON.  A cooperative cancel (``is_cancelled``) is the
    exception: it raises :class:`~aegis_agent.exceptions.OperationCancelled`
    so the caller can stop the turn immediately.
    """
    entry = _servers.get(server_name)
    if entry is None:
        return json.dumps({"error": f"MCP server {server_name!r} is not connected"}, ensure_ascii=False)

    session = entry["session"]
    effective_timeout = timeout if timeout is not None else entry.get("tool_timeout", 120)

    try:
        raw = _run_on_loop(
            lambda: _call_async(session, tool_name, arguments),
            effective_timeout,
            is_cancelled=is_cancelled,
        )
    except OperationCancelled:
        raise
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": _sanitize_error(f"MCP call failed: {exc}")}, ensure_ascii=False)

    return raw


async def _call_async(session, tool_name: str, arguments: dict) -> str:
    result = await session.call_tool(tool_name, arguments=arguments)
    if getattr(result, "is_error", False):
        error_text = ""
        for block in (result.content or []):
            if hasattr(block, "text") and block.text:
                error_text += block.text
        return json.dumps({"error": _sanitize_error(error_text or "MCP tool returned an error")}, ensure_ascii=False)

    # Collect text blocks.
    parts: list[str] = []
    for block in (result.content or []):
        if hasattr(block, "text") and block.text:
            parts.append(block.text)

    text_result = "\n".join(parts) if parts else ""

    # If the result has structured content, include it alongside text.
    structured = getattr(result, "structuredContent", None) or getattr(result, "structured_content", None)
    if structured is not None and isinstance(structured, dict):
        return json.dumps(
            {"result": text_result, "structured_content": structured},
            ensure_ascii=False,
        )
    return json.dumps({"result": text_result}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


def disconnect_all() -> None:
    """Close all server sessions and stop the background event loop."""
    global _loop, _thread
    with _lock:
        names = list(_servers.keys())
        for name in names:
            entry = _servers[name]
            shutdown_evt: asyncio.Event | None = entry.get("_shutdown")
            task = entry.get("_task")
            if shutdown_evt is not None and _loop is not None and _loop.is_running():
                # Signal the session coroutine to exit its context managers.
                _loop.call_soon_threadsafe(shutdown_evt.set)
            if task is not None and not task.done():
                try:
                    # Give the task a short window to clean up.
                    # We can't await here — just cancel as a last resort.
                    task.cancel()
                except Exception:
                    logger.debug("error cancelling task for %r", name, exc_info=True)
        _servers.clear()

        if _loop is not None:
            try:
                _loop.call_soon_threadsafe(_loop.stop)
            except Exception:
                logger.debug("error stopping MCP event loop", exc_info=True)
            if _thread is not None and _thread.is_alive():
                _thread.join(timeout=5)
            _loop = None
            _thread = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

import os as _os

_stdio_devnull: Any = None


def _stdio_errlog() -> Any:
    """Return a file-like object that discards MCP server stderr output."""
    global _stdio_devnull
    if _stdio_devnull is None:
        _stdio_devnull = open(_os.devnull, "w")
    return _stdio_devnull


def _sanitize_error(text: str) -> str:
    """Strip credential-like substrings from error text."""
    return _CREDENTIAL_RE.sub(_CREDENTIAL_SUB, str(text))


def get_server_tools(name: str) -> list:
    """Return the raw MCP tool objects discovered from *name*, or empty list."""
    entry = _servers.get(name)
    if entry is None:
        return []
    return list(entry.get("tools", []))


def get_server_tool_timeout(name: str) -> float:
    """Return the configured tool timeout for *name*, default 120."""
    entry = _servers.get(name)
    if entry is None:
        return 120.0
    return float(entry.get("tool_timeout", 120))


def connected_server_names() -> list[str]:
    """Return the names of currently connected MCP servers."""
    return list(_servers.keys())


__all__ = [
    "call_tool",
    "connect_server",
    "connect_servers_parallel",
    "connected_server_names",
    "disconnect_all",
    "get_server_tool_timeout",
    "get_server_tools",
]
