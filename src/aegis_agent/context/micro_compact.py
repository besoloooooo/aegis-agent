# Portions adapted from Hermes (hermes-agent), © 2025 Nous Research.
# Licensed under the MIT License. See THIRD_PARTY_NOTICES.md.
#
# Behavioural source (port with import adaptation):
#   * ``ctx-compress-opt/micro_compact.py`` — threshold-triggered progressive
#     micro-compaction (local cleanup, no LLM call).  Only the import style was
#     adapted (flat same-dir imports -> absolute package imports); the
#     algorithm is unchanged.
"""按阈值触发的渐进式微压缩（Threshold-Based Microcompact）。

由调用方在「当前 token 数已超过阈值」时直接调用，本地做一轮渐进式清理。
典型用在 compress.py 的 `_compress_context` 里——超大工具剪裁之后、按轮 LLM 摘要之前
先跑一遍，往往能把上下文压回阈值内，从而省掉昂贵的摘要调用。

与其它两套机制的分工：
  - tool_budget.py（按大小触发）：超大工具结果转存磁盘 + 换预览，小心保住缓存。
  - 本模块（按阈值触发）：上下文超限时，本地清理旧内容（去重 / 摘要化 tool result、
    截断 tool call 参数、删旧 reasoning），不转存、不留预览。纯本地、无 LLM 调用。
  - compress.py 的按轮摘要（本模块之后）：仍超限才调用 LLM 逐轮摘要。

处理策略（渐进式级联，每步后重估 token，达标即返回）：
  Step 0  掐头去尾：保护「系统消息 + 运行时上下文标记 + 已压缩摘要区」（头）
          与「末尾最近 keep_recent 条消息」（尾）。中间部分可清理。
          - 历史轮：tool result / 参数 / reasoning 都可清。
          - 当前轮（最新一轮 user 起）：早于末尾 keep_recent 条的 tool result / 参数
            也一并清理，但当前轮的 reasoning_content 全部保留。
  Step 1  对可清理区间做工具结果压缩：
            1a. 去重：同一个结果出现多次时保留最新一份，旧的替换成回指占位；
            1b. 摘要化：把旧 tool result 替换为有信息量的一行摘要
                （如 "[terminal] ran `npm test` -> exit 0, 47 lines output"），
                而非无信息量的通用占位符。
  Step 2  截断可清理区间里 tool call 的过长参数：parse JSON → 只截断过长字符串字段
          → 重新序列化（保证合法 JSON，不清空整个 arguments）。
  Step 3  删除可清理区间里的 reasoning_content（仅历史轮；当前轮的保留）。
  Step 4  兜底：若仍超限，对区间内所有 tool result / 参数做同样处理（扩大范围）。

只作用于传入列表的副本，产出新 dict；绝不修改系统消息、运行时标记、已压缩摘要区，
也绝不删除消息（删除交给后续摘要 / 单轮兜底）。

消息格式为 OpenAI Chat Completions 的 dict 形状（与 tool_budget.py 一致）：
  assistant: {"role":"assistant","tool_calls":[{"id":..,"function":{"name":..}}]}
  tool:      {"role":"tool","tool_call_id":..,"content":..,"name":..}
"""

from __future__ import annotations

import copy
import hashlib
import json
import re

# 常量：标记串 + 可压缩工具集合（统一定义在 compress_config.py）。
# 别名在导入后显式赋值（保持原本地名），避免 isort 将别名导入拆成多个块。
from aegis_agent.context.compress_config import (
    ARGS_HEAD_CHARS,
    ARGS_TRUNCATE_THRESHOLD,
    COMPACTABLE_TOOLS_MICRO,
    CONTEXT_SUMMARY_TAG,
    DUPLICATE_TOOL_RESULT_MARKER,
    KEEP_RECENT_MESSAGES,
    PERSISTED_OUTPUT_TAG,
    RUNTIME_CONTEXT_MARKER,
    TOOL_RESULT_MIN_CHARS,
)

