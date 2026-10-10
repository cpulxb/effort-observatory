#!/usr/bin/env python3
"""Launch an unmodified Codex with local, event-driven observability."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid

from event_capture import RAW_TYPES, NATIVE_TYPES, _usage
from observation_fields import safe_failure_fields, safe_session_metadata

ROOT = Path(__file__).resolve().parent
EVENT_ROOT = ROOT / "data" / "events"
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
MAX_LINE = 64 * 1024 * 1024
TEXT_TIMESTAMP = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d))\s")


def utcnow():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def extract_response(line):
    """Only response lifecycle events; never persist prompts/output text/headers."""
    text = ANSI.sub("", line)
    try:
        obj = json.loads(text)
        message = obj.get("fields", {}).get("message", "")
        stamp = obj.get("timestamp")
    except (ValueError, AttributeError):
        message, stamp = text, None
    if not isinstance(message, str):
        return None
    if not isinstance(stamp, str):
        match = TEXT_TIMESTAMP.match(text)
        stamp = match[1] if match else None
    marker = next((key for key in ("SSE event: ", "Received message ") if key in message), None)
    if not marker:
        return None
    try:
        event, _ = json.JSONDecoder().raw_decode(message.split(marker, 1)[1].lstrip())
    except ValueError:
        return None
    if not isinstance(event, dict) or event.get("type") not in RAW_TYPES:
        return None
    body = event.get("response")
    if not isinstance(body, dict) or not isinstance(body.get("id"), str) or not body["id"]:
        return None
    safe = {key: body[key] for key in ("id", "model", "status") if isinstance(body.get(key), str)}
    safe.update({key: body[key] for key in ("created_at", "completed_at")
                 if isinstance(body.get(key), (int, float)) and not isinstance(body[key], bool)})
    if isinstance(body.get("reasoning"), dict):
        effort = body["reasoning"].get("effort")
        safe["reasoning"] = {"effort": effort if isinstance(effort, str) else None}
    usage = _usage(body.get("usage"))
    if usage is not None:
        safe["usage"] = usage
    if event["type"] == "response.failed" or safe.get("status") == "failed":
        safe.update(safe_failure_fields(body))
    return {"timestamp": stamp or utcnow(), "transport": "sse" if marker.startswith("SSE") else "websocket",
            "event": {"type": event["type"], "response": safe}}


def forwardable_stderr(raw):
    """Never send raw inference TRACE messages to Electron's log subscriber."""
    text = ANSI.sub("", raw.decode("utf-8", errors="replace"))
    if "SSE event: " in text or "Received message " in text:
        return False
    try:
        level = json.loads(text).get("level", "")
        return not isinstance(level, str) or level.upper() != "TRACE"
    except (ValueError, AttributeError):
        return re.search(r"\bTRACE\b", text) is None


def receive_stderr(stream, output, recorder, errors):
    while True:
        line = stream.readline(MAX_LINE + 1)
        if not line:
            return
        if len(line) > MAX_LINE:
            # Discard the entire oversized line, including unmarked fragments.
            while line and not line.endswith(b"\n"):
                line = stream.readline(MAX_LINE + 1)
            errors.append("response log line exceeds local observation limit")
            continue
        try:
            recorder.line(line)
        except Exception as exc:
            errors.append(type(exc).__name__)
        if forwardable_stderr(line):
            try:
                output.write(line)
                output.flush()
            except BrokenPipeError:
                pass


def resolve_binary(client, explicit=None):
    if explicit:
        path = Path(explicit).resolve()
    elif client == "app":
        candidates = list(Path("/mnt/c/Users").glob("*/.codex/bin/wsl/*/codex"))
        candidates = [p for p in candidates if p.is_file() and re.fullmatch(r"[a-f0-9]{16}", p.parent.name)]
        if not candidates:
            raise RuntimeError("Codex App managed WSL binary was not found")
        path = max(candidates, key=lambda p: p.stat().st_mtime_ns)
    else:
        candidates = list(Path("/root/.nvm/versions/node").glob("*/lib/node_modules/@openai/codex/node_modules/@openai/codex-linux-x64/vendor/*/bin/codex"))
        if not candidates:
            raise RuntimeError("Codex CLI vendor binary was not found")
        path = max(candidates, key=lambda p: p.stat().st_mtime_ns)
    if not path.is_file() or not os.access(path, os.X_OK):
        raise RuntimeError("Codex executable is unavailable")
    return path


