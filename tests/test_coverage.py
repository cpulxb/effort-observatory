"""Coverage uses synthetic metadata stores, never credentials or chat bodies."""

import datetime as dt
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from coverage import coverage_status, _threads


BASE = dt.datetime(2026, 10, 7, 16, 32, tzinfo=dt.timezone.utc).timestamp()


def iso(seconds):
    return dt.datetime.fromtimestamp(seconds, dt.timezone.utc).isoformat()


class CoverageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "state_5.sqlite"
        self.logs = self.root / "logs_2.sqlite"
        self.evidence = self.root / "evidence.sqlite"
        with sqlite3.connect(self.state) as db:
            db.execute("CREATE TABLE threads(id TEXT PRIMARY KEY, updated_at INTEGER, originator TEXT, source TEXT, rollout_path TEXT)")
            db.execute("CREATE INDEX thread_time ON threads(updated_at DESC)")
        with sqlite3.connect(self.logs) as db:
            db.execute("CREATE TABLE logs(id INTEGER PRIMARY KEY, ts INTEGER, ts_nanos INTEGER, target TEXT, process_uuid TEXT, thread_id TEXT, feedback_log_body TEXT)")
            db.execute("CREATE INDEX thread_log_time ON logs(thread_id, ts DESC, ts_nanos DESC, id DESC)")
        with sqlite3.connect(self.evidence) as db:
            db.execute("CREATE TABLE evidence(id TEXT PRIMARY KEY, thread_id TEXT, observed_at TEXT, body TEXT)")
            db.execute("CREATE INDEX evidence_thread_time ON evidence(thread_id,observed_at)")
        self.store = SimpleNamespace(source_paths=[self.state], db_path=self.evidence)

    def thread(self, thread="cli", originator="codex-tui", source="vscode", updated=BASE + 14, rollout=""):
        with sqlite3.connect(self.state) as db:
            db.execute("INSERT INTO threads VALUES (?,?,?,?,?)", (thread, updated, originator, source, rollout))

    def http(self, seconds, thread="cli", turn="turn-cli", status=200, path="/v1/responses"):
        body = (f'session_loop{{thread_id={thread}}}:turn{{turn.id={turn} codex.turn.reasoning_effort=xhigh}}:'
                f'Request completed method=POST url=https://example.invalid{path} status={status} OK headers={{}}')
        with sqlite3.connect(self.logs) as db:
            db.execute("INSERT INTO logs(ts,ts_nanos,target,process_uuid,thread_id,feedback_log_body) VALUES (?,?,?,?,?,?)",
                       (int(seconds), round((seconds - int(seconds)) * 1e9), "codex_http_client::client", "pid:42:uuid", thread, body))

    def capture(self, seconds, thread="cli", turn="turn-cli", client="Codex CLI", **extra):
        value = dict(source="codex_trace", kind="request", client=client, thread_id=thread,
                     turn_id=turn, event_time=iso(seconds), observed_at=iso(seconds), **extra)
        with sqlite3.connect(self.evidence) as db:
            db.execute("INSERT INTO evidence VALUES (?,?,?,?)", (str(seconds), thread, value["observed_at"], json.dumps(value)))

    def status(self, current=BASE + 60):
        return coverage_status(self.store, now=current)

    def test_real_incident_shape_two_http_calls_are_unobserved(self):
        self.thread()
        self.http(BASE + 4.433319439)
        self.http(BASE + 10.060290216)
        item = self.status()["clients"]["Codex CLI"]
        self.assertEqual(item["capture_state"], "unobserved")
        self.assertEqual(item["sampled_http_requests"], 2)
        self.assertEqual(item["last_activity_at"], iso(BASE + 10.060290216))
        self.assertEqual(item["last_uncaptured_at"], item["last_activity_at"])
        self.assertEqual(item["thread_id"], "cli")
        self.assertNotIn("effort", json.dumps(item))
        with sqlite3.connect(self.evidence) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM evidence").fetchone()[0], 0)

    def test_old_capture_in_same_turn_does_not_cover_later_request(self):
        self.thread()
        self.http(BASE + 4)
        self.http(BASE + 10)
        self.capture(BASE)
        self.assertEqual(self.status()["clients"]["Codex CLI"]["capture_state"], "unobserved")
        self.capture(BASE + 6)
        self.assertEqual(self.status()["clients"]["Codex CLI"]["capture_state"], "observed")

    def test_old_turn_capture_does_not_cover_a_new_turn(self):
        self.thread()
        self.http(BASE + 10, turn="new-turn")
        self.capture(BASE, turn="old-turn")
        self.assertEqual(self.status()["clients"]["Codex CLI"]["capture_state"], "unobserved")

    def test_new_http_waits_for_collector_grace(self):
        self.thread()
        self.http(BASE + 10)
        item = self.status(BASE + 15)["clients"]["Codex CLI"]
        self.assertEqual(item["capture_state"], "unknown")
        self.assertIsNone(item["last_uncaptured_at"])
        self.assertEqual(self.status(BASE + 21)["clients"]["Codex CLI"]["capture_state"], "unobserved")

    def test_source_vscode_is_disambiguated_by_originator(self):
        self.thread()
        self.thread("app", originator="Codex Desktop", updated=BASE + 20)
        self.thread("other", originator="VS Code", updated=BASE + 30)
        self.http(BASE + 10)
        self.http(BASE + 20, "app", "app-turn")
        self.http(BASE + 30, "other", "other-turn")
        self.capture(BASE + 15, "app", "app-turn", "Codex App")
        clients = self.status()["clients"]
        self.assertEqual(clients["Codex CLI"]["capture_state"], "unobserved")
        self.assertEqual(clients["Codex App"]["capture_state"], "observed")
        self.assertEqual(clients["Codex App"]["thread_id"], "app")

    def test_codex_cli_rs_originator_is_cli(self):
        self.thread(originator="codex_cli_rs")
        self.http(BASE + 10)
        self.assertEqual(self.status()["clients"]["Codex CLI"]["capture_state"], "unobserved")

    def test_new_native_request_supersedes_http_gap_and_preserves_history(self):
        self.thread()
        self.http(BASE + 4)
        self.http(BASE + 10)
        self.capture(BASE + 40, "new-native-thread", "new-turn")
        item = self.status()["clients"]["Codex CLI"]
        self.assertEqual(item["capture_state"], "observed")
        self.assertEqual(item["last_activity_at"], iso(BASE + 40))
        self.assertEqual(item["thread_id"], "new-native-thread")
        self.assertEqual(item["activity_source"], "codex_trace")
        self.assertEqual(item["last_uncaptured_at"], iso(BASE + 10))
        self.assertEqual(item["last_uncaptured_thread_id"], "cli")
        self.assertNotIn("http_status", item)
        self.assertNotIn("sampled_http_requests", item)

    def test_native_activity_without_any_http_logging_is_observed(self):
        self.capture(BASE + 20)
        item = self.status()["clients"]["Codex CLI"]
        self.assertEqual(item["capture_state"], "observed")
        self.assertEqual(item["last_activity_at"], iso(BASE + 20))
        self.assertEqual(item["last_capture_at"], iso(BASE + 20))
        self.assertIsNone(item["last_uncaptured_at"])

    def test_newer_http_gap_is_not_hidden_by_older_native_activity(self):
        self.thread()
        self.http(BASE + 10)
        self.capture(BASE, turn="older-turn")
        item = self.status()["clients"]["Codex CLI"]
        self.assertEqual(item["capture_state"], "unobserved")
        self.assertEqual(item["last_activity_at"], iso(BASE + 10))
        self.assertEqual(item["last_capture_at"], iso(BASE))

    def test_latest_client_request_wins_over_old_missing_app_capture(self):
        self.thread("old", "Codex Desktop", updated=BASE)
        self.thread("new", "Codex Desktop", updated=BASE + 20)
        self.http(BASE, "old", "old-turn")
        self.http(BASE + 20, "new", "new-turn")
        self.capture(BASE + 15, "new", "new-turn", "Codex App")
        item = self.status()["clients"]["Codex App"]
        self.assertEqual(item["thread_id"], "new")
        self.assertEqual(item["capture_state"], "observed")
        self.assertIsNone(item["last_uncaptured_at"])

    def test_rollout_fallback_reads_only_session_metadata(self):
        path = self.root / "rollout.jsonl"
        path.write_text(json.dumps(dict(type="session_meta", payload=dict(originator="codex-tui"))) + "\n" + "private transcript" * 200000)
        self.thread(originator=None, rollout=str(path))
        self.http(BASE + 10)
        self.assertEqual(self.status()["clients"]["Codex CLI"]["capture_state"], "unobserved")

    def test_runtime_or_raw_response_records_do_not_certify_request(self):
        self.thread()
        self.http(BASE + 10)
        for i, source in enumerate(("runtime_log", "codex_event_log")):
            value = dict(source=source, kind="response", client="Codex CLI", thread_id="cli", turn_id="turn-cli", event_time=iso(BASE + i), effort="xhigh")
            with sqlite3.connect(self.evidence) as db:
                db.execute("INSERT INTO evidence VALUES (?,?,?,?)", (str(i), "cli", iso(BASE + i), json.dumps(value)))
        self.assertEqual(self.status()["clients"]["Codex CLI"]["capture_state"], "unobserved")

    def test_nonresponses_and_stale_activity_are_not_claimed(self):
        self.thread(updated=BASE + 14)
        self.http(BASE + 10, path="/api/models")
        self.http(BASE - 7 * 3600)
        self.assertEqual(self.status()["clients"]["Codex CLI"]["capture_state"], "unknown")

    def test_missing_database_is_unknown_and_is_not_created(self):
        missing = self.root / "missing" / "state_5.sqlite"
        status = coverage_status(self.store, source_paths=[missing], now=BASE + 60)
        self.assertEqual(status["source_read_errors"], 1)
        self.assertTrue(all(c["capture_state"] == "unknown" for c in status["clients"].values()))
        self.assertFalse(missing.exists())

    def test_bounded_thread_discovery(self):
        for i in range(50):
            self.thread(str(i), updated=BASE + i)
        self.assertEqual(len(_threads(self.state, BASE - 1)), 24)

    def test_cached_status_expires_and_caller_cannot_mutate_cache(self):
        self.thread()
        self.http(BASE + 10)
        with patch("coverage.time.time", return_value=BASE + 60), patch("coverage.time.monotonic", return_value=100):
            result = coverage_status(self.store)
            result["clients"]["Codex CLI"]["capture_state"] = "observed"
            self.assertEqual(coverage_status(self.store)["clients"]["Codex CLI"]["capture_state"], "unobserved")
            self.capture(BASE + 5)
            self.assertEqual(coverage_status(self.store)["clients"]["Codex CLI"]["capture_state"], "unobserved")
        with patch("coverage.time.time", return_value=BASE + 60), patch("coverage.time.monotonic", return_value=106):
            self.assertEqual(coverage_status(self.store)["clients"]["Codex CLI"]["capture_state"], "observed")


if __name__ == "__main__":
    unittest.main()
