/* Lean Formalizer UI — clean result view, theme, optional statement */

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const state = {
  lastResult: null, pdfFile: null, lastInput: null, lastHealth: null,
  backendStatus: null, backendCheckRequest: 0,
};
const LS_KEY = "lean_formalizer_ui_v1";
const sessionTokens = { prompt: 0, completion: 0, total: 0, calls: 0 };


function wirePageNav() {
  $$(".nav-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const page = btn.dataset.page;
      $$(".nav-btn").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      $("#page-formalize")?.classList.toggle("hidden", page !== "formalize");
      $("#page-stats")?.classList.toggle("hidden", page !== "stats");
      $("#page-history")?.classList.toggle("hidden", page !== "history");
      if (page === "stats") loadStats();
      if (page === "history") loadHistory();
    });
  });
  $("#btn-refresh-stats")?.addEventListener("click", loadStats);
  $("#btn-refresh-history")?.addEventListener("click", loadHistory);
}

function _fmtTime(iso) {
  if (!iso) return "";
  try {
    const d = new Date(iso);
    return d.toLocaleString();
  } catch (_) {
    return String(iso).slice(0, 19);
  }
}

async function loadHistory() {
  const list = $("#history-list");
  const summary = $("#history-summary");
  const note = $("#history-note");
  if (list) list.innerHTML = `<p class="muted">Loading…</p>`;
  try {
    const d = await api("/api/history?limit=20", {}, 15000);
    if (note) note.textContent = d.note || "";
    const items = d.items || [];
    const running = d.running || [];
    const counts = { verified: 0, failed: 0, timeout: 0, quota: 0, error: 0 };
    items.forEach((it) => {
      const o = it.outcome || "failed";
      if (counts[o] != null) counts[o] += 1;
      else counts.failed += 1;
    });
    if (summary) {
      summary.innerHTML = [
        ["Total", items.length],
        ["Verified", counts.verified],
        ["Failed", counts.failed],
        ["Timeout", counts.timeout],
        ["Quota", counts.quota],
        ["Error", counts.error],
        ["Running", running.length],
      ]
        .map(
          ([label, n]) =>
            `<span class="history-pill"><strong>${n}</strong> ${label}</span>`
        )
        .join("");
    }
    const all = [
      ...running.map((r) => ({ ...r, finished_at: null })),
      ...items,
    ];
    if (!all.length) {
      if (list) {
        list.innerHTML =
          `<p class="muted">No runs yet in this server session. Formalize something on the Formalize page, then refresh here.</p>`;
      }
      return;
    }
    if (list) {
      list.innerHTML = all
        .map((it) => {
          const outcome = it.outcome || "failed";
          const tokens = it.tokens || {};
          const tokStr =
            tokens.total || tokens.calls
              ? `${tokens.total || 0} tok · ${tokens.calls || 0} calls`
              : "";
          const stages = (it.stage_summary || [])
            .map((s) => {
              const mark = s.ok === false ? "✗" : "✓";
              return `${mark}${s.stage}${s.len != null ? `(${s.len})` : ""}`;
            })
            .join(" · ");
          const fails = (it.failed_stages || [])
            .map(
              (f) =>
                `• ${f.stage}${f.error ? ": " + f.error : ""}`
            )
            .join("\n");
          return (
            `<article class="history-card">` +
            `<div class="history-card-head">` +
            `<span class="outcome-badge outcome-${outcome}">${outcome}</span>` +
            `<span class="history-meta">${_fmtTime(it.finished_at) || "now"}</span>` +
            `<span class="history-meta">${it.domain || "?"} ${
              it.difficulty ? "· " + it.difficulty : ""
            }</span>` +
            (tokStr ? `<span class="history-meta">${tokStr}</span>` : "") +
            (it.has_code
              ? `<span class="history-meta">code ${it.code_len} chars</span>`
              : `<span class="history-meta">no Lean code</span>`) +
            (it.job_id
              ? `<span class="history-meta">#${String(it.job_id).slice(0, 8)}</span>`
              : "") +
            `</div>` +
            `<div class="history-claim">${(it.claim || "(no claim text)").replace(
              /</g,
              "&lt;"
            )}</div>` +
            (it.message
              ? `<div class="history-msg">${String(it.message)
                  .replace(/</g, "&lt;")
                  .slice(0, 280)}</div>`
              : "") +
            (stages
              ? `<div class="history-stages">${stages}</div>`
              : "") +
            (fails
              ? `<div class="history-fail">${fails.replace(/</g, "&lt;")}</div>`
              : "") +
            `</article>`
          );
        })
        .join("");
    }
  } catch (e) {
    if (list) {
      list.innerHTML = `<p class="muted">Error loading history: ${String(
        e.message || e
      )}</p>`;
    }
  }
}

function renderBars(el, obj) {
  if (!el) return;
  const entries = Object.entries(obj || {});
  if (!entries.length) {
    el.innerHTML = "<span class=\"muted\">(empty)</span>";
    return;
  }
  const max = Math.max(...entries.map(([, v]) => Number(v) || 0), 1);
  el.innerHTML = entries
    .map(([k, v]) => {
      const n = Number(v) || 0;
      const pct = Math.round((n / max) * 100);
      return (
        `<div class="bar-row"><span>${k}</span>` +
        `<div class="bar-track"><div class="bar-fill" style="width:${pct}%"></div></div>` +
        `<span>${n}</span></div>`
      );
    })
    .join("");
}

async function loadStats() {
  const cards = $("#stats-cards");
  if (cards) cards.innerHTML = `<div class="stat-card"><div class="stat-label">Loading…</div></div>`;
  try {
    const d = await api("/api/stats", {}, 15000);
    const items = [
      ["Examples", d.example_count ?? 0],
      ["Verified (memory)", d.translator_success_count ?? d.success_count ?? 0],
      ["Repairs", d.repair_count ?? 0],
      ["With NL proof", d.with_nl_proof ?? 0],
      ["Session verified", d.session_verified ?? 0],
      ["Session failed", d.session_failed ?? 0],
      ["Session timeouts", d.session_timed_out ?? 0],
      ["Jobs (process)", d.jobs_total ?? 0],
    ];
    if (cards) {
      cards.innerHTML = items
        .map(
          ([label, val]) =>
            `<div class="stat-card"><div class="stat-value">${val}</div><div class="stat-label">${label}</div></div>`
        )
        .join("");
    }
    renderBars($("#stats-domains"), {
      ...(d.examples_by_domain || {}),
    });
    // prefer showing both domains merged labels for examples; also show success domains below text
    const dom = $("#stats-domains");
    if (dom) {
      const ex = d.examples_by_domain || {};
      const su = d.success_by_domain || {};
      const keys = new Set([...Object.keys(ex), ...Object.keys(su)]);
      const merged = {};
      keys.forEach((k) => {
        merged[k] = (ex[k] || 0) + (su[k] || 0);
      });
      renderBars(dom, merged);
    }
    renderBars($("#stats-difficulty"), {
      ...(d.examples_by_difficulty || {}),
      ...(d.success_by_difficulty || {}),
    });
    // merge difficulty properly
    const diffEl = $("#stats-difficulty");
    if (diffEl) {
      const a = d.examples_by_difficulty || {};
      const b = d.success_by_difficulty || {};
      const keys = new Set([...Object.keys(a), ...Object.keys(b)]);
      const merged = {};
      keys.forEach((k) => {
        merged[k] = (a[k] || 0) + (b[k] || 0);
      });
      renderBars(diffEl, merged);
    }
    if ($("#stats-session")) {
      $("#stats-session").textContent = [
        `jobs_total: ${d.jobs_total ?? 0}`,
        `jobs_done: ${d.jobs_done ?? 0}`,
        `jobs_running: ${d.jobs_running ?? 0}`,
        `jobs_error: ${d.jobs_error ?? 0}`,
        `session_verified: ${d.session_verified ?? 0}`,
        `session_failed: ${d.session_failed ?? 0}`,
        `session_timed_out: ${d.session_timed_out ?? 0}`,
        "",
        "(Session counters reset when the server restarts.)",
      ].join("\n");
    }
    if ($("#stats-recent")) {
      const rows = d.recent_successes || [];
      $("#stats-recent").textContent = rows.length
        ? rows
            .map(
              (r, i) =>
                `${i + 1}. [${r.domain || "?"}/${r.difficulty || "?"}] ${(r.nl || "").slice(0, 100)}`
            )
            .join("\n")
        : "(no translator-verified successes saved yet)";
    }
    if ($("#stats-env")) {
      $("#stats-env").textContent = [
        `version: ${d.version || "?"}`,
        `llm_backend: ${d.llm_backend || "?"}`,
        `lean_available: ${d.lean_available}`,
        `lean_project: ${d.lean_project || "(not set)"}`,
        `memory_dir: ${d.memory_dir || "?"}`,
      ].join("\n");
    }
  } catch (e) {
    if (cards) {
      cards.innerHTML = `<div class="stat-card"><div class="stat-label">Error: ${String(e.message || e)}</div></div>`;
    }
  }
}


document.addEventListener("DOMContentLoaded", init);