_ARGS_HEAD_CHARS = ARGS_HEAD_CHARS                      # 截断后字段头部长度
_ARGS_TRUNCATE_THRESHOLD = ARGS_TRUNCATE_THRESHOLD      # 参数截断阈值（字符）
COMPACTABLE_TOOLS = COMPACTABLE_TOOLS_MICRO             # 可压缩工具名单
_DUPLICATE_MARKER = DUPLICATE_TOOL_RESULT_MARKER        # 去重回指占位串
_RUNTIME_CONTEXT_MARKER = RUNTIME_CONTEXT_MARKER        # 运行时上下文标记
_TOOL_RESULT_MIN_CHARS = TOOL_RESULT_MIN_CHARS          # 去重/摘要最小长度

# 判断「是否已被本模块处理过」的前缀，幂等跳过用（前缀匹配，与完整占位串略有差异）。
_PROCESSED_PREFIXES = (
    "[Duplicate tool output",
    "[Old tool output",
    "[Old tool result content cleared",   # 兼容旧版本遗留内容
)


# =============================================================================
# 二、token 估算（惰性复用 compress.py，避免循环依赖）
# =============================================================================


def _estimate_tokens(messages: list[dict]) -> int:
    """复用 compress.py 的精确 token 估算；导入失败时退化为字符/2.5 粗估。"""
    try:
        from aegis_agent.context.compress import _estimate_tokens as _est
        return _est(messages)
    except Exception:  # noqa: BLE001 — 导入失败时退化为字符粗估
        total = 0
        for m in messages:
            c = m.get("content")
            if isinstance(c, str):
                total += len(c)
            elif isinstance(c, list):
                for b in c:
                    if isinstance(b, dict):
                        total += len(str(b))
            if isinstance(m.get("reasoning_content"), str):
                total += len(m["reasoning_content"])
            if m.get("tool_calls"):
                total += len(str(m["tool_calls"]))
        return int(total / 2.5)


# =============================================================================
# 三、结构切分：保护区 vs 可清理历史
# =============================================================================


# 与 compress.py 的 _compress_context / _split_into_rounds 保持一致的运行时标记文本
# （统一定义在 compress_config.RUNTIME_CONTEXT_MARKER，顶部已导入别名为本模块名）。


def _protected_head_count(messages: list[dict]) -> int:
    """算出开头需要整体保护、不参与清理的消息条数：

      - 首条若是 system → 保护；
      - 其后若是带「运行时上下文」标记的 user → 一并保护；
      - 其后连续的「已压缩摘要区」（user + 带 [Context Summary] 的 assistant 成对）
        → 一并保护。
    返回保护的前缀长度（下标 < 该值的消息都在保护区）。
    """
    n = len(messages)
    idx = 0

    # system
    if idx < n and messages[idx].get("role") == "system":
        idx += 1

    # 运行时上下文标记（紧跟 system 的那条 user）
    if (
        idx < n
        and messages[idx].get("role") == "user"
        and isinstance(messages[idx].get("content"), str)
        and _RUNTIME_CONTEXT_MARKER in messages[idx]["content"]
    ):
        idx += 1

    # 已压缩摘要区：连续的 (user, assistant[Context Summary]) 成对出现
    while idx + 1 < n:
        u = messages[idx]
        a = messages[idx + 1]
        if (
            u.get("role") == "user"
            and a.get("role") == "assistant"
            and isinstance(a.get("content"), str)
            and CONTEXT_SUMMARY_TAG in a["content"]
        ):
            idx += 2
        else:
            break

    return idx


def _current_session_start(messages: list[dict]) -> int:
    """当前进行中的一轮（session）的起始下标 = 最后一条 user 消息的位置。

    找不到 user 则返回 len(messages)（整段都算历史）。
    """
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            return i
    return len(messages)