def cli_package_environment(binary, inherited):
    """Restore the package ownership markers set by the npm JS entrypoint.

    A custom executable must not acquire npm ownership merely because the
    observer was installed through a shell PATH entry.
    """
    env = dict(inherited)
    binary = Path(binary).resolve()
    package_root = None
    for candidate in binary.parents:
        if candidate.name != "codex" or candidate.parent.name != "@openai":
            continue
        try:
            package = json.loads((candidate / "package.json").read_text())
            relative = binary.relative_to(candidate).parts
            entrypoint = package.get("bin", {}).get("codex")
            if package.get("name") != "@openai/codex" or entrypoint != "bin/codex.js":
                continue
            if not (candidate / entrypoint).is_file():
                continue
            nested_vendor = (len(relative) == 7 and relative[:2] == ("node_modules", "@openai")
                             and relative[2].startswith("codex-") and relative[3] == "vendor"
                             and relative[5:] == ("bin", "codex")
                             and "@openai/" + relative[2] in package.get("optionalDependencies", {}))
            bundled_vendor = len(relative) == 4 and relative[0] == "vendor" and relative[2:] == ("bin", "codex")
            if not (nested_vendor or bundled_vendor):
                continue
            package_root = candidate
            break
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    if package_root is None:
        return env

    # Match the original entrypoint's ownership checks, not package.json's
    # packageManager field (which describes how upstream built the package).
    manager = "NPM"
    for ancestor in (package_root, *package_root.parents):
        if ancestor.name == "packages":
            try:
                metadata = json.loads((ancestor / "@openai" / "codex.json").read_text())
                install_id = metadata.get("installId") or ""
                install_dir = (ancestor / ("@openai/codex" + install_id) if install_id.startswith("#")
                               else ancestor / "@openai" / "codex" / install_id)
                if metadata.get("name") == "@openai/codex" and any(
                    (install_dir / suffix / "@openai" / "codex").resolve() == package_root
                    for suffix in ("lib/node_modules", "node_modules")
                ):
                    manager = "VITE_PLUS"
                    break
            except (OSError, ValueError, TypeError, AttributeError):
                pass
        modules = ancestor / "node_modules"
        if (modules / ".modules.yaml").is_file() and (modules / "@openai" / "codex").resolve() == package_root:
            manager = "PNPM"
            break
    else:
        if (re.search(r"\bbun/", env.get("npm_config_user_agent", ""))
                or "bun" in env.get("npm_execpath", "")
                or ".bun/install/global" in package_root.as_posix()):
            manager = "BUN"
    for name in ("NPM", "BUN", "PNPM", "VITE_PLUS"):
        env.pop("CODEX_MANAGED_BY_" + name, None)
    env["CODEX_MANAGED_PACKAGE_ROOT"] = str(package_root)
    env["CODEX_MANAGED_BY_" + manager] = "1"
    return env


def cli_invocation(arguments):
    """Identify transport options without interpreting prompt/option values."""
    value_options = {"-c", "--config", "--enable", "--disable", "--remote-auth-token-env",
                     "-i", "--image", "-m", "--model", "--local-provider", "-p", "--profile",
                     "-s", "--sandbox", "-C", "--cd", "--add-dir", "-a", "--ask-for-approval"}
    commands = {"agents", "exec", "e", "review", "login", "logout", "mcp", "plugin", "app-server",
                "remote-control", "completion", "update", "doctor", "sandbox", "debug", "apply", "a",
                "resume", "queue", "archive", "delete", "migrate-rollouts", "unarchive", "fork",
                "cloud", "exec-server", "features", "help"}
    command, remote, positional_seen, index = None, False, False, 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--":
            break
        if argument == "--remote":
            remote = True
            index += 2
            continue
        if argument.startswith("--remote="):
            remote = True
        elif argument in value_options:
            index += 2
            continue
        elif not argument.startswith("-"):
            if not positional_seen and argument in commands:
                command = argument
                # Other subcommands own their options; a literal --remote in
                # their arguments does not select the TUI's remote transport.
                if command not in {"agents", "resume", "fork"}:
                    break
            positional_seen = True
        index += 1
    return command, remote


