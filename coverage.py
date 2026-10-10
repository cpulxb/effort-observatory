"""Bounded, read-only audit of HTTP activity versus native capture metadata.

This module never creates evidence. Runtime settings and token counts are not
request/response fields, and a successful HTTP header is not a completed model
response. Only the latest HTTP activity per client is assessed.
"""

from __future__ import annotations

import copy
from contextlib import contextmanager
import datetime as dt
import json
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import urlsplit


CLIENTS = ("Codex App", "Codex CLI")
THREAD_LIMIT = 24
LOG_LIMIT = 512
HTTP_LIMIT = 16
LOOKBACK_SECONDS = 6 * 60 * 60
CACHE_SECONDS = 5
UTC = dt.timezone.utc


def _stamp(value):
    if isinstance(value, (float, int)):
        return float(value)
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=value.tzinfo or UTC).timestamp()
    if isinstance(value, str):
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.replace(tzinfo=parsed.tzinfo or UTC).timestamp()
        except ValueError:
            pass
    return None


def _iso(value):
    return dt.datetime.fromtimestamp(value, UTC).isoformat()


@contextmanager
def _read_db(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=0.3)
    db.row_factory = sqlite3.Row
    # A malformed/missing index must not turn a status request into a full scan.
    steps = [0]

    def bounded():
        steps[0] += 1
        return steps[0] > 100

    db.set_progress_handler(bounded, 10000)
    try:
        yield db
    finally:
        db.close()


def _local_path(value):
    if re.match(r"^[A-Za-z]:[\\/]", value):
        return Path("/mnt/" + value[0].lower() + "/" + value[3:].replace("\\", "/"))
    return Path(value)


def _client(originator, source):
    name = str(originator or "").lower()
    if name in ("codex-tui", "codex-cli", "codex_exec", "codex-exec", "codex_cli_rs"):
        return "Codex CLI"
    if name in ("codex desktop", "codex-desktop", "codex app", "codex-app"):
        return "Codex App"
    # source=vscode is also written by the managed TUI daemon; it is ambiguous.
    if not name and source in ("cli", "exec"):
        return "Codex CLI"
    return None


def _thread_client(row):
    found = _client(row.get("originator"), row.get("source"))
    if found:
        return found
    # Older schemas lack originator. Read at most one bounded metadata line.
    try:
        with _local_path(row.get("rollout_path") or "").open("rb") as stream:
            line = stream.readline(65537)
        if len(line) > 65536:
            return None
        item = json.loads(line)
        if item.get("type") == "session_meta":
            meta = item.get("payload", {})
            return _client(meta.get("originator"), meta.get("source"))
    except (OSError, ValueError, TypeError):
        pass
    return None


def _threads(path, cutoff):
    with _read_db(path) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(threads)")}
        optional = [name for name in ("source", "originator", "rollout_path") if name in columns]
        fields = ",".join(["id", "updated_at"] + optional)
        rows = db.execute(
            f"SELECT {fields} FROM threads WHERE updated_at>=? ORDER BY updated_at DESC LIMIT ?",
            (int(cutoff), THREAD_LIMIT),
        ).fetchall()
    return [dict(row) for row in rows]


def _http_activity(db, thread_id, cutoff):
    rows = db.execute(
        "SELECT * FROM (SELECT id,ts,ts_nanos,target,process_uuid,"
        "substr(feedback_log_body,1,6144) AS body FROM logs "
        "WHERE thread_id=? AND ts>=? ORDER BY ts DESC,ts_nanos DESC,id DESC LIMIT ?) "
        "WHERE target='codex_http_client::client' AND body LIKE '%Request completed%' "
        "ORDER BY ts DESC,ts_nanos DESC,id DESC LIMIT ?",
        (thread_id, int(cutoff), LOG_LIMIT, HTTP_LIMIT),
    ).fetchall()
    result = []
    for row in rows:
        body = row["body"] or ""
        match = re.search(r"Request completed method=POST url=(\S+) status=(\d{3})", body)
        if not match or not urlsplit(match[1].strip('"')).path.rstrip("/").endswith("/responses"):
            continue
        turn = re.search(r"(?:\bturn_id|\bturn\.id)=\"?([a-zA-Z0-9-]+)", body)
        result.append(dict(
            at=float(row["ts"]) + (row["ts_nanos"] or 0) / 1_000_000_000,
            thread_id=thread_id, turn_id=turn[1] if turn else None,
            process_uuid=row["process_uuid"], http_status=int(match[2]), log_id=row["id"],
        ))
    return result


def _captured_request(store, latest, previous_at, cutoff):
    # The thread/time index bounds this read; raw response events without an
    # attributed native request cannot certify request capture.
    with _read_db(store.db_path) as db:
        rows = db.execute(
            "SELECT substr(body,1,32768) FROM evidence WHERE thread_id=? "
            "AND observed_at>=? ORDER BY observed_at DESC LIMIT 256",
            (latest["thread_id"], _iso(cutoff)),
        ).fetchall()
    for (body,) in rows:
        try:
            record = json.loads(body)
        except (ValueError, TypeError):
            continue
        if record.get("source") != "codex_trace" or record.get("kind") != "request":
            continue
        if record.get("client") != latest["client"]:
            continue
        if latest["turn_id"] and record.get("turn_id") != latest["turn_id"]:
            continue
        # observed_at may be an import time. Native event_time must establish
        # that this is the current call, not an older capture from this thread.
        stamp = _stamp(record.get("event_time"))
        if stamp is not None and previous_at < stamp <= latest["at"] + 1:
            return True
    return False