def _clearable_ranges(messages: list[dict], keep_recent: int) -> tuple[int, int, int]:
    """返回三个下标：(lo, tool_hi, reason_hi)。

      lo        = 保护头之后（system + 运行时标记 + 已压缩摘要区之后）。
      tool_hi   = tool result / tool call 参数的可清理上界。
                  = 「末尾保留 keep_recent 条」的起点。
                  → 历史轮 + 当前轮里「早于最近 keep_recent 条」的 tool 都会被清理。
      reason_hi = reasoning_content 的可清理上界。
                  = min(当前 session 起点, tool_hi)。
                  → 只清历史轮的 reasoning；当前轮（最新一轮）的 reasoning 全部保留。

    三段语义：
      - [0, lo)                     保护头，永不动。
      - [lo, tool_hi)               tool result / 参数可清（含当前轮早期的 tool）。
      - [lo, reason_hi)             reasoning 可清（仅历史轮）。
      - [tool_hi, len)             末尾 keep_recent 条，整体保留。
    """
    lo = _protected_head_count(messages)
    session_start = _current_session_start(messages)
    tail_keep_start = max(0, len(messages) - max(0, keep_recent))
    tool_hi = max(lo, tail_keep_start)
    reason_hi = max(lo, min(session_start, tail_keep_start))
    return lo, tool_hi, reason_hi


# =============================================================================
# 四、核心工具函数（移植自 hermes context_compressor.py）
# =============================================================================


def _summarize_tool_result(tool_name: str, args_str: str, content: str) -> str:
    """生成工具调用结果的有信息量的一行摘要（移植自 context_compressor._summarize_tool_result）。

    替代无信息量的通用占位符，保留「工具做了什么 + 结果大概是什么」。
    示例：
        [terminal] ran `npm test` -> exit 0, 47 lines output
        [read_file] read config.py from line 1 (1,200 chars)
        [search_files] content search for 'compress' in agent/ -> 12 matches
        [web_search] query='北京环球影城票价' (4,823 chars result)
    """
    try:
        args = json.loads(args_str) if args_str else {}
    except (json.JSONDecodeError, TypeError):
        args = {}

    content = content or ""
    content_len = len(content)
    line_count = content.count("\n") + 1 if content.strip() else 0

    if tool_name == "terminal":
        cmd = args.get("command", "")
        if len(cmd) > 80:
            cmd = cmd[:77] + "..."
        exit_match = re.search(r'"exit_code"\s*:\s*(-?\d+)', content)
        exit_code = exit_match.group(1) if exit_match else "?"
        return f"[terminal] ran `{cmd}` -> exit {exit_code}, {line_count} lines output"

    if tool_name == "read_file":
        path = args.get("path", "?")
        offset = args.get("offset", 1)
        return f"[read_file] read {path} from line {offset} ({content_len:,} chars)"

    if tool_name == "write_file":
        path = args.get("path", "?")
        written_lines = args.get("content", "").count("\n") + 1 if args.get("content") else "?"
        return f"[write_file] wrote to {path} ({written_lines} lines)"

    if tool_name == "search_files":
        pattern = args.get("pattern", "?")
        path = args.get("path", ".")
        target = args.get("target", "content")
        match_count = re.search(r'"total_count"\s*:\s*(\d+)', content)
        count = match_count.group(1) if match_count else "?"
        return f"[search_files] {target} search for '{pattern}' in {path} -> {count} matches"

    if tool_name == "patch":
        path = args.get("path", "?")
        mode = args.get("mode", "replace")
        return f"[patch] {mode} in {path} ({content_len:,} chars result)"

    if tool_name in {"browser_snapshot", "browser_console",
                     "browser_get_images", "browser_vision"}:
        url = args.get("url", "")
        ref = args.get("ref", "")
        detail = f" {url}" if url else (f" ref={ref}" if ref else "")
        return f"[{tool_name}]{detail} ({content_len:,} chars)"

    if tool_name == "web_search":
        query = args.get("query", "?")
        return f"[web_search] query='{query}' ({content_len:,} chars result)"

    if tool_name == "web_extract":
        urls = args.get("urls", [])
        url_desc = urls[0] if isinstance(urls, list) and urls else args.get("url", "?")
        if isinstance(urls, list) and len(urls) > 1:
            url_desc += f" (+{len(urls) - 1} more)"
        return f"[web_extract] {url_desc} ({content_len:,} chars)"

    if tool_name == "delegate_task":
        goal = args.get("goal", "")
        if len(goal) > 60:
            goal = goal[:57] + "..."
        return f"[delegate_task] '{goal}' ({content_len:,} chars result)"

    if tool_name == "execute_code":
        code_preview = (args.get("code") or "")[:60].replace("\n", " ")
        if len(args.get("code", "")) > 60:
            code_preview += "..."
        return f"[execute_code] `{code_preview}` ({line_count} lines output)"

    if tool_name == "process":
        action = args.get("action", "?")
        sid = args.get("session_id", "?")
        return f"[process] {action} session={sid}"

    if tool_name in {"mcp_amap_maps_direction_driving",
                     "mcp_amap_maps_direction_transit_integrated",
                     "mcp_amap_maps_geo"}:
        short = tool_name.replace("mcp_amap_maps_", "amap.")
        first_arg = ""
        for k, v in list(args.items())[:2]:
            sv = str(v)[:40]
            first_arg += f" {k}={sv}"
        return f"[{short}]{first_arg} ({content_len:,} chars)"

    # 通用兜底
    first_arg = ""
    for k, v in list(args.items())[:2]:
        sv = str(v)[:40]
        first_arg += f" {k}={sv}"
    return f"[{tool_name}]{first_arg} ({content_len:,} chars result)"


