import { formatTime } from "./format.js";
import { callRows, callDetail } from "./dashboard-views.js";
import { hasTextSelection, updateMarkup } from "./dom.js";

const el = (id) => document.getElementById(id);
const CLIENT_HASHES = { "Codex App": "#codex-app", "Codex CLI": "#codex-cli", unknown: "#unknown" };
const clientFromHash = () => Object.keys(CLIENT_HASHES).find((client) => CLIENT_HASHES[client] === location.hash) || "Codex App";
const state = {
  client: clientFromHash(), zone: "Asia/Shanghai", offset: 0, limit: 25,
  data: null, status: null, paused: false, version: 0, pendingRender: false,
  loading: false, detail: null, detailVersion: 0, detailId: null,
};
const cache = new Map();
let listController;
let searchTimer;
let detailContainer;

async function api(path, options = {}) {
  const method = options.method || "GET";
  const cached = cache.get(path);
  const headers = {};
  if (method === "GET" && cached?.etag) headers["If-None-Match"] = cached.etag;
  const response = await fetch(path, { ...options, method, headers, cache: "no-cache" });
  if (response.status === 304 && cached) return cached.body;
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
  if (method === "GET" && response.headers.get("ETag")) {
    if (cache.size >= 100 && !cache.has(path)) cache.delete(cache.keys().next().value);
    cache.set(path, { etag: response.headers.get("ETag"), body });
  }
  return body;
}

function showError(message = "") {
  el("error").textContent = message;
  el("error").hidden = !message;
}

function renderStatus() {
  if (!state.status) return;
  const { event_monitor: monitor = {}, instrumentation = {}, coverage = {}, time, sources } = state.status;
  const running = monitor.running === true;
  const processes = Array.isArray(instrumentation.clients) ? instrumentation.clients : [];
  const activeProcesses = processes.filter((item) => item.active === true);
  const missed = Object.entries(coverage.clients || {}).filter(([, item]) => item.capture_state === "unobserved");
  const selectedMissed = missed.find(([client]) => client === state.client);
  const selectedCoverage = coverage.clients?.[state.client];
  const historicalMiss = selectedCoverage?.capture_state === "observed" && selectedCoverage.last_uncaptured_at;
  el("monitor-state").textContent = monitor.error ? "事件读取异常" : !running ? "事件监听未运行" : selectedMissed ? "检测到漏采" : activeProcesses.length ? "客户端已连接" : "等待客户端连接";
  el("capture-status").classList.toggle("active", running && !monitor.error && activeProcesses.length > 0 && !selectedMissed);
  el("capture-status").classList.toggle("warning", Boolean(selectedMissed));
  el("coverage-warning").hidden = !selectedMissed && !historicalMiss;
  if (selectedMissed) {
    const [client, item] = selectedMissed;
    el("coverage-warning").textContent = `${client} 在 ${formatTime(item.last_uncaptured_at || item.last_activity_at, state.zone)} 有实际请求，但没有采集到对应 Request。${client === "Codex CLI" ? "请退出仍在运行的旧 CLI 后重新运行 codex；已修复原命令入口。" : "当前 App 后端尚未加载采集配置，需要完整退出后重新打开。"}缺失记录不会用客户端设置补齐。`;
  } else if (historicalMiss) {
    el("coverage-warning").textContent = `历史漏采：${formatTime(selectedCoverage.last_uncaptured_at, state.zone)} 的请求未保存原始 Request / Response。${formatTime(selectedCoverage.last_activity_at, state.zone)} 的新调用已采集；旧记录无法事后补齐。`;
  }
  const configured = Array.isArray(instrumentation.configured_clients) ? instrumentation.configured_clients : [];
  const clientStatus = ["Codex App", "Codex CLI"].map((client) => {
    const active = processes.filter((item) => item.client === client && item.active === true);
    if (active.length) return `${client}：已接入 ${active.length} 个进程`;
    if (configured.includes(client)) return `${client}：已配置，${client === "Codex App" ? "等待重启" : "等待新进程"}`;
    return `${client}：未接入事件采集`;
  });
  const parts = [running ? "常驻事件监听" : "事件监听未运行", ...clientStatus];
  if (monitor.last_event) parts.push(`最近事件 ${formatTime(monitor.last_event, state.zone)}`);
  el("capture-status").textContent = parts.join(" · ");
  if (monitor.error) el("capture-status").textContent += ` · 读取错误：${monitor.error}`;
  for (const process of processes) {
    if (process.active && process.capture_error) el("capture-status").textContent += ` · ${process.client} 采集异常：${process.capture_error}`;
  }
  el("updated").textContent = `更新于 ${formatTime(time, state.zone)}`;
  const sourceList = Array.isArray(sources) ? sources : Object.values(sources || {});
  const ready = sourceList.filter((item) => item.state === "ready").length;
  el("source-status").textContent = `本地数据 · ${ready} 个来源可用`;
}