async function init() {
  wireTabs();
  wireBackend();
  wireDropzone();
  wireButtons();
  wireTheme();
  wireDetailsTabs();
  wirePageNav();
  loadSavedOptions();
  updateTimeoutEstimate();
  [
    "#llm-backend", "#llm-http-url", "#llm-http-key", "#llm-http-model",
    "#openai-api-key", "#openai-model", "#anthropic-api-key", "#anthropic-model",
    "#domain", "#num-candidates", "#llm-candidate-workers", "#max-repairs", "#temperature", "#lean-compile-workers",
    "#llm-timeout", "#job-timeout", "#input-statement",
  ].forEach((sel) => {
    $(sel)?.addEventListener("change", saveOptionsFromForm);
    $(sel)?.addEventListener("blur", saveOptionsFromForm);
  });
  [
    "#input-text", "#input-statement", "#llm-backend", "#llm-http-url",
    "#llm-http-model", "#openai-model", "#anthropic-model",
    "#num-candidates", "#llm-candidate-workers", "#max-repairs", "#lean-compile-workers",
  ].forEach((sel) => {
    $(sel)?.addEventListener("input", updateTimeoutEstimate);
    $(sel)?.addEventListener("change", updateTimeoutEstimate);
  });
  $("#job-timeout")?.addEventListener("input", () => {
    $("#job-timeout").dataset.userEdited = "true";
    saveOptionsFromForm();
  });
  ["#llm-backend", "#llm-http-url", "#llm-http-key", "#llm-http-model"].forEach((sel) => {
    $(sel)?.addEventListener("change", () => { void checkBackendConfig(); });
    $(sel)?.addEventListener("blur", () => { void checkBackendConfig(); });
  });
  await refreshHealth();
  // The server configuration is authoritative at first load.  Without this,
  // the HTML's default "mock" selection overwrote a reachable configured HTTP
  // backend immediately after /api/health correctly reported it.
  if (state.lastHealth?.llm_backend === "http" && state.lastHealth.http_available) {
    const backendSelect = $("#llm-backend");
    if (backendSelect && backendSelect.value === "mock") {
      backendSelect.value = "http";
      backendSelect.dispatchEvent(new Event("change"));
    }
  }
  await checkBackendConfig();
}

function wireTheme() {
  const saved = localStorage.getItem("lean_formalizer_theme") || "dark";
  document.documentElement.setAttribute("data-theme", saved);
  $("#btn-theme")?.addEventListener("click", () => {
    const cur = document.documentElement.getAttribute("data-theme") || "dark";
    const next = cur === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    localStorage.setItem("lean_formalizer_theme", next);
  });
}

function wireTabs() {
  $$(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      $$(".tab").forEach((t) => t.classList.remove("active"));
      $$(".tab-panel").forEach((p) => p.classList.remove("active"));
      tab.classList.add("active");
      $(`#tab-${tab.dataset.tab}`)?.classList.add("active");
    });
  });
}

function wireDetailsTabs() {
  $$(".dtab").forEach((tab) => {
    tab.addEventListener("click", () => {
      $$(".dtab").forEach((t) => t.classList.remove("active"));
      $$(".dtab-panel").forEach((p) => p.classList.add("hidden"));
      tab.classList.add("active");
      const panel = $(`#dtab-${tab.dataset.dtab}`);
      panel?.classList.remove("hidden");
    });
  });
}

function wireBackend() {
  const sel = $("#llm-backend");
  if (!sel) return;
  const show = () => {
    $$(".api-cfg").forEach((el) => el.classList.add("hidden"));
    const v = sel.value;
    if (v === "openai") $("#cfg-openai")?.classList.remove("hidden");
    if (v === "anthropic") $("#cfg-anthropic")?.classList.remove("hidden");
    if (v === "http") $("#cfg-http")?.classList.remove("hidden");
  };
  sel.addEventListener("change", show);
  show();
}

function httpCheckPayload() {
  const selected = $("#llm-backend")?.value || "mock";
  const payload = { llm_backend: selected };
  const url = $("#llm-http-url")?.value.trim() || "";
  if (selected === "http" && url) {
    // Preserve the endpoint exactly as entered.  URL normalization belongs to
    // the HTTP client, not to this form field or its connection check.
    payload.llm_http_url = url;
    payload.llm_http_key = $("#llm-http-key")?.value.trim() || "";
    payload.llm_http_model = $("#llm-http-model")?.value.trim() || "";
  }
  return payload;
}

function updateHttpConnectionUI(status) {
  const selected = $("#llm-backend")?.value || "mock";
  const blankEndpoint = !($("#llm-http-url")?.value.trim());
  const useServerConfig = selected === "http" && blankEndpoint &&
    !!status?.server_http_configured && !!status?.http_available;
  $("#http-key-field")?.classList.toggle("hidden", useServerConfig);
  const hint = $("#http-connection-status");
  if (hint) hint.textContent = selected === "http" ? (status?.reason || "Checking endpoint...") : "";
}

function renderBackendPill(status) {
  const pill = $("#status-pill");
  if (!pill || !status) return;
  const health = state.lastHealth || {};
  const model = status.model ? ` · ${status.model}` : "";
  pill.textContent = `${health.lean_available ? "Lean ✓" : "Lean ✕"} · ${status.backend}${model}`;
  pill.classList.add("ok");
  pill.classList.remove("bad");
}

async function checkBackendConfig() {
  const requestId = ++state.backendCheckRequest;
  const selected = $("#llm-backend")?.value || "mock";
  if (selected !== "http") {
    const status = { backend: selected, model: activeModel({ llm_backend: selected }), reason: "" };
    state.backendStatus = status;
    updateHttpConnectionUI(status);
    renderBackendPill(status);
    return status;
  }
  try {
    const status = await api("/api/backend/check", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(httpCheckPayload()),
    }, 5000);
    if (requestId !== state.backendCheckRequest) return state.backendStatus;
    state.backendStatus = status;
    updateHttpConnectionUI(status);
    renderBackendPill(status);
    return status;
  } catch (_) {
    if (requestId !== state.backendCheckRequest) return state.backendStatus;
    const status = { backend: "mock", model: "offline memory replay", reason: "Could not check endpoint. Using mock." };
    state.backendStatus = status;
    updateHttpConnectionUI(status);
    renderBackendPill(status);
    return status;
  }
}

function activeModel(cfg = collectConfig()) {
  if (cfg.llm_backend === "http") return cfg.llm_http_model || "default";
  if (cfg.llm_backend === "openai") return cfg.openai_model || "gpt-4o";
  if (cfg.llm_backend === "anthropic") return cfg.anthropic_model || "claude-sonnet-4-20250514";
  return "offline memory replay";
}

function estimateTimeoutSeconds(text, cfg = collectConfig()) {
  const words = String(text || "").trim().split(/\s+/).filter(Boolean).length;
  const candidates = Math.max(1, Number(cfg.num_candidates) || 1);
  const candidateWorkers = Math.min(candidates, Math.max(1, Number(cfg.max_parallel_candidates) || 1));
  const repairs = Math.max(0, Number(cfg.max_repair_rounds) || 0);
  const leanWorkers = Math.min(candidates, Math.max(1, Number(cfg.lean_compile_workers) || 1));
  const leanPerCheck = Math.max(15, Number(cfg.lean_compile_timeout) || 120);
  const graduate = /graduate|finite.?dimensional|banach|galois|noetherian|manifold|compact|measure|topolog|spectral|linear algebra/.test(String(text || "").toLowerCase());
  const base = cfg.llm_backend === "mock" ? 30 : 75;
  const generationWaves = Math.ceil(candidates / candidateWorkers);
  const leanChecks = 2 + Math.ceil(candidates / leanWorkers) + candidates * repairs + 1;
  const estimate = base + Math.min(words, 1200) * 0.45 + generationWaves * 35 + repairs * 45 +
    leanChecks * Math.min(45, leanPerCheck) + (graduate ? 120 : 0);
  return Math.max(30, Math.min(1800, Math.ceil(estimate / 10) * 10));
}

function updateTimeoutEstimate() {
  const cfg = collectConfig();
  const text = `${$("#input-text")?.value || ""}\n${$("#input-statement")?.value || ""}`;
  const seconds = estimateTimeoutSeconds(text, cfg);
  const candidates = Math.max(1, Number(cfg.num_candidates) || 1);
  const repairs = Math.max(0, Number(cfg.max_repair_rounds) || 0);
  const leanWorkers = Math.min(candidates, Math.max(1, Number(cfg.lean_compile_workers) || 1));
  const leanChecks = 2 + Math.ceil(candidates / leanWorkers) + candidates * repairs + 1;
  const note = $("#timeout-estimate");
  if (note) {
    note.dataset.leanChecks = String(leanChecks);
    const fallback = cfg.requested_backend === "http" && cfg.llm_backend === "mock"
      ? " HTTP has no endpoint, so this run will use mock."
      : "";
    const checkNote = ` Up to ${leanChecks} Lean checks (${candidates} candidates, ${repairs} repairs each).`;
    note.dataset.checkNote = checkNote;
    // appended after the base recommendation below
    note.textContent = `Recommended time limit: ${seconds}s (${cfg.llm_backend} · ${activeModel(cfg)}).${fallback}`;
    note.textContent = `${note.textContent}${checkNote}`;
  }
  const input = $("#job-timeout");
  if (input && (!input.dataset.userEdited || Number(input.value || 0) < seconds)) input.value = String(seconds);
}

