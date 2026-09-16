// STEPS.md Step 16 -- 4-panel dashboard frontend.
//
// Plain fetch()/WebSocket, no framework, no build step (served directly by
// web/app.py's StaticFiles mount -- STEPS.md Step 15's REST/WS backend).
// DOM nodes are built with el()/textContent rather than innerHTML
// throughout: `rationale`/`error` fields are model-generated free text
// (the project already treats agent message content as untrusted data when
// it's fed back into a prompt -- see agents/junction.py's
// wrap_untrusted_data -- the same caution applies to rendering it in the
// DOM, so it is never interpreted as HTML).

const SEQ_SLOTS = ["--seq-100", "--seq-250", "--seq-400", "--seq-550", "--seq-700"];

let currentRunId = null;
let ws = null;
let runInfo = null;
let messages = [];
let decisions = [];
let llmCalls = [];
const latestMetricByJunction = new Map();

// ---------------------------------------------------------------------- //
// small DOM helpers -- text content only, never innerHTML with fetched data
// ---------------------------------------------------------------------- //

function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else node.setAttribute(k, v);
  }
  for (const child of children) {
    node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
  }
  return node;
}

function emptyState(text) {
  return el("div", { class: "empty-state" }, [text]);
}

async function fetchJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${url}: HTTP ${res.status}`);
  return res.json();
}

// Walks every page of a since_id/since_sim_time-cursored REST endpoint
// (STEPS.md Step 15) until a short page says there's no more -- needed
// because a hour-long run's `metrics` table alone can hold ~13k rows,
// far past the server's default page size.
async function fetchAllPages(path, cursor, onRow, limit = 1000) {
  let value = cursor.initial;
  for (;;) {
    const url = new URL(path, location.origin);
    url.searchParams.set(cursor.name, value);
    url.searchParams.set("limit", String(limit));
    const rows = await fetchJSON(url);
    for (const row of rows) onRow(row);
    if (rows.length < limit) return;
    value = cursor.next(rows[rows.length - 1]);
  }
}

function fmtMoney(n) {
  return n == null ? "—" : `$${n.toFixed(4)}`;
}
function fmtPct(n) {
  return n == null ? "—" : `${n.toFixed(1)}%`;
}
function fmtMs(n) {
  return n == null ? "—" : `${Math.round(n)} ms`;
}
function percentile(sortedArr, p) {
  if (sortedArr.length === 0) return null;
  const idx = Math.min(sortedArr.length - 1, Math.max(0, Math.ceil(p * sortedArr.length) - 1));
  return sortedArr[idx];
}

// ---------------------------------------------------------------------- //
// panel 1 -- network snapshot (sequential-hue load grid, one hue = blue,
// per the dataviz skill's "compare magnitude on a grid -> heatmap,
// sequential" guidance -- a stand-in for a real geo map, see index.html's
// hint text)
// ---------------------------------------------------------------------- //

function seqColorFor(value, max) {
  const root = getComputedStyle(document.documentElement);
  if (max <= 0) return root.getPropertyValue(SEQ_SLOTS[0]).trim();
  const ratio = Math.min(1, value / max);
  const idx = Math.min(SEQ_SLOTS.length - 1, Math.floor(ratio * SEQ_SLOTS.length));
  return root.getPropertyValue(SEQ_SLOTS[idx]).trim();
}

// Picks readable text ink for a label placed *inside* a colored fill --
// the one exception to "text never wears the data color" (marks-and-
// anatomy.md), decided by the fill's luminance.
function textColorFor(bgHex) {
  const hex = bgHex.replace("#", "");
  const r = parseInt(hex.substring(0, 2), 16);
  const g = parseInt(hex.substring(2, 4), 16);
  const b = parseInt(hex.substring(4, 6), 16);
  const luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255;
  return luminance > 0.6 ? "#0b0b0b" : "#ffffff";
}

function renderJunctionGrid() {
  const grid = document.getElementById("junction-grid");
  grid.textContent = "";
  const entries = [...latestMetricByJunction.values()].sort((a, b) => a.junction_id.localeCompare(b.junction_id));
  if (entries.length === 0) {
    grid.appendChild(emptyState("Chưa có mẫu metric nào."));
    return;
  }
  const maxQueue = Math.max(1, ...entries.map((e) => e.queue_len ?? 0));
  for (const m of entries) {
    const bg = seqColorFor(m.queue_len ?? 0, maxQueue);
    const tile = el(
      "div",
      { class: "junction-tile", style: `background:${bg};color:${textColorFor(bg)}` },
      [el("span", { class: "jid" }, [m.junction_id]), el("span", { class: "qlen" }, [String(m.queue_len ?? "—")])],
    );
    tile.title = `t=${m.sim_time}s  mean_waiting_s=${m.mean_waiting_s ?? "—"}`;
    grid.appendChild(tile);
  }
}

// ---------------------------------------------------------------------- //
// panel 2 -- agent chat (timeline grouped by cycle_id)
// ---------------------------------------------------------------------- //

function chatMessageElement(m) {
  const meta = el("div", { class: "meta" }, [`t=${m.sim_time}s · ${m.sender} → ${m.recipients.join(", ")}`]);
  const intent = el("span", { class: `intent intent-${m.intent}` }, [m.intent]);
  const rationale = el("div", { class: "rationale" }, [m.rationale || ""]);
  return el("div", { class: "chat-message" }, [meta, intent, rationale]);
}

function appendChatMessages(newMsgs) {
  const log = document.getElementById("chat-log");
  const empty = log.querySelector(".empty-state");
  if (empty) empty.remove();
  let lastCycle = log.dataset.lastCycle ? Number(log.dataset.lastCycle) : null;
  for (const m of newMsgs) {
    if (lastCycle === null || m.cycle_id !== lastCycle) {
      log.appendChild(el("div", { class: "chat-cycle-divider" }, [`Cycle ${m.cycle_id} · t=${m.sim_time}s`]));
      lastCycle = m.cycle_id;
    }
    log.appendChild(chatMessageElement(m));
  }
  log.dataset.lastCycle = String(lastCycle);
  log.scrollTop = log.scrollHeight;
}

function renderChat() {
  const log = document.getElementById("chat-log");
  log.textContent = "";
  delete log.dataset.lastCycle;
  if (messages.length === 0) {
    log.appendChild(emptyState("Chưa có tin nhắn coalition nào."));
    return;
  }
  appendChatMessages(messages);
}

// ---------------------------------------------------------------------- //
// panel 3 -- decision log (newest on top; status badges use the dataviz
// skill's fixed status palette -- icon+label, never color alone)
// ---------------------------------------------------------------------- //

const STATUS_CLASS = {
  validator: { ok: "good", clamped: "warning", rejected: "critical" },
  supervisor: { approved: "good", modified: "warning", denied: "critical" },
};
const STATUS_ICON = { good: "✓", warning: "⚠", critical: "✕" };

function statusBadge(kind, value) {
  if (!value) return el("span", { class: "badge badge-neutral" }, ["—"]);
  const cls = STATUS_CLASS[kind]?.[value] ?? "neutral";
  const icon = STATUS_ICON[cls] ?? "";
  return el("span", { class: `badge badge-${cls}` }, [`${icon} ${value}`]);
}

function decisionRow(d) {
  const tr = document.createElement("tr");
  for (const text of [String(d.cycle_id), d.sim_time.toFixed(0), d.junction_id, d.action_type]) {
    tr.appendChild(el("td", {}, [text]));
  }
  tr.appendChild(el("td", {}, [statusBadge("validator", d.validator_status)]));
  tr.appendChild(el("td", {}, [statusBadge("supervisor", d.supervisor_verdict)]));
  tr.appendChild(el("td", { class: "num" }, [d.applied ? "✓" : "—"]));
  tr.appendChild(el("td", {}, [d.effect ? JSON.stringify(d.effect) : "—"]));
  return tr;
}

function prependDecisionRows(newRows) {
  const body = document.getElementById("decisions-body");
  const empty = body.querySelector(".empty-row");
  if (empty) empty.remove();
  for (const d of newRows) body.insertBefore(decisionRow(d), body.firstChild);
}

function renderAllDecisions() {
  const body = document.getElementById("decisions-body");
  body.textContent = "";
  if (decisions.length === 0) {
    const tr = document.createElement("tr");
    tr.className = "empty-row";
    tr.appendChild(el("td", { colspan: "8" }, ["Chưa có decision nào."]));
    body.appendChild(tr);
    return;
  }
  for (let i = decisions.length - 1; i >= 0; i--) body.appendChild(decisionRow(decisions[i]));
}

// ---------------------------------------------------------------------- //
// panel 4 -- monitoring (KPI row of stat tiles -- "a handful of headline
// numbers" per the dataviz skill's choosing-a-form guidance, not a chart)
// ---------------------------------------------------------------------- //

function statTile({ label, value, sub }) {
  const children = [el("div", { class: "label" }, [label]), el("div", { class: "value" }, [value])];
  if (sub) children.push(el("div", { class: "sub" }, [sub]));
  return el("div", { class: "stat-tile" }, children);
}

function renderMonitoring() {
  const row = document.getElementById("kpi-row");
  row.textContent = "";

  const totalCost = llmCalls.reduce((s, c) => s + (c.cost_usd || 0), 0);
  const okCalls = llmCalls.filter((c) => c.status === "ok");
  const totalInputTokens = okCalls.reduce((s, c) => s + (c.input_tokens || 0), 0);
  const totalCachedTokens = okCalls.reduce((s, c) => s + (c.cached_tokens || 0), 0);
  const cacheHitRate = totalInputTokens > 0 ? (totalCachedTokens / totalInputTokens) * 100 : null;
  const latencies = okCalls.map((c) => c.latency_ms).filter((v) => v != null).sort((a, b) => a - b);
  const p50 = percentile(latencies, 0.5);
  const p95 = percentile(latencies, 0.95);
  const rejected = decisions.filter((d) => d.validator_status === "rejected").length;
  const rejectRate = decisions.length > 0 ? (rejected / decisions.length) * 100 : null;
  // n_skipped_cycles is genuinely only known once the run finishes -- a
  // skipped cycle never writes a `decisions` row at all (STEPS.md Step 14
  // follow-up analysis), so there is no way to infer it from live rows.
  const skipped = runInfo?.summary?.n_skipped_cycles;

  const tiles = [
    { label: "Tổng chi phí LLM", value: fmtMoney(totalCost), sub: `${llmCalls.length} lệnh gọi` },
    { label: "Cache hit rate", value: fmtPct(cacheHitRate), sub: `${totalCachedTokens}/${totalInputTokens} input tokens` },
    { label: "Latency p50 / p95", value: `${fmtMs(p50)} / ${fmtMs(p95)}`, sub: `${latencies.length} lệnh gọi ok` },
    { label: "Tỉ lệ reject (validator)", value: fmtPct(rejectRate), sub: `${rejected}/${decisions.length} decision` },
    { label: "Chu kỳ bị skip", value: skipped != null ? String(skipped) : "—", sub: skipped != null ? "" : "biết được khi run xong" },
  ];
  for (const t of tiles) row.appendChild(statTile(t));
}

// ---------------------------------------------------------------------- //
// run selection, history load, live WebSocket wiring
// ---------------------------------------------------------------------- //

function updateRunStatusBadge() {
  const badge = document.getElementById("run-status");
  if (!runInfo) {
    badge.textContent = "—";
    badge.className = "run-status";
    return;
  }
  if (runInfo.finished_at) {
    badge.textContent = `Đã xong · ${new Date(runInfo.finished_at).toLocaleTimeString()}`;
    badge.className = "run-status finished";
  } else {
    badge.textContent = "● Đang chạy (live)";
    badge.className = "run-status running";
  }
}

function markDisconnected() {
  if (runInfo && !runInfo.finished_at) {
    const badge = document.getElementById("run-status");
    badge.textContent = "⚠ mất kết nối live (thử chọn lại run)";
    badge.className = "run-status";
  }
}

function closeSocket() {
  if (ws) {
    ws.onclose = null;
    ws.close();
    ws = null;
  }
}

function openSocket(runId) {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  ws = new WebSocket(`${proto}//${location.host}/ws/runs/${runId}`);
  ws.onmessage = (ev) => {
    const event = JSON.parse(ev.data);
    switch (event.type) {
      case "messages":
        messages.push(...event.data);
        appendChatMessages(event.data);
        break;
      case "decisions":
        decisions.push(...event.data);
        prependDecisionRows(event.data);
        renderMonitoring();
        break;
      case "llm_calls":
        llmCalls.push(...event.data);
        renderMonitoring();
        break;
      case "metrics":
        for (const m of event.data) latestMetricByJunction.set(m.junction_id, m);
        renderJunctionGrid();
        break;
      case "run_finished":
        runInfo = event.data;
        updateRunStatusBadge();
        renderMonitoring();
        break;
      default:
        console.warn("unknown event type", event);
    }
  };
  ws.onclose = () => {
    if (currentRunId === runId) markDisconnected();
  };
}

