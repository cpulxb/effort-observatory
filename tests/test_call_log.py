"""Call views use synthetic evidence and never query live Codex stores."""

import json
from pathlib import Path
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request

from call_log import call_rows, calls_page
from observatory import Observatory
from server import make_handler
from test_http import LocalServer


def record(identifier, kind="response", **fields):
    return dict(id=identifier, kind=kind, observed_at="2026-10-07T10:00:00Z", **fields)


class CallGroupingTests(unittest.TestCase):
    def test_same_turn_and_effort_do_not_pair_request_response(self):
        records = [record("req", "request", thread_id="s", turn_id="t", effort="high"),
                   record("res", thread_id="s", turn_id="t", effort="high")]
        rows = call_rows(records, {})
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["result"] == "pending" and not row["paired"] for row in rows))
        self.assertEqual({row["request"]["state"] for row in rows}, {"recorded", "not_captured"})

    def test_response_lifecycle_deduplicates_and_terminal_usage_is_not_summed(self):
        records = [
            record("created", response_id="resp-a", status="in_progress", effort="high",
                   event_time="2026-10-07T09:00:00Z", usage={"output_tokens": 1}),
            record("completed", response_id="resp-a", status="completed", effort="high",
                   event_time="2026-10-07T09:01:00Z", usage={"output_tokens": 10}),
            record("wrapped", response_id="resp-a", status="completed", effort="high",
                   event_type="response.completed", event_time="2026-10-07T09:01:00Z",
                   usage={"output_tokens": 10}),
        ]
        first_id = call_rows(records[:1], {})[0]["id"]
        row = call_rows(records, {})[0]
        self.assertEqual(row["id"], first_id)
        self.assertEqual(row["response"]["status"], "completed")
        self.assertEqual(row["response"]["usage"]["output_tokens"], 10)
        self.assertEqual(row["response"]["count"], 3)
        self.assertFalse(row["paired"])
        self.assertEqual(row["time_source"], "event_time")

    def test_terminal_without_usage_does_not_claim_interim_usage(self):
        row = call_rows([
            record("created", response_id="r", status="in_progress", usage={"output_tokens": 1}),
            record("failed", response_id="r", status="failed"),
        ], {})[0]
        self.assertEqual(row["response"]["status"], "failed")
        self.assertIsNone(row["response"]["usage"])

    def test_shared_capture_pairs_and_explicit_missing_effort_stays_missing(self):
        for response_effort, expected in (("high", "match"), ("low", "difference"), (None, "pending")):
            with self.subTest(effort=response_effort):
                row = call_rows([
                    record("req", "request", capture_id="c", effort="high"),
                    record("res", capture_id="c", effort=response_effort),
                ], {})[0]
                self.assertTrue(row["paired"])
                self.assertEqual(row["result"], expected)
                if response_effort is None:
                    self.assertEqual(row["response"]["state"], "not_echoed")

    def test_capture_identifier_is_available_in_list_and_detail(self):
        records = [record("req", "request", capture_id="capture-real"),
                   record("res", capture_id="capture-real")]
        self.assertEqual(call_rows(records, {})[0]["capture_id"], "capture-real")
        self.assertEqual(call_rows(records, {}, detail_id="capture:capture-real")["capture_id"], "capture-real")
        self.assertIsNone(call_rows([record("single", response_id="r")], {})[0]["capture_id"])

    def test_cancelled_terminal_survives_later_observation_of_in_progress(self):
        for usage in (None, {"output_tokens": 8}):
            with self.subTest(usage=usage):
                cancelled = record("cancelled", response_id="r", status="cancelled", usage=usage,
                                   event_time="2026-10-07T09:01:00Z")
                old = record("created-copy", response_id="r", status="in_progress", usage={"output_tokens": 1},
                             event_time="2026-10-07T09:00:00Z")
                old["observed_at"] = "2026-10-07T11:00:00Z"
                row = call_rows([cancelled, old], {})[0]
                self.assertEqual(row["response"]["status"], "cancelled")
                self.assertEqual(row["response"]["usage"], usage)

    def test_multiple_models_on_same_side_are_explicit_conflicts(self):
        for kind in ("request", "response"):
            with self.subTest(kind=kind):
                records = [record(kind + "-" + model, kind, capture_id="c", model=model, effort="high")
                           for model in ("model-a", "model-b")]
                other_kind = "response" if kind == "request" else "request"
                records.append(record("opposite", other_kind, capture_id="c", model="model-a", effort="high"))
                row = call_rows(records, {})[0]
                self.assertEqual(row[kind]["models"], ["model-a", "model-b"])
                self.assertIn(kind + "_model", row["conflicts"])
                self.assertEqual(row["result"], "difference")
                self.assertFalse(row["paired"])

    def test_conflicting_capture_metadata_is_not_a_match(self):
        row = call_rows([
            record("req", "request", capture_id="c", effort="high", thread_id="s1", client="Codex App"),
            record("res", capture_id="c", effort="high", thread_id="s2", client="Codex CLI"),
        ], {})[0]
        self.assertFalse(row["paired"])
        self.assertTrue(row["conflict"])
        self.assertEqual(row["client"], "unknown")
        self.assertEqual(set(row["conflicts"]), {"client", "thread_id"})
        self.assertEqual(row["result"], "difference")

    def test_changed_response_effort_is_preserved_as_conflicting_evidence(self):
        row = call_rows([
            record("created", response_id="r", status="in_progress", effort="high"),
            record("completed", response_id="r", status="completed", effort="low"),
        ], {}, detail_id="response:r")
        self.assertEqual(row["response"]["state"], "conflicting")
        self.assertEqual(row["response"]["efforts"], ["high", "low"])
        self.assertEqual(row["verdict"], "mixed")
        self.assertEqual({r["effort"] for r in row["response"]["evidence"]}, {"high", "low"})

    def test_fragments_without_response_id_are_independent_even_with_output_overlap(self):
        rows = call_rows([
            record("response", response_id="r", output_ids=["msg-a"]),
            record("fragment", "response_fragment", output_ids=["msg-a"]),
        ], {})
        self.assertEqual(len(rows), 2)

    def test_reused_response_id_never_merges_two_distinct_capture_ids(self):
        records = [record("req-" + c, "request", capture_id=c, effort="high") for c in ("a", "b")]
        records += [record("res-" + c, capture_id=c, response_id="same", effort="high") for c in ("a", "b")]
        records += [record("memory-copy", response_id="same", effort="high")]
        rows = call_rows(records, {})
        self.assertEqual(len(rows), 3)
        self.assertEqual(sum(row["paired"] for row in rows), 2)

    def test_duplicate_response_joins_single_capture_without_pairing_new_request(self):
        row = call_rows([
            record("req", "request", capture_id="c", thread_id="s", effort="high"),
            record("res", capture_id="c", thread_id="s", response_id="r", effort="high"),
            record("memory", thread_id="s", response_id="r", effort="high"),
        ], {})[0]
        self.assertTrue(row["paired"])
        self.assertEqual(row["observation_count"], 3)

    def test_unlinked_and_diagnostic_only_records_remain_visible(self):
        page = calls_page([record("unlinked", effort="high"), record("diag", "capture_diagnostic")], {})
        self.assertEqual(page["total"], 2)
        self.assertEqual(page["client_counts"]["unknown"], 2)
        self.assertEqual(page["stats"]["diagnostic_only"], 1)

    def test_statistics_ignore_result_but_apply_client_search_and_session(self):
        records = [record("req", "request", capture_id="c", thread_id="s", client="Codex App", effort="high"),
                   record("res", capture_id="c", thread_id="s", client="Codex App", effort="high"),
                   record("pending", thread_id="s", client="Codex App"),
                   record("cli", thread_id="s", client="Codex CLI")]
        page = calls_page(records, {"s": {"title": "Example"}}, client="Codex App", q="example",
                          thread_id="s", result="pending", limit=1)
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["stats"]["total"], 2)
        self.assertEqual(page["stats"]["match"], 1)
        self.assertEqual(page["client_counts"]["Codex CLI"], 1)

    def test_details_exclude_message_credentials_paths_and_nested_private_fields(self):
        records = [record("req", "request", user_message="private message canary", password="secret canary",
                          source_file="private path canary", reasoning={"effort": "high", "private": "reasoning canary"},
                          usage={"output_tokens": 5, "private": "usage canary"},
                          error={"message": "error body canary"})]
        row = call_rows(records, {}, detail_id="observation:req")
        content = json.dumps(row)
        self.assertNotIn("canary", content)
        self.assertEqual(row["request"]["evidence"][0]["usage"]["output_tokens"], 5)

    def test_native_summary_cannot_claim_response_effort_or_model(self):
        row = call_rows([
            record("request", "request", source="codex_trace", capture_id="c", effort="high", model="request-model"),
            record("native", source="codex_trace", capture_id="c", response_id="r", status="completed",
                   effort="high", model="request-model", effort_state="not_captured",
                   status_source="client_lifecycle"),
        ], {})[0]
        self.assertEqual(row["response"]["state"], "not_captured")
        self.assertIsNone(row["response"]["effort"])
        self.assertIsNone(row["response"]["model"])
        self.assertEqual(row["verdict"], "unknown")

    def test_raw_effort_is_authoritative_and_still_detects_request_mismatch(self):
        for effort, verdict in (("high", "match"), ("low", "mismatch"), (None, "unknown")):
            with self.subTest(effort=effort):
                row = call_rows([
                    record("request", "request", capture_id="c", effort="high", model="request-model"),
                    record("native", source="codex_trace", capture_id="c", response_id="r", status="completed",
                           effort="high", model="request-model", effort_state="not_captured"),
                    record("old-memory", source="process_memory", response_id="r", effort="medium", model="stale-model"),
                    record("wire", source="codex_event_log", response_id="r", status="completed", effort=effort,
                           model="actual-model", effort_state="recorded" if effort else "not_echoed"),
                ], {})[0]
                self.assertEqual(row["verdict"], verdict)
                self.assertEqual(row["response"]["effort"], effort)
                self.assertEqual(row["response"]["model"], "actual-model")
                self.assertEqual(row["conflicts"], [])
                self.assertNotIn("stale-model", row["models"])
                if effort is None:
                    self.assertEqual(row["response"]["state"], "not_echoed")

    def test_native_completed_and_raw_incomplete_use_wire_status_and_usage(self):
        records = [
            record("request", "request", capture_id="c", effort="high"),
            record("native", source="codex_trace", capture_id="c", response_id="r", status="completed",
                   status_source="client_lifecycle", usage={"output_tokens": 999}),
            record("wire", source="codex_event_log", response_id="r", status="incomplete", effort="high",
                   status_source="response.status", usage={"output_tokens": 8}),
        ]
        for ordered in (records, list(reversed(records))):
            with self.subTest(order=ordered[0]["id"]):
                row = call_rows(ordered, {})[0]
                self.assertEqual(row["response"]["status"], "incomplete")
                self.assertEqual(row["response"]["statuses"], ["incomplete"])
                self.assertEqual(row["response"]["usage"], {"output_tokens": 8})
                self.assertTrue(row["paired"])
                self.assertEqual(row["conflicts"], [])

    def test_wire_terminal_missing_usage_does_not_inherit_native_or_interim_counts(self):
        row = call_rows([
            record("native", source="codex_trace", capture_id="c", response_id="r", status="completed",
                   usage={"output_tokens": 99}),
            record("created", source="codex_event_log", response_id="r", status="in_progress",
                   usage={"output_tokens": 5}),
            record("wire", source="codex_event_log", response_id="r", status="completed"),
        ], {})[0]
        self.assertIsNone(row["response"]["usage"])

    def test_native_failure_survives_interim_wire_event(self):
        row = call_rows([
            record("native", source="codex_trace", capture_id="c", response_id="r", status="failed"),
            record("created", source="codex_event_log", response_id="r", status="in_progress", effort="high",
                   usage={"output_tokens": 5}),
        ], {})[0]
        self.assertEqual(row["response"]["status"], "failed")
        self.assertIsNone(row["response"]["usage"])
        self.assertEqual(row["response"]["effort"], "high")

    def test_conflicting_wire_status_and_effort_remain_visible(self):
        row = call_rows([
            record("created", source="codex_event_log", response_id="r", status="in_progress", effort="high"),
            record("completed", source="codex_event_log", response_id="r", status="completed", effort="low"),
            record("failed", source="codex_event_log", response_id="r", status="failed", effort="low"),
        ], {})[0]
        self.assertEqual(row["response"]["state"], "conflicting")
        self.assertEqual(row["response"]["status"], "conflicting")
        self.assertEqual(set(row["conflicts"]), {"response_effort", "response_status"})

    def test_event_provenance_ids_and_transport_are_available_in_detail(self):
        row = call_rows([
            record("native", source="codex_trace", capture_id="c", response_id="r", status="completed",
                   effort_state="not_captured", effort_source="unavailable_in_native_trace",
                   status_source="client_lifecycle", run_id="run", event_id="ev", inference_call_id="inf",
                   upstream_request_id="upstream", transport="sse", trace_id="trace", seq=4,
                   source_file="private canary", arbitrary_payload={"text": "private canary"}),
        ], {}, detail_id="capture:c")
        evidence = row["response"]["evidence"][0]
        self.assertEqual(evidence["effort_state"], "not_captured")
        self.assertEqual(evidence["upstream_request_id"], "upstream")
        self.assertEqual(evidence["transport"], "sse")
        self.assertEqual(evidence["inference_call_id"], "inf")
        self.assertNotIn("canary", json.dumps(row))