function wireDropzone() {
  const dz = $("#dropzone");
  const input = $("#input-pdf");
  const label = $("#pdf-label");
  if (!dz || !input) return;
  const setFile = (file) => {
    if (!file) return;
    state.pdfFile = file;
    if (label) label.textContent = `${file.name} (${(file.size / 1024).toFixed(1)} KB)`;
  };
  input.addEventListener("change", () => setFile(input.files[0]));
  dz.addEventListener("dragover", (e) => { e.preventDefault(); dz.classList.add("drag"); });
  dz.addEventListener("dragleave", () => dz.classList.remove("drag"));
  dz.addEventListener("drop", (e) => {
    e.preventDefault();
    dz.classList.remove("drag");
    const file = e.dataTransfer.files[0];
    if (file) {
      try { input.files = e.dataTransfer.files; } catch (_) {}
      setFile(file);
    }
  });
}

function wireButtons() {
  $("#btn-run")?.addEventListener("click", onRun);
  $("#btn-doctor")?.addEventListener("click", onDoctor);
  $("#btn-memory")?.addEventListener("click", onMemory);
  $("#btn-details")?.addEventListener("click", () => {
    if (state.lastResult) $("#modal-details")?.classList.remove("hidden");
  });
  $("#btn-copy")?.addEventListener("click", onCopy);
  $("#btn-copy-report")?.addEventListener("click", onCopyReport);
  $("#btn-download")?.addEventListener("click", onDownload);
  $$("[data-close]").forEach((btn) => {
    btn.addEventListener("click", () => btn.closest(".modal")?.classList.add("hidden"));
  });
  $$(".modal").forEach((m) => {
    m.addEventListener("click", (e) => { if (e.target === m) m.classList.add("hidden"); });
  });
}

function loadSavedOptions() {
  try {
    const raw = localStorage.getItem(LS_KEY);
    if (!raw) return;
    const o = JSON.parse(raw);
    const set = (id, val) => {
      const el = $(id);
      if (el && val != null && val !== "") el.value = val;
    };
    if (o.llm_backend && $("#llm-backend")) {
      $("#llm-backend").value = o.llm_backend;
      $("#llm-backend").dispatchEvent(new Event("change"));
    }
    set("#llm-http-url", o.llm_http_url);
    set("#llm-http-key", o.llm_http_key);
    set("#llm-http-model", o.llm_http_model);
    set("#openai-api-key", o.openai_api_key);
    set("#openai-model", o.openai_model);
    set("#anthropic-api-key", o.anthropic_api_key);
    set("#anthropic-model", o.anthropic_model);
    set("#domain", o.domain);
    set("#num-candidates", o.num_candidates);
    set("#llm-candidate-workers", o.max_parallel_candidates);
    set("#max-repairs", o.max_repair_rounds);
    set("#lean-compile-workers", o.lean_compile_workers);
    set("#lean-compile-timeout", o.lean_compile_timeout);
    set("#temperature", o.temperature);
    set("#llm-timeout", o.llm_timeout);
    set("#job-timeout", o.job_timeout);
    set("#input-statement", o.target_statement);
  } catch (e) {
    console.warn("loadSavedOptions", e);
  }
}

function saveOptionsFromForm() {
  try {
    localStorage.setItem(LS_KEY, JSON.stringify(collectConfig()));
  } catch (e) {
    console.warn("saveOptionsFromForm", e);
  }
}

function collectConfig() {
  const httpUrl = $("#llm-http-url")?.value.trim() || "";
  const selectedBackend = $("#llm-backend")?.value || "mock";
  const effectiveBackend = selectedBackend === "http"
    ? (state.backendStatus?.backend === "http" ? "http" : "mock")
    : selectedBackend;
  const config = {
    domain: $("#domain")?.value || "general",
    llm_backend: effectiveBackend,
    requested_backend: selectedBackend,
    openai_api_key: $("#openai-api-key")?.value.trim() || "",
    openai_model: $("#openai-model")?.value.trim() || "",
    anthropic_api_key: $("#anthropic-api-key")?.value.trim() || "",
    anthropic_model: $("#anthropic-model")?.value.trim() || "",
    num_candidates: $("#num-candidates")?.value || "2",
    max_parallel_candidates: $("#llm-candidate-workers")?.value || "1",
    lean_compile_workers: $("#lean-compile-workers")?.value || "1",
    lean_compile_timeout: $("#lean-compile-timeout")?.value || "120",
    max_repair_rounds: $("#max-repairs")?.value || "3",
    temperature: $("#temperature")?.value || "0.2",
    save_to_memory: $("#save-memory")?.checked ? "true" : "false",
    max_pages: $("#max-pages")?.value || "",
    llm_timeout: $("#llm-timeout")?.value || "300",
    job_timeout: $("#job-timeout")?.value || "1200",
    target_statement: $("#input-statement")?.value.trim() || "",
  };
  // Leave HTTP fields out when using a working server-side configuration.
  // Sending an explicit blank URL would make the server treat it as a request
  // to clear that configuration.
  if (httpUrl) {
    config.llm_http_url = httpUrl;
    config.llm_http_key = $("#llm-http-key")?.value.trim() || "";
    config.llm_http_model = $("#llm-http-model")?.value.trim() || "";
  }
  return config;
}

async function api(path, options = {}, timeoutMs = 30000) {
  if (location.protocol === "file:") {
    throw new Error("Open via http://127.0.0.1:8765 (run python run_web.py)");
  }
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const res = await fetch(path, { ...options, signal: ctrl.signal });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || res.statusText || "Request failed");
    return data;
  } catch (e) {
    if (e && e.name === "AbortError") {
      throw new Error("Request timed out. Restart: python run_web.py");
    }
    if (e && String(e.message || e).includes("Failed to fetch")) {
      throw new Error("Cannot reach server. Run: python run_web.py");
    }
    throw e;
  } finally {
    clearTimeout(timer);
  }
}

async function refreshHealth() {
  const pill = $("#status-pill");
  if (!pill) return;
  try {
    const h = await api("/api/health", {}, 5000);
    state.lastHealth = h;
    const model = h.llm_model ? ` · ${h.llm_model}` : "";
    pill.textContent = `${h.lean_available ? "Lean ✓" : "Lean ✕"} · ${h.llm_backend}${model}`;
    pill.classList.add("ok");
    pill.classList.remove("bad");
  } catch (e) {
    pill.textContent = "offline";
    pill.classList.add("bad");
    pill.classList.remove("ok");
  }
}

async function pollJob(jobId, serverLimitSeconds = 0) {
  const start = Date.now();
  const hint = $("#run-hint");
  // The server owns terminal state. A browser timer cannot safely cancel a
  // Python worker, so keep polling until the server says the job is done.
  while (true) {
    try {
      const job = await api(`/api/job/${jobId}`, {}, 15000);
      const step = progressEventLabel(job.current_event || { stage: job.step || job.status || "" });
      const elapsed = Math.round((Date.now() - start) / 1000);
      const limit = Number(job.job_timeout_seconds || serverLimitSeconds || 0);
      renderLiveProgress(
        job,
        elapsed,
        limit
      );
      // Retained only for source compatibility with old customized pages.
      // Server-authoritative status is rendered by the block below.
      if (false && hint) {
        const elapsed = Math.round((Date.now() - start) / 1000);
        const left = Math.max(0, Math.round((maxWaitMs - (Date.now() - start)) / 1000));
        hint.textContent = `${job.status}${step ? " / " + step : ""}… (${elapsed}s, limit ${Math.round(maxWaitMs/1000)}s)`;
      }
      if (hint) {
        const overLimit = limit && elapsed > limit;
        hint.textContent = overLimit
          ? `Still running: ${step}. Waiting for the server to finish its active operation (${elapsed}s; requested limit ${limit}s).`
          : `${job.status}${step ? " / " + step : ""}… (${elapsed}s${limit ? ` / ${limit}s requested` : ""})`;
      }
      if (job.status === "done") return job.result;
      if (job.status === "error") throw new Error(job.error || "Job failed");
    } catch (e) {
      // Transient network blip: keep polling until outer limit
      if (String(e.message || e).includes("timed out") || String(e.message || e).includes("Failed to fetch")) {
        if (hint) hint.textContent = "Waiting for server…";
      } else {
        throw e;
      }
    }
    await new Promise((r) => setTimeout(r, 800));
  }
  throw new Error(
    "Browser stopped waiting after " + Math.round(maxWaitMs / 1000) +
    "s. Raise Job timeout in the UI (and ensure it is ≥ this wait). Server may still be running — check the terminal log."
  );
}