def _native_activity(store, cutoff, current):
    """Read a fixed number of recently ingested records using the rowid index.

    The collector may receive native traces without verbose HTTP logs. Their
    request event time is already direct activity evidence, and must supersede
    an older HTTP-only gap. Import time and response-only records cannot do so.
    """
    with _read_db(store.db_path) as db:
        rows = db.execute(
            "SELECT substr(body,1,32768) FROM evidence ORDER BY rowid DESC LIMIT 512"
        ).fetchall()
    latest = {}
    for (body,) in rows:
        try:
            record = json.loads(body)
        except (ValueError, TypeError):
            continue
        client = record.get("client")
        stamp = _stamp(record.get("event_time"))
        if (record.get("source") != "codex_trace" or record.get("kind") != "request"
                or client not in CLIENTS or not record.get("thread_id")
                or stamp is None or not cutoff <= stamp <= current + 1):
            continue
        if stamp > latest.get(client, {}).get("at", 0):
            latest[client] = dict(at=stamp, thread_id=record["thread_id"],
                                  turn_id=record.get("turn_id"), capture_id=record.get("capture_id"))
    return latest


def coverage_status(store, *, source_paths=None, now=None, grace_seconds=10):
    """Return latest per-client HTTP/capture coverage; never mutate the sources.

    Explicit ``now`` disables caching and accepts Unix seconds or ISO time.
    ``observed`` certifies the latest activity has a native Request, not that
    Response effort was echoed. ``last_uncaptured_at`` can retain an earlier
    HTTP-only gap after a later request is successfully captured. ``unknown``
    includes the collector grace period.
    """
    paths = tuple(str(Path(p)) for p in (source_paths if source_paths is not None else store.source_paths))
    cache_key = (paths, float(grace_seconds))
    cached = getattr(store, "_coverage_status_cache", None)
    if now is None and cached and cached[0] == cache_key and time.monotonic() - cached[1] < CACHE_SECONDS:
        return copy.deepcopy(cached[2])
    current = time.time() if now is None else _stamp(now)
    if current is None:
        raise ValueError("now must be Unix seconds, datetime, or ISO timestamp")
    cutoff = current - LOOKBACK_SECONDS
    result = dict(checked_at=_iso(current), grace_seconds=grace_seconds, clients={
        name: dict(last_activity_at=None, thread_id=None, capture_state="unknown",
                   last_uncaptured_at=None, reason="近期有限日志中没有可核验的 HTTP 请求。")
        for name in CLIENTS
    })
    latest_by_client = {}
    issues = 0
    seen = set()
    for source in paths:
        try:
            threads = _threads(source, cutoff)
            logs_path = Path(source).with_name("logs_2.sqlite")
            with _read_db(logs_path) as db:
                for row in threads:
                    client = _thread_client(row)
                    key = (str(logs_path.resolve()), row["id"])
                    if not client or key in seen:
                        continue
                    seen.add(key)
                    events = _http_activity(db, row["id"], cutoff)
                    if not events:
                        continue
                    latest = dict(events[0], client=client)
                    # A previous call on the same thread sets the lower bound.
                    # This prevents one historical capture covering later calls.
                    previous = [event["at"] for event in events[1:]
                                if not latest["turn_id"] or event["turn_id"] == latest["turn_id"]]
                    latest["previous_at"] = previous[0] if previous else cutoff
                    latest["sampled_http_requests"] = sum(
                        event["turn_id"] == latest["turn_id"] and event["process_uuid"] == latest["process_uuid"]
                        for event in events
                    )
                    if latest["at"] > latest_by_client.get(client, {}).get("at", 0):
                        latest_by_client[client] = latest
        except (OSError, sqlite3.Error, ValueError):
            issues += 1
    for client, latest in latest_by_client.items():
        item = result["clients"][client]
        item.update(last_activity_at=_iso(latest["at"]), thread_id=latest["thread_id"],
                    turn_id=latest["turn_id"], sampled_http_requests=latest["sampled_http_requests"],
                    activity_source="http_response_headers", http_status=latest["http_status"])
        try:
            captured = _captured_request(store, latest, latest["previous_at"], cutoff)
        except (OSError, sqlite3.Error):
            item["reason"] = "发现 HTTP 请求，但采集记录暂时无法核验。"
            continue
        if captured:
            item.update(capture_state="observed", reason="最近 HTTP 调用已有对应原生 Request 记录；Response 字段以详情为准。")
        elif current - latest["at"] < grace_seconds:
            item["reason"] = "发现新 HTTP 请求，正在等待采集记录。"
        else:
            item.update(capture_state="unobserved", last_uncaptured_at=item["last_activity_at"],
                        last_uncaptured_thread_id=latest["thread_id"],
                        reason="发现实际 HTTP 请求，但没有对应原生 Request 记录，存在漏采。")
    try:
        native_activity = _native_activity(store, cutoff, current)
    except (OSError, sqlite3.Error):
        native_activity = {}
        issues += 1
    for client, native in native_activity.items():
        item = result["clients"][client]
        item["last_capture_at"] = _iso(native["at"])
        if native["at"] > (_stamp(item["last_activity_at"]) or 0):
            # Keep the previous HTTP gap as history, while describing the more
            # recent successful capture as the current state.
            for key in ("sampled_http_requests", "http_status"):
                item.pop(key, None)
            item.update(last_activity_at=_iso(native["at"]), thread_id=native["thread_id"],
                        turn_id=native["turn_id"], capture_id=native["capture_id"],
                        activity_source="codex_trace", capture_state="observed",
                        reason="最近请求已捕获原生 Request；Response 字段以详情为准。")
    result["source_read_errors"] = issues
    if now is None:
        store._coverage_status_cache = (cache_key, time.monotonic(), copy.deepcopy(result))
    return result
