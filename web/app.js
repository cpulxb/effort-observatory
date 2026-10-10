import { formatTime } from "./format.js";
import { sessionDetail, sessionList } from "./views.js";
import { hasTextSelection, updateMarkup } from "./dom.js";

const element = (id) => document.getElementById(id);
const state = {
  sessions: [],
  selected: decodeURIComponent(location.hash.slice(1)),
  detail: null,
  status: null,
  zone: "Asia/Shanghai",
  refreshing: false,
  paused: false,
  selectionVersion: 0,
  olderLoading: false,
  loadedCount: 0,
  pendingRender: false,
};
const responses = new Map();
const toolContent = new Map();
const evidenceContent = new Map();
const loadingContent = new Set();

async function api(path, method = "GET") {
  const cached = responses.get(path);
  const headers = {};
  if (method === "GET" && cached?.etag) headers["If-None-Match"] = cached.etag;
  const response = await fetch(path, { method, headers, cache: "no-cache" });
  if (response.status === 304 && cached) return cached.body;
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
  if (method === "GET" && response.headers.get("ETag")) {
    // Bound the in-memory cache when browsing many sessions.
    if (responses.size >= 150 && !responses.has(path))
      responses.delete(responses.keys().next().value);
    responses.set(path, { etag: response.headers.get("ETag"), body });
  }
  return body;
}

function showError(message) {
  element("error").textContent = message;
  element("error").hidden = !message;
}

function showToast(message) {
  element("toast").textContent = message;
  element("toast").hidden = false;
  setTimeout(() => {
    element("toast").hidden = true;
  }, 2800);
}

function filteredSessions() {
  const query = element("search").value.trim().toLowerCase();
  const client = element("client-filter").value;
  const resultFilter = element("result-filter").value;
  return state.sessions.filter((session) => {
    if (client && session.client !== client) return false;
    const searchableText =
      `${session.title} ${session.id} ${session.model}`.toLowerCase();
    if (query && !searchableText.includes(query)) return false;
    if (resultFilter === "captured" && !session.evidence) return false;
    if (resultFilter === "difference" && !session.has_difference) return false;
    return true;
  });
}

function renderSessionList() {
  if (hasTextSelection()) {
    state.pendingRender = true;
    return;
  }
  const sessions = filteredSessions();
  element("session-count").textContent = `${sessions.length} 条`;
  updateMarkup(
    element("sessions"),
    sessionList(sessions, state.selected, state.zone),
  );
}

function hydratedDetail() {
  if (!state.detail) return null;
  const hydrateEvidence = (record) => {
    const cached = evidenceContent.get(String(record.id));
    return cached
      ? { ...cached, ...record, raw_content: cached, content_deferred: false }
      : record;
  };
  return {
    ...state.detail,
    turns: state.detail.turns.map((turn) => ({
      ...turn,
      tools: turn.tools.map((tool) => {
        const cacheKey = `${state.detail.id}/${turn.id}/${tool.id}`;
        let cached = toolContent.get(cacheKey);
        if (cached && cached._version !== tool.content_version) {
          toolContent.delete(cacheKey);
          cached = null;
        }
        return cached ? { ...cached, ...tool, content_deferred: false } : tool;
      }),
      evidence: turn.evidence.map(hydrateEvidence),
    })),
    unmatched: (state.detail.unmatched || []).map(hydrateEvidence),
  };
}

function renderSessionDetail(preservePosition = true) {
  if (!state.detail) return;
  if (hasTextSelection()) {
    state.pendingRender = true;
    return;
  }
  updateMarkup(
    element("detail"),
    sessionDetail(hydratedDetail(), state.zone),
    preservePosition,
  );
  element("detail")
    .querySelectorAll(
      "details[open][data-lazy-tool], details[open][data-lazy-evidence]",
    )
    .forEach(loadExpandedContent);
}

