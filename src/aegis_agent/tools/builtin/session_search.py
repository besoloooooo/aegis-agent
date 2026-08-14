"""``session_search`` builtin tool — FTS5-backed historical session recall.

ADAPTED PORT of Hermes' ``tools/session_search_tool.py`` (MIT, © 2025 Nous
Research).  Three calling shapes (inferred from args, no explicit mode param):

  1. DISCOVERY — pass ``query``.  Runs FTS5, dedupes hits by session, returns
     top N sessions each with: snippet, ±5 message window around the match, plus
     ``bookend_start`` (first 3 user+assistant messages) and ``bookend_end``
     (last 3).  Zero LLM cost.

  2. SCROLL — pass ``session_id`` + ``around_message_id``.  Returns a window of
     ±window messages centered on the anchor, no FTS5, no bookends.

  3. READ — pass ``session_id`` only.  Dumps the whole session (head+tail when
     large).

  4. BROWSE — no args.  Returns recent sessions chronologically.

Aegis adaptations (vs. Hermes):
  * cross-profile reads (``profile`` param, ``@session:<profile>/<id>`` links,
    profile scanning) are dropped — Aegis has no profile subsystem;
  * compression lineage (``parent_session_id``) resolution is dropped — Aegis
    never forks sessions, so session identity *is* the lineage root;
  * ``db`` is the Aegis :class:`SQLiteSessionRepository`; session metadata is
    read via ``get_session_dict`` / ``list_sessions_rich`` (dicts), not Hermes'
    ``get_session`` dict rows.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

from aegis_agent.models.base import ToolResult
from aegis_agent.tools import schemas
from aegis_agent.tools.registry import ToolContext

logger = logging.getLogger(__name__)

# 默认排除浏览/检索的来源。第三方集成把会话打上 source=tool，避免污染用户历史。
_HIDDEN_SESSION_SOURCES = ("tool",)


def _tool_error(message: str, **extra: Any) -> str:
    result: dict[str, Any] = {"error": str(message)}
    if extra:
        result.update(extra)
    return json.dumps(result, ensure_ascii=False)


def _format_timestamp(ts: float | str | None) -> str:
    """Unix 时间戳 / ISO 字符串 → 人类可读日期；无法转换则原样返回。"""
    if ts is None:
        return "unknown"
    try:
        if isinstance(ts, (int, float)):
            from datetime import datetime

            # 本地墙钟时间，对齐 Hermes 的可读日期格式（无 tz 偏移）。
            return datetime.fromtimestamp(ts).strftime("%B %d, %Y at %I:%M %p")  # noqa: DTZ006
        if isinstance(ts, str):
            if ts.replace(".", "").replace("-", "").isdigit():
                from datetime import datetime

                return datetime.fromtimestamp(float(ts)).strftime("%B %d, %Y at %I:%M %p")  # noqa: DTZ006
            return ts
    except (ValueError, OSError, OverflowError):
        logger.debug("Failed to format timestamp %s", ts, exc_info=True)
    return str(ts)


def _shape_message(m: Mapping[str, Any], anchor_id: int | None = None) -> dict[str, Any]:
    """把一条消息行瘦身为工具响应结构。content 为空也保留（tool-call-only 回合）。"""
    entry: dict[str, Any] = {
        "id": m.get("id"),
        "role": m.get("role"),
        "content": m.get("content"),
        "timestamp": m.get("timestamp"),
    }
    if m.get("tool_name"):
        entry["tool_name"] = m.get("tool_name")
    if m.get("tool_calls"):
        entry["tool_calls"] = m.get("tool_calls")
    if m.get("tool_call_id"):
        entry["tool_call_id"] = m.get("tool_call_id")
    if anchor_id is not None and m.get("id") == anchor_id:
        entry["anchor"] = True
    # 去掉 None 值以压缩载荷，但 content 始终保留。
    return {k: v for k, v in entry.items() if v is not None or k in ("content",)}


def _list_recent_sessions(db, limit: int, current_session_id: str | None = None) -> str:
    """浏览 shape：返回最近会话元信息（无 LLM、无 FTS5）。"""
    try:
        sessions = db.list_sessions_rich(
            limit=limit + 5,
            exclude_sources=list(_HIDDEN_SESSION_SOURCES),
            order_by_last_active=True,
        )  # 多取几条以跳过当前会话

        results = []
        for s in sessions:
            sid = s.get("id", "")
            if current_session_id and sid == current_session_id:
                continue
            # Aegis 无压缩链/委派子会话（parent_session_id 未迁移），此判断恒 False；
            # 保留以对齐 Hermes 的浏览过滤结构。
            if s.get("parent_session_id"):
                continue
            results.append({
                "session_id": sid,
                "title": s.get("title") or None,
                "source": s.get("source", ""),
                "started_at": s.get("started_at", ""),
                "last_active": s.get("last_active", ""),
                "message_count": s.get("message_count", 0),
                "preview": s.get("preview", ""),
            })
            if len(results) >= limit:
                break

        return json.dumps({
            "success": True,
            "mode": "browse",
            "results": results,
            "count": len(results),
            "message": f"Showing {len(results)} most recent sessions. Pass a query= to search, or session_id+around_message_id to scroll.",
        }, ensure_ascii=False)
    except Exception as e:
        logger.exception("Error listing recent sessions")
        return _tool_error(f"Failed to list recent sessions: {e}", success=False)


def _read_session(db, session_id: str, head: int = 20, tail: int = 10) -> str:
    """读 shape：按 id dump 整段会话（过长时 head + tail）。"""
    try:
        meta = db.get_session_dict(session_id) or {}
    except Exception as e:
        logger.debug("get_session_dict failed for %s: %s", session_id, e, exc_info=True)
        meta = {}
    if not meta:
        return _tool_error(f"session_id not found: {session_id}", success=False)

    try:
        rows = db.get_messages_dict(session_id)
    except Exception as e:
        logger.exception("get_messages_dict failed for %s", session_id)
        return _tool_error(f"failed to load session: {e}", success=False)

    shaped = [_shape_message(m) for m in rows]
    total = len(shaped)
    truncated = total > head + tail
    window = shaped[:head] + shaped[-tail:] if truncated else shaped

    response = {
        "success": True,
        "mode": "read",
        "session_id": session_id,
        "session_meta": {
            "when": _format_timestamp(meta.get("started_at")),
            "source": meta.get("source"),
            "model": meta.get("model"),
            "title": meta.get("title"),
        },
        "message_count": total,
        "truncated": truncated,
        "messages": window,
    }
    if truncated:
        response["message"] = (
            f"Session has {total} messages; showing first {head} + last {tail}. "
            "Pass around_message_id (any id above) to scroll the middle."
        )
    return json.dumps(response, ensure_ascii=False)


def _scroll(
    db,
    session_id: str,
    around_message_id: int,
    window: int = 5,
    current_session_id: str | None = None,
) -> str:
    """滚动 shape：返回以锚点为中心的窗口。无 FTS5、无 bookends。"""
    if not isinstance(session_id, str) or not session_id.strip():
        return _tool_error("scroll requires session_id", success=False)
    session_id = session_id.strip()

    try:
        around_message_id = int(around_message_id)
    except (TypeError, ValueError):
        return _tool_error("scroll requires integer around_message_id", success=False)

    if not isinstance(window, int):
        try:
            window = int(window)
        except (TypeError, ValueError):
            window = 5
    window = max(1, min(window, 20))

    # 拒绝在活动会话内滚动——那些消息已经在上下文里。
    if current_session_id and session_id == current_session_id:
        return _tool_error(
            "scroll rejected: anchor lives in the current session (already in your active context)",
            success=False,
        )

    try:
        session_meta = db.get_session_dict(session_id) or {}
    except Exception as e:
        logger.debug("get_session_dict failed for %s: %s", session_id, e, exc_info=True)
        session_meta = {}
    if not session_meta:
        return _tool_error(f"session_id not found: {session_id}", success=False)

    try:
        view = db.get_messages_around(session_id, around_message_id, window=window)
    except Exception as e:
        logger.exception("get_messages_around failed")
        return _tool_error(f"failed to load messages: {e}", success=False)

    messages = view.get("window") or []
    if not messages:
        return _tool_error(
            f"around_message_id {around_message_id} not in session_id {session_id}",
            success=False,
        )

    response = {
        "success": True,
        "mode": "scroll",
        "session_id": session_id,
        "around_message_id": around_message_id,
        "session_meta": {
            "when": _format_timestamp(session_meta.get("started_at")),
            "source": session_meta.get("source"),
            "model": session_meta.get("model"),
            "title": session_meta.get("title"),
        },
        "window": window,
        "messages": [_shape_message(m, anchor_id=around_message_id) for m in messages],
        "messages_before": view.get("messages_before", 0),
        "messages_after": view.get("messages_after", 0),
    }
    return json.dumps(response, ensure_ascii=False)


def _discover(
    db,
    query: str,
    role_filter: list[str] | None,
    limit: int,
    sort: str | None,
    current_session_id: str | None = None,
) -> str:
    """发现 shape：FTS5 + 锚点窗口 + bookends，单次调用。"""
    role_list = role_filter if role_filter else ["user", "assistant"]

    try:
        raw_results = db.search_messages(
            query=query,
            role_filter=role_list,
            exclude_sources=list(_HIDDEN_SESSION_SOURCES),
            limit=50,  # 放宽以便按 session 去重后仍能找到足够多的不同会话
            offset=0,
            sort=sort,
        )
    except Exception as e:
        logger.exception("FTS5 search failed")
        return _tool_error(f"Search failed: {e}", success=False)

    if not raw_results:
        return json.dumps({
            "success": True,
            "mode": "discover",
            "query": query,
            "results": [],
            "count": 0,
            "message": "No matching sessions found.",
        }, ensure_ascii=False)

    # 按 session 去重（Aegis 无压缩链，session 自身即 lineage root）。
    seen_sessions: dict[str, dict[str, Any]] = {}
    for r in raw_results:
        raw_sid = r["session_id"]
        if current_session_id and raw_sid == current_session_id:
            continue
        if raw_sid not in seen_sessions:
            row = dict(r)
            seen_sessions[raw_sid] = row
        if len(seen_sessions) >= limit:
            break

    results = []
    for hit_sid, match_info in seen_sessions.items():
        msg_id = match_info.get("id")
        try:
            view = db.get_anchored_view(hit_sid, msg_id, window=5, bookend=3)
        except Exception as e:
            logger.warning("get_anchored_view failed for %s/%s: %s", hit_sid, msg_id, e, exc_info=True)
            continue

        try:
            session_meta = db.get_session_dict(hit_sid) or {}
        except Exception:  # noqa: BLE001 — 会话元信息缺失不应拖垮整条命中
            session_meta = {}

        entry = {
            "session_id": hit_sid,
            "when": _format_timestamp(
                session_meta.get("started_at") or match_info.get("session_started")
            ),
            "source": session_meta.get("source") or match_info.get("source", "unknown"),
            "model": session_meta.get("model") or match_info.get("model") or "unknown",
            "title": session_meta.get("title") or None,
            "matched_role": match_info.get("role"),
            "match_message_id": msg_id,
            "snippet": match_info.get("snippet") or "",
            "bookend_start": [_shape_message(m) for m in (view.get("bookend_start") or [])],
            "messages": [_shape_message(m, anchor_id=msg_id) for m in (view.get("window") or [])],
            "bookend_end": [_shape_message(m) for m in (view.get("bookend_end") or [])],
            "messages_before": view.get("messages_before", 0),
            "messages_after": view.get("messages_after", 0),
        }
        results.append(entry)

    return json.dumps({
        "success": True,
        "mode": "discover",
        "query": query,
        "results": results,
        "count": len(results),
        "sessions_searched": len(seen_sessions),
    }, ensure_ascii=False)


def session_search(
    query: str = "",
    role_filter: str | None = None,
    limit: int = 3,
    db=None,
    current_session_id: str | None = None,
    # Scroll shape
    session_id: str | None = None,
    around_message_id: int | None = None,
    window: int = 5,
    # Discovery shape
    sort: str | None = None,
) -> str:
    """单 shape 工具，按传入参数推断调用模式。

    Discovery: 传 ``query``。Scroll: 传 ``session_id`` + ``around_message_id``。
    Read: 只传 ``session_id``。Browse: 什么都不传。
    """
    if db is None or not hasattr(db, "search_messages"):
        return _tool_error(
            "session search unavailable: the session store does not support "
            "full-text search (in-memory store, or SQLite without FTS5)",
            success=False,
        )

    # Scroll 优先——显式锚点压过任何 query。
    if (isinstance(session_id, str) and session_id.strip()) and around_message_id is not None:
        return _scroll(
            db=db,
            session_id=session_id,
            around_message_id=around_message_id,
            window=window,
            current_session_id=current_session_id,
        )

    # Read shape：有 session_id 但无锚点 → dump 整段会话。
    if isinstance(session_id, str) and session_id.strip():
        return _read_session(db, session_id.strip())

    # limit 夹取 [1, 10]
    if not isinstance(limit, int):
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 3
    limit = max(1, min(limit, 10))

    # Browse shape：无 query → 最近会话。
    if not query or not isinstance(query, str) or not query.strip():
        return _list_recent_sessions(db, limit, current_session_id)

    role_list: list[str] | None = None
    if isinstance(role_filter, str) and role_filter.strip():
        role_list = [r.strip() for r in role_filter.split(",") if r.strip()]

    sort_norm: str | None = None
    if isinstance(sort, str):
        candidate = sort.strip().lower()
        if candidate in ("newest", "oldest"):
            sort_norm = candidate

    return _discover(
        db=db,
        query=query.strip(),
        role_filter=role_list,
        limit=limit,
        sort=sort_norm,
        current_session_id=current_session_id,
    )


class SessionSearchTool:
    """``session_search`` 工具：查询真实历史聊天（与 Auto Memory 的提炼记忆不同）。

    依赖注入一个 SQLite 会话仓库；仓库不支持 FTS5（如内存仓库）时优雅降级为
    ``success=false`` 的 JSON 错误，而不是抛异常。
    """

    definition = schemas.SESSION_SEARCH

    def __init__(self, db=None) -> None:
        self._db = db

    def run(self, arguments: Mapping[str, Any], context: ToolContext | None = None) -> ToolResult:
        current_session_id = getattr(context, "session_id", None) if context is not None else None
        result = session_search(
            query=arguments.get("query") or "",
            role_filter=arguments.get("role_filter"),
            limit=arguments.get("limit", 3),
            session_id=arguments.get("session_id"),
            around_message_id=arguments.get("around_message_id"),
            window=arguments.get("window", 5),
            sort=arguments.get("sort"),
            db=self._db,
            current_session_id=current_session_id,
        )
        return ToolResult(tool_call_id="", name=self.definition.name, content=result)


__all__ = ["SessionSearchTool", "session_search"]
