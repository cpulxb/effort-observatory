"""Opt-in HTTP relay with exact request/response correlation."""

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
import uuid

from capture import request_record, response_record
from observatory import now, redact

UPSTREAM_ROUTES = {
    "/relay/v1/responses": "https://anyrouter.top/v1/responses",
}
MAX_REQUEST_BYTES = 32 * 1024 * 1024
MAX_EVENT_BYTES = 4 * 1024 * 1024
LOGGER = logging.getLogger(__name__)


class SSEDecoder:
    """Bound capture memory per event; discard oversized events until a boundary."""

    def __init__(self, receive, diagnostic=None):
        self.receive = receive
        self.diagnostic = diagnostic
        self.buffer = b""
        self.data_lines = []
        self.event_bytes = 0
        self.discarding = False
        self.line_has_content = False
        self.had_errors = False

    def report(self, status, **details):
        self.had_errors = True
        if self.diagnostic:
            try:
                self.diagnostic(status, **details)
            except Exception:
                # Diagnostic sinks are also observational, never transport work.
                pass

    def feed(self, chunk):
        offset = 0
        while offset < len(chunk):
            end = chunk.find(b"\n", offset)
            complete = end >= 0
            end = end if complete else len(chunk)
            fragment = chunk[offset:end]
            offset = end + 1
            self.line_has_content |= bool(fragment.strip(b"\r"))
            if not self.discarding:
                self.event_bytes += len(fragment) + int(complete)
                if self.event_bytes > MAX_EVENT_BYTES:
                    self.buffer = b""
                    self.data_lines.clear()
                    self.event_bytes = 0
                    self.discarding = True
                    self.report("capture_truncated", stage="sse", limit=MAX_EVENT_BYTES)
                else:
                    self.buffer += fragment
            if not complete:
                continue
            empty_line = not self.line_has_content
            self.line_has_content = False
            if self.discarding:
                if empty_line:
                    self.discarding = False
                    self.event_bytes = 0
                continue
            line, self.buffer = self.buffer.rstrip(b"\r"), b""
            if line.startswith(b"data:"):
                data = line[5:]
                self.data_lines.append(data[1:] if data.startswith(b" ") else data)
            elif empty_line:
                self.dispatch()
                self.event_bytes = 0

    def finish(self):
        if self.buffer or self.line_has_content:
            self.feed(b"\n")
        if not self.discarding:
            self.dispatch()

    def dispatch(self):
        if not self.data_lines:
            return
        payload = b"\n".join(self.data_lines)
        self.data_lines.clear()
        if payload.strip() == b"[DONE]":
            return
        try:
            event = json.loads(payload)
        except Exception as error:
            self.report("capture_error", stage="json", error=type(error).__name__)
            return
        if isinstance(event, dict):
            try:
                self.receive(event)
            except Exception as error:
                self.report("capture_error", stage="response", error=type(error).__name__)
        else:
            self.report("capture_error", stage="json", error="ExpectedObject")


class ResponseRecorder:
    def __init__(self, store, request):
        self.store = store
        self.request = request
        self.terminal_seen = False
        self.capture_failed = False
        self.terminal_uncertain = False
        self.diagnostics = []
        self._diagnostic_keys = set()

    def common_fields(self):
        return {
            "source": "http_relay",
            "capture_id": self.request["capture_id"],
            "endpoint": self.request["endpoint"],
        }

    def receive(self, event):
        event_type = event.get("type")
        terminal_status = {
            "response.completed": "completed",
            "response.failed": "failed",
            "response.incomplete": "incomplete",
        }
        if event_type in terminal_status or event_type == "error":
            # Receiving the terminal event is independent of normalizing/saving it.
            self.terminal_seen = True
        try:
            if event_type == "error":
                self.failure("failed", error=redact(event.get("error", event)))
                return
            if event_type != "response.created" and event_type not in terminal_status:
                return
            body = event.get("response", {})
            record = response_record(body, **self.common_fields())
            if event_type in terminal_status:
                record["status"] = terminal_status[event_type]
            for key in ("incomplete_details", "error"):
                if key in body:
                    record[key] = redact(body[key])
            details = body.get("incomplete_details")
            if isinstance(details, dict):
                record["reason"] = details.get("reason")
            record["event_type"] = event_type
            self.save(record)
        except Exception as error:
            self.capture_diagnostic("capture_error", stage="response", error=type(error).__name__)

    def capture_diagnostic(self, status, **details):
        self.capture_failed = True
        if details.get("stage") in ("sse", "json", "response"):
            self.terminal_uncertain = True
        key = (status, details.get("stage"), details.get("error"))
        # Malformed upstream events cannot grow diagnostic memory/storage forever.
        if key in self._diagnostic_keys or len(self.diagnostics) >= 16:
            return
        self._diagnostic_keys.add(key)
        record = {
            "kind": "capture_diagnostic",
            "status": status,
            "observed_at": now(),
            **self.common_fields(),
            **details,
        }
        self.diagnostics.append(record)
        # Log only bounded classification fields; never raw payloads or exceptions.
        try:
            LOGGER.warning(
                "Relay capture %s stage=%s error=%s capture_id=%s",
                status, details.get("stage"), details.get("error"),
                self.request["capture_id"],
            )
        except Exception:
            pass
        self.save(record, diagnostic=True)

    def save(self, record, diagnostic=False):
        record.update(
            thread_id=self.request["thread_id"],
            turn_id=self.request["turn_id"],
        )
        try:
            self.store.add(record)
        except Exception as error:
            if not diagnostic:
                self.capture_diagnostic("capture_error", stage="storage", error=type(error).__name__)

    def failure(self, status, **details):
        self.terminal_seen = True
        self.save(
            {
                "kind": "response",
                "effort": None,
                "status": status,
                "observed_at": now(),
                **self.common_fields(),
                **details,
            }
        )


