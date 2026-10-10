export const verdictLabels = {
  match: "请求 / 响应一致",
  mismatch: "请求 / 响应不一致",
  runtime_diff: "运行档位 / 响应不同",
  client_mismatch: "客户端与出站档位不同",
  client_request_diff: "客户端与请求档位不同",
  mixed: "本轮存在多个档位",
  unpaired: "尚未配对",
  unknown: "证据待补全",
};

export const sourceLabels = {
  http_relay: "HTTP 精确配对",
  codex_trace: "客户端调用事件",
  codex_event_log: "原始响应事件",
  process_memory: "历史内存 · 完整 JSON",
  process_memory_fragment: "历史内存 · 响应片段",
  archived_memory: "历史取证 · 已导入",
};

export function escapeHtml(value) {
  const entities = {
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  };
  return String(value ?? "").replace(
    /[&<>"']/g,
    (character) => entities[character],
  );
}

export function pretty(value) {
  return typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

export function formatTime(value, zone, short = false) {
  if (!value) return "未记录";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "时间无效";
  const options = {
    timeZone: zone,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
  };
  if (!short) {
    options.month = "2-digit";
    options.day = "2-digit";
  }
  return new Intl.DateTimeFormat("zh-CN", options).format(date);
}

export function hasDifference(verdict) {
  return [
    "mismatch",
    "runtime_diff",
    "client_mismatch",
    "client_request_diff",
    "mixed",
  ].includes(verdict);
}
