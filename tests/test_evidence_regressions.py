"""Synthetic regressions; never read live client databases or archive directories."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from observatory import Observatory, attach_evidence


def session_fixture():
    return {
        "id": "thread-a",
        "turns": [
            {"id": "turn-a", "client_effort": None, "output_ids": ["msg-a"], "tools": []},
            {"id": "turn-b", "client_effort": None, "output_ids": ["msg-b"], "tools": []},
        ],
    }


def record(kind, effort, capture_id=None, turn_id="turn-a", **fields):
    return dict(thread_id="thread-a", turn_id=turn_id, kind=kind, effort=effort,
                capture_id=capture_id, **fields)


class CallPairingTests(unittest.TestCase):
    def attach(self, records):
        return attach_evidence(session_fixture(), records)

    def test_split_missing_fields_never_fabricate_match_or_mismatch(self):
        for response_effort in ("high", "low"):
            with self.subTest(response=response_effort):
                turn = self.attach([
                    record("request", "high", "a"), record("response", None, "a"),
                    record("request", None, "b"), record("response", response_effort, "b"),
                ])["turns"][0]
                self.assertEqual(turn["verdict"], "unknown")
                self.assertFalse(turn["has_difference"])
                self.assertEqual([c["verdict"] for c in turn["calls"]], ["unknown", "unknown"])
                self.assertEqual(turn["calls"][0]["response_state"], "not_echoed")
                self.assertEqual(turn["calls"][1]["request_state"], "not_carried")

    def test_no_call_identifier_does_not_create_pair(self):
        turn = self.attach([record("request", "high"), record("response", "high")])["turns"][0]
        self.assertEqual(turn["verdict"], "unknown")
        self.assertEqual(len(turn["calls"]), 2)
        self.assertTrue(all(not c["paired"] for c in turn["calls"]))

    def test_actual_pair_matches_and_retains_evidence_ids(self):
        turn = self.attach([record("request", "high", "a", id="req"),
                            record("response", "high", "a", id="res")])["turns"][0]
        self.assertEqual(turn["verdict"], "match")
        self.assertEqual(turn["calls"][0]["evidence_ids"], ["req", "res"])
        self.assertTrue(turn["calls"][0]["paired"])

    def test_different_matched_calls_are_mixed_but_not_differences(self):
        records = [record(kind, effort, effort) for effort in ("high", "low")
                   for kind in ("request", "response")]
        turn = self.attach(records)["turns"][0]
        self.assertEqual(turn["verdict"], "mixed")
        self.assertFalse(turn["has_difference"])

    def test_explicit_identifiers_conflict_instead_of_using_turn_precedence(self):
        result = self.attach([record("response", "high", output_ids=["msg-b"])])
        self.assertTrue(result["unmatched"][0]["association_conflict"])
        self.assertTrue(all(not t["evidence"] for t in result["turns"]))

    def test_unknown_explicit_turn_conflicts_with_known_output(self):
        result = self.attach([record("response", "high", turn_id="missing", output_ids=["msg-a"])])
        self.assertTrue(result["unmatched"][0]["association_conflict"])

    def test_capture_can_associate_response_without_turn_identifier(self):
        turn = self.attach([record("request", "high", "a"),
                            record("response", "low", "a", turn_id=None)])["turns"][0]
        self.assertEqual(turn["verdict"], "mismatch")
        self.assertTrue(turn["has_difference"])

    def test_capture_with_two_strong_turns_is_conflict(self):
        result = self.attach([record("request", "high", "a"),
                              record("response", "high", "a", turn_id="turn-b")])
        self.assertEqual(len(result["unmatched"]), 2)
        self.assertTrue(all(e["association_conflict"] for e in result["unmatched"]))

    def test_diagnostics_are_not_response_observations(self):
        call = self.attach([record("request", "high", "a"),
                            record("capture_diagnostic", None, "a")])["turns"][0]["calls"][0]
        self.assertEqual(call["response_state"], "not_captured")
        self.assertEqual(call["verdict"], "unknown")


class StorageSummaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Observatory(self.root / "data", source_paths=[], archive_root=self.root / "archives",
                                 logs_path=self.root / "absent-logs.sqlite")

    def write_log(self):
        path = self.root / "session.jsonl"
        events = []
        for name in ("a", "b"):
            events.extend([
                {"type": "turn_context", "timestamp": f"2026-10-03T00:0{len(events)}:00Z",
                 "payload": {"turn_id": f"turn-{name}", "effort": "high", "model": "test"}},
                {"type": "response_item", "payload": {"id": f"msg-{name}", "type": "message",
                 "role": "assistant", "content": [{"type": "output_text", "text": "message text"}]}},
                {"type": "response_item", "payload": {"type": "function_call", "name": "read",
                 "call_id": f"call-{name}", "arguments": '{"text":"large tool body"}'}},
            ])
        path.write_text("\n".join(map(json.dumps, events)), encoding="utf-8")
        self.store.catalog["thread-a"] = dict(id="thread-a", title="synthetic", path=str(path))
        return path

    def test_old_thread_survives_over_3000_newer_records(self):
        self.store.add(record("response", "high", observed_at="2020-01-01T00:00:00Z"))
        with self.store.connect() as db:
            db.executemany("INSERT INTO evidence VALUES (?,?,?,?)", [
                (f"new-{i}", "new-thread", "2026-10-03T00:00:00Z", json.dumps(dict(
                    id=f"new-{i}", thread_id="new-thread", kind="response", effort="high")))
                for i in range(3005)
            ])
        self.assertEqual(self.store.count_evidence(), 3006)
        self.assertEqual(len(self.store.all_evidence("thread-a")), 1)
        self.assertIsNotNone(self.store.session("thread-a"))
        self.assertIn("thread-a", {s["id"] for s in self.store.sessions()})
        first = self.store.evidence_page("new-thread", limit=12)
        second = self.store.evidence_page("new-thread", limit=12, offset=12)
        self.assertEqual(first["total"], 3005)
        self.assertEqual(len(first["items"]), 12)
        self.assertFalse({e["id"] for e in first["items"]} & {e["id"] for e in second["items"]})
        self.assertIsNotNone(self.store.evidence_item(first["items"][0]["id"], "new-thread"))
        self.assertIsNone(self.store.evidence_item(first["items"][0]["id"], "thread-a"))

    def test_cold_summaries_are_complete_and_keep_historical_difference(self):
        self.write_log()
        for name, response_effort in (("a", "low"), ("b", "high")):
            self.store.add(record("request", "high", name, turn_id=f"turn-{name}"))
            self.store.add(record("response", response_effort, name, turn_id=f"turn-{name}"))
        before = self.store.sessions()[0]
        self.assertEqual((before["turns"], before["tools"], before["summary_state"]), (2, 2, "ready"))
        self.assertEqual(before["latest_verdict"], "match")
        self.assertTrue(before["has_difference"])
        self.assertFalse(self.store.cache)
        self.store.session("thread-a")
        self.assertEqual(before, self.store.sessions()[0])

    def test_no_evidence_cold_summary_is_known_without_detail(self):
        self.write_log()
        summary = self.store.sessions()[0]
        self.assertEqual((summary["turns"], summary["tools"]), (2, 2))
        with patch("observatory.parse_summary", side_effect=AssertionError("unchanged file reparsed")):
            self.assertEqual(summary, self.store.sessions()[0])

    def test_summary_invalidates_when_log_or_evidence_changes(self):
        path = self.write_log()
        self.assertFalse(self.store.sessions()[0]["has_difference"])
        self.store.add(record("response", "low", turn_id="turn-a"))
        self.assertTrue(self.store.sessions()[0]["has_difference"])
        with path.open("a") as stream:
            stream.write('\n' + json.dumps({"type": "turn_context", "payload": {"turn_id": "turn-c"}}))
        self.assertEqual(self.store.sessions()[0]["turns"], 3)

    def test_unavailable_counts_are_not_false_zeroes(self):
        self.store.catalog["missing"] = dict(id="missing", path=str(self.root / "missing.jsonl"))
        summary = self.store.sessions()[0]
        self.assertIsNone(summary["turns"])
        self.assertIsNone(summary["tools"])
        self.assertEqual(summary["summary_state"], "unavailable")
        self.assertTrue(any(s["state"] == "unavailable" for s in self.store.source_statuses()))

    def test_archive_import_uses_output_id_and_deduplicates_previous_bad_record(self):
        path = self.write_log()
        directory = self.root / "archives" / "codex-app-capture-test"
        directory.mkdir(parents=True)
        archive = directory / "verified-result.json"
        archive.write_text(json.dumps(dict(thread_id="thread-a", session_file=str(path),
            turn_context=[dict(turn_id="turn-a"), dict(turn_id="turn-b")],
            response_evidence=[dict(verified_output_matches=[dict(id="msg-a")], reasoning=dict(effort="low"))])))
        self.store.import_archives()
        result = self.store.session("thread-a")
        self.assertEqual(len(result["turns"][0]["evidence"]), 1)
        self.assertFalse(result["turns"][1]["evidence"])
        self.assertIsNone(result["turns"][0]["evidence"][0]["turn_id"])
        self.store.import_archives()
        self.assertEqual(self.store.count_evidence(), 1)
        # A persisted pre-fix association stays byte-for-byte intact and is visibly conflicted.
        with self.store.connect() as db:
            row = db.execute("SELECT id,body FROM evidence").fetchone()
            old = json.loads(row[1])
            old["turn_id"] = "turn-b"
            db.execute("UPDATE evidence SET body=? WHERE id=?", (json.dumps(old), row[0]))
        self.store.import_archives()
        self.assertEqual(self.store.count_evidence(), 1)
        self.assertTrue(self.store.session("thread-a")["unmatched"][0]["association_conflict"])


if __name__ == "__main__":
    unittest.main()
