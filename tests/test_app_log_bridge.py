import json
from pathlib import Path
import tempfile
import unittest

from app_log_bridge import AppLogBridge, StderrAssembler, desktop_stderr_chunk
from observed_launcher import Recorder


def event(response_id="resp-app"):
    return json.dumps({"timestamp": "2026-10-07T12:21:42.952Z", "level": "TRACE",
                       "fields": {"message": "SSE event: " + json.dumps({
                           "type": "response.completed", "response": {
                               "id": response_id, "model": "returned-model",
                               "reasoning": {"effort": "medium"},
                               "usage": {"input_tokens": 3, "output_tokens": 2},
                               "output": [{"text": "PRIVATE_OUTPUT"}],
                               "instructions": "PRIVATE_PROMPT"}})}})


def outer(chunk):
    return "2026-10-07T12:21:42.952Z debug [AppServerConnection] Codex CLI stderr message=" + json.dumps(chunk) + "\n"


class AppLogBridgeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.logs = self.root / "logs"
        self.day = self.logs / "2026/10/07"
        self.day.mkdir(parents=True)
        self.run = self.root / "events/app/desktop-native"
        self.log = self.day / "codex-desktop-f6cb2b0b-382e-4240-a033-32df8b7bb94b-29944-t0-i1-122142-0.log"

    def responses(self):
        path = self.run / "raw-events.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_accepts_exact_local_logger_shape_only(self):
        self.assertEqual(desktop_stderr_chunk(outer(event())), event())
        self.assertIsNone(desktop_stderr_chunk(outer(event()).replace("[AppServerConnection]", "[other]")))
        self.assertIsNone(desktop_stderr_chunk(outer(event()).rstrip() + " other=data"))

    def test_multiline_and_split_json_chunks_are_minimal(self):
        value = event()
        self.log.write_text(outer(value[:70]) + outer(value[70:] + "\n" + event("resp-second")))
        bridge = AppLogBridge(self.logs, self.run, from_start=True)
        bridge.scan_once()
        records = self.responses()
        self.assertEqual([r["event"]["response"]["id"] for r in records], ["resp-app", "resp-second"])
        self.assertNotIn("PRIVATE_", json.dumps(records))
        self.assertEqual(records[0]["client"], "Codex App")
        self.assertEqual(records[0]["run_id"], "desktop-native")
        self.assertEqual(records[0]["event"]["response"]["reasoning"]["effort"], "medium")
        bridge.scan_once()
        self.assertEqual(len(self.responses()), 2)

    def test_initial_eof_then_live_append_and_restart(self):
        self.log.write_text(outer(event("old")))
        bridge = AppLogBridge(self.logs, self.run)
        bridge.scan_once()
        self.assertEqual(self.responses(), [])
        with self.log.open("a") as stream:
            stream.write(outer(event("live")))
        bridge.scan_once()
        self.assertEqual(len(self.responses()), 1)
        AppLogBridge(self.logs, self.run).scan_once()
        self.assertEqual(len(self.responses()), 1)

    def test_new_file_is_read_from_start_after_initial_scan(self):
        bridge = AppLogBridge(self.logs, self.run)
        bridge.scan_once()
        self.log.write_text(outer(event()))
        bridge.scan_once()
        self.assertEqual(len(self.responses()), 1)

    def test_partial_outer_line_waits_for_newline(self):
        line = outer(event())
        self.log.write_text(line[:180])
        bridge = AppLogBridge(self.logs, self.run, from_start=True)
        bridge.scan_once()
        self.assertEqual(self.responses(), [])
        with self.log.open("a") as stream:
            stream.write(line[180:])
        bridge.scan_once()
        self.assertEqual(len(self.responses()), 1)

    def test_partial_inner_record_can_recover_after_bridge_restart(self):
        value = event()
        self.log.write_text(outer(value[:70]))
        bridge = AppLogBridge(self.logs, self.run, from_start=True)
        bridge.scan_once()
        self.assertEqual(self.responses(), [])
        with self.log.open("a") as stream:
            stream.write(outer(value[70:]))
        AppLogBridge(self.logs, self.run).scan_once()
        self.assertEqual(len(self.responses()), 1)

    def test_rotation_rewrite_detected_even_same_inode_and_larger_size(self):
        self.log.write_text(outer(event("one")))
        bridge = AppLogBridge(self.logs, self.run, from_start=True)
        bridge.scan_once()
        self.log.write_text(outer(event("two-longer")))
        bridge.scan_once()
        self.assertEqual(len(self.responses()), 2)
        self.assertEqual(bridge.status["rotations"], 1)

    def test_partial_inner_record_recovers_across_rotation_and_restart(self):
        value = event()
        second = self.log.with_name(self.log.name.replace("-0.log", "-1.log"))
        self.log.write_text(outer(value[:70]))
        second.write_text(outer(value[70:100]))
        bridge = AppLogBridge(self.logs, self.run, from_start=True)
        bridge.scan_once()
        with second.open("a") as stream:
            stream.write(outer(value[100:]))
        AppLogBridge(self.logs, self.run).scan_once()
        self.assertEqual(len(self.responses()), 1)

    def test_non_response_and_oversize_buffer_do_not_emit(self):
        self.run.mkdir(parents=True)
        assembler = StderrAssembler(Recorder(self.run, "Codex App", "desktop-native"), max_bytes=200)
        assembler.feed("{" + "x" * 201)
        self.assertEqual(assembler.errors, 1)
        self.assertEqual(self.responses(), [])
        self.assertEqual(assembler.pending, "")


if __name__ == "__main__":
    unittest.main()