function renderStatus() {
  if (!state.status) return;
  const { event_monitor: monitor = {}, instrumentation = {}, evidence_count, time, sources } = state.status;
  const processes = (Array.isArray(instrumentation.clients) ? instrumentation.clients : [])
    .filter((item) => item.active === true);
  const configured = Array.isArray(instrumentation.configured_clients) ? instrumentation.configured_clients : [];
  const running = monitor.running === true;
  const missed = Object.entries(state.status.coverage?.clients || {}).filter(([, item]) => item.capture_state === "unobserved");
  element("monitor-state").textContent = monitor.error ? "事件读取异常" : !running ? "事件监听未运行" : missed.length ? "检测到漏采" : processes.length ? "客户端已连接" : "等待客户端连接";
  const clientStatus = ["Codex App", "Codex CLI"].map((client) => {
    const active = processes.filter((item) => item.client === client);
    if (active.length) return `${client}：已接入 ${active.length} 个进程`;
    if (configured.includes(client)) return `${client}：已配置，${client === "Codex App" ? "等待重启" : "等待新进程"}`;
    return `${client}：未接入事件采集`;
  });
  const parts = [running ? "常驻事件监听" : "事件监听未运行", ...clientStatus];
  for (const [client, item] of missed) parts.push(`${client} ${formatTime(item.last_uncaptured_at, state.zone)} 的请求未采集`);
  if (monitor.last_event) parts.push(`最近事件 ${formatTime(monitor.last_event, state.zone)}`);
  if (monitor.error) parts.push(`读取错误：${monitor.error}`);
  for (const process of processes) {
    if (process.capture_error) parts.push(`${process.client} 采集异常：${process.capture_error}`);
  }
  element("capture-status").textContent = parts.join(" · ");
  element("stat-sessions").textContent = state.sessions.length;
  element("stat-evidence").textContent = evidence_count;
  element("stat-diff").textContent = state.sessions.filter(
    (session) => session.has_difference,
  ).length;
  element("stat-processes").textContent = processes.length;
  const clients = [...new Set(processes.map((process) => process.client))];
  element("process-note").textContent =
    clients.join(" / ") || (configured.length ? "等待已配置客户端启动" : "等待客户端接入");
  element("updated").textContent = `更新于 ${formatTime(time, state.zone)}`;
  if (sources) {
    const entries = Array.isArray(sources)
      ? sources
      : Object.entries(sources).map(([name, item]) => ({ name, ...item }));
    element("source-status").textContent = entries
      .map((source) => {
        const label = source.name || source.source || source.path || "来源";
        const successful = source.last_success || source.last_success_at;
        const description =
          source.error ||
          source.reason ||
          (successful
            ? `最近成功 ${formatTime(successful, state.zone)}`
            : source.status || source.state || "状态未报告");
        return `${label}：${description}`;
      })
      .join(" · ");
  }
}

function detailPath(identifier, offset = 0) {
  return `/api/session?id=${encodeURIComponent(identifier)}&limit=20&offset=${offset}`;
}

function acceptNewestPage(page, retainOlder = true) {
  let old =
    retainOlder && state.detail?.id === page.id ? state.detail.turns : [];
  if (
    old.length &&
    page.turns.length &&
    page.turns[0].position > old.at(-1).position + 1
  )
    old = [];
  const incoming = new Map(page.turns.map((turn) => [turn.id, turn]));
  const merged = old.map((turn) => incoming.get(turn.id) || turn);
  const oldIds = new Set(merged.map((turn) => turn.id));
  for (const turn of page.turns) if (!oldIds.has(turn.id)) merged.push(turn);
  state.loadedCount = merged.length;
  state.detail = {
    ...page,
    turns: merged,
    pagination: page.pagination
      ? { ...page.pagination, has_more: merged.length < page.pagination.total }
      : null,
  };
}

async function selectSession(identifier, userInitiated = false) {
  const version = ++state.selectionVersion;
  const switching =
    state.selected !== identifier || state.detail?.id !== identifier;
  state.selected = identifier;
  history.replaceState(null, "", `#${encodeURIComponent(identifier)}`);
  renderSessionList();
  element("detail").setAttribute("aria-busy", "true");
  try {
    const detail = await api(detailPath(identifier));
    if (version !== state.selectionVersion) return;
    acceptNewestPage(detail, !switching);
    renderSessionDetail(!switching);
    showError("");
    if (userInitiated && matchMedia("(max-width: 740px)").matches) {
      element("session-browser").classList.remove("expanded");
      element("toggle-sessions").setAttribute("aria-expanded", "false");
      element("detail").scrollIntoView({ block: "start" });
    }
  } catch (error) {
    if (version === state.selectionVersion)
      showError(`读取会话失败：${error.message}`);
  } finally {
    if (version === state.selectionVersion)
      element("detail").removeAttribute("aria-busy");
  }
}

