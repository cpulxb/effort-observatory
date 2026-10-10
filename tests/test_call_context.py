import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from call_context import classify_submission, enrich_call_contexts


def submission(sid="s", tid="t", trigger="None", schema="None", user="private canary"):
    return (f'session_loop{{thread_id={sid}}}: Submission sub=Submission {{ id: "{tid}", '
            f'op: TurnInput {{ request: TurnInputRequest {{ input: UserInput {{ text: {json.dumps(user)} }}, '
            f'start: TurnStartOptions {{ turn_trigger: {trigger}, final_output_json_schema: {schema}, '
            'service_tier: None } } } }')


TITLE = ('Some(Object {"type": String("object"), "properties": Object {"title": Object '
         '{"type": String("string"), "minLength": Number(1), "maxLength": Number(36)}}, '
         '"required": Array [String("title")], "additionalProperties": Bool(false)})')


class CallContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / "state_5.sqlite"
        self.logs = self.root / "logs_2.sqlite"
        with sqlite3.connect(self.state) as db:
            db.execute("CREATE TABLE threads (id TEXT PRIMARY KEY,thread_source TEXT)")
        with sqlite3.connect(self.logs) as db:
            db.execute("CREATE TABLE logs (id INTEGER PRIMARY KEY,thread_id TEXT,target TEXT,feedback_log_body TEXT)")
            db.execute("CREATE INDEX idx_logs_thread_id ON logs(thread_id)")
        self.store = SimpleNamespace(source_paths=[self.state], logs_path=self.logs)

    def tearDown(self):
        self.temp.cleanup()

    def log(self, body, sid="s"):
        with sqlite3.connect(self.logs) as db:
            db.execute("INSERT INTO logs(thread_id,target,feedback_log_body) VALUES(?,?,?)",
                       (sid, "codex_core::session::handlers", body))

    def records(self, sid="s", tid="t", **extra):
        return [{"thread_id": sid, "turn_id": tid, "observed_at": "2026-10-08T01:45:00Z", **extra}]

    def test_long_native_memory_trigger_and_exact_turn(self):
        self.log(submission(trigger='Some("memory_consolidation")', user="secret canary " * 5000))
        result = enrich_call_contexts(self.store, self.records() + self.records(tid="other"))
        self.assertEqual(result[("s", "t")]["purpose"], "memory_consolidation")
        self.assertEqual(result[("s", "other")]["purpose"], "unknown")
        self.assertNotIn("secret", str(result))
        self.assertNotIn("parent", str(result))

    def test_title_requires_only_title_schema_and_same_thread(self):
        self.log(submission(schema=TITLE))
        result = enrich_call_contexts(self.store, self.records())
        self.assertEqual(result[("s", "t")]["purpose"], "title_generation")
        self.assertIsNone(classify_submission("other", submission(schema=TITLE)))
        self.assertIsNone(classify_submission("s", submission(schema=TITLE.replace('"title": Object', '"answer": Object'))))
        self.assertIsNone(classify_submission("s", submission(schema=TITLE.replace('"required": Array [String("title")]', '"required": Array []'))))

    def test_user_text_cannot_spoof_trigger_or_title_schema(self):
        spoof = ('start: TurnStartOptions { turn_trigger: Some("memory_consolidation"), '
                 'final_output_json_schema: None')
        self.assertIsNone(classify_submission("s", submission(user=spoof)))
        self.assertIsNone(classify_submission("s", submission(user=TITLE)))

    def test_explicit_user_thread_not_model_or_time(self):
        with sqlite3.connect(self.state) as db:
            db.execute("INSERT INTO threads VALUES('main','user')")
        result = enrich_call_contexts(self.store, self.records("main", model="gpt-5.6-terra", effort="medium")
                                      + self.records("other", model="gpt-6-astra", effort="xhigh"))
        self.assertEqual(result[("main", "t")]["purpose"], "conversation")
        self.assertEqual(result[("other", "t")]["purpose"], "unknown")

    def test_native_trigger_wins_over_thread_default(self):
        self.log(submission(trigger='Some("memory_consolidation")'))
        result = enrich_call_contexts(self.store, self.records(), {"s": {"thread_source": "user"}})
        self.assertEqual(result[("s", "t")]["purpose"], "memory_consolidation")

    def test_unrecognized_trigger_does_not_become_conversation(self):
        self.log(submission(trigger='Some("future_background_task")'))
        result = enrich_call_contexts(self.store, self.records(), {"s": {"thread_source": "user"}})
        self.assertEqual(result[("s", "t")]["purpose"], "unknown")

    def test_unicode_large_user_text_does_not_hide_tail_metadata(self):
        self.log(submission(trigger='Some("memory_consolidation")', user="私密内容" * 10000))
        result = enrich_call_contexts(self.store, self.records())
        self.assertEqual(result[("s", "t")]["purpose"], "memory_consolidation")

    def test_db_thread_field_does_not_override_body_identity(self):
        self.log(submission(sid="someone_else", trigger='Some("memory_consolidation")'))
        self.assertEqual(enrich_call_contexts(self.store, self.records())[("s", "t")]["purpose"], "unknown")

    def test_cache_is_bounded_and_does_not_leak_mutation(self):
        self.log(submission(trigger='Some("user")'))
        with patch("call_context.time.monotonic", return_value=10):
            result = enrich_call_contexts(self.store, self.records())
        result[("s", "t")]["purpose"] = "tampered"
        with patch("call_context.time.monotonic", return_value=11), patch("call_context._read_contexts") as reader:
            self.assertEqual(enrich_call_contexts(self.store, self.records())[("s", "t")]["purpose"], "conversation")
            reader.assert_not_called()
        with patch("call_context.time.monotonic", return_value=30), patch("call_context._read_contexts", return_value={}) as reader:
            enrich_call_contexts(self.store, self.records())
            reader.assert_called_once()

    def test_missing_or_invalid_sources_remain_unknown(self):
        self.store.source_paths = [self.root / "missing.sqlite"]
        self.logs.write_text("broken database")
        result = enrich_call_contexts(self.store, self.records())
        self.assertEqual(result[("s", "t")]["purpose"], "unknown")


if __name__ == "__main__":
    unittest.main()
