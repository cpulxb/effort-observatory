import {
  escapeHtml as h,
  formatTime,
  hasDifference,
  pretty,
  sourceLabels,
  verdictLabels,
} from "./format.js";

function verdictBadge(verdict) {
  return `<span class="badge ${h(verdict)}">${h(verdictLabels[verdict] || "待核验")}</span>`;
}

export function sessionList(sessions, selected, zone) {
  if (!sessions.length) return '<p class="muted">没有符合筛选条件的会话。</p>';
  return sessions
    .map((session) => sessionButton(session, selected, zone))
    .join("");
}

function sessionButton(session, selected, zone) {
  const isSelected = session.id === selected;
  const dotClass = session.has_difference ? "diff" : "";
  let recordCount = "点击读取";
  if (session.turns) recordCount = `${session.turns} 轮次`;
  if (session.evidence) recordCount = `${session.evidence} 证据`;
  return `
    <button class="session ${isSelected ? "selected" : ""}"
      data-session="${h(session.id)}" aria-pressed="${isSelected}">
      <span class="session-top">
        <span><i class="tiny-dot ${dotClass}"></i>${h(session.client)}</span>
        <span>${h(session.client_effort || "—")}</span>
      </span>
      <span class="session-title">${h(session.title || "未命名会话")}</span>
      <span class="session-bottom">
        <span>${h(formatTime(session.updated, zone))}</span>
        <span>${recordCount}</span>
      </span>
      <span class="session-verdict">最新轮：${h(verdictLabels[session.latest_verdict || session.verdict] || "证据待补全")}${session.has_difference ? " · 历史有差异" : ""}</span>
    </button>
  `;
}

function evidenceCard(evidence, zone) {
  let title = "RESPONSE";
  if (evidence.kind === "request") title = "REQUEST";
  if (evidence.kind === "response_fragment") title = "RESPONSE · 片段";
  const diagnostic = evidence.kind === "capture_diagnostic";
  if (diagnostic) title = "观测诊断";
  const diagnosticLabels = {
    capture_truncated: "证据截断",
    capture_error: "捕获失败",
    capture_incomplete: "终态未捕获",
  };
  const missingLabel = diagnostic
    ? diagnosticLabels[evidence.status] || evidence.status || "诊断"
    : evidence.kind === "request"
      ? "未携带"
      : "未回显";
  const sourceLabel = sourceLabels[evidence.source] || evidence.source;
  const serverTime = evidence.event_time
    ? `<br>服务端 ${h(formatTime(evidence.event_time, zone))}`
    : "";
  return `
    <details class="evidence" data-key="e-${h(evidence.id)}" ${evidence.content_deferred ? `data-lazy-evidence="${h(evidence.id)}"` : ""}>
      <summary>
        <span class="evidence-title">
          ${title}<span class="evidence-status">${h(evidence.effort || missingLabel)}</span>
        </span>
        <small>
          ${h(sourceLabel)}<br>捕获 ${h(formatTime(evidence.observed_at, zone))}
          ${serverTime}
        </small>
      </summary>
      <div class="evidence-content">
        <p>${h(evidence.association || "等待关联")}<br>${h(evidence.note || "")}</p>
        ${
          evidence.content_deferred
            ? '<p class="deferred-content">展开后读取原始证据…</p>'
            : `<button class="button secondary evidence-reader-button" data-action="read-evidence">全宽阅读</button><pre>${h(pretty(evidence.raw_content || evidence))}</pre>`
        }
      </div>
    </details>
  `;
}

function messageEvent(message, zone) {
  const author = message.role === "user" ? "你 · 用户消息" : "助手 · 回答";
  return `
    <div class="event ${h(message.role)}" data-key="message-${h(message.id || `${message.line}-${message.role}-${message.time}`)}">
      <div class="event-head">
        <span>${author}</span>
        <time title="${h(message.time)}">${h(formatTime(message.time, zone, true))}</time>
      </div>
      <p class="event-text">${h(message.text)}</p>
    </div>
  `;
}

function toolEvent(tool, zone, turnId) {
  const statusLabels = {
    returned: "已返回",
    error: "错误",
    pending: "等待返回",
  };
  const endTime = tool.end_time
    ? ` → ${h(formatTime(tool.end_time, zone, true))}`
    : "";
  return `
    <div class="event tool" data-key="event-tool-${h(tool.id)}">
      <div class="event-head">
        <span>工具调用</span>
        <time>${h(formatTime(tool.start_time, zone, true))}${endTime}</time>
      </div>
      <details data-key="tool-${h(tool.id)}" ${tool.content_deferred ? `data-lazy-tool="${h(tool.id)}" data-turn="${h(turnId)}"` : ""}>
        <summary>
          ${h(tool.name)}
          <span class="tool-state">${h(statusLabels[tool.status] || tool.status)}</span>
        </summary>
        <div class="tool-info">
          <h4>CALL ID</h4><pre>${h(tool.id)}</pre>
          ${
            tool.content_deferred
              ? '<p class="deferred-content">展开后读取调用参数与返回内容…</p>'
              : `<h4>调用参数</h4><pre>${h(pretty(tool.arguments))}</pre>
               <h4>工具返回</h4><pre>${h(pretty(tool.result ?? "尚未发现匹配的返回记录"))}</pre>`
          }
        </div>
      </details>
    </div>
  `;
}