const stageLabels = {
  queued: "Queued", started: "Preparing request", done: "Finalizing result", error: "Job failed", mock_fast: "Checking offline memory",
  input: "Reading input", structure: "Understanding theorem", oneshot: "Generating Lean proof",
  statement: "Generating Lean statement", proof: "Generating proof", complete_proof: "Completing proof",
  proof_known_completion: "Applying known Mathlib completion", proof_extract: "Extracting proof",
  proof_audit: "Reviewing proof against final Lean result",
  statement_extract: "Extracting statement", oneshot_extract: "Extracting Lean code",
  candidate_generated: "Candidate generated",
  final_compile_skipped: "Reusing prior Lean timeout",
  pipeline: "Starting pipeline", llm_send: "Sending request to LLM",
  llm_wait: "Waiting for LLM response", llm_response: "LLM response received",
  lean_compile: "Compiling with Lean", verify_candidate: "Verifying with Lean",
  verify_result: "Lean verification complete", rank: "Selecting final candidate",
  empty_result: "No usable Lean code",
  candidate_duplicate: "Duplicate candidate reused", candidate_timeout: "Lean compilation timed out",
  timeout_recovery: "Simplifying after Lean timeout",
  repair_noop: "Unchanged repair skipped", scratch_record: "Saving compiler record",
  import_recovery: "Fixing invalid import", import_repair: "Replacing umbrella import",
  import_repair_result: "Import repair result", lean_compile_wave: "Starting Lean compile wave",
  import_preflight: "Checking imports against local Mathlib",
  statement_preflight: "Preparing Lean statement", statement_invalid: "Lean statement rejected",
  statement_preflight_timeout: "Statement check was slow; continuing", statement_repair: "Repairing Lean statement",
  statement_repair_result: "Statement repair result",
  candidate_generation_skipped: "Candidate generation skipped",
  structure_placeholder_guard: "Malformed structure guarded",
  local_lean_query: "Looking up local Lean facts",
  verification_lock: "Waiting for compiler files", compiler_context: "Preparing compiler files",
};

const operationLabels = {
  structure: "theorem analysis", understand: "mathematical plan",
  statement: "Lean statement", proof: "Lean proof",
  critique: "proof review", complete_proof: "proof completion",
  repair: "Lean repair", timeout_recovery: "Lean timeout recovery", complete_proof_repair: "repair proof completion",
  oneshot: "Lean proof", oneshot_user_lean: "user Lean declaration",
  nl_proof: "natural-language proof summary",
};

function progressEventLabel(entry = {}) {
  const stage = entry.stage || "pipeline";
  const operation = operationLabels[entry.request_stage] || entry.request_stage || "request";
  const candidate = entry.candidate ? `, candidate ${entry.candidate}` : "";
  const repairRound = entry.round ? `, repair round ${entry.round}` : "";
  const attempt = entry.attempt ? `, attempt ${entry.attempt}` : "";
  if (stage === "llm_send") return `Sending ${operation} to LLM${candidate}${repairRound}`;
  if (stage === "llm_wait") return `Waiting for ${operation}${candidate}${repairRound}`;
  if (stage === "llm_response") return `Received ${operation} response${candidate}${repairRound}`;
  if (stage === "timeout_budget") {
    const llm = entry.llm_call_seconds ? `${entry.llm_call_seconds}s API timeout` : "API timeout";
    const remaining = entry.job_deadline_seconds != null ? `, ${entry.job_deadline_seconds}s job time remaining` : "";
    const lean = entry.lean_compile_seconds ? `${entry.lean_compile_seconds}s Lean` : "Lean budget";
    const requested = entry.requested_candidates ?? "?";
    const repairs = entry.max_repair_rounds ?? "?";
    const workers = entry.lean_compile_workers ? `${entry.lean_compile_workers} Lean worker` : "? Lean workers";
    const llmWorkers = entry.llm_candidate_workers ? `${entry.llm_candidate_workers} LLM worker` : "? LLM workers";
    return `Limits: ${llm} per call${remaining}, ${lean} per check; ${requested} candidates, up to ${repairs} repairs each; ${llmWorkers}, ${workers}`;
  }
  if (stage === "lean_compile") {
    if (entry.phase === "final_start") return "Compiling final Lean code";
    if (entry.phase === "final_result") return entry.ok === false ? "Final Lean compilation failed" : "Final Lean compilation succeeded";
    if (entry.phase === "memory_hit_start") return "Compiling saved Lean code";
    if (entry.phase === "memory_hit_result") return entry.ok === false ? "Saved Lean code failed to compile" : "Saved Lean code compiled";
    if (entry.phase === "mock_start") return "Compiling saved mock result";
    if (entry.phase === "mock_result") return entry.ok === false ? "Saved mock result failed to compile" : "Saved mock result compiled";
    if (entry.phase === "statement_start") return "Checking Lean statement";
    if (entry.phase === "statement_result") return entry.ok === false ? "Lean statement rejected" : "Lean statement accepted";
    return /start$/.test(entry.phase || "")
      ? `Compiling candidate${candidate}${attempt}`
      : entry.ok === false
        ? `Candidate rejected by Lean${candidate}${attempt}`
        : `Candidate compiled by Lean${candidate}${attempt}`;
  }
  if (stage === "candidate_duplicate") return `Candidate ${entry.candidate} duplicates candidate ${entry.duplicate_of}; reusing result`;
  if (stage === "candidate_timeout") return `Lean timed out for candidate ${entry.candidate}; no further timeout retry`;
  if (stage === "timeout_recovery") return `Lean timed out for candidate ${entry.candidate}; requesting one simplified proof${repairRound}`;
  if (stage === "final_compile_skipped") return "Reusing selected candidate's prior Lean timeout; no redundant final compile";
  if (stage === "candidate_generation_skipped") return entry.preview || `Candidate generation skipped (0 / ${entry.requested_candidates ?? "?"})`;
  if (stage === "repair_noop") return `Repair ${repairRound.replace(", ", "")} was unchanged; no recompilation`;
  return stageLabels[stage] || stage;
}

const pipelinePhases = [
  ["input", "Input"], ["structure", "Structure"], ["candidate", "Generation"],
  ["verify", "Verification"], ["rank", "Ranking"], ["final", "Final compile"], ["audit", "Proof review"],
];

function pipelinePhase(entry = {}) {
  const stage = entry.stage || "";
  if (["input"].includes(stage)) return "input";
  if (["structure", "understand", "statement", "statement_extract", "statement_preflight", "statement_invalid", "statement_repair", "statement_repair_result", "candidate_generation_skipped", "local_lean_query"].includes(stage) || (stage === "lean_compile" && String(entry.phase || "").startsWith("statement"))) return "structure";
  if (["candidate_parallel", "candidate_generated", "proof", "proof_extract", "oneshot", "oneshot_extract", "complete_proof", "critique", "llm_send", "llm_wait", "llm_response"].includes(stage) && !entry.candidate) return "candidate";
  if (["verify_candidate", "verify_result", "candidate_duplicate", "candidate_timeout", "timeout_recovery", "repair_noop"].includes(stage) || (stage === "lean_compile" && !String(entry.phase || "").startsWith("final"))) return "verify";
  if (stage === "rank") return "rank";
  if ((stage === "lean_compile" && String(entry.phase || "").startsWith("final")) || stage === "final_compile_skipped") return "final";
  if (stage === "proof_audit") return "audit";
  return "candidate";
}

function renderPipelinePhases(entries, currentEvent) {
  const list = $("#run-progress-phases");
  if (!list) return;
  const current = pipelinePhase(currentEvent || {});
  const currentIndex = pipelinePhases.findIndex(([key]) => key === current);
  list.innerHTML = "";
  pipelinePhases.forEach(([key, name], index) => {
    const phaseEntries = (entries || []).filter((entry) => pipelinePhase(entry) === key);
    const item = document.createElement("li");
    const isCurrent = key === current;
    const failed = phaseEntries.some((entry) => entry.ok === false) && !isCurrent;
    item.className = `${index < currentIndex ? "complete" : ""} ${isCurrent ? "active" : ""} ${failed ? "failed" : ""}`.trim();
    item.textContent = name;
    list.appendChild(item);
  });
}

function renderLeanFeedback({ state: feedbackState, message, live = false, statusText = "" }) {
  const panel = $("#lean-feedback");
  const status = $("#lean-feedback-status");
  const output = $("#out-lean-feedback");
  if (!panel || !status || !output) return;
  panel.classList.remove("hidden", "compiled", "failed", "compiling");
  const isCompiling = feedbackState === "compiling";
  const compiled = feedbackState === "compiled";
  const failed = feedbackState === "failed";
  panel.classList.add(isCompiling ? "compiling" : compiled ? "compiled" : "failed");
  status.textContent = isCompiling ? "Compiling…" : compiled ? "Compiled successfully" : "Compilation failed";
  if (statusText) status.textContent = statusText;
  output.textContent = message || (live
    ? "Lean compilation has not started yet."
    : "No compiler output was returned.");
}

function renderLiveLeanFeedback(entries) {
  const compilation = [...(entries || [])]
    .reverse()
    .find((entry) => entry?.stage === "lean_compile");
  if (!compilation) {
    // Do not show diagnostics from an earlier run while this one is still
    // waiting for its first Lean compilation event.
    $("#lean-feedback")?.classList.add("hidden");
    return;
  }
  if (compilation.phase && /start$/.test(compilation.phase)) {
    renderLeanFeedback({
      state: "compiling",
      live: true,
      statusText: progressEventLabel(compilation),
      message: "Lean is compiling the generated code…",
    });
    return;
  }
  renderLeanFeedback({
    state: compilation.ok === false ? "failed" : "compiled",
    live: true,
    statusText: compilation.phase === "final_result"
      ? (compilation.ok === false ? "Final compilation failed" : "Final code compiled successfully")
      : (compilation.ok === false
        ? "Candidate rejected; repair or another candidate may still run"
        : "Candidate compiled; selecting final result"),
    message: compilation.error || (compilation.ok === false
      ? "Lean rejected the code without a diagnostic message."
      : "Lean compiled this candidate successfully."),
  });
}