async function loadOlder() {
  if (state.olderLoading || !state.detail?.pagination?.has_more) return;
  state.olderLoading = true;
  const identifier = state.selected;
  const version = state.selectionVersion;
  const button = element("detail").querySelector('[data-action="older"]');
  if (button) {
    button.disabled = true;
    button.textContent = "正在读取…";
  }
  try {
    const page = await api(detailPath(identifier, state.loadedCount));
    if (version !== state.selectionVersion || identifier !== state.selected)
      return;
    const present = new Set(state.detail.turns.map((turn) => turn.id));
    state.detail = {
      ...state.detail,
      turns: [
        ...page.turns.filter((turn) => !present.has(turn.id)),
        ...state.detail.turns,
      ],
      pagination: { ...page.pagination },
    };
    state.loadedCount = state.detail.turns.length;
    state.detail.pagination.has_more =
      state.loadedCount < state.detail.pagination.total;
    renderSessionDetail();
  } catch (error) {
    showError(`读取较早轮次失败：${error.message}`);
  } finally {
    state.olderLoading = false;
    if (button?.isConnected) {
      button.disabled = false;
      button.textContent = "加载较早的 20 轮";
    }
  }
}

async function refresh(force = false) {
  if (state.refreshing || state.olderLoading || (!force && state.paused))
    return;
  state.refreshing = true;
  try {
    if (force) await api("/api/refresh", "POST");
    const [catalog, status] = await Promise.all([
      api("/api/sessions"),
      api("/api/status"),
    ]);
    state.sessions = catalog.sessions;
    state.status = status;
    renderSessionList();
    renderStatus();
    const identifier =
      state.selected ||
      state.sessions.find((session) => session.has_difference)?.id ||
      state.sessions[0]?.id;
    if (identifier && !element("detail").hasAttribute("aria-busy"))
      await selectSession(identifier);
    else showError("");
  } catch (error) {
    showError(
      `无法读取本地服务：${error.message}。请检查服务是否运行，或点击刷新重试。`,
    );
  } finally {
    state.refreshing = false;
  }
}

function previewExport() {
  if (!state.detail || state.detail.id !== state.selected) return;
  element("export-scope").value = "evidence";
  element("export-title").textContent = state.detail.title || "当前会话";
  element("export-dialog").dataset.session = state.detail.id;
  updateExportPreview();
  element("export-dialog").showModal();
}

function updateExportPreview() {
  const full = element("export-scope").value === "conversation";
  element("export-preview").textContent = full
    ? "会话与工具记录：当前解析出的所有轮次、消息、工具参数和返回内容，以及关联与未归属证据。包含当前未加载的较早轮次；长文本保留解析器的长度限制。"
    : "仅核验证据：会话标识、轮次与调用关联、客户端档位、请求与响应档位、来源和判定。包含本会话全部证据；不包含消息全文、工具参数及返回内容。";
}

async function exportSession() {
  const identifier = element("export-dialog").dataset.session;
  const scope = element("export-scope").value;
  element("confirm-export").disabled = true;
  try {
    const data = await api(
      `/api/export?id=${encodeURIComponent(identifier)}&scope=${scope}`,
    );
    const blob = new Blob([JSON.stringify(data, null, 2)], {
      type: "application/json",
    });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `effort-${scope}-${identifier}.json`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    element("export-dialog").close();
    showToast(scope === "evidence" ? "已导出核验证据" : "已导出会话与工具记录");
  } catch (error) {
    showError(error.message);
  } finally {
    element("confirm-export").disabled = false;
  }
}

