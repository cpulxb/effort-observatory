import copy
import json
from pathlib import Path
import tempfile
import unittest

from capture import request_record
from observatory import Observatory, attach_evidence, parse_session, redact, verdict
from relay import ResponseRecorder, SSEDecoder


class EvidenceAssociationTests(unittest.TestCase):
    def setUp(self):
        self.session = {
            "id": "thread-main",
            "turns": [
                {
                    "id": "turn-a",
                    "client_effort": "ultra",
                    "output_ids": ["msg-a"],
                    "evidence": [],
                },
                {
                    "id": "turn-b",
                    "client_effort": "xhigh",
                    "output_ids": ["msg-b"],
                    "evidence": [],
                },
            ],
        }

    def test_does_not_assign_by_time_or_thread_only(self):
        evidence = [
            {
                "thread_id": "thread-main",
                "kind": "response",
                "effort": "max",
                "observed_at": "2026-10-03T06:02:00Z",
            }
        ]
        result = attach_evidence(copy.deepcopy(self.session), evidence)
        self.assertEqual(len(result["unmatched"]), 1)
        self.assertTrue(all(not turn["evidence"] for turn in result["turns"]))

    def test_output_id_matches_correct_turn_not_latest(self):
        evidence = [
            {
                "thread_id": "thread-main",
                "kind": "response_fragment",
                "output_ids": ["msg-a"],
                "effort": "high",
            }
        ]
        result = attach_evidence(copy.deepcopy(self.session), evidence)
        self.assertEqual(result["turns"][0]["verdict"], "runtime_diff")
        self.assertEqual(result["turns"][0]["request_efforts"], [])
        self.assertEqual(result["turns"][1]["verdict"], "unknown")

    def test_other_thread_cannot_contaminate_result(self):
        evidence = [
            {
                "thread_id": "thread-other",
                "turn_id": "turn-a",
                "kind": "response",
                "effort": "max",
            }
        ]
        result = attach_evidence(copy.deepcopy(self.session), evidence)
        self.assertEqual(result["turns"][0]["verdict"], "unknown")

    def test_missing_response_is_not_a_match(self):
        self.assertEqual(verdict("xhigh", "xhigh", None), "unknown")
        self.assertEqual(verdict("xhigh", "xhigh", "high"), "mismatch")
        self.assertEqual(verdict("xhigh", "xhigh", "xhigh"), "match")

    def test_client_request_conversion_is_visible(self):
        self.assertEqual(verdict("ultra", "xhigh", "xhigh"), "client_mismatch")
        self.assertEqual(verdict("ultra", "xhigh", None), "client_request_diff")

    def test_mixed_efforts_are_not_silently_collapsed(self):
        evidence = [
            {
                "thread_id": "thread-main",
                "turn_id": "turn-a",
                "kind": "response",
                "effort": effort,
            }
            for effort in ("high", "max")
        ]
        result = attach_evidence(copy.deepcopy(self.session), evidence)
        self.assertEqual(result["turns"][0]["verdict"], "mixed")


class ParserTests(unittest.TestCase):
    def parse(self, events):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            path.write_text(
                "\n".join(json.dumps(event) for event in events) + '\n{"partial":',
                encoding="utf-8",
            )
            return parse_session(path, {"id": "thread-main"})

    def test_interleaved_tool_results_match_call_ids(self):
        events = [
            {
                "type": "turn_context",
                "timestamp": "2026-10-03T06:00:00Z",
                "payload": {"turn_id": "turn-a", "model": "test", "effort": "xhigh"},
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "call_id": "call-a",
                    "name": "read",
                    "arguments": '{"file":"a"}',
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "call_id": "call-b",
                    "name": "exec",
                    "input": "echo hello",
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "call-b",
                    "output": "hello",
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call-a",
                    "output": "contents",
                },
            },
        ]
        result = self.parse(events)
        tools = result["turns"][0]["tools"]
        self.assertEqual(tools[0]["result"], "contents")
        self.assertEqual(tools[1]["result"], "hello")
        self.assertEqual(tools[0]["arguments"], {"file": "a"})

    def test_secret_fields_are_redacted(self):
        data = redact(
            {
                "authorization": "secret",
                "nested": {"api_key": "secret"},
                "text": "Bearer abcdef123",
            }
        )
        self.assertNotIn("secret", json.dumps(data))
        self.assertNotIn("abcdef123", data["text"])


class StreamTests(unittest.TestCase):
    def test_split_utf8_crlf_and_multiple_events(self):
        events = []
        decoder = SSEDecoder(events.append)
        raw = 'data: {"type":"response.created","text":"时间"}\r\n\r\ndata: {"type":"response.completed"}\r\n\r\n'.encode()
        for byte in raw:
            decoder.feed(bytes([byte]))
        decoder.finish()
        self.assertEqual(
            [event["type"] for event in events],
            ["response.created", "response.completed"],
        )
        self.assertEqual(events[0]["text"], "时间")

    def test_request_response_pairing_preserves_missing_effort(self):
        class MemoryStore:
            def __init__(self):
                self.records = []

            def add(self, value):
                self.records.append(value)

        store = MemoryStore()
        request = {
            "capture_id": "capture-1",
            "endpoint": "test",
            "thread_id": "thread-a",
            "turn_id": "turn-a",
        }
        recorder = ResponseRecorder(store, request)
        recorder.receive(
            {
                "type": "response.created",
                "response": {"id": "resp-a", "model": "test", "status": "in_progress"},
            }
        )
        recorder.receive(
            {
                "type": "response.completed",
                "response": {
                    "id": "resp-a",
                    "model": "test",
                    "status": "completed",
                    "usage": {"output_tokens": 8},
                },
            }
        )
        self.assertIsNone(store.records[-1]["effort"])
        self.assertEqual(store.records[-1]["turn_id"], "turn-a")
        self.assertEqual(store.records[-1]["status"], "completed")

    def test_responses_effort_is_read_from_reasoning(self):
        record = request_record(
            {
                "model": "test",
                "input": [{"role": "user", "content": "hello"}],
                "reasoning": {"effort": "xhigh"},
            }
        )
        self.assertEqual(record["effort"], "xhigh")
        self.assertEqual(record["user_message"], "hello")


class StorageTests(unittest.TestCase):
    def test_repeated_memory_observation_is_deduplicated(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Observatory(directory)
            record = {
                "thread_id": "dedup-test",
                "kind": "response",
                "effort": "high",
                "source": "test",
                "observed_at": "2026-10-03T06:00:00Z",
            }
            self.assertEqual(store.add(record), 1)
            record["observed_at"] = "2026-10-03T06:00:10Z"
            self.assertEqual(store.add(record), 0)


if __name__ == "__main__":
    unittest.main()