def read_request(handler):
    parsed = urllib.parse.urlparse(handler.path)
    upstream = UPSTREAM_ROUTES.get(parsed.path)
    if upstream is None:
        handler.respond({"error": "不支持的转发路径"}, 404)
        return None
    authenticated = handler.headers.get("Authorization") or handler.headers.get(
        "x-api-key"
    )
    if not authenticated:
        handler.respond({"error": "转发需要客户端提供自己的鉴权头"}, 401)
        return None

    try:
        size = int(handler.headers.get("Content-Length", "0"))
    except ValueError:
        handler.respond({"error": "无效正文长度"}, 400)
        return None
    if size <= 0 or size > MAX_REQUEST_BYTES:
        handler.respond({"error": "正文限制为 32 MB"}, 413)
        return None

    raw = handler.rfile.read(size)
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeError):
        handler.respond({"error": "需要未压缩的 JSON 请求体"}, 400)
        return None
    if not isinstance(body, dict) or body.get("stream") is not True:
        handler.respond({"error": "此入口用于 stream: true 的 JSON 请求"}, 400)
        return None

    url = upstream + ("?" + parsed.query if parsed.query else "")
    return raw, body, url


def upstream_request(handler, url, raw):
    hop_headers = {
        "host",
        "connection",
        "content-length",
        "accept-encoding",
        "transfer-encoding",
    }
    headers = {
        key: value
        for key, value in handler.headers.items()
        if key.lower() not in hop_headers
    }
    request = urllib.request.Request(url, data=raw, headers=headers, method="POST")
    try:
        return urllib.request.urlopen(request, timeout=180)
    except urllib.error.HTTPError as error:
        return error


def stream_response(handler, response, recorder):
    handler.send_response(response.status)
    handler.send_header(
        "Content-Type", response.headers.get("Content-Type", "application/json")
    )
    handler.send_header("Connection", "close")
    handler.end_headers()
    handler.relay_headers_sent = True
    handler.close_connection = True

    decoder = SSEDecoder(recorder.receive, recorder.capture_diagnostic)
    capture_enabled = True
    while True:
        chunk = response.read1(65536)
        if not chunk:
            break
        handler.wfile.write(chunk)
        handler.wfile.flush()
        if capture_enabled:
            try:
                decoder.feed(chunk)
            except Exception as error:
                capture_enabled = False
                recorder.capture_diagnostic("capture_error", stage="sse", error=type(error).__name__)
    if capture_enabled:
        try:
            decoder.finish()
        except Exception as error:
            recorder.capture_diagnostic("capture_error", stage="sse", error=type(error).__name__)

    if response.status >= 400:
        recorder.failure("http_error", http_status=response.status)
    elif not recorder.terminal_seen:
        if recorder.terminal_uncertain or decoder.had_errors:
            recorder.capture_diagnostic("capture_incomplete", stage="stream", terminal_unknown=True)
        else:
            recorder.failure("incomplete_stream")


def relay_request(handler, store):
    parsed = read_request(handler)
    if parsed is None:
        handler.close_connection = True
        return
    raw, body, url = parsed
    capture_id = str(uuid.uuid4())
    request = {
        "kind": "request", "source": "http_relay", "capture_id": capture_id,
        "endpoint": url.split("?", 1)[0], "thread_id": "http-" + capture_id,
        "turn_id": capture_id, "observed_at": now(),
    }
    recorder = ResponseRecorder(store, request)
    try:
        record = request_record(body, **recorder.common_fields())
        record["thread_id"] = record.get("thread_id") or request["thread_id"]
        record["turn_id"] = record.get("turn_id") or request["turn_id"]
        request.update(record)
        recorder.save(request)
    except Exception as error:
        recorder.capture_diagnostic("capture_error", stage="request", error=type(error).__name__)

    try:
        with upstream_request(handler, url, raw) as response:
            stream_response(handler, response, recorder)
    except (OSError, ValueError, urllib.error.URLError) as error:
        recorder.failure("transport_error", error=type(error).__name__)
        if not getattr(handler, "relay_headers_sent", False):
            handler.respond(
                {"error": "上游连接失败", "type": type(error).__name__}, 502
            )
        handler.close_connection = True