async function loadExpandedContent(details) {
  if (!details.open || !state.detail) return;
  const evidenceId = details.dataset.lazyEvidence;
  const toolId = details.dataset.lazyTool;
  if (!evidenceId && !toolId) return;
  const sessionId = state.detail.id;
  const turnId = details.dataset.turn;
  const cacheKey = evidenceId || `${sessionId}/${turnId}/${toolId}`;
  const cache = evidenceId ? evidenceContent : toolContent;
  if (cache.has(cacheKey) || loadingContent.has(cacheKey)) return;
  const requestedVersion = toolId
    ? state.detail.turns
        .find((turn) => turn.id === turnId)
        ?.tools.find((tool) => tool.id === toolId)?.content_version
    : null;
  loadingContent.add(cacheKey);
  try {
    const path = evidenceId
      ? `/api/evidence/item?id=${encodeURIComponent(evidenceId)}&thread_id=${encodeURIComponent(sessionId)}`
      : `/api/tool?id=${encodeURIComponent(sessionId)}&turn_id=${encodeURIComponent(turnId)}&tool_id=${encodeURIComponent(toolId)}`;
    const data = await api(path);
    const content = data.tool || data.evidence || data;
    if (!evidenceId) content._version = requestedVersion;
    if (cache.size >= 150 && !cache.has(cacheKey))
      cache.delete(cache.keys().next().value);
    cache.set(cacheKey, content);
    if (state.detail?.id === sessionId) renderSessionDetail();
  } catch (error) {
    const placeholder = details.querySelector(".deferred-content");
    if (placeholder)
      placeholder.textContent = `读取失败：${error.message}。收起后重新展开可重试。`;
  } finally {
    loadingContent.delete(cacheKey);
    if (details.isConnected && details.open && !cache.has(cacheKey)) {
      // A newer tool revision may arrive while the previous lazy request is running.
      const fresh = state.detail?.turns
        .find((turn) => turn.id === turnId)
        ?.tools.find((tool) => tool.id === toolId);
      if (fresh && fresh.content_version !== requestedVersion)
        loadExpandedContent(details);
    }
  }
}

function readEvidence(button) {
  const details = button.closest(".evidence");
  element("evidence-reader-title").textContent = details
    .querySelector(".evidence-title")
    .textContent.trim();
  element("evidence-reader-content").textContent =
    details.querySelector("pre")?.textContent ||
    "请先展开证据，等待原始内容读取完成。";
  element("evidence-reader").showModal();
}

function bindEvents() {
  element("search").addEventListener("input", renderSessionList);
  element("client-filter").addEventListener("change", renderSessionList);
  element("result-filter").addEventListener("change", renderSessionList);
  element("sessions").addEventListener("click", (event) => {
    const button = event.target.closest("[data-session]");
    if (button) selectSession(button.dataset.session, true);
  });
  element("detail").addEventListener("click", (event) => {
    const button = event.target.closest("[data-action]");
    if (!button) return;
    if (button.dataset.action === "export") previewExport();
    if (button.dataset.action === "older") loadOlder();
    if (button.dataset.action === "read-evidence") readEvidence(button);
  });
  element("detail").addEventListener(
    "toggle",
    (event) => loadExpandedContent(event.target),
    true,
  );
  element("timezone").addEventListener("change", () => {
    state.zone = element("timezone").value;
    renderSessionList();
    renderSessionDetail();
    renderStatus();
  });
  element("refresh").addEventListener("click", () => refresh(true));
  element("pause-refresh").addEventListener("click", () => {
    state.paused = !state.paused;
    element("pause-refresh").textContent = state.paused
      ? "恢复自动刷新"
      : "暂停自动刷新";
    element("pause-refresh").setAttribute("aria-pressed", String(state.paused));
    if (!state.paused) refresh();
  });
  element("help").addEventListener("click", () => {
    element("help-dialog").showModal();
  });
  for (const [buttonId, panelId] of [
    ["toggle-sessions", "session-browser"],
    ["toggle-overview", "overview"],
  ]) {
    element(buttonId).addEventListener("click", () => {
      const expanded = element(panelId).classList.toggle("expanded");
      element(buttonId).setAttribute("aria-expanded", String(expanded));
    });
  }
  for (const [buttonId, dialogId] of [
    ["close-help", "help-dialog"],
    ["close-export", "export-dialog"],
    ["close-evidence-reader", "evidence-reader"],
  ]) {
    element(buttonId).addEventListener("click", () =>
      element(dialogId).close(),
    );
    element(dialogId).addEventListener("click", (event) => {
      if (event.target === element(dialogId)) element(dialogId).close();
    });
  }
  element("export-scope").addEventListener("change", updateExportPreview);
  element("confirm-export").addEventListener("click", exportSession);
  document.addEventListener("selectionchange", () => {
    if (state.pendingRender && !hasTextSelection()) {
      state.pendingRender = false;
      renderSessionList();
      renderSessionDetail();
    }
  });
  window.addEventListener("hashchange", () => {
    const identifier = decodeURIComponent(location.hash.slice(1));
    if (identifier && identifier !== state.selected)
      selectSession(identifier, true);
  });
}

bindEvents();
refresh();
setInterval(() => {
  if (!document.hidden) refresh();
}, 10000);