function renderTabs() {
  document.querySelectorAll("[data-client]").forEach((button) => {
    const selected = button.dataset.client === state.client;
    button.classList.toggle("active", selected);
    button.setAttribute("aria-pressed", String(selected));
    button.tabIndex = selected ? 0 : -1;
  });
}

function render() {
  renderStatus();
  if (!state.data) return;
  if (hasTextSelection()) {
    state.pendingRender = true;
    return;
  }
  const data = state.data;
  for (const [id, key] of [["stat-total", "total"], ["stat-match", "match"], ["stat-difference", "difference"], ["stat-pending", "pending"]]) {
    el(id).textContent = new Intl.NumberFormat("zh-CN").format(data.stats[key] || 0);
  }
  renderTabs();
  document.querySelectorAll("[data-client-count]").forEach((counter) => {
    counter.textContent = new Intl.NumberFormat("zh-CN").format(data.client_counts[counter.dataset.clientCount] || 0);
  });
  el("unknown-tab").hidden = !data.client_counts.unknown && state.client !== "unknown";
  const sessionSelect = el("session-filter");
  const selectedSession = sessionSelect.value;
  const sessions = (data.sessions || []).filter((session) => session.client === state.client || session.id === selectedSession);
  const sessionSignature = JSON.stringify(sessions);
  if (sessionSelect.dataset.signature !== sessionSignature) {
    const options = [new Option("全部会话", ""), ...sessions.map((session) => new Option(session.title || session.id, session.id))];
    if (selectedSession && !sessions.some((session) => session.id === selectedSession)) options.push(new Option(selectedSession, selectedSession));
    sessionSelect.replaceChildren(...options);
    sessionSelect.value = selectedSession;
    sessionSelect.dataset.signature = sessionSignature;
  }
  updateMarkup(el("call-rows"), callRows(data.items, state.zone));
  const total = data.total;
  el("result-count").textContent = total
    ? `第 ${state.offset + 1}–${state.offset + data.items.length} 条，共 ${total} 条记录`
    : "共 0 条记录";
  el("page-label").textContent = `${Math.floor(state.offset / state.limit) + 1} / ${Math.max(1, Math.ceil(total / state.limit))}`;
  el("prev-page").disabled = !state.offset;
  el("next-page").disabled = !data.pagination.has_more;
  renderDetail();
}

function renderDetail() {
  if (!state.detail || !detailContainer || !el("call-dialog").open) return;
  if (hasTextSelection()) { state.pendingRender = true; return; }
  updateMarkup(detailContainer, callDetail(state.detail, state.zone), false);
}

async function refreshDetail() {
  if (!state.detailId || !el("call-dialog").open) return;
  const id = state.detailId;
  const version = state.detailVersion;
  const row = await api(`/api/call?id=${encodeURIComponent(id)}`);
  if (version !== state.detailVersion || !el("call-dialog").open) return;
  state.detail = row;
  renderDetail();
}

function queryPath() {
  return "/api/calls?" + new URLSearchParams({
    client: state.client, q: el("search").value.trim(), result: el("result-filter").value,
    thread_id: el("session-filter").value, purpose: el("purpose-filter").value, limit: state.limit, offset: state.offset,
  });
}

async function refresh({ force = false, replace = false } = {}) {
  if (state.loading && !replace) return;
  if (state.paused && !force) return;
  if (replace) listController?.abort();
  const controller = new AbortController();
  listController = controller;
  const version = ++state.version;
  state.loading = true;
  el("call-rows").setAttribute("aria-busy", "true");
  try {
    const [data, status] = await Promise.all([
      api(queryPath(), { signal: controller.signal }),
      api("/api/status", { signal: controller.signal }),
    ]);
    if (version !== state.version) return;
    state.data = data;
    state.status = status;
    if (state.offset && state.offset >= data.total) {
      state.offset = Math.max(0, Math.floor((data.total - 1) / state.limit) * state.limit);
      state.loading = false;
      return refresh({ force: true, replace: true });
    }
    render();
    showError();
    await refreshDetail();
  } catch (error) {
    if (error.name !== "AbortError" && version === state.version) showError(`读取失败：${error.message}。点击刷新可重试。`);
  } finally {
    if (version === state.version) {
      state.loading = false;
      el("call-rows").removeAttribute("aria-busy");
    }
  }
}

