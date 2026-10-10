"""Passive process inspection and common HTTP request/response normalization."""

import datetime as dt
import json
import os
from pathlib import Path
import re
import threading
import time

from observatory import now, content_text, unix_time


def request_record(body, **extra):
    metadata = body.get("client_metadata") or {}
    sid = metadata.get("thread_id") or body.get("prompt_cache_key")
    if not sid:
        user = body.get("metadata", {}).get("user_id", "")
        try:
            sid = json.loads(user).get("session_id")
        except (ValueError, TypeError, AttributeError):
            pass
    reasoning = body.get("reasoning") or {}
    messages = body.get("input", [])
    if not isinstance(messages, list):
        messages = []
    latest = next(
        (
            content_text(x.get("content"))
            for x in reversed(messages)
            if isinstance(x, dict) and x.get("role") == "user"
        ),
        "",
    )
    return dict(
        kind="request",
        thread_id=sid,
        turn_id=metadata.get("turn_id"),
        model=body.get("model"),
        effort=reasoning.get("effort"),
        reasoning=reasoning,
        thinking=body.get("thinking"),
        user_message=latest[:3000],
        observed_at=now(),
        event_time=None,
        **extra,
    )


def response_record(body, **extra):
    reasoning = body.get("reasoning") or {}
    outputs = body.get("output") or []
    return dict(
        kind="response",
        thread_id=body.get("prompt_cache_key"),
        response_id=body.get("id"),
        model=body.get("model"),
        effort=reasoning.get("effort"),
        reasoning=reasoning,
        status=body.get("status"),
        usage=body.get("usage"),
        output_ids=[x["id"] for x in outputs if isinstance(x, dict) and x.get("id")],
        tool_calls=[
            dict(id=x.get("call_id"), name=x.get("name"))
            for x in outputs
            if x.get("type") in ("function_call", "custom_tool_call")
        ],
        observed_at=now(),
        event_time=unix_time(body.get("completed_at") or body.get("created_at")),
        **extra,
    )


def processes():
    result = []
    for path in Path("/proc").iterdir():
        if not path.name.isdigit():
            continue
        try:
            cmd = (path / "cmdline").read_bytes().split(b"\0")
            if (
                b"app-server" not in cmd
                or b"pid-update-loop" in cmd
                or b"codex" not in cmd[0]
            ):
                continue
            env = (path / "environ").read_bytes().split(b"\0")
            desktop = any(x.startswith(b"CODEX_HOME=/mnt/c/") for x in env)
            result.append(
                dict(
                    pid=int(path.name),
                    client="Codex App" if desktop else "Codex CLI",
                    exe=os.readlink(path / "exe"),
                )
            )
        except (OSError, ValueError):
            continue
    return result


def scan_process(process, store, stop_event, deadline):
    pid = process["pid"]
    count = 0
    reason = re.compile(rb'"reasoning"\s*:\s*\{[^{}]{0,250}\}')
    begin = re.compile(
        rb'\{\s*"(?:model"\s*:|type"\s*:\s*"response\.(?:created|completed|failed)"|id"\s*:\s*"resp_)'
    )
    with open(f"/proc/{pid}/mem", "rb", buffering=0) as memory:
        for line in Path(f"/proc/{pid}/maps").read_text().splitlines():
            parts = line.split()
            if parts[1] != "rw-p" or (
                len(parts) > 5 and not parts[5].startswith("[heap]")
            ):
                continue
            start, end = [int(x, 16) for x in parts[0].split("-")]
            for address in range(start, end, 4 * 1024 * 1024):
                if stop_event.is_set() or time.monotonic() > deadline:
                    return count
                try:
                    memory.seek(address)
                    data = memory.read(min(4 * 1024 * 1024 + 32768, end - address))
                except OSError:
                    continue
                for match in begin.finditer(data):
                    absolute = address + match.start()
                    try:
                        memory.seek(absolute)
                        raw = memory.read(min(4 * 1024 * 1024, end - absolute))
                        obj, _ = json.JSONDecoder().raw_decode(
                            raw.decode("utf-8", errors="replace")
                        )
                    except (ValueError, OSError):
                        continue
                    response = obj.get("response", obj)
                    if not isinstance(response, dict):
                        continue
                    common = dict(
                        source="process_memory",
                        pid=pid,
                        client=process["client"],
                        address=hex(absolute),
                    )
                    if response.get("object") == "response" and response.get(
                        "reasoning"
                    ):
                        record = response_record(response, **common)
                        record["event_type"] = obj.get("type")
                    elif (
                        isinstance(obj.get("input"), list)
                        and obj.get("model")
                        and obj.get("reasoning")
                    ):
                        record = request_record(obj, **common)
                    else:
                        continue
                    if record.get("thread_id"):
                        count += store.add(record)
                for match in reason.finditer(data):
                    # Require the adjacent cache key and output IDs. A lone effort string is not evidence.
                    before = data[max(0, match.start() - 32768) : match.start()]
                    sid = re.findall(
                        rb'"prompt_cache_key"\s*:\s*"([a-f0-9-]{36})"', before[-1200:]
                    )
                    outputs = re.findall(
                        rb'"id"\s*:\s*"((?:msg_|rs_|ctc_)[a-zA-Z0-9_-]+)"', before
                    )
                    if not sid or not outputs:
                        continue
                    try:
                        reasoning = json.loads(b"{" + match.group() + b"}")["reasoning"]
                    except ValueError:
                        continue
                    count += store.add(
                        dict(
                            kind="response_fragment",
                            source="process_memory_fragment",
                            pid=pid,
                            client=process["client"],
                            thread_id=sid[-1].decode(),
                            output_ids=[x.decode() for x in outputs[-12:]],
                            effort=reasoning.get("effort"),
                            reasoning=reasoning,
                            observed_at=now(),
                            event_time=None,
                            note="存活的响应 JSON 片段。只有输出 ID 匹配后才归属轮次；无法恢复完整响应事件。",
                        )
                    )
    return count


def start_monitor(store):
    with store.lock:
        if store.monitor["running"]:
            return
        store.stop_event.clear()
        store.monitor.update(
            running=True,
            error=None,
            started_at=now(),
            until=unix_time(time.time() + 120),
            saved=0,
            scans=0,
        )

    def worker():
        deadline = time.monotonic() + 120
        try:
            while not store.stop_event.is_set() and time.monotonic() < deadline:
                found = processes()
                store.monitor["processes"] = found
                for process in found:
                    try:
                        store.monitor["saved"] += scan_process(
                            process,
                            store,
                            store.stop_event,
                            min(deadline, time.monotonic() + 6),
                        )
                    except (OSError, ValueError) as error:
                        store.monitor["error"] = f"PID {process['pid']}: {error}"
                store.monitor["scans"] += 1
                store.stop_event.wait(3)
        finally:
            store.monitor["running"] = False

    threading.Thread(target=worker, daemon=True, name="capture-monitor").start()
