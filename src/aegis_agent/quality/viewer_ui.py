"""Self-contained UI asset for the local trace viewer."""

VIEWER_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Aegis Trace Viewer</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #09100f;
      --panel: #101917;
      --panel-2: #14201d;
      --line: #25332f;
      --line-soft: #1c2925;
      --text: #edf7f2;
      --muted: #8fa29b;
      --accent: #65e6ad;
      --accent-2: #32bc84;
      --blue: #72b7ff;
      --amber: #f5c56b;
      --red: #ff8585;
      --shadow: 0 18px 50px rgba(0, 0, 0, .28);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-width: 980px;
      min-height: 100vh;
      overflow: hidden;
      color: var(--text);
      background:
        radial-gradient(circle at 15% -10%, rgba(54, 181, 130, .18), transparent 28%),
        radial-gradient(circle at 90% 5%, rgba(62, 128, 197, .10), transparent 22%),
        var(--bg);
      font: 14px/1.45 Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    button, input { font: inherit; }
    button { color: inherit; }
    .app { height: 100vh; display: grid; grid-template-rows: 72px 112px minmax(0, 1fr); }
    header {
      display: flex; align-items: center; justify-content: space-between;
      padding: 0 24px; border-bottom: 1px solid var(--line);
      background: rgba(9, 16, 15, .84); backdrop-filter: blur(18px);
    }
    .brand { display: flex; align-items: center; gap: 13px; }
    .mark {
      width: 38px; height: 38px; display: grid; place-items: center;
      border: 1px solid rgba(101, 230, 173, .45); border-radius: 11px;
      color: var(--accent); background: rgba(101, 230, 173, .08);
      box-shadow: inset 0 0 24px rgba(101, 230, 173, .06);
      font-weight: 800; letter-spacing: -.04em;
    }
    .brand h1 { margin: 0; font-size: 16px; letter-spacing: -.01em; }
    .brand p { margin: 2px 0 0; color: var(--muted); font-size: 12px; }
    .actions { display: flex; align-items: center; gap: 10px; }
    .search {
      width: 280px; padding: 10px 13px; color: var(--text);
      border: 1px solid var(--line); border-radius: 9px; outline: none;
      background: rgba(20, 32, 29, .78);
    }
    .search:focus { border-color: rgba(101, 230, 173, .65); box-shadow: 0 0 0 3px rgba(101, 230, 173, .09); }
    .refresh {
      padding: 9px 13px; border: 1px solid var(--line); border-radius: 9px;
      background: var(--panel-2); cursor: pointer;
    }
    .refresh:hover { border-color: #40534d; }
    .summary { padding: 10px 24px 12px; border-bottom: 1px solid var(--line-soft); }
    .summary-title { margin-bottom: 7px; color: var(--accent); font-size: 10px; font-weight: 800; letter-spacing: .14em; }
    .summary-grid { display: grid; grid-template-columns: repeat(6, minmax(125px, 1fr)); gap: 9px; }
    .metric {
      display: flex; align-items: center; justify-content: space-between;
      min-width: 0; padding: 10px 12px; border: 1px solid var(--line-soft); border-radius: 10px;
      background: linear-gradient(145deg, rgba(20, 32, 29, .88), rgba(13, 23, 20, .75));
      box-shadow: var(--shadow);
    }
    .metric span { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .08em; }
    .metric strong { font-size: 17px; letter-spacing: -.03em; white-space: nowrap; }
    .workspace { min-height: 0; display: grid; grid-template-columns: 330px minmax(390px, 1fr) minmax(330px, .8fr); }
    .pane { min-width: 0; min-height: 0; overflow: auto; border-right: 1px solid var(--line); background: rgba(11, 19, 17, .68); }
    .pane:last-child { border-right: 0; }
    .pane-head {
      position: sticky; top: 0; z-index: 3; display: flex; align-items: center; justify-content: space-between;
      min-height: 49px; padding: 0 16px; border-bottom: 1px solid var(--line-soft);
      background: rgba(16, 25, 23, .94); backdrop-filter: blur(14px);
    }
    .pane-head h2 { margin: 0; font-size: 12px; text-transform: uppercase; letter-spacing: .09em; color: #c8d8d2; }
    .small { color: var(--muted); font-size: 11px; }
    .run-list { padding: 8px; }
    .session { margin: 0 0 8px; overflow: hidden; border: 1px solid var(--line-soft); border-radius: 11px; background: rgba(14, 24, 21, .54); }
    .session.active { border-color: rgba(101, 230, 173, .42); }
    .session-head {
      width: 100%; padding: 11px 12px; display: grid; grid-template-columns: 18px minmax(0, 1fr) auto;
      align-items: center; gap: 8px; text-align: left; border: 0; background: rgba(20, 32, 29, .72); cursor: pointer;
    }
    .session-head:hover { background: rgba(31, 48, 43, .72); }
    .session-chevron { color: var(--accent); font-size: 11px; transition: transform .14s ease; }
    .session.collapsed .session-chevron { transform: rotate(-90deg); }
    .session-copy { min-width: 0; }
    .session-title { overflow: hidden; white-space: nowrap; text-overflow: ellipsis; font-weight: 700; }
    .session-sub { margin-top: 2px; color: var(--muted); font-size: 10px; }
    .session-stats { margin-top: 7px; color: #b4c6bf; font: 10px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace; }
    .session-runs { padding: 5px 4px 2px 18px; border-top: 1px solid var(--line-soft); }
    .run {
      width: 100%; margin: 0 0 6px; padding: 12px; text-align: left;
      border: 1px solid transparent; border-radius: 10px; background: transparent; cursor: pointer;
    }
    .run:hover { background: rgba(31, 48, 43, .58); }
    .run.active { border-color: rgba(101, 230, 173, .34); background: rgba(47, 103, 79, .22); }
    .run-top { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
    .run-title { overflow: hidden; white-space: nowrap; text-overflow: ellipsis; font-weight: 650; }
    .run-task { margin-top: 7px; color: var(--muted); font-size: 12px; overflow: hidden; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; }
    .run-meta { display: flex; align-items: center; gap: 8px; margin-top: 9px; color: #71847d; font-size: 11px; }
    .turn-summary { margin-top: 7px; color: #b7c9c2; font: 10px/1.55 ui-monospace, SFMono-Regular, Menlo, monospace; white-space: pre-line; }
    .badge, .state {
      display: inline-flex; align-items: center; gap: 5px; padding: 2px 7px;
      border-radius: 999px; border: 1px solid var(--line); color: var(--muted); font-size: 9px; font-weight: 750; letter-spacing: .07em;
    }
    .badge.cloud { color: var(--blue); border-color: rgba(114, 183, 255, .35); }
    .badge.local { color: var(--accent); border-color: rgba(101, 230, 173, .32); }
    .dot { width: 7px; height: 7px; border-radius: 50%; background: #6e817a; box-shadow: 0 0 0 3px rgba(110, 129, 122, .08); }
    .dot.ok { background: var(--accent); }
    .dot.bad { background: var(--red); }
    .dot.warn { background: var(--amber); }
    .trace-wrap { padding: 14px 12px 30px; }
    .session-summary { margin: 0 2px 18px; padding: 13px; border: 1px solid rgba(101, 230, 173, .24); border-radius: 11px; background: rgba(30, 59, 48, .24); }
    .session-summary-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 9px; }
    .session-summary-head strong { font-size: 12px; letter-spacing: .06em; }
    .summary-mini-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 7px; }
    .summary-mini { min-width: 0; padding: 7px 8px; border-radius: 7px; background: rgba(8, 16, 14, .5); }
    .summary-mini label { display: block; color: var(--muted); font-size: 8px; text-transform: uppercase; letter-spacing: .08em; }
    .summary-mini b { display: block; margin-top: 2px; overflow: hidden; text-overflow: ellipsis; font: 10px ui-monospace, SFMono-Regular, Menlo, monospace; white-space: nowrap; }
    .trace-section { margin-bottom: 22px; }
    .trace-label { display: flex; align-items: center; gap: 8px; margin: 0 4px 9px; color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .08em; }
    .node {
      position: relative; width: calc(100% - var(--indent)); margin: 4px 0 4px var(--indent); padding: 9px 11px;
      display: grid; grid-template-columns: 26px minmax(0, 1fr) auto; align-items: center; gap: 8px;
      text-align: left; border: 1px solid var(--line-soft); border-radius: 9px;
      background: rgba(20, 32, 29, .72); cursor: pointer;
    }
    .node:before { content: ""; position: absolute; left: -15px; top: -6px; bottom: 50%; width: 12px; border-left: 1px solid #31443e; border-bottom: 1px solid #31443e; border-radius: 0 0 0 5px; }
    .node.root:before { display: none; }
    .node:hover, .node.active { border-color: rgba(101, 230, 173, .45); background: rgba(32, 55, 47, .82); }
    .node.error { border-color: rgba(255, 133, 133, .48); background: rgba(92, 31, 31, .24); }
    .node.error .node-icon { color: var(--red); background: rgba(255, 133, 133, .1); }
    .node-icon { width: 25px; height: 25px; display: grid; place-items: center; border-radius: 7px; color: var(--accent); background: rgba(101, 230, 173, .08); font-size: 10px; font-weight: 800; }
    .node-name { overflow: hidden; white-space: nowrap; text-overflow: ellipsis; font-weight: 600; }
    .node-sub { color: var(--muted); font-size: 10px; }
    .duration { color: #a5b8b0; font: 11px ui-monospace, SFMono-Regular, Menlo, monospace; }
    .node-tail { display: grid; justify-items: end; gap: 3px; }
    .node-time { color: #71847d; font: 9px ui-monospace, SFMono-Regular, Menlo, monospace; }
    .node-state { display: inline-flex; align-items: center; gap: 5px; color: var(--muted); font-size: 9px; font-weight: 750; letter-spacing: .06em; }
    .conversation { position: relative; margin: 0 4px; padding-left: 8px; }
    .conversation:before { content: ""; position: absolute; left: 20px; top: 14px; bottom: 14px; border-left: 1px solid #31443e; }
    .message {
      position: relative; width: 100%; margin: 5px 0; padding: 9px 11px;
      display: grid; grid-template-columns: 34px minmax(0, 1fr) auto; align-items: center; gap: 9px;
      text-align: left; border: 1px solid var(--line-soft); border-radius: 9px;
      background: rgba(17, 28, 25, .74); cursor: pointer;
    }
    .message:hover, .message.active { border-color: rgba(114, 183, 255, .45); background: rgba(27, 46, 41, .86); }
    .message-role { z-index: 1; width: 30px; height: 24px; display: grid; place-items: center; border-radius: 7px; color: var(--blue); background: #101b1a; font-size: 9px; font-weight: 800; }
    .message[data-role="user"] .message-role { color: var(--accent); }
    .message[data-role="assistant"] .message-role { color: var(--blue); }
    .message[data-role="tool"] .message-role { color: var(--amber); }
    .message-preview { overflow: hidden; white-space: nowrap; text-overflow: ellipsis; color: #cfe2da; font-size: 11px; }
    .message-seq { color: #71847d; font: 10px ui-monospace, SFMono-Regular, Menlo, monospace; }
    .detail { padding: 16px; }
    .detail-empty, .empty { padding: 30px 18px; color: var(--muted); text-align: center; }
    .detail-title { margin: 0; font-size: 18px; letter-spacing: -.02em; }
    .detail-chips { display: flex; flex-wrap: wrap; gap: 7px; margin: 10px 0 18px; }
    .kv { display: grid; grid-template-columns: 112px minmax(0, 1fr); gap: 9px; padding: 8px 0; border-bottom: 1px solid var(--line-soft); }
    .kv label { color: var(--muted); font-size: 11px; }
    .kv div { min-width: 0; overflow-wrap: anywhere; font-size: 12px; }
    .usage-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 7px; margin-top: 8px; }
    .usage-cell { padding: 9px; border: 1px solid var(--line-soft); border-radius: 8px; background: rgba(11, 18, 16, .65); }
    .usage-cell label { display: block; color: var(--muted); font-size: 9px; text-transform: uppercase; letter-spacing: .08em; }
    .usage-cell strong { display: block; margin-top: 3px; font: 12px ui-monospace, SFMono-Regular, Menlo, monospace; }
    .block-title { margin: 19px 0 8px; color: var(--muted); font-size: 10px; font-weight: 700; letter-spacing: .1em; text-transform: uppercase; }
    pre {
      margin: 0; padding: 12px; max-height: 320px; overflow: auto;
      border: 1px solid var(--line-soft); border-radius: 9px; color: #cfe2da;
      background: #0b1210; font: 11px/1.55 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      white-space: pre-wrap; overflow-wrap: anywhere;
    }
    details { margin-top: 20px; border-top: 1px solid var(--line-soft); }
    details summary { padding: 12px 0; color: var(--muted); cursor: pointer; font-size: 10px; font-weight: 700; letter-spacing: .1em; text-transform: uppercase; }
    .error-banner { margin: 10px 12px; padding: 10px 12px; border: 1px solid rgba(245, 197, 107, .28); border-radius: 9px; color: var(--amber); background: rgba(245, 197, 107, .07); font-size: 11px; }
    .spin { animation: spin .85s linear infinite; }
    @keyframes spin { to { transform: rotate(360deg); } }
    @media (max-width: 1200px) {
      .workspace { grid-template-columns: 300px minmax(370px, 1fr) 330px; }
      .summary-grid { grid-template-columns: repeat(6, minmax(110px, 1fr)); }
    }
  </style>
</head>
<body>
<div class="app">
  <header>
    <div class="brand">
      <div class="mark">AE</div>
      <div><h1>Aegis Trace Viewer</h1><p>Runtime records with Langfuse detail</p></div>
    </div>
    <div class="actions">
      <input id="search" class="search" type="search" placeholder="Filter session, task, model, execution id…">
      <button id="refresh" class="refresh" type="button">↻ Refresh</button>
    </div>
  </header>
  <section class="summary">
    <div class="summary-title">ALL SESSIONS</div>
    <div class="summary-grid">
      <div class="metric"><span>Sessions / Turns</span><strong id="m-count">—</strong></div>
      <div class="metric"><span>Model / Tool Calls</span><strong id="m-calls">—</strong></div>
      <div class="metric"><span>Input / Output</span><strong id="m-tokens">—</strong></div>
      <div class="metric"><span>Cache Read / Write</span><strong id="m-cache">—</strong></div>
      <div class="metric"><span>Cost</span><strong id="m-cost">—</strong></div>
      <div class="metric"><span>Errors</span><strong id="m-errors">—</strong></div>
    </div>
  </section>
  <main class="workspace">
    <section class="pane">
      <div class="pane-head"><h2>Sessions</h2><span id="source-status" class="small">Loading…</span></div>
      <div id="list-error"></div>
      <div id="run-list" class="run-list"></div>
    </section>
    <section class="pane">
      <div class="pane-head"><h2>Trace tree</h2><span id="trace-id" class="small"></span></div>
      <div id="trace-error"></div>
      <div id="trace" class="trace-wrap"><div class="empty">Select an execution</div></div>
    </section>
    <section class="pane">
      <div class="pane-head"><h2>Observation</h2><span id="detail-source" class="small"></span></div>
      <div id="detail" class="detail"><div class="detail-empty">Choose a trace node to inspect its data.</div></div>
    </section>
  </main>
</div>
<script>
  const state = { executions: [], filtered: [], selected: null, selectedSession: null, detail: null, node: null, errorRollups: new WeakSet(), expandedSessions: new Set(), sessionsInitialized: false };
  const $ = (id) => document.getElementById(id);
  const text = (value, fallback = "—") => value === null || value === undefined || value === "" ? fallback : String(value);
  const optionalNumber = (value) => value === null || value === undefined || value === "" || !Number.isFinite(Number(value)) ? null : Number(value);
  const number = (value) => optionalNumber(value) ?? 0;
  const compact = (value) => {
    const numeric = optionalNumber(value);
    return numeric === null ? "—" : new Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 1 }).format(numeric);
  };
  const money = (value, detailed = false) => {
    const numeric = optionalNumber(value);
    if (numeric === null) return "—";
    if (numeric === 0) return "$0.00";
    return `$${numeric.toFixed(detailed || Math.abs(numeric) < .01 ? 4 : 2)}`;
  };
  const elapsed = (ms) => {
    if (ms === null || ms === undefined) return "—";
    if (ms < 1000) return `${Math.round(ms)} ms`;
    if (ms < 60000) return `${(ms / 1000).toFixed(2)} s`;
    return `${(ms / 60000).toFixed(1)} min`;
  };
  const when = (value) => {
    if (!value) return "unknown time";
    const date = new Date(value);
    return Number.isNaN(date.valueOf()) ? value : date.toLocaleString();
  };
  const clock = (value) => {
    if (!value) return "—";
    const date = new Date(value);
    return Number.isNaN(date.valueOf()) ? text(value) : date.toLocaleTimeString([], { hour12: false });
  };
  const structured = (value) => {
    let parsed = value;
    for (let depth = 0; depth < 3 && typeof parsed === "string"; depth += 1) {
      const trimmed = parsed.trim();
      if (!trimmed || !["{", "[", '"'].includes(trimmed[0])) break;
      try {
        const next = JSON.parse(trimmed);
        if (next === parsed) break;
        parsed = next;
      } catch (_) {
        break;
      }
    }
    return parsed;
  };
  const formatted = (value) => {
    const parsed = structured(value);
    return typeof parsed === "string" ? parsed : JSON.stringify(parsed, null, 2);
  };
  const element = (tag, className, value) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (value !== undefined) node.textContent = value;
    return node;
  };
  const badge = (value, type = "") => {
    const node = element("span", `badge ${type}`, value);
    return node;
  };
  const sessionKey = (item) => item.session_id ? `session:${item.session_id}` : `execution:${item.id}`;
  const shortId = (value) => value && value.length > 16 ? `…${value.slice(-12)}` : text(value, "no session id");
  const first = (...values) => values.find((value) => value !== null && value !== undefined && value !== "");
  const sumState = (values) => {
    const known = values.map(optionalNumber).filter((value) => value !== null);
    return {
      value: known.length ? known.reduce((total, value) => total + value, 0) : null,
      partial: known.length > 0 && known.length < values.length,
    };
  };
  const displayTotal = (stateValue, formatter = compact) => stateValue.partial && stateValue.value !== null
    ? `≥${formatter(stateValue.value)}`
    : formatter(stateValue.value);
  const cacheInputTotal = (usage) => {
    if (usage.inputIncludingCache !== null) return usage.inputIncludingCache;
    if (usage.total !== null && usage.output !== null) {
      const candidate = usage.total - usage.output;
      return candidate >= 0 ? candidate : null;
    }
    const values = [usage.input, usage.cacheRead, usage.cacheWrite];
    if (values.some((value) => value === null)) return null;
    return values.reduce((sum, value) => sum + value, 0);
  };
  const cacheHitRate = (usage) => {
    const total = cacheInputTotal(usage);
    return usage.cacheRead !== null && total !== null && total > 0
      ? usage.cacheRead / total * 100
      : null;
  };
  const percent = (value) => optionalNumber(value) === null ? "—" : `${Number(value).toFixed(1)}%`;
  const usageOf = (item) => {
    const usage = item?.usage || item?.usageDetails || {};
    const cost = item?.costDetails || {};
    return {
      input: optionalNumber(first(usage.input_tokens, usage.input, usage.prompt_tokens, usage.inputTokens)),
      inputIncludingCache: optionalNumber(first(usage.input_tokens_including_cache, usage.prompt_tokens)),
      output: optionalNumber(first(usage.output_tokens, usage.output, usage.completion_tokens, usage.outputTokens)),
      total: optionalNumber(first(usage.total_tokens, usage.total, usage.totalTokens)),
      cacheRead: optionalNumber(first(usage.cache_read_tokens, usage.cache_read_input_tokens, usage.cache_read, usage.cacheReadInputTokens)),
      cacheWrite: optionalNumber(first(usage.cache_write_tokens, usage.cache_creation_input_tokens, usage.cache_write_input_tokens, usage.cache_write, usage.cacheCreationInputTokens)),
      cost: optionalNumber(first(usage.cost, usage.total_cost, cost.total, item?.calculatedTotalCost)),
    };
  };
  const statsOf = (item) => ({
    modelCalls: optionalNumber(item?.stats?.model_calls),
    toolCalls: optionalNumber(item?.stats?.tool_calls),
    errors: optionalNumber(item?.stats?.errors),
  });
  const aggregateExecutions = (items) => {
    const usages = items.map(usageOf);
    const stats = items.map(statsOf);
    const errorValues = stats.map((item, index) => item.errors ?? (items[index].has_error || items[index].success === false ? 1 : null));
    const input = sumState(usages.map((item) => item.input));
    const cacheRead = sumState(usages.map((item) => item.cacheRead));
    const cacheWrite = sumState(usages.map((item) => item.cacheWrite));
    const cacheInput = sumState(usages.map(cacheInputTotal));
    const exactCacheRate = cacheRead.partial || cacheInput.partial
      ? null
      : cacheHitRate({cacheRead: cacheRead.value, inputIncludingCache: cacheInput.value, total: null, output: null, input: null, cacheWrite: null});
    return {
      turns: items.length,
      duration: sumState(items.map((item) => item.latency_ms)),
      modelCalls: sumState(stats.map((item) => item.modelCalls)),
      toolCalls: sumState(stats.map((item) => item.toolCalls)),
      errors: sumState(errorValues),
      input,
      output: sumState(usages.map((item) => item.output)),
      cacheRead,
      cacheWrite,
      cacheHitRate: exactCacheRate,
      cost: sumState(usages.map((item) => item.cost)),
    };
  };
  const mergeKnown = (primary = {}, supplement = {}) => {
    const merged = {...supplement};
    for (const [key, value] of Object.entries(primary || {})) {
      if (value !== null && value !== undefined) merged[key] = value;
    }
    return merged;
  };
  const mergeStats = (primary = {}, supplement = {}) => {
    const merged = {};
    for (const key of ["model_calls", "tool_calls", "errors"]) {
      const left = optionalNumber(primary?.[key]);
      const right = optionalNumber(supplement?.[key]);
      merged[key] = left === null ? right : right === null ? left : Math.max(left, right);
    }
    return merged;
  };
  const statusOf = (items) => {
    if (items.some((item) => item.has_error || optionalNumber(item.stats?.errors) > 0 || item.success === false)) return {label: "ERROR", className: "bad"};
    if (items.some((item) => item.passed === false)) return {label: "FAIL", className: "warn"};
    if (items.length && items.every((item) => item.success === true)) return {label: "OK", className: "ok"};
    return {label: "UNKNOWN", className: ""};
  };
  const coreValue = (value, keys, depth = 0, seen = new Set()) => {
    const parsed = structured(value);
    if (!parsed || typeof parsed !== "object" || depth > 5 || seen.has(parsed)) return null;
    seen.add(parsed);
    for (const key of keys) {
      if (parsed[key] !== null && parsed[key] !== undefined && parsed[key] !== "") return parsed[key];
    }
    for (const nested of Object.values(parsed)) {
      const found = coreValue(nested, keys, depth + 1, seen);
      if (found !== null) return found;
    }
    return null;
  };
  const observationKind = (item, source) => {
    if (source === "local") return item.type || "span";
    const name = String(item.name || "").toLowerCase();
    const kind = String(item.type || "").toUpperCase();
    if (name === "model call" || kind === "GENERATION") return "model";
    if (name.startsWith("tool call:") || kind === "TOOL") return "tool";
    if (name === "final result") return "final";
    return "span";
  };
  const providerOf = (item) => first(item.metadata?.provider, item.provider, item.modelParameters?.provider);
  const modelOf = (item) => first(item.metadata?.model, item.providedModelName, item.model, coreValue(item.input, ["model"]));
  const finishReasonOf = (item) => first(item.metadata?.finish_reason, coreValue(item.output, ["finish_reason", "finishReason", "stop_reason"]));
  const exitCodeOf = (item) => optionalNumber(first(item.metadata?.exit_code, coreValue(item.output, ["exit_code", "exitCode"])));
  const toolNameOf = (item) => first(item.metadata?.tool_name, String(item.name || "").replace(/^Tool Call:\s*/i, ""), "tool");
  const errorBanner = (target, value) => {
    target.replaceChildren();
    if (value) target.append(element("div", "error-banner", value));
  };

  async function fetchJSON(url) {
    const response = await fetch(url, { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    return payload;
  }

  async function loadExecutions(keepSelection = true) {
    $("refresh").classList.add("spin");
    try {
      const payload = await fetchJSON("/api/executions");
      state.executions = payload.executions || [];
      const status = payload.status || {};
      $("source-status").textContent = status.langfuse_enabled ? "Local · loading Langfuse…" : "Local records";
      errorBanner($("list-error"), null);
      updateMetrics();
      applyFilter();
      const selectedStillExists = state.executions.some((item) => item.id === state.selected);
      if ((!keepSelection || !selectedStillExists) && state.filtered.length) {
        await selectExecution(state.filtered[0].id);
      } else if (!state.filtered.length) {
        $("trace").innerHTML = '<div class="empty">No ExecutionRecords or Langfuse traces found.</div>';
      }
      if (status.langfuse_enabled) loadLangfuseRoots();
    } catch (error) {
      errorBanner($("list-error"), error.message);
    } finally {
      $("refresh").classList.remove("spin");
    }
  }

  async function loadLangfuseRoots() {
    try {
      const payload = await fetchJSON("/api/langfuse/roots");
      const cloud = payload.executions || [];
      const byTrace = new Map(state.executions.filter((item) => item.trace_id).map((item) => [item.trace_id, item]));
      for (const item of cloud) {
        const existing = byTrace.get(item.trace_id);
        if (existing) {
          existing.langfuse_available = true;
          if (!existing.session_id) existing.session_id = item.session_id;
          if (existing.success === null || existing.success === undefined) existing.success = item.success;
          existing.has_error = Boolean(existing.has_error || item.has_error);
          existing.usage = mergeKnown(existing.usage, item.usage);
          existing.stats = mergeStats(existing.stats, item.stats);
        }
        else {
          state.executions.push(item);
          if (item.trace_id) byTrace.set(item.trace_id, item);
        }
      }
      state.executions.sort((a, b) => text(b.started_at, "").localeCompare(text(a.started_at, "")));
      $("source-status").textContent = payload.error ? "Local records" : "Local + Langfuse";
      const warning = payload.error
        ? `Langfuse unavailable: ${payload.error}`
        : payload.stats_error || payload.usage_error
          ? `Trace list loaded; summary unavailable: ${payload.stats_error || payload.usage_error}`
          : null;
      errorBanner($("list-error"), warning);
      updateMetrics();
      applyFilter();
      if (!state.selected && state.filtered.length) await selectExecution(state.filtered[0].id);
    } catch (error) {
      $("source-status").textContent = "Local records";
      errorBanner($("list-error"), `Langfuse unavailable: ${error.message}`);
    }
  }

  function updateMetrics() {
    const sessions = new Set(state.executions.map(sessionKey));
    $("m-count").textContent = `${compact(sessions.size)} / ${compact(state.executions.length)}`;
    const aggregate = aggregateExecutions(state.executions);
    $("m-calls").textContent = `${displayTotal(aggregate.modelCalls)} / ${displayTotal(aggregate.toolCalls)}`;
    $("m-tokens").textContent = `↑${displayTotal(aggregate.input)} / ↓${displayTotal(aggregate.output)}`;
    $("m-cache").textContent = `R ${displayTotal(aggregate.cacheRead)} / W ${displayTotal(aggregate.cacheWrite)} · ${percent(aggregate.cacheHitRate)}`;
    $("m-cost").textContent = displayTotal(aggregate.cost, money);
    $("m-errors").textContent = displayTotal(aggregate.errors);
  }

  function applyFilter() {
    const query = $("search").value.trim().toLowerCase();
    if (!query) {
      state.filtered = [...state.executions];
      renderRuns();
      return;
    }
    const matchedSessions = new Set(state.executions.filter((item) => {
      const haystack = [item.id, item.execution_id, item.trace_id, item.session_id, item.task, item.model, item.provider, item.name]
        .filter(Boolean).join(" ").toLowerCase();
      return haystack.includes(query);
    }).map(sessionKey));
    state.filtered = state.executions.filter((item) => matchedSessions.has(sessionKey(item)));
    renderRuns();
  }

  function renderRuns() {
    const target = $("run-list");
    target.replaceChildren();
    if (!state.filtered.length) {
      target.append(element("div", "empty", "No matching executions."));
      return;
    }
    const grouped = new Map();
    for (const item of state.filtered) {
      const key = sessionKey(item);
      if (!grouped.has(key)) grouped.set(key, []);
      grouped.get(key).push(item);
    }
    const groups = [...grouped.entries()].map(([key, items]) => {
      items.sort((a, b) => text(a.started_at, "").localeCompare(text(b.started_at, "")));
      return {key, items, latest: items[items.length - 1]?.started_at};
    }).sort((a, b) => text(b.latest, "").localeCompare(text(a.latest, "")));
    if (!state.sessionsInitialized && groups.length) {
      state.expandedSessions.add(groups[0].key);
      state.selectedSession = groups[0].key;
      state.sessionsInitialized = true;
    }
    const searching = Boolean($("search").value.trim());
    for (const group of groups) {
      const expanded = searching || state.expandedSessions.has(group.key);
      const session = element("section", `session${expanded ? "" : " collapsed"}${state.selectedSession === group.key ? " active" : ""}`);
      const head = element("button", "session-head");
      head.type = "button";
      head.setAttribute("aria-expanded", String(expanded));
      head.append(element("span", "session-chevron", "▾"));
      const copy = element("span", "session-copy");
      const firstTask = group.items.find((item) => item.task)?.task;
      copy.append(element("div", "session-title", firstTask || "Untitled session"));
      const sessionId = group.items.find((item) => item.session_id)?.session_id;
      copy.append(element("div", "session-sub", `${group.items.length} ${group.items.length === 1 ? "turn" : "turns"} · ${shortId(sessionId)} · ${when(group.latest)}`));
      const sessionAggregate = aggregateExecutions(group.items);
      copy.append(element("div", "session-stats", `${displayTotal(sessionAggregate.duration, elapsed)} · ↑${displayTotal(sessionAggregate.input)} ↓${displayTotal(sessionAggregate.output)} · Cache R ${displayTotal(sessionAggregate.cacheRead)} · Hit ${percent(sessionAggregate.cacheHitRate)} · ${displayTotal(sessionAggregate.cost, money)}`));
      head.append(copy);
      const aggregate = statusOf(group.items);
      const sessionStatus = element("span", "state");
      sessionStatus.append(element("i", `dot ${aggregate.className}`));
      sessionStatus.append(document.createTextNode(aggregate.label));
      head.append(sessionStatus);
      head.addEventListener("click", () => {
        state.selectedSession = group.key;
        if (state.expandedSessions.has(group.key)) state.expandedSessions.delete(group.key);
        else state.expandedSessions.add(group.key);
        if (!group.items.some((item) => item.id === state.selected)) {
          selectExecution(group.items.at(-1).id);
        } else {
          renderRuns();
          renderTrace();
        }
      });
      session.append(head);

      if (expanded) {
        const turns = element("div", "session-runs");
        group.items.forEach((item, index) => {
          const button = element("button", `run${item.id === state.selected ? " active" : ""}`);
          button.type = "button";
          button.addEventListener("click", () => selectExecution(item.id));
          const top = element("div", "run-top");
          top.append(element("div", "run-title", `Turn ${index + 1}`));
          const turnStatusValue = statusOf([item]);
          const turnStatus = element("span", "state");
          turnStatus.append(element("i", `dot ${turnStatusValue.className}`));
          turnStatus.append(document.createTextNode(turnStatusValue.label));
          top.append(turnStatus);
          button.append(top);
          button.append(element("div", "run-task", item.task || item.execution_id || item.trace_id || "No task text"));
          const turn = aggregateExecutions([item]);
          button.append(element("div", "turn-summary", `${displayTotal(turn.duration, elapsed)} · ${displayTotal(turn.modelCalls)} Model · ${displayTotal(turn.toolCalls)} Tool\n↑${displayTotal(turn.input)} ↓${displayTotal(turn.output)} · Cache R ${displayTotal(turn.cacheRead)}/W ${displayTotal(turn.cacheWrite)} · Hit ${percent(turn.cacheHitRate)} · ${displayTotal(turn.cost, money)}`));
          const meta = element("div", "run-meta");
          meta.append(badge(item.source === "local" ? "LOCAL" : "CLOUD", item.source === "local" ? "local" : "cloud"));
          if (item.langfuse_available && item.source === "local") meta.append(badge("LF", "cloud"));
          meta.append(document.createTextNode(`${when(item.started_at)} · ${elapsed(item.latency_ms)}`));
          button.append(meta);
          turns.append(button);
        });
        session.append(turns);
      }
      target.append(session);
    }
  }

  async function selectExecution(id) {
    state.selected = id;
    state.detail = null;
    state.node = null;
    const selectedSummary = state.executions.find((item) => item.id === id);
    if (selectedSummary) {
      state.selectedSession = sessionKey(selectedSummary);
      state.expandedSessions.add(state.selectedSession);
    }
    renderRuns();
    $("trace").innerHTML = '<div class="empty">Loading trace…</div>';
    $("detail").innerHTML = '<div class="detail-empty">Choose a trace node to inspect its data.</div>';
    try {
      const summary = selectedSummary;
      if (summary?.source === "langfuse") {
        state.detail = { record: null, langfuse: { trace_id: summary.trace_id, observations: [], error: null } };
        renderTrace();
        await loadLangfuseTrace(summary.trace_id);
      } else {
        state.detail = await fetchJSON(`/api/executions/${encodeURIComponent(id)}`);
        renderTrace();
        if (state.detail.langfuse?.trace_id) loadLangfuseTrace(state.detail.langfuse.trace_id);
      }
    } catch (error) {
      $("trace").replaceChildren(element("div", "empty", error.message));
    }
  }

  async function loadLangfuseTrace(traceId) {
    if (!traceId) return;
    try {
      const cloud = await fetchJSON(`/api/langfuse/traces/${encodeURIComponent(traceId)}`);
      if (state.detail?.langfuse?.trace_id !== traceId) return;
      state.detail.langfuse = cloud;
      const summary = state.executions.find((item) => item.trace_id === traceId);
      if (summary && cloud.usage) {
        summary.usage = mergeKnown(summary.usage, cloud.usage);
        summary.stats = mergeStats(summary.stats, cloud.stats);
        if (optionalNumber(cloud.stats?.errors) > 0) summary.has_error = true;
        updateMetrics();
        renderRuns();
      }
      renderTrace();
    } catch (error) {
      if (state.detail?.langfuse?.trace_id !== traceId) return;
      state.detail.langfuse.error = error.message;
      renderTrace();
    }
  }

  function renderTrace() {
    const target = $("trace");
    target.replaceChildren();
    const record = state.detail?.record;
    const langfuse = state.detail?.langfuse || {};
    $("trace-id").textContent = langfuse.trace_id ? `…${langfuse.trace_id.slice(-10)}` : "";
    errorBanner($("trace-error"), langfuse.error ? `Langfuse: ${langfuse.error}` : null);
    state.errorRollups = new WeakSet();
    let rendered = false;
    if (state.selectedSession) {
      const sessionItems = state.executions.filter((item) => sessionKey(item) === state.selectedSession);
      if (sessionItems.length) {
        renderSessionSummary(target, sessionItems);
        rendered = true;
      }
    }
    const conversation = extractConversation(record, langfuse.observations || []);
    if (conversation.length) {
      const section = element("section", "trace-section");
      section.append(element("div", "trace-label", `Session conversation · ${conversation.length} messages`));
      renderConversation(section, conversation);
      target.append(section);
      rendered = true;
    }
    if (record?.steps?.length) {
      const section = element("section", "trace-section");
      section.append(element("div", "trace-label", "ExecutionRecord · runtime truth"));
      renderTree(section, record.steps, "local");
      target.append(section);
      rendered = true;
    }
    if (langfuse.observations?.length) {
      const section = element("section", "trace-section");
      section.append(element("div", "trace-label", "Langfuse · reported observations"));
      renderTree(section, langfuse.observations, "langfuse");
      target.append(section);
      rendered = true;
    }
    if (!rendered) target.append(element("div", "empty", "This execution has no recorded steps."));
    if (state.node) showDetail(state.node.item, state.node.source);
    else if (record) showDetail(record, "ExecutionRecord");
    else if (langfuse.observations?.length) showDetail(langfuse.observations[0], "Langfuse");
  }

  function renderSessionSummary(target, items) {
    const aggregate = aggregateExecutions(items);
    const session = element("section", "session-summary");
    const head = element("div", "session-summary-head");
    head.append(element("strong", "", "SESSION SUMMARY"));
    const status = statusOf(items);
    const statusNode = element("span", "state");
    statusNode.append(element("i", `dot ${status.className}`));
    statusNode.append(document.createTextNode(status.label));
    head.append(statusNode);
    session.append(head);
    const grid = element("div", "summary-mini-grid");
    const values = [
      ["Turns", compact(aggregate.turns)],
      ["Model calls", displayTotal(aggregate.modelCalls)],
      ["Tool calls", displayTotal(aggregate.toolCalls)],
      ["Duration", displayTotal(aggregate.duration, elapsed)],
      ["Input / Output", `↑${displayTotal(aggregate.input)} / ↓${displayTotal(aggregate.output)}`],
      ["Cache read / write", `R ${displayTotal(aggregate.cacheRead)} / W ${displayTotal(aggregate.cacheWrite)}`],
      ["Cache hit rate", percent(aggregate.cacheHitRate)],
      ["Cost", displayTotal(aggregate.cost, money)],
      ["Errors", displayTotal(aggregate.errors)],
    ];
    for (const [label, value] of values) {
      const cell = element("div", "summary-mini");
      cell.append(element("label", "", label));
      cell.append(element("b", "", value));
      grid.append(cell);
    }
    session.append(grid);
    target.append(session);
  }

  function extractConversation(record, observations) {
    const cloudModels = observations
      .filter((item) => item.name === "Model Call" || String(item.type || "").toUpperCase() === "GENERATION")
      .sort((a, b) => text(a.startTime, "").localeCompare(text(b.startTime, "")));
    const localModels = (record?.steps || [])
      .filter((item) => item.type === "model" || item.name === "Model Call")
      .sort((a, b) => number(a.sequence) - number(b.sequence));
    const model = cloudModels.at(-1) || localModels.at(-1);
    if (!model) return [];
    const parsed = structured(model.input);
    const rawMessages = Array.isArray(parsed) ? parsed : parsed?.messages;
    if (!Array.isArray(rawMessages)) return [];
    const messages = rawMessages.filter((item) => item && typeof item === "object").map((item, index) => ({
      __conversation: true,
      sequence: index + 1,
      name: `${text(item.role, "message").toUpperCase()} message`,
      type: "message",
      role: text(item.role, "message").toLowerCase(),
      content: item.content,
      tool_calls: item.tool_calls,
      tool_call_id: item.tool_call_id,
      reasoning_content: item.reasoning_content,
      raw: item,
    }));
    const output = structured(model.output);
    if (output && typeof output === "object" && (output.content || output.tool_calls?.length)) {
      messages.push({
        __conversation: true,
        sequence: messages.length + 1,
        name: "ASSISTANT message",
        type: "message",
        role: "assistant",
        content: output.content,
        tool_calls: output.tool_calls,
        finish_reason: output.finish_reason,
        raw: output,
      });
    }
    return messages;
  }

  function messagePreview(item) {
    const content = structured(item.content);
    if (typeof content === "string" && content.trim()) return content.replace(/\s+/g, " ").trim();
    if (content && typeof content === "object") return formatted(content).replace(/\s+/g, " ").trim();
    const calls = item.tool_calls || [];
    if (calls.length) return calls.map((call) => call.name || call.function?.name || "tool").join(", ");
    return "Empty content";
  }

  function renderConversation(target, messages) {
    const timeline = element("div", "conversation");
    for (const item of messages) {
      const node = element("button", "message");
      node.type = "button";
      node.dataset.role = item.role;
      node.addEventListener("click", () => {
        document.querySelectorAll(".node.active, .message.active").forEach((entry) => entry.classList.remove("active"));
        node.classList.add("active");
        state.node = {item, source: "Conversation"};
        showDetail(item, "Conversation");
      });
      node.append(element("span", "message-role", item.role.slice(0, 4).toUpperCase()));
      const labels = element("span", "");
      labels.append(element("div", "node-name", item.name));
      labels.append(element("div", "message-preview", messagePreview(item)));
      node.append(labels);
      node.append(element("span", "message-seq", `#${item.sequence}`));
      timeline.append(node);
    }
    target.append(timeline);
  }

  function nodeStatus(item, source) {
    if (source === "local") {
      if (item.success === false || item.error) return {label: "ERROR", className: "bad"};
      if (item.success === true) return {label: "OK", className: "ok"};
      return {label: item.finished_at ? "DONE" : "RUNNING", className: item.finished_at ? "ok" : "warn"};
    }
    if (item.metadata?.success === true) return {label: "OK", className: "ok"};
    if (item.metadata?.success === false) return {label: "ERROR", className: "bad"};
    const level = String(item.level || "").toUpperCase();
    if (["ERROR", "FATAL"].includes(level) || item.error) return {label: "ERROR", className: "bad"};
    if (level === "WARNING") return {label: "WARN", className: "warn"};
    if (!item.endTime) return {label: "RUNNING", className: "warn"};
    return {label: "OK", className: "ok"};
  }

  const rolledNodeStatus = (item, source) => state.errorRollups.has(item)
    ? {label: "ERROR", className: "bad"}
    : nodeStatus(item, source);

  function latencyOf(item, source) {
    if (source === "local") return optionalNumber(item.latency_ms);
    const seconds = optionalNumber(item.latency);
    if (seconds !== null) return seconds * 1000;
    const started = Date.parse(item.startTime || "");
    const finished = Date.parse(item.endTime || "");
    return Number.isFinite(started) && Number.isFinite(finished) ? Math.max(finished - started, 0) : null;
  }

  function modelSummary(item, source, growth) {
    const usage = usageOf(item);
    const growthText = growth === null || growth === undefined ? "" : ` (${growth >= 0 ? "+" : ""}${compact(growth)})`;
    const hitRate = cacheHitRate(usage);
    return `${elapsed(latencyOf(item, source))} · ↑${compact(usage.input)}${growthText} ↓${compact(usage.output)} · Cache R ${compact(usage.cacheRead)}/W ${compact(usage.cacheWrite)} · Hit ${percent(hitRate)} · ${money(usage.cost, true)}`;
  }

  function toolSummary(item, source) {
    const name = toolNameOf(item);
    const query = coreValue(item.input, ["query"]);
    const command = coreValue(item.input, ["command", "cmd"]);
    const primary = first(command, query);
    const exitCode = exitCodeOf(item);
    const details = [];
    if (primary !== null && primary !== undefined) details.push(String(primary).replace(/\s+/g, " ").slice(0, 180));
    if (exitCode !== null) details.push(`exit ${exitCode}`);
    details.push(elapsed(latencyOf(item, source)));
    return {name, summary: details.join(" · ")};
  }

  function renderTree(target, items, source) {
    const idOf = (item) => source === "local" ? item.step_id : item.id;
    const parentOf = (item) => source === "local" ? item.parent_step_id : item.parentObservationId;
    const byParent = new Map();
    const known = new Set(items.map(idOf));
    const byId = new Map(items.map((item) => [idOf(item), item]));
    for (const item of items) {
      const rawParent = parentOf(item);
      const parent = rawParent && known.has(rawParent) ? rawParent : null;
      if (!byParent.has(parent)) byParent.set(parent, []);
      byParent.get(parent).push(item);
    }
    for (const item of items) {
      if (nodeStatus(item, source).className !== "bad") continue;
      let parent = parentOf(item);
      const visited = new Set();
      while (parent && byId.has(parent) && !visited.has(parent)) {
        visited.add(parent);
        state.errorRollups.add(byId.get(parent));
        parent = parentOf(byId.get(parent));
      }
    }
    for (const children of byParent.values()) {
      children.sort((a, b) => text(a.sequence ?? a.startTime, "").localeCompare(text(b.sequence ?? b.startTime, ""), undefined, { numeric: true }));
    }
    const growthById = new Map();
    let previousInput = null;
    const models = items.filter((item) => observationKind(item, source) === "model")
      .sort((a, b) => text(a.sequence ?? a.startTime, "").localeCompare(text(b.sequence ?? b.startTime, ""), undefined, { numeric: true }));
    for (const model of models) {
      const current = usageOf(model).input;
      growthById.set(idOf(model), current !== null && previousInput !== null ? current - previousInput : null);
      previousInput = current;
    }
    const seen = new Set();
    const visit = (parent, depth) => {
      for (const item of byParent.get(parent) || []) {
        const id = idOf(item);
        if (seen.has(id)) continue;
        seen.add(id);
        const status = rolledNodeStatus(item, source);
        const node = element("button", `node${depth === 0 ? " root" : ""}${status.className === "bad" ? " error" : ""}`);
        node.type = "button";
        node.style.setProperty("--indent", `${Math.min(depth, 7) * 20}px`);
        node.addEventListener("click", () => {
          document.querySelectorAll(".node.active").forEach((entry) => entry.classList.remove("active"));
          node.classList.add("active");
          state.node = {item, source: source === "local" ? "ExecutionRecord" : "Langfuse"};
          showDetail(state.node.item, state.node.source);
        });
        const rawKind = observationKind(item, source);
        const kind = rawKind.toUpperCase();
        node.append(element("span", "node-icon", kind.slice(0, 2)));
        const labels = element("span", "");
        if (rawKind === "model") {
          labels.append(element("div", "node-name", text(modelOf(item), "Unknown model")));
          labels.append(element("div", "node-sub", modelSummary(item, source, growthById.get(id))));
        } else if (rawKind === "tool") {
          const tool = toolSummary(item, source);
          labels.append(element("div", "node-name", tool.name));
          labels.append(element("div", "node-sub", tool.summary));
        } else {
          labels.append(element("div", "node-name", item.name || "Observation"));
          labels.append(element("div", "node-sub", rawKind === "final" ? `Final · ${elapsed(latencyOf(item, source))}` : text(providerOf(item), kind)));
        }
        node.append(labels);
        const duration = latencyOf(item, source);
        const start = source === "local" ? item.started_at : item.startTime;
        const tail = element("span", "node-tail");
        const statusNode = element("span", "node-state");
        statusNode.append(element("i", `dot ${status.className}`));
        statusNode.append(document.createTextNode(`${status.label} · ${elapsed(duration)}`));
        tail.append(statusNode);
        tail.append(element("span", "node-time", clock(start)));
        node.append(tail);
        target.append(node);
        visit(id, depth + 1);
      }
    };
    visit(null, 0);
  }

  function appendField(target, label, value) {
    if (value === null || value === undefined || value === "") return;
    const row = element("div", "kv");
    row.append(element("label", "", label));
    row.append(element("div", "", text(value)));
    target.append(row);
  }

  function appendBlock(target, label, value) {
    if (value === null || value === undefined || value === "") return;
    target.append(element("div", "block-title", label));
    target.append(element("pre", "", formatted(value)));
  }

  function appendUsage(target, item, detailedCost = false) {
    const usage = usageOf(item);
    target.append(element("div", "block-title", "Usage"));
    const grid = element("div", "usage-grid");
    for (const [label, value] of [
      ["Input tokens", compact(usage.input)], ["Output tokens", compact(usage.output)],
      ["Cache read", compact(usage.cacheRead)], ["Cache write", compact(usage.cacheWrite)],
      ["Cache hit rate", percent(cacheHitRate(usage))],
      ["Cost", money(usage.cost, detailedCost)],
    ]) {
      const cell = element("div", "usage-cell");
      cell.append(element("label", "", label));
      cell.append(element("strong", "", value));
      grid.append(cell);
    }
    target.append(grid);
  }

  function appendRaw(target, item) {
    const fold = element("details", "");
    fold.append(element("summary", "", "Raw Payload"));
    fold.append(element("pre", "", formatted(item)));
    target.append(fold);
  }

  function showDetail(item, source) {
    $("detail-source").textContent = source;
    const target = $("detail");
    target.replaceChildren();
    const sourceKind = source === "Langfuse" ? "langfuse" : "local";
    const kind = item.__conversation
      ? "message"
      : (item.step_id || source === "Langfuse" ? observationKind(item, sourceKind) : "execution");
    const title = kind === "model" ? text(modelOf(item), "Model Call")
      : kind === "tool" ? toolNameOf(item)
      : item.name || item.identity?.task_name || item.identity?.execution_id || "Execution";
    target.append(element("h3", "detail-title", title));
    const chips = element("div", "detail-chips");
    chips.append(badge(source === "Langfuse" ? "LANGFUSE" : source === "Conversation" ? "SESSION" : "LOCAL", source === "Langfuse" ? "cloud" : "local"));
    chips.append(badge(String(kind).toUpperCase()));
    target.append(chips);

    if (kind === "model") {
      const status = rolledNodeStatus(item, sourceKind);
      appendField(target, "Model", modelOf(item));
      appendField(target, "Provider", providerOf(item));
      appendField(target, "Status", status.label);
      appendField(target, "Latency", elapsed(latencyOf(item, sourceKind)));
      appendField(target, "Finish Reason", finishReasonOf(item));
      appendUsage(target, item, true);
      appendBlock(target, "Request", item.input);
      appendBlock(target, "Response", item.output);
      const error = first(item.error, item.metadata?.error, coreValue(item.output, ["error"]));
      appendBlock(target, "Error", error);
      appendRaw(target, item);
      return;
    }

    if (kind === "tool") {
      const status = rolledNodeStatus(item, sourceKind);
      const query = coreValue(item.input, ["query"]);
      const command = coreValue(item.input, ["command", "cmd"]);
      appendField(target, "Tool", toolNameOf(item));
      appendField(target, "Status", status.label);
      appendField(target, "Duration", elapsed(latencyOf(item, sourceKind)));
      appendField(target, "Query", query);
      appendField(target, "Command", command);
      appendField(target, "Exit Code", exitCodeOf(item));
      appendBlock(target, "Request", item.input);
      appendBlock(target, "Response", item.output);
      const error = first(item.error, item.metadata?.error, coreValue(item.output, ["error"]));
      appendBlock(target, "Error", error);
      appendRaw(target, item);
      return;
    }

    if (kind === "message") {
      appendField(target, "Sequence", item.sequence);
      appendField(target, "Role", item.role);
      appendField(target, "Tool Call ID", item.tool_call_id);
      appendField(target, "Finish Reason", item.finish_reason);
      appendBlock(target, "Content", item.content);
      appendBlock(target, "Tool Calls", item.tool_calls);
      appendBlock(target, "Reasoning", item.reasoning_content);
      appendRaw(target, item.raw || item);
      return;
    }

    if (kind === "execution") {
      const summary = state.executions.find((value) => value.id === state.selected);
      appendField(target, "Execution", item.identity?.execution_id);
      appendField(target, "Trace", item.identity?.trace_id);
      appendField(target, "Session", item.identity?.session_id);
      appendField(target, "Model", item.agent?.model);
      appendField(target, "Provider", item.agent?.provider);
      appendField(target, "Status", summary ? statusOf([summary]).label : item.execution?.success === true ? "OK" : item.execution?.success === false ? "ERROR" : "UNKNOWN");
      appendField(target, "Runtime Result", item.execution?.success === true ? "SUCCESS" : item.execution?.success === false ? "FAILED" : "UNKNOWN");
      appendField(target, "Duration", elapsed(item.execution?.latency_ms));
      appendUsage(target, item);
      appendBlock(target, "Response", item.execution?.final_output);
      appendBlock(target, "Error", item.execution?.error);
      appendRaw(target, item);
      return;
    }

    const status = rolledNodeStatus(item, sourceKind);
    appendField(target, "Status", status.label);
    appendField(target, "Latency", elapsed(latencyOf(item, sourceKind)));
    appendField(target, "Finish Reason", finishReasonOf(item));
    appendBlock(target, "Request", item.input);
    appendBlock(target, "Response", item.output);
    appendBlock(target, "Error", first(item.error, item.metadata?.error, coreValue(item.output, ["error"])));
    appendRaw(target, item);
  }

  $("search").addEventListener("input", applyFilter);
  $("refresh").addEventListener("click", () => loadExecutions(true));
  loadExecutions(false);
</script>
</body>
</html>
"""

__all__ = ["VIEWER_HTML"]
