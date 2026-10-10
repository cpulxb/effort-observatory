"""Launcher tests use generated local executables and synthetic logs only."""

import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

from call_log import call_rows
from event_capture import EventCollector
from observatory import Observatory
import observed_launcher as launcher


def response_event(response_id="resp-test"):
    return {"type": "response.completed", "response": {
        "id": response_id, "model": "model-returned", "status": "completed",
        "reasoning": {"effort": "low", "private": "CANARY_REASONING"},
        "usage": {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15,
                  "output_tokens_details": {"reasoning_tokens": 1, "private": "CANARY_USAGE"}},
        "output": [{"text": "CANARY_OUTPUT"}], "instructions": "CANARY_PROMPT",
        "headers": {"Authorization": "CANARY_AUTH"}}}


def log_line(event=None, *, structured=True, marker="SSE event: "):
    message = marker + json.dumps(event or response_event())
    if structured:
        return (json.dumps({"timestamp": "2026-10-07T10:11:12.123Z", "level": "TRACE",
                            "fields": {"message": message}}) + "\n").encode()
    return ("2026-10-07T10:11:12.123Z TRACE codex_api: " + message + "\n").encode()


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def npm_binary(self):
        package = self.root / "node-version" / "lib" / "node_modules" / "@openai" / "codex"
        (package / "bin").mkdir(parents=True)
        (package / "bin" / "codex.js").write_text("// Original package entrypoint\n")
        (package / "package.json").write_text(json.dumps({"name": "@openai/codex", "version": "999.0.1",
            "bin": {"codex": "bin/codex.js"}, "packageManager": "pnpm@999",
            "optionalDependencies": {"@openai/codex-linux-x64": "npm:@openai/codex@999.0.1-linux-x64"}}))
        binary = package / "node_modules" / "@openai" / "codex-linux-x64" / "vendor" / "test-target" / "bin" / "codex"
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o700)
        return package, binary

    def test_npm_vendor_restores_original_package_markers_without_version_guessing(self):
        package, binary = self.npm_binary()
        inherited = {"PATH": "/local/bin", "CODEX_MANAGED_BY_BUN": "1", "CODEX_MANAGED_BY_PNPM": "1",
                     "CODEX_MANAGED_BY_VITE_PLUS": "1", "CODEX_MANAGED_PACKAGE_ROOT": "/stale"}
        result = launcher.cli_package_environment(binary, inherited)
        self.assertEqual(result, {"PATH": "/local/bin", "CODEX_MANAGED_BY_NPM": "1",
                                 "CODEX_MANAGED_PACKAGE_ROOT": str(package)})
        self.assertEqual(inherited["CODEX_MANAGED_PACKAGE_ROOT"], "/stale")

    def test_custom_or_unverified_binary_keeps_inherited_environment(self):
        package, binary = self.npm_binary()
        inherited = {"CUSTOM": "unchanged"}
        self.assertEqual(launcher.cli_package_environment(self.root / "custom-codex", inherited), inherited)
        other = package / "other" / "codex"
        other.parent.mkdir()
        other.write_text("custom")
        self.assertEqual(launcher.cli_package_environment(other, inherited), inherited)
        (package / "package.json").write_text('{"name":"different"}')
        self.assertEqual(launcher.cli_package_environment(binary, inherited), inherited)

    def test_cli_package_manager_matches_bun_hint_and_pnpm_ownership(self):
        package, binary = self.npm_binary()
        result = launcher.cli_package_environment(binary, {"npm_config_user_agent": "bun/1.2.0"})
        self.assertEqual(result["CODEX_MANAGED_BY_BUN"], "1")
        self.assertNotIn("CODEX_MANAGED_BY_NPM", result)
        modules = package.parent.parent
        (modules / ".modules.yaml").write_text("{}")
        result = launcher.cli_package_environment(binary, {"npm_config_user_agent": "bun/1.2.0"})
        self.assertEqual(result["CODEX_MANAGED_BY_PNPM"], "1")
        self.assertNotIn("CODEX_MANAGED_BY_BUN", result)

    def test_cli_invocation_does_not_treat_option_values_or_prompts_as_commands(self):
        cases = [(["--remote", "unix://", "resume", "abc"], ("resume", True)),
                 (["resume", "abc", "--remote=ws://localhost:1234"], ("resume", True)),
                 (["-m", "agents", "--no-alt-screen", "hello"], (None, False)),
                 (["--config", 'label="agents"', "exec", "--", "--remote"], ("exec", False)),
                 (["--remote-auth-token-env", "agents", "hello"], (None, False)),
                 (["--", "agents", "--remote"], (None, False)),
                 (["Explain this", "agents"], (None, False)),
                 (["-c", "model=example", "agents"], ("agents", False)),
                 (["--model=agents", "hello"], (None, False))]
        for args, expected in cases:
            with self.subTest(args=args):
                self.assertEqual(launcher.cli_invocation(args), expected)

    def test_remote_and_agents_passthrough_preserve_arguments_without_active_manifest(self):
        package, binary = self.npm_binary()
        for args in (["--remote=unix://", "hello"], ["agents"], ["resume", "abc", "--remote", "unix://"]):
            with self.subTest(args=args), mock.patch.object(launcher.os, "execve", return_value=0) as execute, \
                    mock.patch.object(launcher.sys, "stderr", new_callable=io.StringIO) as stderr, \
                    mock.patch.dict(launcher.os.environ, {"PATH": "/local/bin"}, clear=True):
                self.assertEqual(launcher.run("cli", binary, args, self.root / "events"), 0)
                command, passed, env = execute.call_args.args
                self.assertEqual(command, str(binary))
                self.assertEqual(passed, [str(binary), *args])
                self.assertEqual(env["CODEX_MANAGED_PACKAGE_ROOT"], str(package))
                self.assertEqual(env["CODEX_MANAGED_BY_NPM"], "1")
                self.assertNotIn("CODEX_ROLLOUT_TRACE_ROOT", env)
                self.assertIn("无法采集", stderr.getvalue())
                self.assertFalse((self.root / "events").exists())

    def native_desktop_process(self, *, pid=123, env_changes=None, args=None, executable="codex"):
        proc_root = self.root / "proc"
        process = proc_root / str(pid)
        process.mkdir(parents=True)
        (proc_root / "stat").write_text("cpu 1 2 3\nbtime 1700000000\n")
        (process / "stat").write_text(f"{pid} (codex app) S " + "0 " * 18 + "12300 0\n")
        (process / "exe").symlink_to("/unused/bin/" + executable)
        arguments = args if args is not None else ["codex", "app-server", "--stdio"]
        (process / "cmdline").write_bytes(b"\0".join(arg.encode() for arg in arguments) + b"\0")
        env = {"CODEX_HOME": "/mnt/c/Users/tester/.codex",
               "CODEX_ROLLOUT_TRACE_ROOT": str(self.root / "events" / "app" / "desktop-native" / "native"),
               "RUST_LOG": "warn,codex_api::sse::responses=trace,tungstenite::protocol=trace",
               "SECRET_KEY": "CANARY_SECRET"}
        env.update(env_changes or {})
        (process / "environ").write_bytes(b"\0".join(f"{key}={value}".encode() for key, value in env.items()) + b"\0")
        return proc_root, process

    def test_native_desktop_is_active_only_from_matching_live_process_environment(self):
        proc_root, _ = self.native_desktop_process(env_changes={"CODEX_HOME": "/MNT/C/USERS/TESTER/.CODEX/"})
        with mock.patch.object(launcher.os, "sysconf", return_value=100):
            status = launcher.instrumentation_status(self.root / "events", proc_root)
        self.assertEqual(status["pending_clients"], ["Codex CLI"])
        self.assertEqual(status["clients"], [{"client": "Codex App", "pid": 123, "run_id": "desktop-native",
            "process_start": "12300", "started_at": "2023-11-14T22:15:23+00:00", "active": True, "mode": "native_desktop"}])
        self.assertNotIn("CANARY", json.dumps(status))
        self.assertNotIn("CODEX_HOME", json.dumps(status))

    def test_native_desktop_rejects_wrong_client_or_incomplete_environment(self):
        cases = [dict(env_changes={"CODEX_HOME": "/root/.codex"}),
                 dict(env_changes={"CODEX_ROLLOUT_TRACE_ROOT": str(self.root / "unrelated")}),
                 dict(env_changes={"CODEX_ROLLOUT_TRACE_ROOT": ""}),
                 dict(env_changes={"RUST_LOG": "warn"}),
                 dict(env_changes={"RUST_LOG": "warn,codex_api::sse::responses=trace_extra"}),
                 dict(args=["codex", "exec"]), dict(executable="other-program")]
        for number, options in enumerate(cases):
            self.native_desktop_process(pid=100 + number, **options)
        status = launcher.instrumentation_status(self.root / "events", self.root / "proc")
        self.assertEqual(status["clients"], [])
        self.assertIn("Codex App", status["pending_clients"])

    def test_native_desktop_setup_and_spool_directory_do_not_mean_active(self):
        native = self.root / "events" / "app" / "desktop-native" / "native"
        native.mkdir(parents=True)
        setup = self.root / "native-app-setup.json"
        setup.write_text(json.dumps({"enabled": True, "app_restart_required": True}))
        # The currently running desktop has not inherited the new trace settings.
        proc_root, _ = self.native_desktop_process(env_changes={"CODEX_ROLLOUT_TRACE_ROOT": ""})
        status = launcher.instrumentation_status(self.root / "events", proc_root)
        self.assertEqual(status["clients"], [])
        self.assertEqual(status["pending_reasons"], {"Codex App": "app_restart_required"})
        setup.write_text(json.dumps({"enabled": True, "app_restart_required": False}))
        status = launcher.instrumentation_status(self.root / "events", proc_root)
        self.assertEqual(status["pending_reasons"], {"Codex App": "instrumented_app_process_not_running"})

    def test_native_desktop_and_launcher_manifest_are_deduplicated_by_pid(self):
        proc_root, _ = self.native_desktop_process()
        manifest = {"client": "Codex App", "pid": 123, "run_id": "observed-launcher",
                    "process_start": "12300", "active": True}
        for run_id in ("observed-launcher", "duplicate"):
            path = self.root / "events" / "app" / run_id / "launcher.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(manifest))
        status = launcher.instrumentation_status(self.root / "events", proc_root)
        self.assertEqual(len(status["clients"]), 1)
        self.assertEqual(status["clients"][0]["pid"], 123)
        self.assertEqual(status["clients"][0]["run_id"], "observed-launcher")

    def test_native_desktop_tolerates_exited_and_unreadable_processes(self):
        proc_root, process = self.native_desktop_process()
        (process / "environ").unlink()
        status = launcher.instrumentation_status(self.root / "events", proc_root)
        self.assertEqual(status["clients"], [])
        self.native_desktop_process(pid=124)
        (proc_root / "124" / "stat").unlink()
        status = launcher.instrumentation_status(self.root / "events", proc_root)
        self.assertEqual(status["clients"], [])

    def test_stale_launcher_pid_does_not_mark_app_active(self):
        proc_root, _ = self.native_desktop_process(env_changes={"RUST_LOG": "warn"})
        manifest = self.root / "events" / "app" / "old-launcher" / "launcher.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"client": "Codex App", "pid": 123, "process_start": "999", "active": True}))
        self.assertEqual(launcher.instrumentation_status(self.root / "events", proc_root)["clients"], [])

    def test_configured_clients_are_distinct_from_active_processes(self):
        (self.root / "cli-setup.json").write_text(json.dumps({"enabled": True, "mode": "shell_path"}))
        (self.root / "native-app-setup.json").write_text(json.dumps({"enabled": True, "app_restart_required": True}))
        status = launcher.instrumentation_status(self.root / "events", self.root / "missing-proc")
        self.assertEqual(status["configured_clients"], ["Codex CLI", "Codex App"])
        self.assertEqual(status["clients"], [])
        self.assertEqual(status["pending_clients"], ["Codex App", "Codex CLI"])

    def test_disabled_and_invalid_setup_do_not_mark_configured(self):
        (self.root / "cli-setup.json").write_text(json.dumps({"enabled": "true"}))
        (self.root / "native-app-setup.json").write_text("invalid json")
        status = launcher.instrumentation_status(self.root / "events", self.root / "missing-proc")
        self.assertEqual(status["configured_clients"], [])
        self.assertEqual(status["clients"], [])

    def test_structured_and_plain_sse_and_websocket_are_minimal(self):
        for structured in (True, False):
            for marker, transport in (("SSE event: ", "sse"), ("Received message ", "websocket")):
                with self.subTest(structured=structured, marker=marker):
                    result = launcher.extract_response(log_line(structured=structured, marker=marker).decode())
                    self.assertEqual(result["timestamp"], "2026-10-07T10:11:12.123Z")
                    self.assertEqual(result["transport"], transport)
                    self.assertEqual(result["event"]["response"]["reasoning"], {"effort": "low"})
                    self.assertNotIn("CANARY", json.dumps(result))

    def test_non_response_malformed_and_non_string_messages_are_ignored(self):
        inputs = ['{"fields":{"message":null}}', '{"fields":{"message":[]}}', '[]',
                  'TRACE SSE event: {"type":"response.output_text.delta","delta":"CANARY"}',
                  'TRACE SSE event: {"type":"response.completed","response":{}}',
                  "TRACE SSE event: broken"]
        for raw in inputs:
            self.assertIsNone(launcher.extract_response(raw))

    def test_stderr_trace_and_long_fragments_never_reach_desktop(self):
        recorder = launcher.Recorder(self.root, "Codex App", "run")
        output, errors = io.BytesIO(), []
        content = log_line() + b"normal diagnostic\n" + b"TRACE " + b"CANARY" * 100 + b"\nfinal diagnostic\n"
        with mock.patch.object(launcher, "MAX_LINE", 256):
            launcher.receive_stderr(io.BytesIO(content), output, recorder, errors)
        self.assertEqual(output.getvalue(), b"normal diagnostic\nfinal diagnostic\n")
        self.assertTrue(errors)

    def test_recorder_deduplicates_file_and_stderr_copy(self):
        recorder = launcher.Recorder(self.root, "Codex App", "run")
        recorder.line(log_line())
        recorder.line(log_line(structured=False))
        events = (self.root / "raw-events.jsonl").read_text().splitlines()
        self.assertEqual(len(events), 1)
        self.assertEqual(json.loads(events[0])["client"], "Codex App")
        self.assertNotIn("CANARY", events[0])

    def make_bundle(self):
        bundle = self.root / "native" / "bundle"
        (bundle / "payloads").mkdir(parents=True)
        def payload(number, body):
            path = bundle / "payloads" / f"{number}.json"
            path.write_text(json.dumps(body))
            return {"path": f"payloads/{number}.json", "raw_payload_id": f"raw:{number}", "kind": {"type": "inference_request"}}
        req = payload(1, {"model": "model-requested", "reasoning": {"effort": "high"},
                          "input": [{"text": "CANARY_PROMPT"}], "authorization": "CANARY_AUTH"})
        res = payload(2, {"response_id": "resp-test", "upstream_request_id": "req-upstream",
                          "token_usage": {"output_tokens": 3, "reasoning_output_tokens": 1, "private": "CANARY_USAGE"},
                          "output_items": [{"text": "CANARY_OUTPUT"}]})
        tool = payload(3, {"text": "CANARY_TOOL"})
        def event(seq, kind, **fields):
            return {"schema_version": 1, "seq": seq, "wall_time_unix_ms": 1791367872123 + seq,
                    "rollout_id": "rollout", "thread_id": "thread", "codex_turn_id": "turn",
                    "payload": {"type": kind, "inference_call_id": "call", **fields}}
        events = [event(1, "inference_started", request_payload=req),
                  event(2, "code_cell_started", source_js="CANARY_INLINE_CODE"),
                  event(3, "tool_call_ended", result_payload=tool),
                  event(4, "inference_completed", response_id="resp-test", response_payload=res)]
        trace = bundle / "trace.jsonl"
        trace.write_text("".join(json.dumps(e) + "\n" for e in events))
        return bundle, trace, events

    def test_compaction_only_touches_closed_references_and_finalization_strips_inline(self):
        bundle, trace, _ = self.make_bundle()
        unreferenced = bundle / "payloads" / "not-yet-referenced.json"
        unreferenced.write_text('{"still_writing":"CANARY_UNFINISHED"}')
        original_rollout = self.root / ".codex" / "session.jsonl"
        original_rollout.parent.mkdir()
        original_rollout.write_text("USER ORIGINAL ROLLOUT")
        original_trace = trace.read_bytes()
        offsets, processed = {}, set()
        self.assertEqual(launcher.compact_payloads(bundle.parent, offsets, processed), 4)
        self.assertEqual(trace.read_bytes(), original_trace)
        self.assertIn("CANARY_UNFINISHED", unreferenced.read_text())
        self.assertEqual(original_rollout.read_text(), "USER ORIGINAL ROLLOUT")
        self.assertNotIn("CANARY", (bundle / "payloads" / "2.json").read_text())
        self.assertEqual(json.loads((bundle / "payloads" / "2.json").read_text())["upstream_request_id"], "req-upstream")
        launcher.finalize_trace_logs(bundle.parent)
        self.assertNotIn("CANARY", trace.read_text())
        self.assertEqual(len(trace.read_text().splitlines()), 2)

    def test_partial_native_event_does_not_trigger_payload_rewrite(self):
        bundle, trace, events = self.make_bundle()
        trace.write_text(json.dumps(events[0]))
        before = (bundle / "payloads" / "1.json").read_bytes()
        self.assertEqual(launcher.compact_payloads(bundle.parent, {}, set()), 0)
        self.assertEqual((bundle / "payloads" / "1.json").read_bytes(), before)

    def test_failure_and_thread_metadata_survive_safe_compaction_and_finalize(self):
        bundle, trace, events = self.make_bundle()
        metadata_path = bundle / "payloads" / "metadata.json"
        metadata_path.write_text(json.dumps({"session_source": {"internal": "memory_consolidation"},
            "thread_source": "user", "instructions": "CANARY_INSTRUCTIONS", "cwd": "CANARY_CWD",
            "tools": ["CANARY_TOOL"]}))
        metadata = {"path": "payloads/metadata.json", "raw_payload_id": "metadata:1", "kind": {"type": "session_metadata"}}
        thread_event = {"schema_version": 1, "seq": 0, "thread_id": "thread",
                        "payload": {"type": "thread_started", "metadata_payload": metadata, "agent_path": "CANARY_AGENT_PATH"}}
        failure = events[-1]
        failure["payload"].update(type="inference_failed", error="Unexpected status code 404: model CANARY_MODEL is not supported")
        response_path = bundle / "payloads" / "2.json"
        response = json.loads(response_path.read_text())
        response["error"] = {"code": "model_not_supported", "message": "CANARY_SERVER_ERROR"}
        response_path.write_text(json.dumps(response))
        trace.write_text("".join(json.dumps(e) + "\n" for e in [thread_event, *events]))
        launcher.compact_payloads(bundle.parent, {}, set())
        reduced_metadata = json.loads(metadata_path.read_text())
        self.assertEqual(reduced_metadata, {"session_source": {"internal": "memory_consolidation"}, "thread_source": "user"})
        self.assertNotIn("CANARY", response_path.read_text())
        self.assertEqual(json.loads(response_path.read_text())["error_code"], "model_not_supported")
        launcher.finalize_trace_logs(bundle.parent)
        reduced = [json.loads(line) for line in trace.read_text().splitlines()]
        self.assertEqual(len(reduced), 3)
        self.assertEqual(reduced[0]["payload"]["metadata_payload"], metadata)
        self.assertEqual(reduced[-1]["payload"]["error_code"], "model_not_supported")
        self.assertEqual(reduced[-1]["payload"]["http_status"], 404)
        self.assertNotIn("CANARY", trace.read_text())
        before = trace.read_bytes()
        launcher.finalize_trace_logs(bundle.parent)
        self.assertEqual(trace.read_bytes(), before)

    def test_raw_failure_extract_retains_safe_reason_without_server_text(self):
        event = {"type": "response.failed", "response": {"id": "resp-failed", "status": "failed",
            "error": {"code": "model_not_found", "message": "CANARY_API_KEY model missing", "status_code": 404},
            "output": ["CANARY_OUTPUT"]}}
        result = launcher.extract_response("TRACE SSE event: " + json.dumps(event))
        self.assertEqual(result["event"]["response"]["error_code"], "model_not_found")
        self.assertEqual(result["event"]["response"]["http_status"], 404)
        self.assertNotIn("CANARY", json.dumps(result))

    def test_compaction_rejects_external_trace_or_payload_symlinks(self):
        bundle, trace, events = self.make_bundle()
        outside = self.root / "outside.json"
        outside.write_text('{"private":"CANARY_OUTSIDE"}')
        (bundle / "payloads" / "1.json").unlink()
        (bundle / "payloads" / "1.json").symlink_to(outside)
        with self.assertRaises(ValueError):
            launcher.compact_payloads(bundle.parent, {}, set())
        self.assertIn("CANARY_OUTSIDE", outside.read_text())
        trace.unlink()
        external_trace = self.root / "outside-trace.jsonl"
        external_trace.write_text(json.dumps(events[0]) + "\n")
        trace.symlink_to(external_trace)
        with self.assertRaises(ValueError):
            launcher.compact_payloads(bundle.parent, {}, set())

    def test_raw_to_collector_to_call_rows_pairs_exact_ids(self):
        bundle, _, _ = self.make_bundle()
        event_root = self.root / "events"
        run = event_root / "app" / "run"
        run.mkdir(parents=True)
        import shutil
        shutil.move(str(bundle.parent), run / "native")
        recorder = launcher.Recorder(run, "Codex App", "run")
        recorder.line(log_line())
        launcher.compact_payloads(run / "native", {}, set())
        launcher.finalize_trace_logs(run / "native")
        store = Observatory(self.root / "db", source_paths=[], archive_root=self.root / "none")
        collector = EventCollector(store, event_root)
        self.addCleanup(collector.close)
        collector.poll_once()
        rows = call_rows(store.all_evidence(), {})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["verdict"], "mismatch")
        records = store.all_evidence()
        self.assertEqual(next(r for r in records if r["kind"] == "request")["inference_call_id"], "call")
        self.assertEqual(next(r for r in records if r["source"] == "codex_trace" and r["kind"] == "response")["upstream_request_id"], "req-upstream")
        self.assertEqual(next(r for r in records if r["source"] == "codex_event_log")["transport"], "sse")
        self.assertNotIn("CANARY", json.dumps(records))

    def test_mock_process_keeps_stdin_stdout_exit_code_and_drains_final_logs(self):
        binary = self.root / "fake-codex"
        binary.write_text("#!" + sys.executable + "\n" + textwrap.dedent("""
            import json, os, pathlib, sys
            arguments = sys.argv[1:]
            log_dir = pathlib.Path(json.loads(next(a.split('=',1)[1] for a in arguments if a.startswith('log_dir='))))
            event = {'type':'response.completed','response':{'id':'stderr-response','status':'completed','reasoning':{'effort':'low'},'output':[{'text':'CANARY_OUTPUT'}]}}
            sys.stderr.write(json.dumps({'level':'TRACE','fields':{'message':'SSE event: '+json.dumps(event)}})+'\\n')
            sys.stderr.write('normal diagnostic\\n')
            with (log_dir/'codex-tui.log').open('w') as stream:
                for index in range(2105):
                    event['response']['id'] = 'file-response-'+str(index)
                    stream.write('TRACE SSE event: '+json.dumps(event)+'\\n')
            sys.stdout.buffer.write(sys.stdin.buffer.read())
            sys.exit(7)
        """))
        binary.chmod(0o700)
        payload = b'{"jsonrpc":"2.0","id":1}\n\x00binary\n'
        result = subprocess.run([sys.executable, str(Path(launcher.__file__).resolve()), "--client", "app",
                                 "--binary", str(binary), "--event-root", str(self.root / "events"),
                                 "--", "app-server"], input=payload, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 7, result.stderr.decode(errors="replace"))
        self.assertEqual(result.stdout, payload)
        self.assertEqual(result.stderr, b"normal diagnostic\n")
        run = next((self.root / "events" / "app").iterdir())
        self.assertEqual(len((run / "raw-events.jsonl").read_text().splitlines()), 2106)
        self.assertFalse((run / "temporary-logs").exists())
        self.assertFalse(json.loads((run / "launcher.json").read_text())["active"])
        self.assertNotIn("CANARY", (run / "raw-events.jsonl").read_text())


if __name__ == "__main__":
    unittest.main()
