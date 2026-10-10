"""Consume client-written event logs; never inspect process memory or credentials.

Only the dedicated root/{app,cli}/{run_id}/ tree is read. Native trace payloads
are reduced to model/effort/identity/usage before entering the evidence store.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import threading

from observation_fields import safe_failure_fields


CLIENTS = {"app": "Codex App", "cli": "Codex CLI"}
MAX_BYTES = 64 * 1024 * 1024
RAW_TYPES = {"response.created", "response.in_progress", "response.completed",
             "response.failed", "response.incomplete", "response.cancelled"}
NATIVE_TYPES = {"inference_started", "inference_completed", "inference_failed",
                "inference_cancelled"}


def _string(value):
    return value if isinstance(value, str) and value else None


def _timestamp(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return dt.datetime.fromtimestamp(value / 1000 if value > 1e12 else value,
                                            dt.timezone.utc).isoformat()
        except (ValueError, OverflowError, OSError):
            return None
    return _string(value)


def _usage(value, native=False):
    if not isinstance(value, dict):
        return None
    def number(v):
        return isinstance(v, (float, int)) and not isinstance(v, bool)
    result = {k: value[k] for k in ("input_tokens", "output_tokens", "total_tokens")
              if number(value.get(k))}
    for nested, field, native_field in (
            ("input_tokens_details", "cached_tokens", "cached_input_tokens"),
            ("output_tokens_details", "reasoning_tokens", "reasoning_output_tokens")):
        source = value.get(nested)
        count = source.get(field) if isinstance(source, dict) else None
        if native and number(value.get(native_field)):
            count = value[native_field]
        if number(count):
            result[nested] = {field: count}
    return result or None


class CaptureReadError(Exception):
    """An incomplete/missing payload may be retried without skipping its event."""

    def __init__(self, message, *, retryable=False):
        super().__init__(message)
        self.retryable = retryable


class _Tail:
    def __init__(self, path):
        self.path = path
        self.stream = path.open("rb")
        stat = os.fstat(self.stream.fileno())
        self.identity = (stat.st_dev, stat.st_ino)
        self.last_size, self.last_mtime_ns = stat.st_size, stat.st_mtime_ns
        self.offset = 0
        self.anchor = b""

    def close(self):
        self.stream.close()


class EventCollector:
    def __init__(self, store, root, *, max_bytes=MAX_BYTES):
        self.store = store
        self.root = Path(root).resolve()
        self.max_bytes = max_bytes
        self.tails = {}
        self.problems = {}
        self.status = dict(running=False, root=str(self.root), clients={}, last_event=None,
                           error=None, files=0, events=0, saved=0, errors=0, rotations=0)
        self.store.event_status = self.status

    def _problem(self, key, message):
        # Never include JSON contents, model error text, or sensitive payloads.
        if self.problems.get(key) != message:
            self.status["errors"] += 1
        self.problems[key] = message
        self.status["error"] = message

    def _clear_problem(self, key):
        self.problems.pop(key, None)
        self.status["error"] = next(reversed(self.problems.values()), None)

    def _location(self, path):
        resolved = path.resolve()
        if not resolved.is_relative_to(self.root):
            raise CaptureReadError("event log path leaves the dedicated capture root")
        parts = resolved.relative_to(self.root).parts
        if len(parts) < 3 or parts[0] not in CLIENTS:
            raise CaptureReadError("event log must be under app/run_id or cli/run_id")
        return parts[0], parts[1]

    def _payload(self, bundle, reference):
        if not isinstance(reference, dict) or not _string(reference.get("path")):
            raise CaptureReadError("native event has no valid payload reference")
        relative = Path(reference["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise CaptureReadError("native payload path traversal rejected")
        candidate = (bundle / relative).resolve()
        if not candidate.is_relative_to(bundle.resolve()):
            raise CaptureReadError("native payload symlink leaves its bundle")
        try:
            with candidate.open("rb") as stream:
                if os.fstat(stream.fileno()).st_size > self.max_bytes:
                    raise CaptureReadError(f"native payload exceeds {self.max_bytes} byte limit", retryable=True)
                content = stream.read(self.max_bytes + 1)
            if len(content) > self.max_bytes:
                raise CaptureReadError(f"native payload exceeds {self.max_bytes} byte limit", retryable=True)
            value = json.loads(content)
        except (OSError, ValueError, RecursionError) as exc:
            raise CaptureReadError("native payload is missing, incomplete, or invalid", retryable=True) from exc
        if not isinstance(value, dict):
            raise CaptureReadError("native payload is not an object")
        return value

    def _native(self, event, path, client_key, run_id):
        payload = event.get("payload")
        if not isinstance(payload, dict) or payload.get("type") not in NATIVE_TYPES:
            return None
        kind = payload["type"]
        inference_id = _string(payload.get("inference_call_id"))
        if not inference_id:
            raise CaptureReadError("native inference event is missing inference_call_id")
        stamp = _timestamp(event.get("wall_time_unix_ms"))
        record = dict(source="codex_trace", client=CLIENTS[client_key], run_id=run_id,
                      capture_id=f"trace:{client_key}:{run_id}:{inference_id}",
                      inference_call_id=inference_id,
                      thread_id=_string(payload.get("thread_id")) or _string(event.get("thread_id")),
                      turn_id=_string(payload.get("codex_turn_id")) or _string(event.get("codex_turn_id")),
                      event_type=kind, event_time=stamp,
                      event_id=f"trace:{client_key}:{run_id}:{event.get('rollout_id')}:{event.get('seq')}")
        if stamp:
            record["observed_at"] = stamp
        if kind == "inference_started":
            body = self._payload(path.parent, payload.get("request_payload"))
            reasoning = body.get("reasoning")
            effort = _string(reasoning.get("effort")) if isinstance(reasoning, dict) else None
            record.update(kind="request", model=_string(body.get("model")), effort=effort,
                          effort_state="recorded" if effort else "not_carried",
                          effort_source="request.reasoning.effort", status="sent")
        else:
            reference = payload.get("response_payload") or payload.get("partial_response_payload")
            body = self._payload(path.parent, reference) if reference else {}
            response_id = _string(payload.get("response_id")) or _string(body.get("response_id"))
            record.update(kind="response", response_id=response_id,
                          upstream_request_id=_string(payload.get("upstream_request_id")) or _string(body.get("upstream_request_id")),
                          status={"inference_completed": "completed", "inference_failed": "failed",
                                  "inference_cancelled": "cancelled"}[kind],
                          status_source="client_lifecycle",
                          effort=None, effort_state="not_captured",
                          effort_source="unavailable_in_native_trace",
                          usage=_usage(body.get("token_usage"), native=True))
            if kind == "inference_failed":
                record.update(safe_failure_fields(payload, body))
        return record

    def _raw(self, wrapper, client_key, run_id):
        if wrapper.get("client") != CLIENTS[client_key] or wrapper.get("run_id") != run_id:
            raise CaptureReadError("raw event client/run_id disagrees with its capture directory")
        event = wrapper.get("event")
        if not isinstance(event, dict) or event.get("type") not in RAW_TYPES:
            return None
        body = event.get("response")
        if not isinstance(body, dict) or not _string(body.get("id")):
            raise CaptureReadError("raw response event is missing an explicit response.id")
        reasoning = body.get("reasoning")
        effort = _string(reasoning.get("effort")) if isinstance(reasoning, dict) else None
        stamp = _timestamp(wrapper.get("timestamp"))
        kind = event["type"]
        record = dict(kind="response", source="codex_event_log", client=CLIENTS[client_key],
                      run_id=run_id, response_id=body["id"], model=_string(body.get("model")),
                      effort=effort, effort_state="recorded" if effort else "not_echoed",
                      effort_source="response.reasoning.effort", event_type=kind,
                      status_source="response.status",
                      event_time=stamp, status=_string(body.get("status")) or {
                          "response.created": "in_progress", "response.in_progress": "in_progress",
                          "response.completed": "completed", "response.failed": "failed",
                          "response.incomplete": "incomplete", "response.cancelled": "cancelled"}[kind],
                      usage=_usage(body.get("usage")))
        if kind == "response.failed" or record["status"] == "failed":
            record.update(safe_failure_fields(body))
        if wrapper.get("transport") in ("sse", "websocket"):
            record["transport"] = wrapper["transport"]
        if stamp:
            record["observed_at"] = stamp
        return record

    def _consume(self, tail, client_key, run_id):
        stream = tail.stream
        stream.seek(tail.offset)
        # Bound work per file so one large log cannot starve other clients.
        for _ in range(1000):
            begin = stream.tell()
            line = stream.readline(self.max_bytes + 1)
            if not line:
                break
            if len(line) > self.max_bytes:
                self._problem(str(tail.path), f"event log line exceeds {self.max_bytes} byte limit")
                break
            if not line.endswith(b"\n"):
                break  # Keep offset at the beginning until the writer finishes.
            try:
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError("not object")
            except (ValueError, RecursionError):
                self._problem(str(tail.path) + ":malformed", "invalid complete JSON event line skipped")
                tail.offset = stream.tell()
                continue
            try:
                record = (self._native(event, tail.path, client_key, run_id)
                          if tail.path.name == "trace.jsonl" else self._raw(event, client_key, run_id))
                if record:
                    saved = self.store.add(record)
                    self.status["saved"] += int(saved or 0)
                    self.status["last_event"] = record.get("event_time")
                    client = self.status["clients"].setdefault(CLIENTS[client_key], dict(events=0, last_event=None))
                    client["events"] += 1
                    client["last_event"] = record.get("event_time")
                self.status["events"] += 1
                tail.offset = stream.tell()
                self._clear_problem(str(tail.path))
            except CaptureReadError as exc:
                self._problem(str(tail.path) + ("" if exc.retryable else ":invalid"), str(exc))
                if exc.retryable:
                    stream.seek(begin)
                    break  # A missing/incomplete payload can become readable next poll.
                tail.offset = stream.tell()
            except Exception as exc:
                self._problem(str(tail.path), "evidence storage failed: " + type(exc).__name__)
                stream.seek(begin)
                break
        stream.seek(max(0, tail.offset - 128))
        tail.anchor = stream.read(min(128, tail.offset))

    def poll_once(self):
        if not self.root.exists():
            self.status["files"] = 0
            return
        paths = sorted(set(self.root.rglob("trace.jsonl")) | set(self.root.rglob("raw-events.jsonl")))
        self.status["files"] = len(paths)
        for path in paths:
            try:
                client_key, run_id = self._location(path)
                stat = path.stat()
                tail = self.tails.get(path)
                if tail and tail.identity != (stat.st_dev, stat.st_ino):
                    self._consume(tail, client_key, run_id)  # Drain renamed/unlinked inode first.
                    tail.close()
                    self.tails.pop(path)
                    tail = None
                    self.status["rotations"] += 1
                if tail:
                    tail.stream.seek(max(0, tail.offset - len(tail.anchor)))
                    anchor = tail.stream.read(len(tail.anchor))
                    overwritten = (stat.st_mtime_ns != tail.last_mtime_ns
                                   and stat.st_size <= tail.last_size)
                    if stat.st_size < tail.offset or anchor != tail.anchor or overwritten:
                        tail.offset, tail.anchor = 0, b""
                        self.status["rotations"] += 1
                if tail is None:
                    tail = self.tails[path] = _Tail(path)
                self._consume(tail, client_key, run_id)
                tail.last_size, tail.last_mtime_ns = stat.st_size, stat.st_mtime_ns
            except (OSError, CaptureReadError) as exc:
                self._problem(str(path), str(exc) if isinstance(exc, CaptureReadError)
                              else "event log could not be read: " + type(exc).__name__)
        # Close disappeared files after draining any append made before rename.
        for path in set(self.tails) - set(paths):
            tail = self.tails.pop(path)
            try:
                client_key, run_id = self._location(path)
                self._consume(tail, client_key, run_id)
            finally:
                tail.close()

    def close(self):
        for tail in self.tails.values():
            tail.close()
        self.tails.clear()


def start_event_monitor(store, root, *, poll_interval=0.5):
    """Start one daemon per store; stop only through store.event_stop_event."""
    existing = getattr(store, "event_thread", None)
    if existing and existing.is_alive():
        if Path(root).resolve() != Path(store.event_status["root"]):
            raise ValueError("event monitor is already watching a different root")
        return existing
    collector = EventCollector(store, root)
    stop = store.event_stop_event = threading.Event()
    def run():
        collector.status["running"] = True
        try:
            while not stop.is_set():
                try:
                    collector.poll_once()
                except Exception as exc:
                    collector._problem("monitor", "event monitor read failed: " + type(exc).__name__)
                stop.wait(poll_interval)
        finally:
            collector.close()
            collector.status["running"] = False
    thread = store.event_thread = threading.Thread(target=run, name="codex-event-monitor", daemon=True)
    thread.start()
    return thread