def process_start(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def atomic_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


class Recorder:
    def __init__(self, run_dir, client, run_id):
        self.run_dir, self.client, self.run_id = run_dir, client, run_id
        self.lock = threading.Lock()
        self.seen = set()
        self.saved = 0

    def line(self, raw):
        event = extract_response(raw.decode("utf-8", errors="replace"))
        if not event:
            return
        # The same event can reach both file and stderr subscribers.
        fingerprint = json.dumps(event["event"], sort_keys=True)
        with self.lock:
            if fingerprint in self.seen:
                return
            self.seen.add(fingerprint)
            if len(self.seen) > 5000:
                self.seen.clear()
            event.update(client=self.client, run_id=self.run_id)
            with (self.run_dir / "raw-events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, ensure_ascii=False) + "\n")
            self.saved += 1


def compact_payloads(trace_root, offsets, processed):
    """Native trace has no fields-only switch. Reduce our dedicated spool in place.

    A trace event is flushed only after its referenced payload file is closed.
    Atomic replacement lets the collector read either complete version safely.
    This spool is owned by the observer, never the client's original rollout.
    """
    trace_root = Path(trace_root).resolve()
    advanced = 0
    for log in trace_root.rglob("trace.jsonl"):
        if not log.resolve().is_relative_to(trace_root):
            raise ValueError("native trace path leaves launcher-owned root")
        with log.open("rb") as stream:
            stream.seek(offsets.get(log, 0))
            for _ in range(1000):
                start = stream.tell()
                line = stream.readline(MAX_LINE + 1)
                if not line or not line.endswith(b"\n") or len(line) > MAX_LINE:
                    break
                event = json.loads(line)
                payload = event.get("payload", {})
                event_type = payload.get("type")
                def references(value):
                    if isinstance(value, dict):
                        if "raw_payload_id" in value and isinstance(value.get("path"), str):
                            yield value
                        for child in value.values():
                            yield from references(child)
                    elif isinstance(value, list):
                        for child in value:
                            yield from references(child)
                for ref in references(payload):
                    path = (log.parent / ref["path"]).resolve()
                    if Path(ref["path"]).is_absolute() or ".." in Path(ref["path"]).parts or not path.is_relative_to(log.parent.resolve()):
                        raise ValueError("native payload path leaves launcher-owned bundle")
                    if path in processed:
                        continue
                    if event_type in {"inference_started", "inference_completed", "inference_failed", "inference_cancelled"}:
                        if path.stat().st_size > MAX_LINE:
                            raise ValueError("native payload exceeded local observation limit")
                        content = json.loads(path.read_bytes())
                        if event_type == "inference_started":
                            safe = {k: content[k] for k in ("model",) if k in content}
                            if isinstance(content.get("reasoning"), dict):
                                safe["reasoning"] = {"effort": content["reasoning"].get("effort")}
                        else:
                            safe = {k: content[k] for k in ("response_id", "upstream_request_id") if isinstance(content.get(k), str)}
                            usage = _usage(content.get("token_usage"), native=True)
                            if usage is not None:
                                safe["token_usage"] = usage
                            if event_type == "inference_failed":
                                safe.update(safe_failure_fields(content, payload))
                    elif event_type == "thread_started" and ref == payload.get("metadata_payload"):
                        if path.stat().st_size > MAX_LINE:
                            raise ValueError("native metadata exceeded local observation limit")
                        safe = safe_session_metadata(json.loads(path.read_bytes()))
                    else:
                        safe = {"observation_omitted": True}
                    atomic_json(path, safe)
                    processed.add(path)
                offsets[log] = stream.tell()
                advanced += 1
    return advanced


def finalize_trace_logs(trace_root):
    """After child exit only, remove inline tool/code/chat fields from our spool."""
    trace_root = Path(trace_root).resolve()
    for log in trace_root.rglob("trace.jsonl"):
        if not log.resolve().is_relative_to(trace_root):
            raise ValueError("native trace path leaves launcher-owned root")
        temporary = log.with_suffix(".minimal.tmp")
        try:
            with log.open("rb") as stream, temporary.open("w", encoding="utf-8") as target:
                while line := stream.readline(MAX_LINE + 1):
                    if len(line) > MAX_LINE or not line.endswith(b"\n"):
                        raise ValueError("native trace ended with incomplete or oversized event")
                    event = json.loads(line)
                    payload = event.get("payload", {})
                    if payload.get("type") not in NATIVE_TYPES | {"thread_started"}:
                        continue
                    safe = {k: event[k] for k in ("schema_version", "seq", "wall_time_unix_ms", "rollout_id", "thread_id", "codex_turn_id") if k in event}
                    safe["payload"] = {k: payload[k] for k in ("type", "inference_call_id", "thread_id", "codex_turn_id",
                        "model", "response_id", "upstream_request_id", "request_payload", "response_payload", "partial_response_payload", "metadata_payload") if k in payload}
                    if payload.get("type") == "inference_failed":
                        safe["payload"].update(safe_failure_fields(payload))
                    target.write(json.dumps(safe, ensure_ascii=False) + "\n")
            temporary.replace(log)
        finally:
            temporary.unlink(missing_ok=True)


def collect_file_logs(log_dir, offsets, recorder):
    advanced = 0
    for path in log_dir.glob("*.log"):
        with path.open("rb") as stream:
            offset = offsets.get(path, 0)
            stream.seek(offset if path.stat().st_size >= offset else 0)
            for _ in range(1000):
                line = stream.readline(MAX_LINE + 1)
                if len(line) > MAX_LINE:
                    raise ValueError("response log line exceeds local observation limit")
                if not line or not line.endswith(b"\n"):
                    break
                recorder.line(line)
                offsets[path] = stream.tell()
                advanced += 1
    return advanced


def run(client, binary, arguments, event_root=EVENT_ROOT):
    env = cli_package_environment(binary, os.environ) if client == "cli" else dict(os.environ)
    command, remote = cli_invocation(arguments) if client == "cli" else (None, False)
    if client == "cli" and (remote or command == "agents"):
        target = "远程 app-server" if remote else "共享后台 app-server"
        print(f"[档位观测台] 此命令连接{target}，当前启动器无法采集该服务器的模型请求；命令将正常运行。",
              file=sys.stderr, flush=True)
        # Preserve remote/daemon behavior and never advertise this frontend as
        # an instrumented local inference process.
        return os.execve(str(binary), [str(binary), *arguments], env)
    client_label = "Codex App" if client == "app" else "Codex CLI"
    run_id = uuid.uuid4().hex
    os.umask(0o077)
    run_dir = Path(event_root) / client / run_id
    run_dir.mkdir(parents=True, mode=0o700)
    trace_root, log_dir = run_dir / "native", run_dir / "temporary-logs"
    trace_root.mkdir()
    log_dir.mkdir()
    env["CODEX_ROLLOUT_TRACE_ROOT"] = str(trace_root)
    env["RUST_LOG"] = "warn,codex_api::sse::responses=trace,tungstenite::protocol=trace"
    env["LOG_FORMAT"] = "json"
    args = [str(binary), "-c", "log_dir=" + json.dumps(str(log_dir)), *arguments]
    if client == "cli" and command in {None, "resume", "fork"} and "--no-daemon" not in arguments:
        args.insert(1, "--no-daemon")
    # TUI stderr stays attached to its terminal; native TUI tracing goes to log_dir.
    pipe_stderr = client == "app" or command in {"exec", "e", "app-server"}
    child = subprocess.Popen(args, stdin=None, stdout=None, stderr=subprocess.PIPE if pipe_stderr else None, env=env)
    manifest = dict(client=client_label, run_id=run_id, pid=child.pid,
                    process_start=process_start(child.pid), launcher_pid=os.getpid(),
                    executable=str(binary), started_at=utcnow(), active=True)
    manifest_path = run_dir / "launcher.json"
    atomic_json(manifest_path, manifest)
    recorder = Recorder(run_dir, client_label, run_id)
    errors = []
    reader = None
    if pipe_stderr:
        reader = threading.Thread(target=receive_stderr,
                                  args=(child.stderr, sys.stderr.buffer, recorder, errors), daemon=True)
        reader.start()
    def forward(signum, frame):
        if child.poll() is None:
            child.send_signal(signum)
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, forward)
    file_offsets, trace_offsets, processed = {}, {}, set()
    try:
        while True:
            try:
                collect_file_logs(log_dir, file_offsets, recorder)
                compact_payloads(trace_root, trace_offsets, processed)
            except Exception as exc:
                errors.append(type(exc).__name__)
            if errors:
                manifest["capture_error"] = errors[-1]
                atomic_json(manifest_path, manifest)
                errors.clear()
            if child.poll() is not None:
                break
            time.sleep(0.1)
    except KeyboardInterrupt:
        # Terminal SIGINT also reaches the child in the same foreground group.
        child.wait()
    finally:
        if reader:
            reader.join(timeout=3)
        try:
            # Child exit closes its writers. Drain every batch before cleanup.
            while collect_file_logs(log_dir, file_offsets, recorder):
                pass
            while compact_payloads(trace_root, trace_offsets, processed):
                pass
            finalize_trace_logs(trace_root)
        except Exception as exc:
            manifest["capture_error"] = type(exc).__name__
        if errors:
            manifest["capture_error"] = errors[-1]
        manifest.update(active=False, ended_at=utcnow(), exit_code=child.poll(), response_events=recorder.saved)
        atomic_json(manifest_path, manifest)
        # Only launcher-created temporary stderr logs; minimal events remain replayable.
        shutil.rmtree(log_dir)
    return child.returncode or 0


def instrumentation_status(root=EVENT_ROOT, proc_root=Path("/proc")):
    root, proc_root = Path(root), Path(proc_root)

    def start_ticks(pid):
        try:
            return (proc_root / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()[19]
        except (OSError, IndexError):
            return None

    clients = []
    seen_pids = set()
    for path in root.glob("*/*/launcher.json"):
        try:
            item = json.loads(path.read_text())
            if int(item["pid"]) in seen_pids:
                continue
            alive = bool(item.get("active") and item.get("process_start") and start_ticks(item["pid"]) == item["process_start"])
            if alive:
                clients.append({k: item.get(k) for k in ("client", "pid", "run_id", "started_at", "capture_error")} | {"active": True})
                seen_pids.add(int(item["pid"]))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue

    # The stock desktop starts its own WSL app-server. Its inherited environment
    # is the evidence of instrumentation, not a directory or setup manifest.
    expected_trace = (root / "app" / "desktop-native" / "native").resolve()
    try:
        boot_time = next(int(line.split()[1]) for line in (proc_root / "stat").read_text().splitlines()
                         if line.startswith("btime "))
        clock_ticks = os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError, StopIteration):
        boot_time, clock_ticks = None, None
    try:
        processes = list(proc_root.iterdir())
    except OSError:
        processes = []
    for process in processes:
        if not process.name.isdecimal() or int(process.name) in seen_pids:
            continue
        try:
            if Path(os.readlink(process / "exe")).name != "codex":
                continue
            arguments = (process / "cmdline").read_bytes().split(b"\0")
            if b"app-server" not in arguments:
                continue
            env = {}
            for field in (process / "environ").read_bytes().split(b"\0"):
                key, separator, value = field.partition(b"=")
                if separator and key in {b"CODEX_HOME", b"CODEX_ROLLOUT_TRACE_ROOT", b"RUST_LOG"}:
                    env[key.decode()] = value.decode("utf-8", errors="replace")
            if not re.fullmatch(r"/mnt/c/users/[^/]+/\.codex", env.get("CODEX_HOME", "").replace("\\", "/").rstrip("/").casefold()):
                continue
            trace = Path(env.get("CODEX_ROLLOUT_TRACE_ROOT", ""))
            if not trace.is_absolute() or trace.resolve() != expected_trace:
                continue
            if "codex_api::sse::responses=trace" not in {part.strip() for part in env.get("RUST_LOG", "").split(",")}:
                continue
            ticks = start_ticks(process.name)
            if ticks is None:
                continue
            started_at = (dt.datetime.fromtimestamp(boot_time + int(ticks) / clock_ticks, dt.timezone.utc).isoformat()
                          if boot_time is not None and clock_ticks else None)
            clients.append(dict(client="Codex App", pid=int(process.name), run_id="desktop-native",
                                process_start=ticks, started_at=started_at, active=True, mode="native_desktop"))
            seen_pids.add(int(process.name))
        except (OSError, ValueError, OverflowError):
            # Processes can exit while procfs is being inspected.
            continue
    active = {item["client"] for item in clients}
    result = dict(clients=clients, pending_clients=[c for c in ("Codex App", "Codex CLI") if c not in active],
                  configured_clients=[])
    for client, filename in (("Codex CLI", "cli-setup.json"), ("Codex App", "native-app-setup.json")):
        try:
            setup = json.loads((root.parent / filename).read_text())
            if setup.get("enabled") is True:
                result["configured_clients"].append(client)
                if client == "Codex App" and client not in active:
                    result["pending_reasons"] = {client: "app_restart_required" if setup.get("app_restart_required") is True
                                                else "instrumented_app_process_not_running"}
        except (OSError, ValueError, AttributeError):
            pass
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", choices=("app", "cli"), required=True)
    parser.add_argument("--binary")
    parser.add_argument("--event-root", default=str(EVENT_ROOT))
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    options = parser.parse_args()
    args = options.arguments
    if args[:1] == ["--"]: args = args[1:]
    binary = resolve_binary(options.client, options.binary)
    return run(options.client, binary, args, options.event_root)


if __name__ == "__main__":
    raise SystemExit(main())
