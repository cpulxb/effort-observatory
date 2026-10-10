"""Small allowlists for diagnostic fields retained by local observation.

Untrusted upstream error text is only inspected to classify an error. It is
never returned. Session metadata is similarly reduced to known enum values.
"""

import re


ERROR_MESSAGES = {
    "model_not_supported": "上游不支持请求的模型",
    "model_not_found": "上游未找到请求的模型",
    "rate_limit": "请求受到速率或额度限制",
    "authentication": "上游身份验证或访问权限失败",
    "connection_timeout": "连接或请求超时",
    "unknown": "请求失败，未识别到具体原因",
}
_HTTP = re.compile(r"(?:\bHTTP(?:/\d(?:\.\d)?)?\s*|\bstatus(?:\s+code)?\s*[:=]?\s*)([45]\d{2})(?!\d)", re.I)
_UUID = re.compile(r"^[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")


def safe_failure_fields(*values):
    """Return only a fixed error category/message and an optional HTTP status.

    Accept raw native errors, structured API errors, or already reduced fields.
    Restrict traversed keys so unrelated request bodies cannot become evidence.
    """
    texts, statuses, codes = [], [], []

    def inspect(value, depth=0):
        if depth > 4:
            return
        if isinstance(value, str):
            text = value[:65536].lower()
            texts.append(text)
            match = _HTTP.search(text)
            if match:
                statuses.append(int(match[1]))
        elif isinstance(value, dict):
            for field in ("http_status", "status_code", "status"):
                status = value.get(field)
                if isinstance(status, int) and not isinstance(status, bool) and 400 <= status <= 599:
                    statuses.append(status)
                elif isinstance(status, str) and re.fullmatch(r"[45]\d{2}", status):
                    statuses.append(int(status))
            for field in ("error_code", "code"):
                code = value.get(field)
                if isinstance(code, str) and code in ERROR_MESSAGES:
                    codes.append(code)
            for field in ("error", "code", "type", "message"):
                inspect(value.get(field), depth + 1)

    for value in values:
        inspect(value)
    text = "\n".join(texts)
    code = next((value for value in codes if value != "unknown"), None)
    if code is None:
        if ("model_not_supported" in text or "unsupported_model" in text
                or ("model" in text and ("not supported" in text or "unsupported" in text))
                or ("模型" in text and "不支持" in text)):
            code = "model_not_supported"
        elif ("model_not_found" in text or ("model" in text and
                ("not found" in text or "does not exist" in text))
                or ("模型" in text and "不存在" in text)):
            code = "model_not_found"
        elif any(value in text for value in ("rate_limit", "rate limit", "quota", "too many requests")):
            code = "rate_limit"
        elif any(value in text for value in ("authentication", "unauthorized", "invalid_api_key", "invalid api key", "permission_denied")):
            code = "authentication"
        elif any(value in text for value in ("connection_timeout", "timed out", "timeout")):
            code = "connection_timeout"
        elif statuses and statuses[0] == 429:
            code = "rate_limit"
        elif statuses and statuses[0] in (401, 403):
            code = "authentication"
        elif statuses and statuses[0] in (408, 504):
            code = "connection_timeout"
        else:
            code = "unknown"
    result = {"error_code": code, "error_message": ERROR_MESSAGES[code]}
    if statuses:
        result["http_status"] = statuses[0]
    return result


def safe_session_metadata(value):
    """Keep known native origin enums; omit paths, roles, tools and instructions."""
    if not isinstance(value, dict):
        return {}

    def source(item):
        if isinstance(item, str):
            return item if item in {"cli", "vscode", "exec", "mcp", "unknown"} else None
        if not isinstance(item, dict):
            return None
        internal = item.get("internal")
        if isinstance(internal, str) and internal in {"memory_consolidation", "guardian"}:
            return {"internal": internal}
        subagent = item.get("subagent")
        if isinstance(subagent, str) and subagent in {"review", "compact", "memory_consolidation"}:
            return {"subagent": subagent}
        if isinstance(subagent, dict) and isinstance(subagent.get("thread_spawn"), dict):
            spawn = subagent["thread_spawn"]
            safe = {}
            parent = spawn.get("parent_thread_id")
            if isinstance(parent, str) and _UUID.fullmatch(parent):
                safe["parent_thread_id"] = parent
            depth = spawn.get("depth")
            if isinstance(depth, int) and not isinstance(depth, bool) and 0 <= depth <= 1000:
                safe["depth"] = depth
            return {"subagent": {"thread_spawn": safe}}
        return None

    result = {}
    for field in ("session_source", "source"):
        reduced = source(value.get(field))
        if reduced is not None:
            result[field] = reduced
    # ThreadSource is an open string in the protocol. Keep only known labels.
    if isinstance(value.get("thread_source"), str) and value["thread_source"] in {
            "user", "memory_consolidation", "guardian"}:
        result["thread_source"] = value["thread_source"]
    parent = value.get("parent_thread_id")
    if isinstance(parent, str) and _UUID.fullmatch(parent):
        result["parent_thread_id"] = parent
    return result