function finalLeanFeedback(data) {
  const verification = data.verification || {};
  const candidates = data.candidates || [];
  const failedCandidate = candidates.find((candidate) =>
    candidate && candidate.compiled === false && (candidate.error_log || []).length
  );
  const message = verification.raw_message ||
    (failedCandidate?.error_log || []).slice(-1)[0] ||
    verification.summary || data.message || "";
  const code = data.best_code || candidates.find((candidate) =>
    (candidate?.lean_code || "").trim()
  )?.lean_code || "";
  const compilerRan = (data.stage_trace || []).some(
    (entry) => entry?.stage === "lean_compile"
  );
  if (!compilerRan && !String(code).trim()) {
    $("#lean-feedback")?.classList.add("hidden");
    return;
  }
  renderLeanFeedback({
    state: verification.compiled || data.success ? "compiled" : "failed",
    message,
  });
}

function formatDuration(seconds) {
  const value = Number(seconds || 0);
  if (value >= 60) return `${Math.floor(value / 60)}m ${(value % 60).toFixed(1)}s`;
  return `${value.toFixed(value >= 10 ? 1 : 2)}s`;
}

function timingLabel(entry = {}) {
  let label = progressEventLabel(entry);
  if (entry.candidate && !label.includes(`candidate ${entry.candidate}`)) label += ` · candidate ${entry.candidate}`;
  return label;
}

function renderTimingSummary(entries = []) {
  const section = $("#timing-summary");
  const list = $("#timing-list");
  const total = $("#timing-total");
  if (!section || !list) return;
  const timed = entries.filter((entry) => Number.isFinite(Number(entry?.elapsed_seconds)));
  if (timed.length < 2) {
    section.classList.add("hidden");
    return;
  }
  const rows = [];
  for (let index = 0; index < timed.length - 1; index += 1) {
    const start = timed[index];
    const end = timed[index + 1];
    const duration = Math.max(0, Number(end.elapsed_seconds) - Number(start.elapsed_seconds));
    if (start.stage === "llm_send" && end.stage === "llm_wait") continue;
    if (start.stage === "llm_wait" && end.stage === "llm_response") {
      rows.push({ label: timingLabel(start), duration, failed: end.ok === false });
      continue;
    }
    if (start.stage === "lean_compile" && start.phase === "start" &&
        end.stage === "lean_compile" && end.phase === "result") {
      rows.push({ label: timingLabel(start), duration, failed: end.ok === false });
      continue;
    }
    rows.push({ label: timingLabel(start), duration, failed: start.ok === false || end.ok === false });
  }
  if (!rows.length) {
    section.classList.add("hidden");
    return;
  }
  section.classList.remove("hidden");
  if (total) total.textContent = `Pipeline: ${formatDuration(timed[timed.length - 1].elapsed_seconds)}`;
  const compileChecks = timed.filter((entry) => entry?.stage === "lean_compile" &&
    ["start", "statement_start", "final_start", "memory_hit_start"].includes(entry?.phase)).length;
  if (total) total.textContent += ` · Lean checks started: ${compileChecks}`;
  list.innerHTML = "";
  rows.forEach((row) => {
    const item = document.createElement("li");
    item.className = row.duration >= 20 || row.failed ? "slow" : "";
    const label = document.createElement("span");
    label.className = "timing-label";
    label.textContent = row.label;
    const value = document.createElement("span");
    value.className = "timing-value";
    value.textContent = formatDuration(row.duration);
    item.append(label, value);
    list.appendChild(item);
  });
}

function renderProofAudit(audit) {
  const section = $("#proof-audit");
  const status = $("#proof-audit-status");
  const summary = $("#proof-audit-summary");
  const steps = $("#proof-audit-steps");
  if (!section || !status || !summary || !steps) return;
  if (!audit) {
    section.classList.add("hidden");
    steps.innerHTML = "";
    return;
  }
  section.classList.remove("hidden");
  // API review is shown separately from the Lean compiler result.
  /* legacy status retained for source compatibility:
  status.textContent = `${audit.overall || "inconclusive"} · ${audit.theorem_alignment || "unverified"}`;
  */
  status.textContent = `API review: ${audit.overall || "inconclusive"}; ${audit.theorem_alignment || "unverified"}`;
  summary.textContent = audit.summary || "No advisory proof-review summary was returned.";
  steps.innerHTML = "";
  (audit.steps || []).forEach((step) => {
    const item = document.createElement("li");
    item.className = `proof-step ${step.status || "inconclusive"}`;
    const title = document.createElement("strong");
    title.textContent = `Step ${step.index}: ${step.status || "inconclusive"}`;
    const body = document.createElement("span");
    body.textContent = step.explanation || step.claim || step.source || "";
    item.append(title, body);
    if (Array.isArray(step.missing) && step.missing.length) {
      const missing = document.createElement("small");
      missing.textContent = `Missing: ${step.missing.join("; ")}`;
      item.append(missing);
    }
    steps.appendChild(item);
  });
}

function candidateState(candidate, entries = [], selected = false) {
  if (candidate) {
    const statuses = {
      verified: ["Verified", "verified"], compile_error: ["Compile error", "failed"],
      compile_timeout: ["Lean timed out", "timeout"], lean_unavailable: ["Lean unavailable", "failed"],
      generation_timeout: ["Generation timed out", "timeout"], duplicate: ["Duplicate", "duplicate"],
      repair_noop: ["Unchanged repair", "failed"],
      lean_project_not_writable: ["Project not writable", "failed"],
    };
    if (candidate.status && statuses[candidate.status]) {
      const [label, className] = statuses[candidate.status];
      return { label: selected ? `${label} · selected` : label, className };
    }
    if (selected) return { label: candidate.compiled === true ? "Selected result" : "Selected for feedback", className: candidate.compiled === true ? "verified" : "failed" };
    if (candidate.compiled === true) return { label: "Verified", className: "verified" };
    if (candidate.compiled === false) return { label: "Not verified", className: "failed" };
  }
  const latest = [...entries].reverse().find((entry) => entry?.candidate != null);
  if (!latest) return { label: "Queued", className: "" };
  if (latest.stage === "verify_result") {
    return latest.ok === false
      ? { label: "Not verified", className: "failed" }
      : { label: "Verified", className: "verified" };
  }
  if (latest.ok === false) return { label: "Needs repair", className: "failed" };
  return { label: "Running", className: "active" };
}

function candidateSummary(candidate, entries = []) {
  if (candidate) {
    if (candidate.status === "duplicate") return `Duplicate of candidate ${candidate.duplicate_of}; result reused`;
    const metrics = [];
    if (candidate.compile_attempts != null) metrics.push(`${candidate.compile_attempts} compile attempt${candidate.compile_attempts === 1 ? "" : "s"}`);
    if (candidate.repair_rounds) metrics.push(`${candidate.repair_rounds} repair round${candidate.repair_rounds === 1 ? "" : "s"}`);
    if (candidate.cache_hit) metrics.push("cache hit");
    if (metrics.length) return metrics.join(" · ");
    if (candidate.compiled === true) return "Lean compilation succeeded";
    if (candidate.error_log?.length) return String(candidate.error_log.slice(-1)[0]).split("\n")[0];
    return candidate.source_stage ? `Finished from ${candidate.source_stage}` : "Candidate completed";
  }
  if (entries.some((entry) => entry?.stage === "candidate_generation_skipped")) {
    return { label: "Not started", className: "failed" };
  }
  const latest = [...entries].reverse().find((entry) => entry?.candidate != null);
  if (!latest) return "Waiting to start";
  return latest.error ? String(latest.error).split("\n")[0] : progressEventLabel(latest);
}

function showCandidateDetails(index) {
  const candidates = state.lastResult?.candidates || [];
  const candidate = candidates[index];
  if (!candidate) return;
  const candTab = document.querySelector('[data-dtab="cands"]');
  candTab?.click();
  $("#modal-details")?.classList.remove("hidden");
  requestAnimationFrame(() => {
    const item = $("#out-candidates")?.querySelector(`[data-candidate-index="${index}"]`);
    item?.scrollIntoView({ block: "nearest" });
  });
}

