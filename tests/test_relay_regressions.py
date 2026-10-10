"""Relay observation failures must not change business response bytes."""

import io
import json
import sqlite3
import unittest
from unittest.mock import patch

from relay import MAX_EVENT_BYTES, ResponseRecorder, SSEDecoder, relay_request, stream_response


def sse(event):
    return b"data: " + json.dumps(event, ensure_ascii=False).encode() + b"\r\n\r\n"


def terminal(status="completed"):
    response = {
        "id": "resp-regression",
        "status": status,
        "reasoning": {"effort": "high"},
        "usage": {"input_tokens": 11, "output_tokens": 29},
        "output": [],
    }
    if status == "incomplete":
        response["incomplete_details"] = {"reason": "max_output_tokens"}
    if status == "failed":
        response["error"] = {"code": "server_error", "message": "synthetic failure"}
    return {"type": "response." + status, "response": response}


class MemoryStore:
    def __init__(self):
        self.records = []

    def add(self, record):
        self.records.append(record.copy())


class MemoryHandler:
    def __init__(self, body=None):
        body = body or {"stream": True, "input": [], "reasoning": {"effort": "high"}}
        self.raw = json.dumps(body).encode()
        self.path = "/relay/v1/responses"
        self.rfile = io.BytesIO(self.raw)
        self.wfile = io.BytesIO()
        self.headers = {"Authorization": "Bearer synthetic", "Content-Length": str(len(self.raw))}
        self.status = None
        self.close_connection = False

    def send_response(self, status):
        self.status = status

    def send_header(self, *args):
        pass

    def end_headers(self):
        pass

    def respond(self, body, status=200):
        self.status = status
        self.wfile.write(json.dumps(body).encode())


class MemoryResponse(io.BytesIO):
    status = 200
    headers = {"Content-Type": "text/event-stream"}


class RelayRegressionTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.handler = MemoryHandler()
        self.request = {
            "capture_id": "capture-regression", "endpoint": "synthetic",
            "thread_id": "thread-regression", "turn_id": "turn-regression",
        }
        self.recorder = ResponseRecorder(self.store, self.request)
        log = patch("relay.LOGGER")
        log.start()
        self.addCleanup(log.stop)

    def forward(self, payload):
        stream_response(self.handler, MemoryResponse(payload), self.recorder)
        self.assertEqual(self.handler.wfile.getvalue(), payload)
        self.assertEqual(self.handler.status, 200)

    def test_oversized_valid_event_is_fully_forwarded_and_next_event_is_captured(self):
        payload = sse({"type": "response.output_text.delta", "delta": "x" * (MAX_EVENT_BYTES + 200000)})
        payload += sse(terminal())
        self.forward(payload)
        self.assertTrue(self.recorder.terminal_seen)
        self.assertEqual(self.store.records[-1]["status"], "completed")
        self.assertTrue(any(r["status"] == "capture_truncated" for r in self.recorder.diagnostics))
        self.assertFalse(any(r["status"] == "transport_error" for r in self.store.records))

    def test_multiline_event_memory_is_bounded_and_recovers_at_blank_line(self):
        events, diagnostics = [], []
        decoder = SSEDecoder(events.append, lambda status, **details: diagnostics.append(status))
        # Valid JSON with many separate SSE data lines, each individually below the limit.
        decoder.feed(b"data: {\n")
        for _ in range(100):
            decoder.feed(b'data: "padding": "' + b"x" * 60000 + b'",\n')
            self.assertLessEqual(len(decoder.buffer) + sum(map(len, decoder.data_lines)), MAX_EVENT_BYTES)
            self.assertLessEqual(decoder.event_bytes, MAX_EVENT_BYTES)
        decoder.feed(b'data: "type": "response.output_text.delta"}\n\n')
        decoder.feed(sse(terminal()))
        decoder.finish()
        self.assertEqual([e["type"] for e in events], ["response.completed"])
        self.assertEqual(diagnostics, ["capture_truncated"])

    def test_incomplete_completed_and_failed_preserve_terminal_evidence(self):
        for status in ("incomplete", "completed", "failed"):
            with self.subTest(status=status):
                store = MemoryStore()
                handler = MemoryHandler()
                recorder = ResponseRecorder(store, self.request)
                payload = sse(terminal(status))
                stream_response(handler, MemoryResponse(payload), recorder)
                self.assertEqual(handler.wfile.getvalue(), payload)
                self.assertEqual(len(store.records), 1)
                record = store.records[0]
                self.assertEqual(record["status"], status)
                self.assertEqual(record["effort"], "high")
                self.assertEqual(record["usage"], {"input_tokens": 11, "output_tokens": 29})
                self.assertEqual(record["capture_id"], self.request["capture_id"])
                if status == "incomplete":
                    self.assertEqual(record["reason"], "max_output_tokens")
                    self.assertEqual(record["incomplete_details"], {"reason": "max_output_tokens"})
                if status == "failed":
                    self.assertEqual(record["error"]["code"], "server_error")

    def test_request_and_response_storage_failure_do_not_change_forwarding(self):
        payload = sse(terminal()) + b": bytes after terminal\n\n"
        attempted = []

        def fail(record):
            attempted.append(record.copy())
            raise sqlite3.OperationalError("synthetic database failure")

        self.store.add = fail
        with patch("relay.upstream_request", return_value=MemoryResponse(payload)) as upstream:
            relay_request(self.handler, self.store)
        self.assertEqual(upstream.call_args.args[2], self.handler.raw)
        self.assertEqual(self.handler.wfile.getvalue(), payload)
        self.assertEqual(self.handler.status, 200)
        self.assertTrue(any(r["kind"] == "request" for r in attempted))
        self.assertTrue(any(r.get("status") == "completed" for r in attempted))
        self.assertFalse(any(r.get("status") == "transport_error" for r in attempted))

    def test_unexpected_decoder_failure_disables_capture_only(self):
        payload = sse({"type": "response.output_text.delta", "delta": "x" * 200000}) + sse(terminal())
        with patch("relay.SSEDecoder.feed", side_effect=RuntimeError("synthetic parser error")):
            self.forward(payload)
        statuses = [r["status"] for r in self.store.records]
        self.assertIn("capture_error", statuses)
        self.assertIn("capture_incomplete", statuses)
        self.assertNotIn("transport_error", statuses)
        self.assertNotIn("incomplete_stream", statuses)

    def test_bad_json_capture_recovers_and_preserves_following_terminal(self):
        self.forward(b"data: {bad-json}\n\n" + sse(terminal()))
        self.assertTrue(self.recorder.terminal_seen)
        self.assertEqual(self.store.records[-1]["status"], "completed")
        self.assertEqual(self.recorder.diagnostics[0]["stage"], "json")

    def test_response_normalization_failure_does_not_hide_received_terminal(self):
        payload = sse(terminal()) + b": keep forwarding\n\n"
        with patch("relay.response_record", side_effect=AttributeError("synthetic schema failure")):
            self.forward(payload)
        self.assertTrue(self.recorder.terminal_seen)
        self.assertEqual([r["status"] for r in self.store.records], ["capture_error"])

    def test_request_normalization_failure_still_forwards_original_request(self):
        payload = sse(terminal())
        with patch("relay.request_record", side_effect=TypeError("synthetic schema failure")):
            with patch("relay.upstream_request", return_value=MemoryResponse(payload)) as upstream:
                relay_request(self.handler, self.store)
        self.assertEqual(upstream.call_args.args[2], self.handler.raw)
        self.assertEqual(self.handler.wfile.getvalue(), payload)
        self.assertEqual(self.store.records[0]["kind"], "capture_diagnostic")
        self.assertEqual(self.store.records[0]["stage"], "request")
        self.assertEqual(self.store.records[-1]["status"], "completed")

    def test_healthy_parser_without_terminal_retains_incomplete_stream(self):
        self.forward(sse({"type": "response.created", "response": {"status": "in_progress"}}))
        self.assertEqual(self.store.records[-1]["status"], "incomplete_stream")

    def test_storage_error_does_not_hide_a_known_missing_terminal(self):
        original_add = self.store.add
        failed = False

        def fail_once(record):
            nonlocal failed
            if not failed:
                failed = True
                raise sqlite3.OperationalError("synthetic transient storage failure")
            original_add(record)

        self.store.add = fail_once
        self.forward(sse({"type": "response.created", "response": {"status": "in_progress"}}))
        self.assertEqual(self.store.records[-1]["status"], "incomplete_stream")
        self.assertEqual(self.store.records[0]["stage"], "storage")

    def test_actual_upstream_failure_is_classified_as_transport_error(self):
        with patch("relay.upstream_request", side_effect=OSError("synthetic connection failure")):
            relay_request(self.handler, self.store)
        self.assertEqual(self.handler.status, 502)
        self.assertEqual(self.store.records[-1]["status"], "transport_error")


if __name__ == "__main__":
    unittest.main()
