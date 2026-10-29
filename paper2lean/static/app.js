const $ = id => document.getElementById(id);
let analysis = null;
let busy = false;
let timer = null;
let monitor = null;
let lastReport = null;
let lockedControls = new Map();
const stageNames = {
  ingest: "Read source", structure_map: "Classify blocks", normalize_blocks: "Normalize claims",
  notation_ledger: "Build notation", proof_linking: "Link proofs", dependency_plan: "Plan dependencies",
  inventory: "Save inventory", formalization: "Start formalization", representation_plan: "Plan Lean representation",
  mathlib_names: "Check Mathlib names", declaration: "Generate declaration", candidate: "Proof candidate",
  verification: "Lean verification", repair: "Repair", candidate_ranking: "Rank candidates",
  unit: "Unit", resume: "Resume saved unit", trust_audit: "Check axioms", fidelity_audit: "Check correspondence",
  assembly: "Assemble Lean", report: "Save report",
};

function element(tag, text, className = "") {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function config() {
  return {
    llm_http_url: $("url").value.trim(), llm_http_model: $("model").value.trim(),
    llm_http_key: $("key").value.trim(), num_candidates: Number($("candidates").value) || 2,
    max_parallel_workers: Number($("workers").value) || 1,
    unit_time_budget_seconds: (Number($("unit-budget").value) || 10) * 60,
  };
}

async function request(url, options = {}) {
  const response = await fetch(url, {cache: "no-store", ...options});
  const data = await response.json();
  if (!response.ok) throw Error(data.error || `HTTP ${response.status}`);
  return data;
}

function post(url, payload) {
  return request(url, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)});
}

function setBusy(value) {
  busy = value;
  if (value) {
    lockedControls = new Map();
    document.querySelectorAll("input, button, select, textarea").forEach(control => {
      lockedControls.set(control, control.disabled);
      control.disabled = true;
    });
  } else {
    lockedControls.forEach((disabled, control) => { control.disabled = disabled; });
    lockedControls.clear();
  }
  $("analyze").disabled = value;
  $("run").disabled = value || !analysis?.id;
  $("retry-failed").disabled = value || !analysis?.id || !lastReport?.retryable_units?.length;
  for (const id of ["file", "clear"]) $(id).disabled = value;
}