function renderCandidateOverview({ candidates = [], entries = [], configuredCount = 0, selectedCandidate = 0, live = false }) {
  const section = $("#candidate-overview");
  const grid = $("#candidate-grid");
  const note = $("#candidate-overview-note");
  const total = Math.max(candidates.length, Number(configuredCount) || 0);
  if (!section || !grid || total <= 1) {
    section?.classList.add("hidden");
    return;
  }
  section.classList.remove("hidden");
  const skipped = entries.some((entry) => entry?.stage === "candidate_generation_skipped") ||
    (!live && total > candidates.length && candidates.length === 0);
  if (note) note.textContent = live
    ? (skipped ? `Candidate generation not started; 0 / ${total} generated` : `${total} candidates in progress`)
    : (skipped ? `Candidate generation skipped; 0 / ${total} generated`
      : (selectedCandidate ? `${total} candidates evaluated; final result: candidate ${selectedCandidate}` : `${total} candidates evaluated`));
  grid.innerHTML = "";
  for (let i = 0; i < total; i += 1) {
    const candidate = candidates[i] || null;
    const ownEntries = entries.filter((entry) => Number(entry?.candidate) === i + 1);
    const selected = Number(selectedCandidate) === i + 1;
    const stateInfo = candidateState(candidate, ownEntries, selected);
    const card = document.createElement("button");
    card.type = "button";
    card.className = `candidate-card ${stateInfo.className}`.trim();
    card.setAttribute("aria-label", `Candidate ${i + 1}: ${stateInfo.label}. Open details.`);
    card.innerHTML = "";
    const head = document.createElement("span");
    head.className = "candidate-card-head";
    const name = document.createElement("span");
    name.className = "candidate-card-name";
    name.textContent = selected ? `Candidate ${i + 1} · final result` : `Candidate ${i + 1}`;
    const status = document.createElement("span");
    status.className = "candidate-card-state";
    status.textContent = stateInfo.label;
    head.append(name, status);
    const line = document.createElement("span");
    line.className = "candidate-card-line";
    line.textContent = candidateSummary(candidate, ownEntries);
    card.append(head, line);
    if (!live && candidate) card.addEventListener("click", () => showCandidateDetails(i));
    else card.disabled = true;
    grid.appendChild(card);
  }
}

function renderLiveProgress(job, elapsed = 0, limit = 0) {
  $("#empty-state")?.classList.add("hidden");
  $("#result-view")?.classList.remove("hidden");
  const box = $("#run-progress");
  if (!box) return;
  box.classList.remove("hidden");
  const entries = job.progress || [];
  if ($("#proof-audit")) {
    $("#proof-audit").classList.remove("hidden");
    $("#proof-audit-status").textContent = "Auditing submitted proof…";
    $("#proof-audit-status").textContent = "Waiting for final Lean compilation…";
    $("#proof-audit-summary").textContent = "The API review runs after the selected Lean code has been compiled.";
    $("#proof-audit-steps").innerHTML = "";
  }
  renderLiveLeanFeedback(entries);
  renderTimingSummary(entries);
  renderCandidateOverview({
    entries,
    configuredCount: state.lastInput?.num_candidates || 0,
    live: true,
  });
  renderPreflight(job, true);
  const currentEvent = job.current_event || { stage: job.step || "queued" };
  const title = $("#run-progress-title");
  if (title) title.textContent = progressEventLabel(currentEvent);
  const detail = $("#run-progress-detail");
  if (detail) detail.textContent = `${job.backend || "backend"} · ${job.model || "model"} · ${elapsed}s elapsed${limit ? ` / ${limit}s limit` : ""}`;
  renderPipelinePhases(entries, currentEvent);
  const list = $("#run-progress-steps");
  if (list) {
    list.innerHTML = "";
    const visible = entries.slice(-6);
    if (!visible.length) visible.push(currentEvent);
    visible.forEach((entry) => {
      const item = document.createElement("li");
      const isCurrent = entry.stage === currentEvent.stage &&
        entry.request_stage === currentEvent.request_stage &&
        entry.round === currentEvent.round &&
        entry.attempt === currentEvent.attempt &&
        entry.phase === currentEvent.phase;
      item.className = `${isCurrent ? "active" : ""} ${entry.ok === false ? "failed" : ""}`;
      const label = progressEventLabel(entry);
      item.textContent = entry.error ? `${label}: ${String(entry.error).slice(0, 140)}` : label;
      list.appendChild(item);
    });
  }
  if ($("#out-verdict")) $("#out-verdict").textContent = "Formalization in progress…";
  if ($("#out-lean")) $("#out-lean").textContent = "Lean code will appear after generation and verification.";
}

function renderPreflight(data = {}, live = false) {
  const panel = $("#preflight-panel");
  const summary = $("#preflight-summary");
  const items = $("#preflight-items");
  if (!panel || !summary || !items) return;
  const result = data.result || data;
  const trace = result.stage_trace || data.progress || [];
  const events = trace.filter((entry) => entry?.stage === "import_preflight");
  const latest = events[events.length - 1];
  if (!latest) { panel.classList.add("hidden"); return; }
  panel.classList.remove("hidden");
  summary.textContent = latest.ok === false
    ? (latest.error || "A generated import is not available in the local Mathlib checkout.")
    : (latest.preview || "Imports validated against the configured local Mathlib checkout.");
  items.innerHTML = "";
  (latest.suggestions || []).forEach((value) => {
    const li = document.createElement("li"); li.textContent = value; items.appendChild(li);
  });
}

function clearPreviousResult() {
  // A new run must never inherit badges, theorem text, Lean output, or
  // compiler diagnostics from its predecessor while it is still loading.
  state.lastResult = null;
  $("#status-row") && ($("#status-row").innerHTML = "");
  $("#out-verdict") && ($("#out-verdict").textContent = "Formalization in progress…");
  $("#out-theorem-line") && ($("#out-theorem-line").textContent = "");
  $("#out-lean") && ($("#out-lean").textContent = "Lean code will appear after generation and verification.");
  $("#lean-feedback")?.classList.add("hidden");
  $("#proof-audit")?.classList.add("hidden");
  $("#proof-audit-steps") && ($("#proof-audit-steps").innerHTML = "");
  $("#candidate-overview")?.classList.add("hidden");
  $("#timing-summary")?.classList.add("hidden");
  if ($("#timing-list")) $("#timing-list").innerHTML = "";
  if ($("#timing-total")) $("#timing-total").textContent = "";
  const grid = $("#candidate-grid");
  if (grid) grid.innerHTML = "";
  $("#btn-details")?.setAttribute("disabled", "disabled");
}

async function onRun() {
  const btn = $("#btn-run");
  const spinner = $("#run-spinner");
  const errBox = $("#error-view");
  if (errBox) {
    errBox.classList.add("hidden");
    errBox.textContent = "";
  }
  clearPreviousResult();

  const activeTab = $(".tab.active")?.dataset.tab || "text";
  const text = $("#input-text")?.value.trim() || "";
  let cfg = collectConfig();
  // Keep for the debug report (no API keys)
  state.lastInput = {
    text,
    target_statement: cfg.target_statement || "",
    domain: cfg.domain || "",
    llm_backend: cfg.llm_backend || "",
    num_candidates: cfg.num_candidates,
    max_parallel_candidates: cfg.max_parallel_candidates,
    max_repair_rounds: cfg.max_repair_rounds,
    lean_compile_workers: cfg.lean_compile_workers,
    lean_compile_timeout: cfg.lean_compile_timeout,
    temperature: cfg.temperature,
    job_timeout: cfg.job_timeout,
    llm_timeout: cfg.llm_timeout,
    tab: activeTab,
    pdf: activeTab === "pdf" ? (state.pdfFile?.name || $("#input-pdf")?.files?.[0]?.name || "") : "",
  };

  if (btn) btn.disabled = true;
  spinner?.classList.remove("hidden");
  const label = $(".btn-label");
  if (label) label.textContent = "Formalizing…";

  try {
    await checkBackendConfig();
    cfg = collectConfig();
    saveOptionsFromForm();
    await api("/api/health", {}, 5000);
    if ($("#run-hint")) {
      $("#run-hint").textContent = `Starting ${cfg.llm_backend} · ${activeModel(cfg)}…`;
    }

    let startResp;
    if (activeTab === "pdf" && (state.pdfFile || $("#input-pdf")?.files?.[0])) {
      const file = state.pdfFile || $("#input-pdf").files[0];
      const fd = new FormData();
      fd.append("pdf", file);
      if (text) fd.append("text", text);
      Object.entries(cfg).forEach(([k, v]) => fd.append(k, v ?? ""));
      startResp = await api("/api/formalize/upload", { method: "POST", body: fd }, 60000);
    } else {
      if (!text && !cfg.target_statement) {
        throw new Error("Enter natural-language text and/or an optional formal statement.");
      }
      const payload = { text: text || cfg.target_statement, ...cfg };
      startResp = await api(
        "/api/formalize",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        },
        15000
      );
    }

    if (!startResp.job_id) {
      if (startResp.best_code !== undefined || startResp.success !== undefined) {
        state.lastResult = startResp;
        renderResult(startResp);
        return;
      }
      throw new Error("Server did not return a job_id");
    }

    const data = await pollJob(
      startResp.job_id,
      Number(startResp.job_timeout_seconds || cfg.job_timeout || 0)
    );
    state.lastResult = data;
    renderResult(data);
  } catch (e) {
    console.error("Formalize error:", e);
    if (errBox) {
      errBox.textContent = String(e.message || e);
      errBox.classList.remove("hidden");
    }
    $("#result-view")?.classList.add("hidden");
  } finally {
    if (btn) btn.disabled = false;
    spinner?.classList.add("hidden");
    if (label) label.textContent = "Formalize → Lean 4";
    if ($("#run-hint")) {
      $("#run-hint").textContent = "Statement first, then proof · verify · repair";
    }
    $("#run-progress")?.classList.add("hidden");
    refreshHealth();
  }
}