async function selectRun(runId) {
  if (!runId) return;
  currentRunId = runId;
  closeSocket();
  messages = [];
  decisions = [];
  llmCalls = [];
  latestMetricByJunction.clear();

  runInfo = await fetchJSON(`/runs/${runId}`);
  updateRunStatusBadge();

  await fetchAllPages(`/runs/${runId}/messages`, { name: "since_id", initial: 0, next: (r) => r.id }, (m) =>
    messages.push(m),
  );
  await fetchAllPages(`/runs/${runId}/decisions`, { name: "since_id", initial: 0, next: (r) => r.id }, (d) =>
    decisions.push(d),
  );
  await fetchAllPages(`/runs/${runId}/llm_calls`, { name: "since_id", initial: 0, next: (r) => r.id }, (c) =>
    llmCalls.push(c),
  );
  await fetchAllPages(
    `/runs/${runId}/metrics`,
    { name: "since_sim_time", initial: -1, next: (r) => r.sim_time },
    (m) => latestMetricByJunction.set(m.junction_id, m),
  );

  renderChat();
  renderAllDecisions();
  renderMonitoring();
  renderJunctionGrid();

  // Nothing left to stream for a run that already finished before we ever
  // opened the dashboard -- REST above already has the whole history.
  if (!runInfo.finished_at) openSocket(runId);
}

function runOptionLabel(r) {
  const started = new Date(r.started_at).toLocaleString();
  return `${r.mode} · seed=${r.seed} · ${started}${r.finished_at ? "" : " (đang chạy)"}`;
}

async function loadRuns(preserveSelection) {
  const select = document.getElementById("run-select");
  const previous = select.value;
  const runs = await fetchJSON("/runs");
  select.textContent = "";
  if (runs.length === 0) {
    select.appendChild(el("option", { value: "" }, ["Chưa có run nào"]));
    return;
  }
  for (const r of runs) {
    select.appendChild(el("option", { value: r.run_id }, [runOptionLabel(r)]));
  }
  const stillThere = [...select.options].some((o) => o.value === previous);
  if (preserveSelection && stillThere) {
    select.value = previous;
  } else {
    select.value = runs[0].run_id;
    await selectRun(runs[0].run_id);
  }
}

document.getElementById("run-select").addEventListener("change", (e) => selectRun(e.target.value));

loadRuns(false);
setInterval(() => loadRuns(true), 10_000);