class CallAPITests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = Observatory(self.root / "data", source_paths=[], archive_root=self.root / "none",
                                 logs_path=self.root / "no-logs")
        self.store.add(record("request", "request", client="Codex App", effort="high"))
        self.store.add(record("response", response_id="r", client="Codex CLI", effort="low"))
        self.local = LocalServer(make_handler(self.store))
        self.local.__enter__()
        self.addCleanup(self.local.__exit__)
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def get(self, path, headers=None):
        with self.opener.open(urllib.request.Request(self.local.url + path, headers=headers or {}), timeout=5) as response:
            return json.load(response), response.headers

    def test_paging_filter_detail_and_etag(self):
        page, headers = self.get("/api/calls?limit=1")
        self.assertEqual(page["total"], 2)
        self.assertTrue(page["pagination"]["has_more"])
        with self.assertRaises(urllib.error.HTTPError) as failure:
            self.get("/api/calls?limit=1", {"If-None-Match": headers["ETag"]})
        self.assertEqual(failure.exception.code, 304)
        cli, _ = self.get("/api/calls?" + urllib.parse.urlencode({"client": "Codex CLI"}))
        self.assertEqual(cli["total"], 1)
        detail, _ = self.get("/api/call?id=response:r")
        self.assertEqual(len(detail["response"]["evidence"]), 1)
        self.assertEqual(detail["request"]["evidence"], [])

    def test_bad_filters_and_missing_call_are_rejected(self):
        for path in ("/api/calls?client=wrong", "/api/calls?result=wrong", "/api/calls?limit=0", "/api/call"):
            with self.subTest(path=path), self.assertRaises(urllib.error.HTTPError) as failure:
                self.get(path)
            self.assertEqual(failure.exception.code, 400)
        with self.assertRaises(urllib.error.HTTPError) as failure:
            self.get("/api/call?id=missing")
        self.assertEqual(failure.exception.code, 404)

    def test_explicit_rollout_identity_overrides_path_based_default_only_when_record_client_absent(self):
        path = self.root / "session.jsonl"
        path.write_text(json.dumps({"type": "session_meta", "payload": {"originator": "Codex Desktop"}}))
        self.store.catalog["s"] = dict(id="s", title="session", client="Codex CLI", path=str(path))
        self.store.add(record("unlabeled", thread_id="s"))
        rows = self.store.calls(thread_id="s")["items"]
        self.assertEqual(rows[0]["client"], "Codex App")
        self.assertEqual(rows[0]["client_state"], "session")
        self.store.catalog["unknown"] = dict(id="unknown", client="Codex App", path="")
        self.store.add(record("unknown-client", thread_id="unknown"))
        self.assertEqual(self.store.calls(thread_id="unknown")["items"][0]["client"], "unknown")


if __name__ == "__main__":
    unittest.main()