def _truncate_tool_call_args_json(args: str, head_chars: int = _ARGS_HEAD_CHARS) -> str:
    """截断 tool call arguments 里过长的字符串字段，保持 JSON 合法（移植自
    context_compressor._truncate_tool_call_args_json）。

    直接硬切 JSON 字符串会产生无效 JSON，导致下游 provider 400 拒绝。
    本函数 parse → 只截断过长的字符串叶节点 → 重新序列化，路径/查询等短字段原样保留。

    非合法 JSON 的 arguments（部分 provider 使用非标准格式）原样返回，不改动。
    """
    try:
        parsed = json.loads(args)
    except (ValueError, TypeError):
        return args

    def _shrink(obj):
        if isinstance(obj, str):
            if len(obj) > head_chars:
                return obj[:head_chars] + "...[truncated]"
            return obj
        if isinstance(obj, dict):
            return {k: _shrink(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_shrink(v) for v in obj]
        return obj

    shrunken = _shrink(parsed)
    return json.dumps(shrunken, ensure_ascii=False)


# =============================================================================
# 五、可清理区间内的各处理步骤
# =============================================================================


def _build_call_id_map(messages: list[dict]) -> dict[str, tuple[str, str]]:
    """从 assistant.tool_calls 建立 tool_call_id → (工具名, arguments) 映射。

    tool result 消息本身通常只带 tool_call_id，需要反查对应的工具名和参数，
    才能生成有信息量的一行摘要。
    """
    mapping: dict[str, tuple[str, str]] = {}
    for m in messages:
        if m.get("role") != "assistant":
            continue
        for tc in m.get("tool_calls") or []:
            if isinstance(tc, dict):
                cid = tc.get("id", "")
                fn = tc.get("function") or {}
                name = fn.get("name", "unknown")
                args = fn.get("arguments", "")
            else:
                cid = getattr(tc, "id", "") or ""
                fn = getattr(tc, "function", None)
                name = getattr(fn, "name", "unknown") if fn else "unknown"
                args = getattr(fn, "arguments", "") if fn else ""
            if cid:
                mapping[cid] = (str(name), str(args))
    return mapping


def _is_compactable_call(call: dict) -> bool:
    name = (call.get("function") or {}).get("name", "")
    return name in COMPACTABLE_TOOLS


# 匹配所有 _summarize_tool_result 产出的一行摘要（以 [工具名] 开头）。
# 用于避免 Step 4 对 Step 1 已摘要化的结果二次摘要。
# [a-z_][a-z._]+ 可匹配 terminal / read_file / amap.geo 等工具名前缀，
# 但不会匹配 [{"key": ...} 形式的原始 JSON 数组（后者首字符为 {）。
_SUMMARY_LINE_RE = re.compile(r"^\[[a-z_][a-z._]+\] ")


def _is_already_processed(content: str) -> bool:
    """是否已被本模块（或兼容的历史版本）处理过，跳过避免重复。

    涵盖：去重占位符、通用 cleared 占位符、以及 Step 1 产出的一行工具摘要。
    """
    if any(content.startswith(p) for p in _PROCESSED_PREFIXES):
        return True
    # 检测 _summarize_tool_result 产出的一行摘要（防止 Step 4 对已摘要内容二次处理）
    return bool(_SUMMARY_LINE_RE.match(content))


def _deduplicate_tool_results(messages: list[dict], lo: int, hi: int) -> int:
    """对 [lo, hi) 区间内的 tool result 做去重：同一内容只保留最新一份，
    更旧的重复项替换为回指占位。

    扫描顺序：从 messages 末尾往前扫到 lo。
    这样可以正确处理「旧结果在 [lo, hi) 内，新结果在保护尾部 [hi, len)」的情况——
    先记录保护尾部的哈希，再遇到 [lo, hi) 内的同内容旧结果时将其标记为重复。
    仅处理长度 >= _TOOL_RESULT_MIN_CHARS 字符的字符串内容（对齐 hermes 的去重门槛）。
    返回去重的条数。
    """
    content_hashes: dict[str, int] = {}   # hash → 最新的下标（已保留）
    duped = 0
    # 从消息列表末尾扫到 lo（而非只扫 [lo, hi)），以覆盖保护尾部的哈希
    for i in range(len(messages) - 1, lo - 1, -1):
        m = messages[i]
        if m.get("role") != "tool":
            continue
        content = m.get("content")
        if not isinstance(content, str) or len(content) < _TOOL_RESULT_MIN_CHARS:
            continue
        h = hashlib.md5(content.encode("utf-8", errors="replace")).hexdigest()[:12]
        if h in content_hashes:
            # 比已记录的更旧（下标更小）
            if i < hi:
                # 在可清理区间内 → 替换为回指占位
                messages[i] = {**m, "content": _DUPLICATE_MARKER}
                duped += 1
            # 保护尾部内的重复项（i >= hi）不改动，维持字节稳定
        else:
            content_hashes[h] = i
    return duped


def _summarize_old_tool_results(
    messages: list[dict],
    lo: int,
    hi: int,
    call_id_map: dict[str, tuple[str, str]],
) -> int:
    """对 [lo, hi) 区间内符合条件的 tool result 生成一行摘要（替换大内容）。

    条件：
      - role == "tool"
      - content 是字符串且长度 > _TOOL_RESULT_MIN_CHARS 字符
      - 尚未被处理过（不以 _PROCESSED_PREFIXES 任一开头）
      - 不是 tool_budget 转存的预览块（PERSISTED_OUTPUT_TAG）

    返回被摘要化的条数。
    """
    summarized = 0
    for i in range(lo, hi):
        m = messages[i]
        if m.get("role") != "tool":
            continue
        content = m.get("content", "")
        if not isinstance(content, str):
            continue
        if len(content) <= _TOOL_RESULT_MIN_CHARS:
            continue
        if _is_already_processed(content):
            continue
        if content.startswith(PERSISTED_OUTPUT_TAG):
            continue

        call_id = m.get("tool_call_id", "")
        tool_name, args_str = call_id_map.get(call_id, ("unknown", ""))
        summary = _summarize_tool_result(tool_name, args_str, content)
        messages[i] = {**m, "content": summary}
        summarized += 1
    return summarized


def _truncate_tool_call_args(messages: list[dict], lo: int, hi: int) -> int:
    """对 [lo, hi) 区间内 assistant 消息的过长 tool call arguments 做 JSON 感知截断。

    只截断长度 > _ARGS_TRUNCATE_THRESHOLD 的 arguments，保留路径/查询等短字段。
    保证结果仍是合法 JSON（通过 parse → shrink → re-serialize），不会产生破损历史。
    返回被截断的 tool_call 数量。
    """
    truncated = 0
    for i in range(lo, hi):
        m = messages[i]
        if m.get("role") != "assistant" or not m.get("tool_calls"):
            continue
        new_tcs = []
        modified = False
        for tc in m["tool_calls"]:
            if isinstance(tc, dict):
                fn = tc.get("function") or {}
                args = fn.get("arguments", "")
                if isinstance(args, str) and len(args) > _ARGS_TRUNCATE_THRESHOLD:
                    new_args = _truncate_tool_call_args_json(args)
                    if new_args != args:
                        tc = {**tc, "function": {**fn, "arguments": new_args}}
                        modified = True
                        truncated += 1
            new_tcs.append(tc)
        if modified:
            messages[i] = {**m, "tool_calls": new_tcs}
    return truncated


def _clear_reasoning(messages: list[dict], lo: int, hi: int) -> int:
    """删除 [lo, hi) 区间内所有消息的 reasoning_content（清空为 ""）。

    区间已排除当前 session（含最新一轮 user），所以最新 reasoning 天然保留。
    返回被清空的条数。
    """
    cleared = 0
    for i in range(lo, hi):
        m = messages[i]
        if isinstance(m.get("reasoning_content"), str) and m["reasoning_content"]:
            m["reasoning_content"] = ""
            cleared += 1
    return cleared


# =============================================================================
# 六、主入口
# =============================================================================


def micro_compact(
    messages: list[dict],
    max_tokens: int,
    keep_recent: int = KEEP_RECENT_MESSAGES,
) -> list[dict]:
    """阈值触发的渐进式微压缩。

    在 `_estimate_tokens(messages) > max_tokens` 时调用：按 Step0~4 逐级本地清理，
    每步后重估，达标即提前返回。始终返回一个新的消息列表（深拷贝），不改入参；
    绝不触碰保护区（系统消息 / 运行时标记 / 已压缩摘要区）与当前 session 尾部。

    未达标也照常返回（尽力压缩），由调用方决定后续（如按轮 LLM 摘要）。
    """
    result = copy.deepcopy(messages)

    def fits() -> bool:
        return _estimate_tokens(result) <= max_tokens

    if fits():
        return result

    # Step 0：确定可清理区间。
    #   tool result / 参数可清到 tool_hi（含当前轮早于最近 keep_recent 条的 tool）；
    #   reasoning 只清到 reason_hi（仅历史轮，当前轮 reasoning 全保留）。
    lo, tool_hi, reason_hi = _clearable_ranges(result, keep_recent)
    if tool_hi <= lo and reason_hi <= lo:
        # 没有可清理的内容（几乎全是保护区 / 最近 keep_recent 条）→ 原样返回
        return result

    # 预先建立 tool_call_id → (工具名, arguments) 映射，摘要化 step 需要
    call_id_map = _build_call_id_map(result)

    # Step 1：工具结果压缩（去重 + 摘要化）
    _deduplicate_tool_results(result, lo, tool_hi)
    _summarize_old_tool_results(result, lo, tool_hi, call_id_map)
    if fits():
        return result

    # Step 2：截断过长的 tool call 参数（JSON 感知，保留路径/查询等短字段）
    _truncate_tool_call_args(result, lo, tool_hi)
    if fits():
        return result

    # Step 3：删除历史 reasoning_content（当前 session / 最新一轮的保留）
    _clear_reasoning(result, lo, reason_hi)
    if fits():
        return result

    # Step 4：兜底——对区间内所有 tool result 全量摘要化（不限 200 字符门槛），
    #         参数再做一轮截断。仍不删消息，保留结构完整性。
    for i in range(lo, tool_hi):
        m = result[i]
        if m.get("role") != "tool":
            continue
        content = m.get("content", "")
        if not isinstance(content, str):
            continue
        if _is_already_processed(content) or content.startswith(PERSISTED_OUTPUT_TAG):
            continue
        # 低于 200 字符之前跳过的，现在也一并摘要化
        call_id = m.get("tool_call_id", "")
        tool_name, args_str = call_id_map.get(call_id, ("unknown", ""))
        result[i] = {**m, "content": _summarize_tool_result(tool_name, args_str, content)}

    _truncate_tool_call_args(result, lo, tool_hi)

    return result