function timeline(turn, zone) {
  const messages = turn.messages.map((message) => ({
    ...message,
    kind: "message",
  }));
  const tools = turn.tools.map((tool) => ({
    ...tool,
    kind: "tool",
    time: tool.start_time,
  }));
  const events = [...messages, ...tools];
  events.sort((first, second) => {
    const lineDifference = (first.line || 0) - (second.line || 0);
    return (
      lineDifference || String(first.time).localeCompare(String(second.time))
    );
  });
  if (!events.length) {
    return '<div class="evidence-empty">没有可关联的消息记录。捕获到的字段仍会保留在右侧证据中。</div>';
  }
  return events
    .map((event) => {
      return event.kind === "message"
        ? messageEvent(event, zone)
        : toolEvent(event, zone, turn.id);
    })
    .join("");
}

function comparisonNote(turn) {
  const explanations = {
    client_mismatch:
      "请求与响应字段一致，但与客户端运行记录不同。请核查客户端档位到出站参数的转换。",
    client_request_diff:
      "已捕获的出站参数与客户端运行记录不同；尚未取得带档位的响应，不能判断服务端最终采用值。",
    runtime_diff:
      "客户端运行记录与响应报告的档位不同；尚无完整请求体，不能据此判断是在哪一层发生转换。",
    mismatch:
      "实际请求与响应报告的档位不同。请展开证据，核对响应 ID、配对方式与时间。",
    mixed:
      "本轮包含多次调用或冲突字段。请查看每次调用的配对结果。不同调用的请求和响应不会交叉拼成结论。",
    unpaired:
      "已关联到本轮，但请求与响应缺少可靠的同次调用标识，尚未配对；不能据此验证请求与响应一致。",
  };
  if (explanations[turn.verdict]) return explanations[turn.verdict];
  if (!turn.response_efforts.length) {
    return "尚无带档位的响应证据。客户端配置、HTTP 成功或思考 token 数都不能替代服务端回显。";
  }
  return "证据按轮次 ID 或响应输出 ID 关联。响应字段是服务端报告值。";
}

function effortCard({ type, label, value, missing, note }) {
  return `
    <div class="effort-card ${type}">
      <div class="label">${label}</div>
      <div class="effort-value ${value ? "" : "missing"}">${h(value || missing)}</div>
      <small>${note}</small>
    </div>
  `;
}

function comparison(turn) {
  const hasRequest = turn.evidence.some(
    (evidence) => evidence.kind === "request",
  );
  const hasResponse = turn.evidence.some(
    (evidence) => evidence.kind === "response",
  );
  const hasFragment = turn.evidence.some(
    (evidence) => evidence.kind === "response_fragment",
  );
  const cards = [
    {
      type: "client",
      label: "01 / CLIENT",
      value: turn.client_effort,
      missing: "未记录",
      note: "客户端运行记录 · 非请求体",
    },
    {
      type: "request",
      label: "02 / REQUEST",
      value: turn.request_efforts.join(" / "),
      missing: hasRequest ? "未携带" : "未捕获",
      note: "真实出站参数 · 多次调用见下方",
    },
    {
      type: "response",
      label: "03 / RESPONSE",
      value: turn.response_efforts.join(" / "),
      missing: hasResponse || hasFragment ? "未回显" : "未捕获",
      note: hasFragment ? "含内存片段 · 见证据来源" : "服务端报告值",
    },
  ];
  return `
    <div class="comparison">${cards.map(effortCard).join("")}</div>
    <p class="comparison-note ${turn.has_difference ? "warning" : ""}">
      ${h(comparisonNote(turn))}
    </p>
  `;
}

function callState(value, status, response = false) {
  if (value) return value;
  const labels = {
    not_captured: "未捕获",
    missing: response ? "未回显" : "未携带",
    not_provided: "未携带",
    not_carried: "未携带",
    not_echoed: "未回显",
    recorded: "已记录",
    captured: response ? "未回显" : "未携带",
    conflicting: "字段冲突",
    conflict: "字段冲突",
  };
  return labels[status] || status || "未捕获";
}

