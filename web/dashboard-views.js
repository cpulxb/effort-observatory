import { escapeHtml as h, formatTime, pretty, sourceLabels } from "./format.js";

const number = (value) =>
  typeof value === "number" && Number.isFinite(value)
    ? new Intl.NumberFormat("zh-CN").format(value)
    : "—";
const source = (value) => sourceLabels[value] || value || "来源未记录";
const statusLabels = {
  completed: "已完成", in_progress: "进行中", queued: "排队中",
  incomplete: "未完成", failed: "失败", cancelled: "已取消",
};

export function resultLabel(row) {
  if (row.conflict) return "字段冲突";
  if (row.result === "difference") return "回显不同";
  if (row.result === "match") return "回显一致";
  if (["failed", "http_error", "transport_error"].includes(row.response?.status)) return row.auxiliary ? "后台调用失败" : "请求失败";
  if (!row.request?.count && !row.response?.count) return "捕获诊断";
  if (!row.request?.count) return "等待配对";
  if (!row.response?.count) return "等待响应";
  if (row.paired) return "档位待补全";
  return "待核验";
}

function resultClass(row) {
  if (row.conflict) return "conflict";
  if (row.response?.status === "failed" && row.result === "pending") return "failed";
  return row.result || "pending";
}

function sideValue(side, isResponse) {
  if (!side?.count) return "未捕获";
  if (side.state === "conflicting") return (side.efforts || []).join(" / ") || "字段冲突";
  if (side.state === "not_captured") return isResponse ? "回显未采集" : "档位未采集";
  if (side.state === "not_echoed") return "未回显";
  return side.effort || (isResponse ? "未回显" : "未携带");
}

function sideCell(side, isResponse) {
  const hasSide = Boolean(side?.count);
  const model = side?.models?.length ? side.models.join(" / ") : side?.model;
  return `<div class="model-name ${hasSide ? "" : "muted"}" title="${h(model || "")}">${h(model || "—")}</div>
    <div class="side-meta"><span class="${side?.effort || side?.efforts?.length ? `effort-pill ${isResponse ? "response-pill" : ""}` : "empty-value"}">${h(sideValue(side, isResponse))}</span>${hasSide ? '<span class="field-label">reasoning.effort</span>' : ""}</div>`;
}

function timeSource(row) {
  return row.time_source === "event_time" ? "服务端时间" : "捕获时间";
}

function usageMarkup(usage) {
  if (!usage || !Object.keys(usage).length) return '<span class="muted">—</span>';
  const reasoning = usage.output_tokens_details?.reasoning_tokens;
  const cached = usage.input_tokens_details?.cached_tokens;
  return `<span class="mono">${number(usage.input_tokens)} <span class="muted">/</span> ${number(usage.output_tokens)}</span>
    ${typeof reasoning === "number" ? `<small>推理 ${number(reasoning)}</small>` : ""}
    ${typeof cached === "number" ? `<small>缓存 ${number(cached)}</small>` : ""}`;
}

function purposeMarkup(row) {
  return `<span class="purpose-badge ${row.auxiliary ? "auxiliary" : ""}">${h(row.purpose_label || "用途未确认")}</span>`;
}

function failureMarkup(row) {
  const response = row.response || {};
  if (!response.error_message) return "";
  return `<small class="failure-reason">${response.http_status ? `HTTP ${h(response.http_status)} · ` : ""}${h(response.error_message)}</small>`;
}

export function callRows(rows, zone) {
  if (!rows.length) return `<tr><td colspan="7" class="table-empty"><strong>没有符合条件的记录</strong><p>可以切换客户端或清除筛选。确认客户端已接入后，在 Codex 中发送消息，调用记录会自动显示。</p></td></tr>`;
  return rows.map((row) => `<tr class="log-row" data-key="${h(row.id)}" data-anchor>
    <td class="time-cell"><time class="mono" title="${h(row.time || "")}">${h(formatTime(row.time, zone))}</time><small>${timeSource(row)}</small><small>${h((row.sources || []).map(source).join(" · "))}</small></td>
    <td>${sideCell(row.request, false)}</td>
    <td>${sideCell(row.response, true)}</td>
    <td><span class="result-badge ${h(resultClass(row))}">${h(resultLabel(row))}</span>${failureMarkup(row)}<small>${h(statusLabels[row.response?.status] || row.response?.status || (row.paired ? "同次调用" : ""))}${row.attempt_count > 1 ? ` · 尝试 ${row.attempt_index}/${row.attempt_count}` : ""}</small><small>${row.observation_count > 1 ? `${number(row.observation_count)} 次观测已合并` : "1 次观测"}</small></td>
    <td class="usage-cell">${usageMarkup(row.response?.usage)}</td>
    <td>${purposeMarkup(row)}<span class="session-name" title="${h(row.session_title || row.thread_id || "")}">${h(row.session_title || "会话未记录")}</span><small class="mono" title="${h(row.thread_id || "")}">${h(row.thread_id?.slice(0, 8) || "—")}${row.thread_id ? "…" : ""}</small></td>
    <td><button class="row-detail-button" data-call="${h(row.id)}" aria-label="查看 ${h(formatTime(row.time, zone))} 的请求与响应">查看 <span aria-hidden="true">↗</span></button></td>
  </tr>`).join("");
}

