"""Synthetic event logs only: no live processes, model requests, or Codex homes."""

import json
import os
from pathlib import Path
import tempfile
import time
import unittest

from call_log import call_rows
from event_capture import EventCollector, start_event_monitor
from observatory import Observatory
from observation_fields import safe_failure_fields, safe_session_metadata


class EventCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "capture"
        self.store = Observatory(self.base / "db", source_paths=[], archive_root=self.base / "none")
        self.collector = EventCollector(self.store, self.root)

    def tearDown(self):
        self.collector.close()
        stop = getattr(self.store, "event_stop_event", None)
        if stop:
            stop.set()
            self.store.event_thread.join(2)
        self.temp.cleanup()

    def bundle(self, client="app", run="run-one", name="bundle-one"):
        path = self.root / client / run / "traces" / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def native(self, bundle, kind, call="call-a", seq=1, **fields):
        event = dict(schema_version=1, seq=seq, wall_time_unix_ms=1791396000000 + seq,
                     rollout_id=bundle.name, thread_id="thread-a", codex_turn_id="turn-a",
                     payload=dict(type=kind, inference_call_id=call, **fields))
        self.append(bundle / "trace.jsonl", event)
        return event

    def payload(self, bundle, name, value):
        path = bundle / "payloads" / (name + ".json")
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(value))
        return {"path": str(path.relative_to(bundle))}

    def request(self, bundle, call="call-a", effort="high", seq=1):
        ref = self.payload(bundle, call + "-req", dict(model="gpt-test", reasoning={"effort": effort},
                           input=[{"role": "user", "content": "SECRET PROMPT"}],
                           authorization="SECRET AUTH"))
        return self.native(bundle, "inference_started", call, seq, request_payload=ref)

    def completed(self, bundle, call="call-a", response="resp-a", seq=2, **fields):
        ref = self.payload(bundle, call + "-res", dict(response_id=response,
                           token_usage={"input_tokens": 20, "output_tokens": 10, "total_tokens": 30,
                                        "cached_input_tokens": 3, "reasoning_output_tokens": 7},
                           output_items=[{"content": "SECRET OUTPUT"}], **fields))
        return self.native(bundle, "inference_completed", call, seq, response_id=response, response_payload=ref)

    def raw(self, response="resp-a", effort="high", client="app", run="run-one", kind="response.completed", **fields):
        body = dict(id=response, model="gpt-test", status="completed" if kind.endswith("completed") else "in_progress",
                    reasoning={"effort": effort}, usage={"output_tokens": 10})
        body.update(fields)
        wrapper = dict(timestamp="2026-10-07T18:00:00+00:00", client={"app": "Codex App", "cli": "Codex CLI"}[client],
                       run_id=run, event=dict(type=kind, response=body))
        path = self.root / client / run / "raw-events.jsonl"
        self.append(path, wrapper)
        return wrapper

    @staticmethod
    def append(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as stream:
            stream.write(json.dumps(value) + "\n")

    def records(self):
        return self.store.all_evidence()

    def test_concurrent_clients_and_interleaved_ids_pair_exactly(self):
        app, cli = self.bundle(), self.bundle("cli")
        self.request(app, "a", "high")
        self.request(app, "b", "low", 2)
        self.request(cli, "c", "medium")
        self.raw("resp-b", "low")
        self.raw("resp-c", "medium", "cli")
        self.raw("resp-a", "high")
        self.completed(app, "b", "resp-b", 3)
        self.completed(cli, "c", "resp-c")
        self.completed(app, "a", "resp-a", 4)
        self.collector.poll_once()
        rows = call_rows(self.records(), {})
        self.assertEqual(len(rows), 3)
        self.assertEqual({row["result"] for row in rows}, {"match"})
        self.assertEqual({row["client"] for row in rows}, {"Codex App", "Codex CLI"})
        self.assertEqual(self.collector.status["saved"], 9)
        self.assertNotIn("SECRET", json.dumps(self.records()))

    def test_terminal_arrives_before_request_then_id_bridge_stays_stable(self):
        bundle = self.bundle()
        self.raw("resp-a", "low")
        self.collector.poll_once()
        self.completed(bundle)
        self.collector.poll_once()
        self.request(bundle, seq=3)
        self.collector.poll_once()
        rows = call_rows(self.records(), {})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["result"], "difference")
        self.assertEqual(rows[0]["request"]["effort"], "high")
        self.assertEqual(rows[0]["response"]["effort"], "low")

    def test_native_effort_missing_is_not_claimed_as_server_omission(self):
        bundle = self.bundle()
        self.request(bundle)
        # Even a native summary polluted with a reasoning field is not wire evidence.
        self.completed(bundle, reasoning={"effort": "low"})
        self.collector.poll_once()
        response = next(r for r in self.records() if r["kind"] == "response")
        self.assertIsNone(response["effort"])
        self.assertEqual(response["effort_state"], "not_captured")
        self.assertEqual(response["effort_source"], "unavailable_in_native_trace")
        self.assertEqual(response["usage"]["output_tokens_details"], {"reasoning_tokens": 7})
        self.assertEqual(call_rows(self.records(), {})[0]["response"]["state"], "not_captured")
        self.raw(effort=None)
        self.collector.poll_once()
        wire = next(r for r in self.records() if r["source"] == "codex_event_log")
        self.assertEqual(wire["effort_state"], "not_echoed")
        self.assertEqual(call_rows(self.records(), {})[0]["response"]["state"], "not_echoed")

    def test_native_completion_does_not_conflict_with_wire_incomplete(self):
        bundle = self.bundle()
        self.request(bundle)
        self.completed(bundle)
        self.raw(kind="response.incomplete", status="incomplete", usage={"output_tokens": 4})
        self.collector.poll_once()
        row = call_rows(self.records(), {})[0]
        self.assertEqual(row["response"]["status"], "incomplete")
        self.assertEqual(row["response"]["usage"], {"output_tokens": 4})
        self.assertEqual(row["conflicts"], [])
        self.assertEqual(row["verdict"], "match")

    def test_failed_and_cancelled_without_response_id_keep_the_capture_id(self):
        bundle = self.bundle()
        for seq, kind in enumerate(("inference_failed", "inference_cancelled"), 1):
            self.request(bundle, call=kind, seq=seq * 2)
            self.native(bundle, kind, call=kind, seq=seq * 2 + 1, error="SECRET ERROR",
                        reason="SECRET REASON")
        self.collector.poll_once()
        rows = call_rows(self.records(), {})
        self.assertEqual({row["response"]["status"] for row in rows}, {"failed", "cancelled"})
        self.assertTrue(all(row["paired"] for row in rows))
        self.assertNotIn("SECRET", json.dumps(self.records()))

    def test_native_failure_retains_only_classified_reason_and_http_status(self):
        bundle = self.bundle()
        self.request(bundle)
        self.native(bundle, "inference_failed", seq=2,
                    error="Unexpected status code 404: The model CANARY_MODEL is not supported. CANARY_AUTH",
                    reason="CANARY_REASON")
        self.collector.poll_once()
        failure = next(r for r in self.records() if r["kind"] == "response")
        self.assertEqual(failure["http_status"], 404)
        self.assertEqual(failure["error_code"], "model_not_supported")
        self.assertEqual(failure["error_message"], "上游不支持请求的模型")
        self.assertNotIn("CANARY", json.dumps(failure))
        self.collector.poll_once()
        self.assertEqual(len(self.records()), 2)

    def test_reduced_native_and_raw_failure_fields_are_accepted(self):
        bundle = self.bundle()
        self.native(bundle, "inference_failed", http_status=404, error_code="model_not_supported",
                    error_message="CANARY_UNTRUSTED_LABEL")
        self.raw(kind="response.failed", status="failed", http_status=429,
                 error_code="rate_limit", error_message="CANARY_UNTRUSTED_LABEL")
        self.collector.poll_once()
        self.assertEqual({r["error_code"] for r in self.records()}, {"model_not_supported", "rate_limit"})
        self.assertNotIn("CANARY", json.dumps(self.records()))

    def test_error_classifier_uses_fixed_categories_and_ignores_arbitrary_fields(self):
        cases = [
            ({"error": {"message": "HTTP 404 model CANARY does not exist"}}, "model_not_found", 404),
            ({"status_code": "429", "message": "CANARY"}, "rate_limit", 429),
            ({"status": 401, "error": {"code": "invalid_api_key"}}, "authentication", 401),
            ("HTTP/1.1 504 CANARY timed out", "connection_timeout", 504),
            ({"error": "CANARY", "request": {"error_code": "model_not_found"}}, "unknown", None),
            ({"http_status": True, "code": {"CANARY": 1}}, "unknown", None),
            ({"http_status": 200, "error_code": "CANARY"}, "unknown", None),
        ]
        for value, code, status in cases:
            with self.subTest(code=code, status=status):
                fields = safe_failure_fields(value)
                self.assertEqual(fields["error_code"], code)
                self.assertEqual(fields.get("http_status"), status)
                self.assertNotIn("CANARY", json.dumps(fields))
                self.assertEqual(fields, safe_failure_fields(fields))

    def test_session_metadata_keeps_only_known_origins_and_valid_parent_ids(self):
        parent = "019a0000-0000-7000-8000-000000000001"
        safe = safe_session_metadata({
            "session_source": {"subagent": {"thread_spawn": {"parent_thread_id": parent,
                "depth": 2, "agent_path": "CANARY_PATH", "agent_nickname": "CANARY_NAME"}}},
            "thread_source": "user", "instructions": "CANARY_PROMPT", "tools": ["CANARY_TOOL"],
            "cwd": "CANARY_CWD", "rollout_path": "CANARY_ROLLOUT", "agent_role": "CANARY_ROLE",
        })
        self.assertEqual(safe["thread_source"], "user")
        self.assertEqual(safe["session_source"], {"subagent": {"thread_spawn": {"parent_thread_id": parent, "depth": 2}}})
        self.assertNotIn("CANARY", json.dumps(safe))
        self.assertEqual(safe_session_metadata(safe), safe)
        self.assertEqual(safe_session_metadata({"session_source": {"custom": "CANARY"},
                         "thread_source": "CANARY", "parent_thread_id": "CANARY"}), {})
        self.assertEqual(safe_session_metadata({"source": {"internal": "memory_consolidation"}}),
                         {"source": {"internal": "memory_consolidation"}})

    def test_fragmented_utf8_and_json_wait_for_newline(self):
        path = self.root / "app" / "run-one" / "raw-events.jsonl"
        wrapper = self.raw(model="测试模型")
        path.write_bytes(b"")
        encoded = (json.dumps(wrapper, ensure_ascii=False) + "\n").encode()
        cut = encoded.index("测".encode()) + 1
        with path.open("ab") as stream:
            stream.write(encoded[:cut])
        self.collector.poll_once()
        self.assertEqual(self.records(), [])
        with path.open("ab") as stream:
            stream.write(encoded[cut:-1])
        self.collector.poll_once()
        self.assertEqual(self.records(), [])
        with path.open("ab") as stream:
            stream.write(b"\n")
        self.collector.poll_once()
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(self.records()[0]["model"], "测试模型")

    def test_restart_is_idempotent_and_new_events_still_arrive(self):
        bundle = self.bundle()
        self.request(bundle)
        self.completed(bundle)
        self.raw()
        self.collector.poll_once()
        before = {r["id"] for r in self.records()}
        self.collector.close()
        self.collector = EventCollector(self.store, self.root)
        self.collector.poll_once()
        self.assertEqual({r["id"] for r in self.records()}, before)
        self.assertEqual(self.collector.status["saved"], 0)
        self.raw("new-response", "low")
        self.collector.poll_once()
        self.assertEqual(len(self.records()), 4)

    def test_rotation_drains_old_inode_and_truncation_resets_offset(self):
        self.raw("old")
        self.collector.poll_once()
        path = self.root / "app" / "run-one" / "raw-events.jsonl"
        self.raw("old-appended")
        path.rename(path.with_suffix(".old"))
        self.raw("replacement")
        self.collector.poll_once()
        self.assertEqual({r["response_id"] for r in self.records()}, {"old", "old-appended", "replacement"})
        body = self.raw("after-truncate")
        path.write_text(json.dumps(body) + "\n")
        self.collector.poll_once()
        self.assertEqual(len(self.records()), 4)
        self.assertGreaterEqual(self.collector.status["rotations"], 2)

    def test_bad_complete_line_is_diagnosed_and_next_event_is_read(self):
        self.raw("good")
        path = self.root / "app" / "run-one" / "raw-events.jsonl"
        path.write_bytes(b'{broken\n' + path.read_bytes())
        self.collector.poll_once()
        self.assertEqual(len(self.records()), 1)
        self.assertIn("invalid complete", self.collector.status["error"])

    def test_equal_size_rewrite_with_same_trailing_bytes_is_detected(self):
        wrapper = self.raw("response-one", model="a-long-model-name-" * 20)
        self.collector.poll_once()
        path = self.root / "app" / "run-one" / "raw-events.jsonl"
        old_time = path.stat().st_mtime_ns
        old_bytes = path.read_bytes()
        new_bytes = old_bytes.replace(b"response-one", b"response-two")
        self.assertEqual(old_bytes[-128:], new_bytes[-128:])
        path.write_bytes(new_bytes)
        os.utime(path, ns=(old_time + 1_000_000, old_time + 1_000_000))
        self.collector.poll_once()
        self.assertEqual({r["response_id"] for r in self.records()}, {"response-one", "response-two"})

    def test_unrelated_trace_payloads_are_never_opened(self):
        bundle = self.bundle()
        self.native(bundle, "tool_call_started", metadata_payload={"path": "../../outside.json"})
        self.collector.poll_once()
        self.assertEqual(self.records(), [])
        self.assertIsNone(self.collector.status["error"])

    def test_payload_pending_retries_without_advancing_or_leaking_content(self):
        bundle = self.bundle()
        self.native(bundle, "inference_started", request_payload={"path": "payloads/later.json"})
        self.collector.poll_once()
        self.assertEqual(self.records(), [])
        self.assertIn("payload", self.collector.status["error"])
        self.payload(bundle, "later", {"model": "gpt-test", "reasoning": {"effort": "high"}})
        self.collector.poll_once()
        self.assertEqual(len(self.records()), 1)
        self.assertIsNone(self.collector.status["error"])

    def test_payload_traversal_and_symlink_escape_rejected(self):
        outside = self.base / "outside.json"
        outside.write_text('{"model":"SECRET"}')
        for name, relative in (("traversal", "../../../../../../outside.json"), ("link", "payload.json")):
            bundle = self.bundle(name=name)
            if name == "link":
                (bundle / relative).symlink_to(outside)
            self.native(bundle, "inference_started", request_payload={"path": relative})
        self.collector.poll_once()
        self.assertEqual(self.records(), [])
        self.assertEqual(self.collector.status["errors"], 2)

    def test_payload_limit_is_explicit_and_recoverable(self):
        bundle = self.bundle()
        ref = self.payload(bundle, "large", {"padding": "x" * 5000})
        self.native(bundle, "inference_started", request_payload=ref)
        self.collector.max_bytes = 1024
        self.collector.poll_once()
        self.assertIn("exceeds 1024", self.collector.status["error"])
        self.assertEqual(self.records(), [])
        self.payload(bundle, "large", {"model": "gpt-test"})
        self.collector.poll_once()
        self.assertEqual(len(self.records()), 1)

    def test_wrong_client_or_missing_response_id_cannot_be_guessed(self):
        wrapper = self.raw()
        path = self.root / "app" / "run-one" / "raw-events.jsonl"
        path.write_bytes(b"")
        wrapper["client"] = "Codex CLI"
        self.append(path, wrapper)
        wrapper["client"] = "Codex App"
        wrapper["event"]["response"].pop("id")
        self.append(path, wrapper)
        self.collector.poll_once()
        self.assertEqual(self.records(), [])
        self.assertEqual(self.collector.status["errors"], 2)

    def test_monitor_has_independent_stop_event(self):
        self.raw()
        memory_stop = self.store.stop_event
        thread = start_event_monitor(self.store, self.root, poll_interval=0.01)
        self.assertIsNot(self.store.event_stop_event, memory_stop)
        self.assertIs(start_event_monitor(self.store, self.root), thread)
        memory_stop.set()
        deadline = time.monotonic() + 2
        while not self.records() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(self.store.event_status["running"])
        self.assertEqual(len(self.records()), 1)
        self.store.event_stop_event.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertFalse(self.store.event_status["running"])


if __name__ == "__main__":
    unittest.main()