function filterChanged() {
  state.offset = 0;
  refresh({ force: true, replace: true });
}

async function openCall(id) {
  const version = ++state.detailVersion;
  state.detail = null;
  state.detailId = id;
  el("call-title").textContent = "Request / Response";
  detailContainer = document.createElement("div");
  detailContainer.textContent = "正在读取记录…";
  el("call-detail").replaceChildren(detailContainer);
  if (!el("call-dialog").open) el("call-dialog").showModal();
  try {
    const row = await api(`/api/call?id=${encodeURIComponent(id)}`);
    if (version !== state.detailVersion || !el("call-dialog").open) return;
    state.detail = row;
    renderDetail();
  } catch (error) {
    if (version === state.detailVersion) detailContainer.textContent = `读取失败：${error.message}。关闭后重新打开可重试。`;
  }
}

el("client-tabs").addEventListener("click", (event) => {
  const button = event.target.closest("[data-client]");
  if (!button) return;
  location.hash = CLIENT_HASHES[button.dataset.client];
});
window.addEventListener("hashchange", () => {
  const client = clientFromHash();
  if (client === state.client) return;
  state.client = client;
  el("session-filter").value = "";
  filterChanged();
});
el("client-tabs").addEventListener("keydown", (event) => {
  if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
  const buttons = [...el("client-tabs").querySelectorAll("[data-client]")].filter((button) => !button.hidden);
  const current = buttons.indexOf(document.activeElement);
  if (current < 0) return;
  event.preventDefault();
  const next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1 : (current + (event.key === "ArrowRight" ? 1 : -1) + buttons.length) % buttons.length;
  buttons[next].focus();
  buttons[next].click();
});
el("search").addEventListener("input", () => {
  clearTimeout(searchTimer);
  // Invalidate the previous query immediately while waiting for typing to settle.
  listController?.abort();
  state.version++;
  state.loading = false;
  searchTimer = setTimeout(filterChanged, 200);
});
el("result-filter").addEventListener("change", filterChanged);
el("purpose-filter").addEventListener("change", filterChanged);
el("session-filter").addEventListener("change", filterChanged);
el("timezone").addEventListener("change", () => {
  state.zone = el("timezone").value;
  render();
});
el("call-rows").addEventListener("click", (event) => {
  const button = event.target.closest("[data-call]");
  if (button) openCall(button.dataset.call);
});
el("prev-page").addEventListener("click", () => {
  state.offset = Math.max(0, state.offset - state.limit);
  refresh({ force: true, replace: true });
});
el("next-page").addEventListener("click", () => {
  state.offset += state.limit;
  refresh({ force: true, replace: true });
});
el("refresh").addEventListener("click", async () => {
  el("refresh").disabled = true;
  try {
    await api("/api/refresh", { method: "POST" });
    await refresh({ force: true, replace: true });
  } catch (error) { showError(`刷新失败：${error.message}`); }
  finally { el("refresh").disabled = false; }
});
el("pause-refresh").addEventListener("click", () => {
  state.paused = !state.paused;
  el("pause-refresh").textContent = state.paused ? "恢复自动刷新" : "暂停自动刷新";
  el("pause-refresh").setAttribute("aria-pressed", String(state.paused));
  if (!state.paused) refresh();
});
el("help").addEventListener("click", () => {
  el("help-dialog").showModal();
});
for (const [button, dialog] of [["close-call", "call-dialog"], ["close-help", "help-dialog"]]) {
  el(button).addEventListener("click", () => el(dialog).close());
  el(dialog).addEventListener("click", (event) => {
    if (event.target !== el(dialog)) return;
    const rect = el(dialog).getBoundingClientRect();
    if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) el(dialog).close();
  });
}
el("call-dialog").addEventListener("close", () => { state.detailVersion++; state.detail = null; state.detailId = null; });
document.addEventListener("selectionchange", () => {
  if (state.pendingRender && !hasTextSelection()) { state.pendingRender = false; render(); }
});
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
renderTabs();
refresh();
setInterval(() => { if (!document.hidden) refresh(); }, 2000);