function metaItem(label, value) {
  return `<div><dt>${h(label)}</dt><dd>${h(value ?? "未记录")}</dd></div>`;
}

function evidenceMarkup(records, zone) {
  if (!records?.length) return '<p class="note">没有捕获到这一侧的证据。</p>';
  return `<div class="evidence-list">${records.map((record) => `<details class="evidence-item" data-key="evidence-${h(record.id)}">
    <summary><span>${h(record.event_type || statusLabels[record.status] || record.kind || "观测")}</span><span class="muted">${h(formatTime(record.observed_at, zone))}</span></summary>
    <p class="muted">${h(source(record.source))}${record.kind === "response_fragment" ? " · 响应片段" : ""}</p>
    <pre>${h(pretty(record))}</pre>
  </details>`).join("")}</div>`;
}

function sidePanel(side, response, zone) {
  const usage = side?.usage;
  return `<section class="detail-panel ${response ? "response" : "request"}">
    <div class="detail-heading"><h3>${response ? "Response" : "Request"}</h3><span class="muted">${response ? "响应回显" : "请求参数"}</span></div>
    ${sideCell(side, response)}
    <dl class="detail-meta">${metaItem("观测记录", number(side?.count || 0))}${response ? metaItem("响应状态", statusLabels[side?.status] || side?.status || "未记录") + metaItem("观测到的状态", (side?.statuses || []).map((status) => statusLabels[status] || status).join(" / ") || "未记录") : ""}${response && side?.response_ids?.length ? metaItem("Response ID", side.response_ids.join("\n")) : ""}</dl>
    ${response && usage ? `<div class="detail-usage"><span class="muted">输入 / 输出 tokens</span><div class="usage-cell">${usageMarkup(usage)}</div></div>` : ""}
    ${evidenceMarkup(side?.evidence, zone)}
  </section>`;
}

export function callDetail(row, zone) {
  const explanation = row.conflict
    ? "同一记录中存在矛盾字段，请核对下面的观测证据。"
    : row.paired
      ? "Request 与 Response 共享同一调用标识，可直接核对两侧字段。"
      : row.pairing === "response_id"
        ? "同一 Response ID 的生命周期与重复观测已合并；没有可确认配对的 Request。"
        : "这条记录独立保留已捕获的一侧。缺少同次调用标识时，不根据时间或聊天轮次拼接另一侧。";
  return `<div class="detail-summary"><span class="result-badge ${h(resultClass(row))}">${h(resultLabel(row))}</span>${purposeMarkup(row)}<span class="muted">${h(row.client === "unknown" ? "来源待确认" : row.client)} · ${number(row.observation_count)} 次观测</span></div>
    ${row.auxiliary ? `<p class="note">这是${h(row.purpose_label)}调用。它有独立的线程和档位，执行结果不代表主对话是否成功。${row.attempt_count > 1 ? `同一后台轮次记录了 ${row.attempt_count} 次尝试，本条为第 ${row.attempt_index} 次。` : ""}</p>` : ""}
    ${failureMarkup(row)}
    <p class="note">${explanation}</p>
    ${row.conflicts?.length ? `<p class="note warning">${h(row.conflicts.join("；"))}</p>` : ""}
    <dl class="detail-meta detail-overview">
      ${metaItem(timeSource(row), formatTime(row.time, zone))}
      ${metaItem("捕获时间", formatTime(row.observed_at, zone))}
      ${metaItem("会话", row.session_title || row.thread_id)}
      ${metaItem("调用标识", row.capture_id || "未捕获")}
    </dl>
    ${row.thread_id ? `<a class="session-link" href="/sessions#${encodeURIComponent(row.thread_id)}" target="_blank" rel="noopener">查看会话时间线 ↗</a>` : ""}
    <div class="detail-grid">${sidePanel(row.request, false, zone)}${sidePanel(row.response, true, zone)}</div>
    ${row.diagnostics?.length ? `<section class="detail-panel"><h3>捕获诊断</h3>${evidenceMarkup(row.diagnostics, zone)}</section>` : ""}`;
}