function startTimer() {
  clearInterval(timer);
  const started = Date.now();
  const tick = () => {
    const seconds = Math.floor((Date.now() - started) / 1000);
    $("elapsed").textContent = `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
  };
  tick();
  timer = setInterval(tick, 1000);
}

function showOutput(value) {
  $("output").textContent = value;
  $("output").classList.toggle("hidden", !value);
}

function renderSummary(data) {
  $("summary").replaceChildren(...[
    [data.units?.length || 0, "units"], [data.blocks?.length || 0, "source blocks"],
    [data.format || "—", "format"], [data.page_count || "—", "pages"],
  ].map(([value, label]) => {
    const card = element("div");
    card.append(element("b", value), element("span", label));
    return card;
  }));
}

function renderUnits(units) {
  $("unit-panel").classList.remove("hidden");
  $("units-list").replaceChildren(...units.map(unit => {
    const card = element("article", undefined, "unit-card");
    const header = element("div", undefined, "unit-header");
    header.append(element("strong", unit.display_name || unit.label || unit.id), element("span", `${unit.kind || "unit"} · ${unit.status || "pending"}`, "muted"));
    card.append(header, element("p", unit.statement || "Statement unavailable."));
    if (unit.display_name && unit.label) card.append(element("small", `Source reference: ${unit.label}`, "muted"));
    const proof = element("details", undefined, "source-proof");
    proof.open = true;
    const status = unit.proof_status || (unit.proof ? "linked" : "missing");
    proof.append(element("summary", `Source proof · ${status}`));
    if (unit.proof) proof.append(element("pre", unit.proof));
    else proof.append(element("p", status === "ambiguous" ? "Proof ownership needs source review."
      : status === "cited_result" ? "Cited result: no local proof is attached."
      : "No local proof was found for this unit.", "muted"));
    if (unit.proof_evidence?.length) proof.append(element("small", unit.proof_evidence.join(" · "), "muted"));
    card.append(proof);
    return card;
  }));
  if (!units.length) $("units-list").append(element("p", "No theorem-like units were identified.", "muted"));
}

function renderActivity(data, phase) {
  $("activity-panel").classList.remove("hidden");
  const label = phase === "analysis" ? "Analysis" : "Formalization";
  $("activity-state").textContent = `${label}: ${data.status === "running" ? "running on server" : data.status}`;
  $("recent-steps").replaceChildren(...(data.events || []).slice(-5).map(event => {
    const row = element("li");
    const unit = analysis?.units?.find(item => item.id === event.unit);
    const parts = [stageNames[event.stage] || event.stage, event.status];
    if (event.label || event.unit) parts.push(unit?.display_name || event.label || unit?.label || event.unit.slice(0, 8));
    if (event.candidate) parts.push(`candidate ${event.candidate}`);
    if (event.backend) parts.push(event.backend);
    row.append(element("span", new Date(event.time * 1000).toLocaleTimeString(), "muted"), element("strong", parts.join(" · ")));
    if (event.detail || event.error) row.append(element("small", event.detail || event.error));
    return row;
  }));
}

function observeActivity(requestId, phase) {
  const token = {stopped: false, timeout: null};
  monitor = token;
  $("recent-steps").replaceChildren();
  $("activity-panel").classList.remove("hidden");
  $("activity-state").textContent = "Waiting for server status…";
  async function refresh() {
    try {
      const data = await request(`/api/activity?request_id=${encodeURIComponent(requestId)}`, {signal: AbortSignal.timeout(5000)});
      if (!token.stopped) {
        if (data.status === "unknown") $("activity-state").textContent = "Server has no record of this request yet; progress is unconfirmed.";
        else renderActivity(data, phase);
      }
    } catch {
      if (!token.stopped) $("activity-state").textContent = "Status unavailable. Processing request remains pending; progress is unconfirmed.";
    }
    if (!token.stopped) token.timeout = setTimeout(refresh, 2000);
  }
  refresh();
  return async () => {
    token.stopped = true;
    clearTimeout(token.timeout);
    try {
      const data = await request(`/api/activity?request_id=${encodeURIComponent(requestId)}`, {signal: AbortSignal.timeout(5000)});
      if (monitor === token) {
        if (data.status === "unknown") $("activity-state").textContent = "Request status unknown; the server may have restarted.";
        else renderActivity(data, phase);
      }
    } catch {
      if (monitor === token) $("activity-state").textContent = "Final server status unavailable.";
    }
  };
}

async function execute(phase, operation) {
  if (busy) return;
  setBusy(true);
  showOutput("");
  $("run-state").textContent = phase === "analysis" ? "Analyzing…" : "Formalizing…";
  startTimer();
  const requestId = crypto.randomUUID();
  const finishMonitor = observeActivity(requestId, phase);
  try {
    await operation(requestId);
  } catch (error) {
    $("run-state").textContent = `${phase === "analysis" ? "Analysis" : "Formalization"} request failed`;
    showOutput(error.message === "Failed to fetch" ? "Connection lost. Server processing may continue; check the latest status before starting again." : error.message);
  } finally {
    clearInterval(timer);
    await finishMonitor();
    setBusy(false);
  }
}

$("analyze").onclick = () => execute("analysis", async requestId => {
  const file = $("file").files[0];
  if (!file) throw Error("Choose a PDF or TeX file.");
  analysis = null;
  lastReport = null;
  $("retry-summary").textContent = "";
  $("unit-panel").classList.add("hidden");
  $("summary").replaceChildren();
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = "";
  for (let offset = 0; offset < bytes.length; offset += 8192) binary += String.fromCharCode(...bytes.subarray(offset, offset + 8192));
  analysis = await post("/api/upload", {...config(), request_id: requestId, name: file.name, content_base64: btoa(binary)});
  renderSummary(analysis);
  renderUnits(analysis.units || []);
  if (analysis.warnings?.length) showOutput(analysis.warnings.join("\n"));
  $("run-state").textContent = "Analysis complete";
});

async function formalize(requestId, retryFailed = false) {
  const report = await post("/api/run", {...config(), request_id: requestId, paper_id: analysis.id,
    retry_failed: retryFailed, retry_confirmed: retryFailed});
  lastReport = report;
  const outcomes = new Map(report.units.map(unit => [unit.id, unit]));
  analysis.units = analysis.units.map(unit => ({...unit, ...outcomes.get(unit.id)}));
  renderUnits(analysis.units);
  showOutput(JSON.stringify(report, null, 2));
  $("run-state").textContent = `Finished · ${((report.coverage || 0) * 100).toFixed(1)}% compiled coverage`;
  const counts = report.counts;
  $("retry-summary").textContent = counts ? `${counts.compiled} compiled · ${counts.failed} failed/timed out · ${counts.pending} pending. Compilation does not establish mathematical fidelity. Assembly: ${report.assembly?.compiled ? "compiled" : "not compiled"}.` : "";
}

$("run").onclick = () => execute("formalization", requestId => formalize(requestId));
$("retry-failed").onclick = () => {
  if (busy || !lastReport?.retryable_units?.length) return;
  const units = analysis.units.filter(unit => lastReport.retryable_units.includes(unit.id));
  const names = units.map(unit => unit.label || unit.id).join("\n");
  if (!window.confirm(`Retry all ${units.length} failed/timed-out units?\n\n${names}\n\nThis sends new LLM requests and runs local Lean again. Successful units will be kept.`)) return;
  return execute("formalization", requestId => formalize(requestId, true));
};

$("clear").onclick = () => {
  if (busy) return;
  analysis = null;
  lastReport = null;
  $("retry-summary").textContent = "";
  $("summary").replaceChildren();
  $("unit-panel").classList.add("hidden");
  $("activity-panel").classList.add("hidden");
  $("run-state").textContent = "No run started";
  $("elapsed").textContent = "00:00";
  showOutput("");
  setBusy(false);
};

document.body.dataset.theme = localStorage.getItem("paper2lean-theme") || "dark";
$("theme").onclick = () => {
  document.body.dataset.theme = document.body.dataset.theme === "dark" ? "light" : "dark";
  localStorage.setItem("paper2lean-theme", document.body.dataset.theme);
};
function showConnection(text, good) { $("connection").textContent = text; $("connection").className = `status ${good ? "good" : "bad"}`; }
async function health() {
  const data = await request("/api/health");
  $("model-status").textContent = `Model: ${data.llm_model || "not configured"} · environment`;
  showConnection(data.llm_configured ? "Endpoint configured · connection not checked" : "LLM endpoint not configured", data.llm_configured);
}
$("check").onclick = async () => {
  if (busy) return;
  try {
    const data = await post("/api/llm/check", config());
    showConnection(data.connected ? `Connected · ${data.model}` : `Disconnected · ${data.message}`, data.connected);
    $("model-status").textContent = `Model: ${data.model} · ${data.source}`;
  } catch (error) { showConnection(error.message, false); }
};
health().catch(() => showConnection("Server unavailable", false));
$("workers").oninput = () => { const n = Math.max(1, Number($("workers").value) || 1); $("worker-summary").textContent = `${n} parallel worker${n === 1 ? "" : "s"}`; };
$("file").onchange = () => { const file = $("file").files[0]; $("filename").textContent = file ? `${file.name} (${Math.ceil(file.size / 1024)} KB)` : "Choose PDF or TeX file"; };
$("dropzone").ondragover = event => { event.preventDefault(); if (!busy) $("dropzone").classList.add("drag"); };
$("dropzone").ondragleave = () => $("dropzone").classList.remove("drag");
$("dropzone").ondrop = event => { event.preventDefault(); $("dropzone").classList.remove("drag"); if (!busy) { $("file").files = event.dataTransfer.files; $("file").onchange(); } };

async function loadHistory() {
  const data = await request("/api/history");
  $("history-list").replaceChildren(...data.items.map(item => {
    const row = element("article");
    row.append(element("b", item.paper_id), element("span", `${((item.coverage || 0) * 100).toFixed(1)}% coverage`), element("small", `${item.units.length} units · ${item.assembly?.status || "pending"}`));
    return row;
  }));
  if (!data.items.length) $("history-list").append(element("p", "No completed runs.", "muted"));
}
async function loadStats() {
  const data = await request("/api/statistics");
  $("stats-grid").replaceChildren(...[["Runs", data.runs], ["Units", data.units], ["Succeeded", data.succeeded], ["Failed", data.failed], ["Coverage", `${(data.coverage * 100).toFixed(1)}%`]].map(([label, value]) => {
    const card = element("div"); card.append(element("b", value), element("span", label)); return card;
  }));
}
document.querySelectorAll(".tab").forEach(tab => tab.onclick = () => {
  document.querySelectorAll(".tab").forEach(node => node.classList.toggle("active", node === tab));
  document.querySelectorAll(".tab-page").forEach(node => node.classList.toggle("hidden", node.id !== tab.dataset.tab));
  const update = tab.dataset.tab === "history" ? loadHistory : tab.dataset.tab === "statistics" ? loadStats : null;
  if (update) update().catch(error => showOutput(error.message));
});
$("refresh-history").onclick = () => loadHistory().catch(error => showOutput(error.message));