function addSessionUsage(u) {
  if (!u) return;
  sessionTokens.prompt += u.prompt_tokens || 0;
  sessionTokens.completion += u.completion_tokens || 0;
  sessionTokens.total += u.total_tokens || 0;
  sessionTokens.calls += u.calls || 0;
}

function renderResult(data) {
  $("#run-progress")?.classList.add("hidden");
  $("#empty-state")?.classList.add("hidden");
  $("#result-view")?.classList.remove("hidden");
  // Always show actions after a run so Copy report works even on failure
  $("#result-actions")?.classList.remove("hidden");
  $("#btn-details")?.removeAttribute("disabled");

  const row = $("#status-row");
  if (row) {
    row.innerHTML = "";
    const u = data.token_usage || {};
    addSessionUsage(u);
    const badges = [
      [
        data.timed_out
          ? "Timed out"
          : data.quota_exceeded
            ? "Out of quota"
            : data.success
              ? "Verified"
              : "Failed",
        data.timed_out || data.quota_exceeded || !data.success ? "fail" : "ok",
      ],
      data.domain ? [`${data.domain}`, "info"] : null,
      data.selected_candidate ? [`Final: candidate ${data.selected_candidate}`, "info"] : null,
      u.total_tokens ? [`${u.total_tokens} tok`, "info"] : null,
    ].filter(Boolean);
    badges.forEach(([text, cls]) => {
      const span = document.createElement("span");
      span.className = `badge ${cls}`;
      span.textContent = text;
      row.appendChild(span);
    });
  }

  const ver = data.verification || {};
  renderProofAudit(data.proof_audit);
  renderCandidateOverview({
    candidates: data.candidates || [],
    configuredCount: data.requested_candidates || state.lastInput?.num_candidates || 0,
    selectedCandidate: data.selected_candidate || 0,
    live: false,
  });
  renderTimingSummary(data.stage_trace || []);
  renderPreflight(data, false);
  finalLeanFeedback(data);
  if ($("#out-verdict")) {
    $("#out-verdict").textContent =
      ver.summary || data.message || (data.success ? "Lean formally verified this code." : "Lean did not formally verify any candidate.");
  }
  if ($("#out-lean")) {
    let code = (data.best_code || "").trim();
    if (!code && Array.isArray(data.candidates)) {
      for (const c of data.candidates) {
        if (c && (c.lean_code || "").trim()) {
          code = c.lean_code.trim();
          break;
        }
      }
    }
    $("#out-lean").textContent =
      code ||
      "(no Lean code — model returned empty)\n\n" +
        "Open Details → Stage trace to see which LLM stage failed.\n" +
        "Also check the terminal/CMD where the server runs for [pipeline] lines.\n" +
        "Common causes: API empty/refusal, timeout, or statement stage produced nothing.";
    // Keep a recoverable copy for Copy/Download
    if (code && state.lastResult) state.lastResult.best_code = code;
  }
  const th = (data.structured && data.structured.main_theorem) || "";
  if ($("#out-theorem-line")) {
    $("#out-theorem-line").textContent = th ? "Claim: " + th : "";
  }

  if (data.quota_exceeded || data.timed_out) {
    const errBox = $("#error-view");
    if (errBox) {
      errBox.classList.remove("hidden");
      errBox.textContent = data.message || (data.timed_out ? "Timed out." : "Out of quota.");
    }
  }

  // Details modal content
  fillDetails(data);
}

function fillDetails(data) {
  const ver = data.verification || {};
  const issues = (ver.issues || [])
    .map((i) => `• [${i.severity}/${i.category}] ${i.message}${i.hint ? "\n    " + i.hint : ""}`)
    .join("\n");
  const sugg = (ver.suggestions || []).map((s) => `• ${s}`).join("\n");
  if ($("#out-verification")) {
    $("#out-verification").textContent = [
      ver.summary || "",
      "",
      "Compiler-derived formalization feedback:",
      ver.math_feedback || "(none)",
      "",
      issues ? "Issues:\n" + issues : "Issues: (none)",
      sugg ? "\nSuggestions:\n" + sugg : "",
      ver.raw_message ? "\nRaw checker:\n" + String(ver.raw_message).slice(0, 1500) : "",
    ]
      .filter((x) => x !== "")
      .join("\n");
  }

  const s = data.structured || {};
  if ($("#out-structure")) {
    $("#out-structure").textContent = [
      "Definitions:",
      ...(s.definitions || []).map((d) => "  • " + d),
      "",
      "Lemmas:",
      ...(s.lemmas || []).map((d) => "  • " + d),
      "",
      "Proof sketch:",
      ...(s.proof_sketch || []).map((d, i) => `  ${i + 1}. ${d}`),
    ].join("\n");
  }

  const candBox = $("#out-candidates");
  if (candBox) {
    candBox.innerHTML = "";
    (data.candidates || []).forEach((c, i) => {
      const div = document.createElement("div");
      div.className = "candidate";
      div.dataset.candidateIndex = String(i);
      const title = document.createElement("h4");
      title.textContent = `#${c.candidate_index || i + 1} · compiled=${c.compiled} · ${c.source_stage || "?"}`;
      const pre = document.createElement("pre");
      pre.textContent = c.lean_code || "";
      div.appendChild(title);
      div.appendChild(pre);
      if (c.error_log && c.error_log.length) {
        const err = document.createElement("pre");
        err.style.color = "var(--danger)";
        err.textContent = c.error_log.slice(-2).join("\n---\n");
        div.appendChild(err);
      }
      candBox.appendChild(div);
    });
    if (!(data.candidates || []).length) {
      candBox.innerHTML = "<p class='muted'>(none)</p>";
    }
  }

  if ($("#out-memory")) {
    const hits = data.memory_hits || [];
    $("#out-memory").textContent =
      hits.length === 0
        ? "(no retrieval hits)"
        : hits
            .map((h) => {
              const role =
                h.type === "repair"
                  ? "repair"
                  : h.has_nl_proof
                    ? "example/success"
                    : h.type || "hit";
              return `• [${role}] ${(h.nl || "").slice(0, 100)}`;
            })
            .join("\n");
  }

  if ($("#out-trace")) {
    const tr = data.stage_trace || [];
    if (!tr.length) {
      $("#out-trace").textContent =
        "(no stage trace — restart the web server after updating, then run again)\n\n" +
        "Also check the terminal / CMD window where start_translator is running:\n" +
        "  lines starting with [pipeline] show each LLM stage length + preview.";
    } else {
      $("#out-trace").textContent = tr
        .map((e, i) => {
          const ok = e.ok === false ? "FAIL" : "ok";
          const parts = [
            `${i + 1}. [${ok}] ${e.stage}`,
            e.elapsed_seconds != null ? `at=${formatDuration(e.elapsed_seconds)}` : null,
            e.len != null ? `len=${e.len}` : null,
            e.raw_len != null ? `raw_len=${e.raw_len}` : null,
            e.difficulty ? `difficulty=${e.difficulty}` : null,
            e.domain ? `domain=${e.domain}` : null,
            e.sketch_steps != null ? `sketch_steps=${e.sketch_steps}` : null,
            e.error ? `ERROR: ${e.error}` : null,
            e.preview ? `preview: ${e.preview}` : null,
            e.raw_preview ? `raw_preview: ${e.raw_preview}` : null,
          ].filter(Boolean);
          return parts.join("\n   ");
        })
        .join("\n\n");
    }
  }
}

async function onDoctor() {
  const modal = $("#modal-doctor");
  const body = $("#doctor-body");
  if (body) body.textContent = "Loading…";
  modal?.classList.remove("hidden");
  try {
    const d = await api("/api/doctor", {}, 15000);
    if (body) body.textContent = JSON.stringify(d, null, 2);
  } catch (e) {
    if (body) body.textContent = String(e);
  }
}

async function onMemory() {
  const modal = $("#modal-memory");
  const body = $("#memory-body");
  if (body) body.textContent = "Loading…";
  modal?.classList.remove("hidden");
  try {
    const d = await api("/api/memory/stats", {}, 10000);
    if (body) {
      body.textContent = [
        "Curated examples (few-shot library, not translator output):",
        `  ${d.example_count ?? 0}`,
        "",
        "Translator-verified successes (saved by this tool):",
        `  ${d.translator_success_count ?? d.success_count ?? 0}`,
        "",
        "Repair pairs:",
        `  ${d.repair_count ?? 0}`,
        "",
        JSON.stringify(d, null, 2),
      ].join("\n");
    }
  } catch (e) {
    if (body) body.textContent = String(e);
  }
}

function _resultLeanCode() {
  if (!state.lastResult) return "";
  let code = (state.lastResult.best_code || "").trim();
  if (!code && Array.isArray(state.lastResult.candidates)) {
    for (const c of state.lastResult.candidates) {
      if (c && (c.lean_code || "").trim()) return c.lean_code.trim();
    }
  }
  return code;
}

function onCopy() {
  const code = _resultLeanCode();
  if (!code) return;
  navigator.clipboard.writeText(code).then(() => {
    const btn = $("#btn-copy");
    if (!btn) return;
    const old = btn.textContent;
    btn.textContent = "Copied!";
    setTimeout(() => (btn.textContent = old), 1200);
  });
}

function _flashBtn(sel, label) {
  const btn = $(sel);
  if (!btn) return;
  const old = btn.textContent;
  btn.textContent = label;
  setTimeout(() => (btn.textContent = old), 1500);
}

