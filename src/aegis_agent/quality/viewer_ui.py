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
    .app { height: 100vh; display: grid; grid-template-rows: 72px 82px minmax(0, 1fr); }
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
    .summary {
      display: grid; grid-template-columns: repeat(4, minmax(150px, 1fr)); gap: 12px;
      padding: 14px 24px; border-bottom: 1px solid var(--line-soft);
    }
    .metric {
      display: flex; align-items: center; justify-content: space-between;
      padding: 12px 14px; border: 1px solid var(--line-soft); border-radius: 11px;
      background: linear-gradient(145deg, rgba(20, 32, 29, .88), rgba(13, 23, 20, .75));
      box-shadow: var(--shadow);
    }
    .metric span { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .08em; }
    .metric strong { font-size: 19px; letter-spacing: -.03em; }
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
    .node-icon { width: 25px; height: 25px; display: grid; place-items: center; border-radius: 7px; color: var(--accent); background: rgba(101, 230, 173, .08); font-size: 10px; font-weight: 800; }
    .node-name { overflow: hidden; white-space: nowrap; text-overflow: ellipsis; font-weight: 600; }
    .node-sub { color: var(--muted); font-size: 10px; }
    .duration { color: #a5b8b0; font: 11px ui-monospace, SFMono-Regular, Menlo, monospace; }
    .detail { padding: 16px; }
    .detail-empty, .empty { padding: 30px 18px; color: var(--muted); text-align: center; }
    .detail-title { margin: 0; font-size: 18px; letter-spacing: -.02em; }
    .detail-chips { display: flex; flex-wrap: wrap; gap: 7px; margin: 10px 0 18px; }
    .kv { display: grid; grid-template-columns: 112px minmax(0, 1fr); gap: 9px; padding: 8px 0; border-bottom: 1px solid var(--line-soft); }
    .kv label { color: var(--muted); font-size: 11px; }
    .kv div { min-width: 0; overflow-wrap: anywhere; font-size: 12px; }
    .block-title { margin: 19px 0 8px; color: var(--muted); font-size: 10px; font-weight: 700; letter-spacing: .1em; text-transform: uppercase; }
    pre {
      margin: 0; padding: 12px; max-height: 320px; overflow: auto;
      border: 1px solid var(--line-soft); border-radius: 9px; color: #cfe2da;
      background: #0b1210; font: 11px/1.55 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      white-space: pre-wrap; overflow-wrap: anywhere;
    }
    .error-banner { margin: 10px 12px; padding: 10px 12px; border: 1px solid rgba(245, 197, 107, .28); border-radius: 9px; color: var(--amber); background: rgba(245, 197, 107, .07); font-size: 11px; }
    .spin { animation: spin .85s linear infinite; }
    @keyframes spin { to { transform: rotate(360deg); } }
    @media (max-width: 1200px) { .workspace { grid-template-columns: 300px minmax(370px, 1fr) 330px; } }
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
      <input id="search" class="search" type="search" placeholder="Filter task, model, execution id…">
      <button id="refresh" class="refresh" type="button">↻ Refresh</button>
    </div>
  </header>
  <section class="summary">
    <div class="metric"><span>Executions</span><strong id="m-count">—</strong></div>
    <div class="metric"><span>Successful</span><strong id="m-success">—</strong></div>
    <div class="metric"><span>Input tokens</span><strong id="m-input">—</strong></div>
    <div class="metric"><span>Cost</span><strong id="m-cost">—</strong></div>
  </section>
  <main class="workspace">
    <section class="pane">
      <div class="pane-head"><h2>Executions</h2><span id="source-status" class="small">Loading…</span></div>
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
  const state = { executions: [], filtered: [], selected: null, detail: null, node: null };
  const $ = (id) => document.getElementById(id);
  const text = (value, fallback = "—") => value === null || value === undefined || value === "" ? fallback : String(value);
  const number = (value) => Number.isFinite(Number(value)) ? Number(value) : 0;
  const compact = (value) => new Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 1 }).format(value || 0);
  const money = (value) => value ? `$${Number(value).toFixed(value < .01 ? 4 : 2)}` : "$0";
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
        if (existing) existing.langfuse_available = true;
        else {
          state.executions.push(item);
          if (item.trace_id) byTrace.set(item.trace_id, item);
        }
      }
      state.executions.sort((a, b) => text(b.started_at, "").localeCompare(text(a.started_at, "")));
      $("source-status").textContent = payload.error ? "Local records" : "Local + Langfuse";
      errorBanner($("list-error"), payload.error ? `Langfuse unavailable: ${payload.error}` : null);
      updateMetrics();
      applyFilter();
      if (!state.selected && state.filtered.length) await selectExecution(state.filtered[0].id);
    } catch (error) {
      $("source-status").textContent = "Local records";
      errorBanner($("list-error"), `Langfuse unavailable: ${error.message}`);
    }
  }

  function updateMetrics() {
    $("m-count").textContent = compact(state.executions.length);
    $("m-success").textContent = compact(state.executions.filter((item) => item.success === true).length);
    let input = 0;
    let cost = 0;
    for (const item of state.executions) {
      const usage = item.usage || {};
      input += number(usage.input_tokens ?? usage.input ?? 0);
      cost += number(usage.cost ?? usage.total_cost ?? 0);
    }
    $("m-input").textContent = compact(input);
    $("m-cost").textContent = money(cost);
  }

  function applyFilter() {
    const query = $("search").value.trim().toLowerCase();
    state.filtered = state.executions.filter((item) => {
      const haystack = [item.id, item.execution_id, item.trace_id, item.task, item.model, item.provider, item.name]
        .filter(Boolean).join(" ").toLowerCase();
      return !query || haystack.includes(query);
    });
    renderRuns();
  }

  function renderRuns() {
    const target = $("run-list");
    target.replaceChildren();
    if (!state.filtered.length) {
      target.append(element("div", "empty", "No matching executions."));
      return;
    }
    for (const item of state.filtered) {
      const button = element("button", `run${item.id === state.selected ? " active" : ""}`);
      button.type = "button";
      button.addEventListener("click", () => selectExecution(item.id));
      const top = element("div", "run-top");
      top.append(element("div", "run-title", item.name || item.model || "Aegis Run"));
      const status = element("span", "state");
      status.append(element("i", `dot ${item.success === true ? "ok" : item.success === false ? "bad" : item.passed === false ? "warn" : ""}`));
      status.append(document.createTextNode(item.success === true ? "OK" : item.success === false ? "ERROR" : item.passed === false ? "FAIL" : "UNKNOWN"));
      top.append(status);
      button.append(top);
      button.append(element("div", "run-task", item.task || item.execution_id || item.trace_id || "No task text"));
      const meta = element("div", "run-meta");
      meta.append(badge(item.source === "local" ? "LOCAL" : "CLOUD", item.source === "local" ? "local" : "cloud"));
      if (item.langfuse_available && item.source === "local") meta.append(badge("LF", "cloud"));
      meta.append(document.createTextNode(when(item.started_at)));
      meta.append(document.createTextNode(elapsed(item.latency_ms)));
      button.append(meta);
      target.append(button);
    }
  }

  async function selectExecution(id) {
    state.selected = id;
    state.detail = null;
    state.node = null;
    renderRuns();
    $("trace").innerHTML = '<div class="empty">Loading trace…</div>';
    $("detail").innerHTML = '<div class="detail-empty">Choose a trace node to inspect its data.</div>';
    try {
      const summary = state.executions.find((item) => item.id === id);
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
    let rendered = false;
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

  function renderTree(target, items, source) {
    const idOf = (item) => source === "local" ? item.step_id : item.id;
    const parentOf = (item) => source === "local" ? item.parent_step_id : item.parentObservationId;
    const byParent = new Map();
    const known = new Set(items.map(idOf));
    for (const item of items) {
      const rawParent = parentOf(item);
      const parent = rawParent && known.has(rawParent) ? rawParent : null;
      if (!byParent.has(parent)) byParent.set(parent, []);
      byParent.get(parent).push(item);
    }
    for (const children of byParent.values()) {
      children.sort((a, b) => text(a.sequence ?? a.startTime, "").localeCompare(text(b.sequence ?? b.startTime, ""), undefined, { numeric: true }));
    }
    const seen = new Set();
    const visit = (parent, depth) => {
      for (const item of byParent.get(parent) || []) {
        const id = idOf(item);
        if (seen.has(id)) continue;
        seen.add(id);
        const node = element("button", `node${depth === 0 ? " root" : ""}`);
        node.type = "button";
        node.style.setProperty("--indent", `${Math.min(depth, 7) * 20}px`);
        node.addEventListener("click", () => {
          document.querySelectorAll(".node.active").forEach((entry) => entry.classList.remove("active"));
          node.classList.add("active");
          state.node = {item, source: source === "local" ? "ExecutionRecord" : "Langfuse"};
          showDetail(state.node.item, state.node.source);
        });
        const kind = text(source === "local" ? item.type : item.type, "span").toUpperCase();
        node.append(element("span", "node-icon", kind.slice(0, 2)));
        const labels = element("span", "");
        labels.append(element("div", "node-name", item.name || "Observation"));
        labels.append(element("div", "node-sub", source === "local" ? text(item.metadata?.provider || item.metadata?.tool_name, kind) : text(item.providedModelName, kind)));
        node.append(labels);
        const duration = source === "local" ? item.latency_ms : number(item.latency) * 1000;
        node.append(element("span", "duration", elapsed(duration)));
        target.append(node);
        visit(id, depth + 1);
      }
    };
    visit(null, 0);
  }

  function showDetail(item, source) {
    $("detail-source").textContent = source;
    const target = $("detail");
    target.replaceChildren();
    const title = item.name || item.identity?.task_name || item.identity?.execution_id || "Execution";
    target.append(element("h3", "detail-title", title));
    const chips = element("div", "detail-chips");
    chips.append(badge(source === "Langfuse" ? "LANGFUSE" : "LOCAL", source === "Langfuse" ? "cloud" : "local"));
    const kind = item.type || item.schema_version;
    if (kind) chips.append(badge(String(kind).toUpperCase()));
    target.append(chips);

    const fields = source === "Langfuse"
      ? [["ID", item.id], ["Trace", item.traceId], ["Parent", item.parentObservationId], ["Start", item.startTime], ["End", item.endTime], ["Model", item.providedModelName], ["Latency", elapsed(number(item.latency) * 1000)], ["Level", item.level]]
      : item.step_id
        ? [["Step ID", item.step_id], ["Parent", item.parent_step_id], ["Sequence", item.sequence], ["Start", item.started_at], ["End", item.finished_at], ["Latency", elapsed(item.latency_ms)], ["Success", item.success]]
        : [["Execution", item.identity?.execution_id], ["Trace", item.identity?.trace_id], ["Session", item.identity?.session_id], ["Model", item.agent?.model], ["Provider", item.agent?.provider], ["Success", item.execution?.success], ["Verifier", item.evaluation?.passed]];
    for (const [label, value] of fields) {
      if (value === null || value === undefined || value === "") continue;
      const row = element("div", "kv");
      row.append(element("label", "", label));
      row.append(element("div", "", text(value)));
      target.append(row);
    }

    const blocks = source === "Langfuse"
      ? [["Input", item.input], ["Output", item.output], ["Usage", item.usageDetails], ["Cost", item.costDetails], ["Metadata", item.metadata]]
      : item.step_id
        ? [["Input", item.input], ["Output", item.output], ["Usage", item.usage], ["Metadata", item.metadata], ["Error", item.error]]
        : [["Final output", item.execution?.final_output], ["Usage", item.usage], ["Evaluation", item.evaluation], ["Artifacts", item.artifacts], ["Metadata", item.metadata]];
    for (const [label, value] of blocks) {
      if (value === null || value === undefined || value === "") continue;
      target.append(element("div", "block-title", label));
      const pre = element("pre", "", typeof value === "string" ? value : JSON.stringify(value, null, 2));
      target.append(pre);
    }
  }

  $("search").addEventListener("input", applyFilter);
  $("refresh").addEventListener("click", () => loadExecutions(true));
  loadExecutions(false);
</script>
</body>
</html>
"""

__all__ = ["VIEWER_HTML"]
