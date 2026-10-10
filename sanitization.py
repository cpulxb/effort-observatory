"""Best-effort redaction at storage and HTTP output boundaries."""

import json
import re

HIDDEN = "[已隐藏]"
SECRET = re.compile(
    r"authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|password|passwd|secret|cookie|^token$",
    re.I,
)
# Handles JSON fragments, shell assignments, CLI options, and URL query fields.
ASSIGNMENT = re.compile(
    r'''(?P<prefix>(?:["']?)(?P<key>[A-Za-z_][\w.-]*)(?:["']?)\s*[:=]\s*)(?P<value>"(?:\\.|[^"\\])*"|'[^']*'|\[已隐藏\]|[^\s&;,}\]"'<>]+)'''
)
OPTION = re.compile(
    r'''(?P<prefix>--(?P<key>[A-Za-z][\w-]*)\s+)(?P<value>"(?:\\.|[^"\\])*"|'[^']*'|[^\s]+)'''
)


def redact(value):
    if isinstance(value, dict):
        return {key: HIDDEN if SECRET.search(str(key)) else redact(item)
                for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if stripped.startswith(("{", "[")):
        try:
            decoded = json.loads(value)
        except (ValueError, RecursionError):
            pass
        else:
            cleaned = redact(decoded)
            if cleaned != decoded:
                return json.dumps(cleaned, ensure_ascii=False)
    value = re.sub(r"(?i)(Bearer\s+)[\w.~+/=\-]+", r"\1[已隐藏]", value)
    value = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}", HIDDEN, value)
    # URL userinfo (including passwords that do not use a named query field).
    value = re.sub(r"(https?://)[^\s/@:]+:[^\s/@]+@", r"\1[已隐藏]@", value)

    def replace(match):
        if not SECRET.search(match['key']):
            return match.group(0)
        token = match['value']
        quote = token[0] if token.startswith(('"', "'")) else ''
        return match['prefix'] + quote + HIDDEN + quote

    value = re.sub(r"(?P<prefix>[?&](?P<key>[A-Za-z_][\w.-]*)=)(?P<value>\[已隐藏\]|[^&\s#]+)", replace, value)
    value = ASSIGNMENT.sub(replace, value)
    return OPTION.sub(replace, value)