/**
 * Build a single plain-text report with everything useful for debugging /
 * support (input, result, verification, candidates, stage trace, env).
 * API keys are never included.
 */
function buildDebugReport() {
  const data = state.lastResult || {};
  const inp = state.lastInput || {};
  const health = state.lastHealth || {};
  const ver = data.verification || {};
  const structured = data.structured || {};
  const usage = data.token_usage || {};
  const lines = [];

  lines.push("=== Lean Formalizer debug report ===");
  lines.push(`Generated: ${new Date().toISOString()}`);
  lines.push("");

  lines.push("--- Environment ---");
  lines.push(`lean_available: ${health.lean_available ?? "?"}`);
  lines.push(`lean_project: ${health.lean_project || "(not set)"}`);
  lines.push(`llm_backend (server): ${health.llm_backend || "?"}`);
  lines.push(`llm_backend (this run): ${inp.llm_backend || "?"}`);
  lines.push(`domain: ${data.domain || inp.domain || "?"}`);
  lines.push(`candidates: ${inp.num_candidates ?? "?"}`);
  lines.push(`llm_candidate_workers: ${inp.max_parallel_candidates ?? "?"}`);
  lines.push(`max_repair_rounds: ${inp.max_repair_rounds ?? "?"}`);
  lines.push(`temperature: ${inp.temperature ?? "?"}`);
  lines.push(`job_timeout: ${inp.job_timeout ?? "?"}`);
  lines.push(`llm_timeout: ${inp.llm_timeout ?? "?"}`);
  lines.push(`lean_compile_workers: ${inp.lean_compile_workers ?? "?"}`);
  lines.push(`lean_compile_timeout: ${inp.lean_compile_timeout ?? "?"}`);
  lines.push("");

  lines.push("--- Input ---");
  if (inp.pdf) lines.push(`PDF: ${inp.pdf}`);
  lines.push("Natural language:");
  lines.push(inp.text || "(empty)");
  if (inp.target_statement) {
    lines.push("");
    lines.push("Target statement:");
    lines.push(inp.target_statement);
  }
  lines.push("");

  lines.push("--- Outcome ---");
  lines.push(
    `success=${!!data.success}  timed_out=${!!data.timed_out}  quota_exceeded=${!!data.quota_exceeded}`
  );
  lines.push(`message: ${data.message || ""}`);
  if (usage.total_tokens != null) {
    lines.push(
      `tokens: total=${usage.total_tokens} prompt=${usage.prompt_tokens ?? "?"} completion=${usage.completion_tokens ?? "?"} calls=${usage.calls ?? "?"}`
    );
  }
  lines.push(`repair_count: ${data.repair_count ?? 0}`);
  lines.push(`requested_candidates: ${data.requested_candidates ?? inp.num_candidates ?? "?"}`);
  lines.push(`generated_candidates: ${data.generated_candidates ?? (data.candidates || []).length}`);
  lines.push(`max_repair_rounds: ${data.max_repair_rounds ?? inp.max_repair_rounds ?? "?"}`);
  lines.push(`candidate_repairs: ${data.candidate_repairs ?? data.repair_count ?? 0}`);
  lines.push(`statement_repair_attempts: ${data.statement_repair_attempts ?? 0}`);
  lines.push(`llm_candidate_workers: ${data.llm_candidate_workers ?? inp.max_parallel_candidates ?? "?"}`);
  lines.push(`lean_compile_workers: ${data.lean_compile_workers ?? inp.lean_compile_workers ?? "?"}`);
  lines.push("");

  lines.push("--- Best Lean code ---");
  lines.push(_resultLeanCode() || "(none)");
  lines.push("");

  lines.push("--- Verification ---");
  lines.push(ver.summary || "(no summary)");
  if (ver.math_feedback) {
    lines.push("Compiler-derived formalization feedback:");
    lines.push(ver.math_feedback);
  }
  if (Array.isArray(ver.issues) && ver.issues.length) {
    lines.push("Issues:");
    ver.issues.forEach((i) => {
      lines.push(
        `• [${i.severity || "?"}/${i.category || "?"}] ${i.message || ""}${i.hint ? "\n    " + i.hint : ""}`
      );
    });
  }
  if (Array.isArray(ver.suggestions) && ver.suggestions.length) {
    lines.push("Suggestions:");
    ver.suggestions.forEach((s) => lines.push(`• ${s}`));
  }
  if (ver.raw_message) {
    lines.push("Raw checker:");
    lines.push(String(ver.raw_message).slice(0, 8000));
  }
  lines.push("");

  lines.push("--- Structure ---");
  lines.push(`main_theorem: ${structured.main_theorem || ""}`);
  lines.push(`difficulty: ${structured.difficulty || ""}`);
  if (structured.definitions?.length) {
    lines.push("definitions:");
    structured.definitions.forEach((d) => lines.push(`  • ${d}`));
  }
  if (structured.lemmas?.length) {
    lines.push("lemmas:");
    structured.lemmas.forEach((d) => lines.push(`  • ${d}`));
  }
  if (structured.proof_sketch?.length) {
    lines.push("proof_sketch:");
    structured.proof_sketch.forEach((d, i) => lines.push(`  ${i + 1}. ${d}`));
  }
  lines.push("");

  lines.push("--- Candidates ---");
  const cands = data.candidates || [];
  if (!cands.length) lines.push("(none)");
  cands.forEach((c, i) => {
    lines.push(`#${i} compiled=${c.compiled} stage=${c.source_stage || "?"}`);
    lines.push(c.lean_code || "(empty)");
    if (c.error_log?.length) {
      lines.push("errors:");
      lines.push(c.error_log.slice(-3).join("\n---\n"));
    }
    lines.push("");
  });

  lines.push("--- Memory hits ---");
  const hits = data.memory_hits || [];
  if (!hits.length) lines.push("(none)");
  hits.forEach((h) => {
    lines.push(`• [${h.type || "?"}] ${(h.nl || "").slice(0, 160)}`);
  });
  lines.push("");

  lines.push("--- Stage trace ---");
  const tr = data.stage_trace || [];
  if (!tr.length) lines.push("(none)");
  tr.forEach((s, i) => {
    const ok = s.ok === false ? "FAIL" : "ok";
    lines.push(
      `${i + 1}. [${ok}] ${s.stage || "?"}` +
        (s.elapsed_seconds != null ? `  at=${formatDuration(s.elapsed_seconds)}` : "") +
        (s.len != null ? `  len=${s.len}` : "") +
        (s.raw_len != null ? `  raw_len=${s.raw_len}` : "") +
        (s.difficulty ? `  difficulty=${s.difficulty}` : "") +
        (s.domain ? `  domain=${s.domain}` : "")
    );
    if (s.error) lines.push(`   error: ${String(s.error).slice(0, 500)}`);
    if (s.preview) lines.push(`   preview: ${String(s.preview).slice(0, 400)}`);
    if (s.raw_preview) lines.push(`   raw_preview: ${String(s.raw_preview).slice(0, 400)}`);
  });
  lines.push("");
  lines.push("=== end report ===");

  return lines.join("\n");
}

async function onCopyReport() {
  if (!state.lastResult) {
    alert("Run a formalization first, then copy the report.");
    return;
  }
  // Refresh health so the report has current LEAN_PROJECT / lean status
  try {
    await refreshHealth();
  } catch (_) {}
  const text = buildDebugReport();
  try {
    await navigator.clipboard.writeText(text);
    _flashBtn("#btn-copy-report", "Report copied!");
  } catch (e) {
    // Fallback: show selectable text
    const w = window.open("", "_blank");
    if (w) {
      w.document.write("<pre>" + text.replace(/</g, "&lt;") + "</pre>");
      w.document.close();
    } else {
      prompt("Copy this report:", text.slice(0, 2000));
    }
  }
}

function onDownload() {
  const code = _resultLeanCode();
  if (!code) return;
  const blob = new Blob([code], { type: "text/plain" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "formalized.lean";
  a.click();
  URL.revokeObjectURL(a.href);
}

$("#btn-autoconfig")?.addEventListener("click", async () => {
  const st = $("#autoconfig-status");
  if (st) st.textContent = "Detecting…";
  try {
    const info = await api("/api/autoconfig");
    const key = prompt("Optional API key (Cancel to skip):", "");
    const body = { overwrite: true };
    if (key) body.openai_api_key = key;
    if (info.suggested_env?.OPENAI_BASE_URL) {
      body.openai_base_url = info.suggested_env.OPENAI_BASE_URL;
    }
    if (info.suggested_env?.OPENAI_MODEL) {
      body.openai_model = info.suggested_env.OPENAI_MODEL;
    }
    if (info.lean_project) body.lean_project = info.lean_project;
    const res = await api("/api/autoconfig/apply", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (st) st.textContent = "Saved local_config";
    if (res.env?.LLM_BACKEND && $("#llm-backend")) {
      $("#llm-backend").value = res.env.LLM_BACKEND;
      $("#llm-backend").dispatchEvent(new Event("change"));
    }
    alert("Config written.\n" + (res.notes || []).join("\n"));
    refreshHealth();
  } catch (e) {
    if (st) st.textContent = "Failed: " + e.message;
  }
});