function callComparisons(turn) {
  if (!turn.calls?.length) return "";
  return `<section class="call-comparisons" aria-label="逐次调用核验">
    <div class="section-label">逐次调用核验 <span>${turn.calls.length} CALLS</span></div>
    <p class="call-help">仅比较同一调用内的字段。尚未配对的记录会单独展示。</p>
    ${turn.calls
      .map(
        (
          call,
          index,
        ) => `<div class="call-row" data-key="call-${h(call.id || call.capture_id || call.evidence_ids?.join("-") || index)}">
      <div class="call-heading"><strong>${call.paired ? `调用 ${index + 1}` : `调用 ${index + 1} · 尚未配对`}</strong>${verdictBadge(call.verdict)}</div>
      <div class="call-fields"><span>请求 <b>${h(callState(call.request_effort, call.request_state))}</b></span>
      <span>响应 <b>${h(callState(call.response_effort, call.response_state, true))}</b></span></div>
      <small>${call.capture_id ? `配对 ID：${h(call.capture_id)}` : "缺少同次调用标识，仅保留独立证据"} · 证据 ${h((call.evidence_ids || []).join("、") || "—")}</small>
    </div>`,
      )
      .join("")}
  </section>`;
}

function httpLogs(requests, zone) {
  if (!requests?.length) return "";
  const rows = requests.map(
    (request) => `
    <p>
      <span>${h(formatTime(request.time, zone, true))}</span>
      <span>HTTP ${h(request.status || "—")} · ${h(request.runtime_effort || "—")}</span>
    </p>
  `,
  );
  return `
    <div class="http-log">
      <div class="section-label">HTTP 运行日志 · 非请求正文</div>
      ${rows.join("")}
    </div>
  `;
}

function evidencePanel(turn, zone) {
  const cards = turn.evidence.length
    ? turn.evidence.map((evidence) => evidenceCard(evidence, zone)).join("")
    : `<div class="evidence-empty">
         这一轮还没有捕获到原始参数。<br>
         已接入客户端的新调用会自动记录，可返回首页查看。历史记录不会补造缺失参数。
       </div>`;
  return `
    <aside class="evidence-panel">
      <div class="section-label">对应证据 <span>${turn.evidence.length} RECORDS</span></div>
      ${cards}
      ${httpLogs(turn.http_requests, zone)}
    </aside>
  `;
}

function turnView(turn, index, zone) {
  return `
    <article class="turn" data-key="turn-${h(turn.id)}" data-anchor>
      <div class="turn-heading">
        <div class="turn-title">
          <span class="turn-no">TURN ${String(index + 1).padStart(2, "0")}</span>
          ${verdictBadge(turn.verdict)}
        </div>
        <time>${h(formatTime(turn.time, zone))} · ${h(turn.model || "模型未记录")}</time>
      </div>
      ${comparison(turn)}
      ${callComparisons(turn)}
      <div class="turn-body">
        <div class="timeline">
          <div class="section-label">
            消息与工具时间线
            <span>${turn.messages.length} MESSAGES / ${turn.tools.length} TOOLS</span>
          </div>
          ${timeline(turn, zone)}
        </div>
        ${evidencePanel(turn, zone)}
      </div>
    </article>
  `;
}

function unmatchedEvidence(records, zone) {
  if (!records?.length) return "";
  return `
    <div class="unmatched" data-key="unmatched">
      <h3>消息关联待确认 · ${records.length}</h3>
      <p>历史内存证据可能只有会话 ID，缺少可靠消息标识。标识不足或存在冲突时保留证据与来源，不猜测消息归属；这里的档位不会参与上面的差异判断。新的逐调用配对请在首页查看。</p>
      ${records.map((record) => evidenceCard(record, zone)).join("")}
    </div>
  `;
}

export function sessionDetail(session, zone) {
  const total = session.pagination?.total ?? session.turns.length;
  const firstIndex = Math.max(0, total - session.turns.length);
  const turns = session.turns.length
    ? session.turns
        .map((turn, index) =>
          turnView(turn, (turn.position || firstIndex + index + 1) - 1, zone),
        )
        .reverse()
        .join("")
    : `<div class="empty"><h2>尚无消息记录</h2><p>仅能识别会话标识，不能把请求猜测匹配到某条消息。</p></div>`;
  return `
    <div class="session-header" data-key="session-header-${h(session.id)}">
      <div>
        <span class="tag">${h(session.client || "客户端")}</span>
        <h2>${h(session.title || "捕获会话")}</h2>
        <div class="session-meta">
          <span>${h(session.id)}</span>
          <span>${total} 轮次${total > session.turns.length ? ` · 已加载最近 ${session.turns.length} 轮` : ""}${session.available ? "" : " · 原始日志不可用"}</span>
        </div>
      </div>
      <div class="session-actions">
        <button class="button secondary" data-action="export">↓ 导出证据</button>
      </div>
    </div>
    ${turns}
    ${session.pagination?.has_more ? '<div class="pagination" data-key="pagination"><button class="button secondary" data-action="older">加载较早的 20 轮</button></div>' : ""}
    ${unmatchedEvidence(session.unmatched, zone)}
  `;
}
