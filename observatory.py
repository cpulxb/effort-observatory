"""Local, read-only session inspection. No model credentials are loaded."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path

ROOT = Path(__file__).parent
DBS = [
    Path("/root/.codex/sqlite/state_5.sqlite"),
    Path("/root/.codex/state_5.sqlite"),
    *sorted(Path("/mnt/c/Users").glob("*/.codex/state_5.sqlite")),
]
from sanitization import SECRET, redact


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def content_text(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        x.get("text", "")
        for x in content
        if isinstance(x, dict)
        and x.get("type") in ("text", "input_text", "output_text")
    )


def unix_time(value):
    if isinstance(value, (float, int)):
        return dt.datetime.fromtimestamp(
            value / 1000 if value > 1e12 else value, dt.timezone.utc
        ).isoformat()
    return value


def verdict(client, request, response):
    # Never substitute client settings for a captured outgoing request.
    if request and response:
        if request != response:
            return "mismatch"
        if client and client != request:
            return "client_mismatch"
        return "match"
    if client and request and client != request:
        return "client_request_diff"
    if client and response and client != response:
        return "runtime_diff"
    return "unknown"


def windows_path(path):
    if re.match(r"^[A-Za-z]:[\\/]", path):
        return Path("/mnt/" + path[0].lower() + "/" + path[3:].replace("\\", "/"))
    return Path(path)


def parse_session(path, fallback=None):
    from session_parser import SessionParser

    return SessionParser(path, fallback, redact).parse()


def parse_summary(path, fallback=None):
    """Read the same turn protocol without retaining conversation/tool bodies."""
    from session_parser import SessionParser

    class SummaryParser(SessionParser):
        def consume_message(self, item, event, line_number):
            pass

        def record_tool_call(self, part, timestamp, line_number):
            self.current_turn["tools"].append({"id": part.get("call_id")})

        def record_tool_result(self, part, timestamp):
            pass

    return SummaryParser(path, fallback, redact).parse()


DIFFERENCES = {"mismatch", "client_mismatch", "client_request_diff", "runtime_diff"}


def effort_state(records, missing):
    values = sorted({e["effort"] for e in records if e.get("effort")})
    state = "not_captured" if not records else (
        "conflicting" if len(values) > 1 else "recorded" if values else missing
    )
    return values, state


def summarize_turn(turn):
    groups = {}
    for index, record in enumerate(turn["evidence"]):
        # Two observations without a call identifier must never become a pair.
        capture_id = record.get("capture_id")
        key = ("capture", capture_id) if capture_id else ("evidence", record.get("id") or index)
        groups.setdefault(key, []).append(record)
    calls = []
    for (kind, identifier), records in groups.items():
        requests = [e for e in records if e.get("kind") == "request"]
        responses = [e for e in records if e.get("kind") in ("response", "response_fragment")]
        req, request_state = effort_state(requests, "not_carried")
        res, response_state = effort_state(responses, "not_echoed")
        result = "mixed" if len(req) > 1 or len(res) > 1 else verdict(
            turn.get("client_effort"), req[0] if req else None, res[0] if res else None
        )
        calls.append(dict(
            id=f"{kind}:{identifier}", capture_id=identifier if kind == "capture" else None,
            request_effort=req[0] if len(req) == 1 else None,
            response_effort=res[0] if len(res) == 1 else None,
            request_state=request_state, response_state=response_state,
            verdict=result, paired=kind == "capture" and bool(requests and responses),
            evidence_ids=[e["id"] for e in records if e.get("id")],
            has_difference=result in DIFFERENCES or result == "mixed",
        ))
    req, _ = effort_state([e for e in turn["evidence"] if e.get("kind") == "request"], "not_carried")
    res, _ = effort_state([e for e in turn["evidence"] if e.get("kind") in ("response", "response_fragment")], "not_echoed")
    results = {call["verdict"] for call in calls}
    result = "unknown"
    if len(req) > 1 or len(res) > 1 or "mixed" in results:
        result = "mixed"
    else:
        for candidate in ("mismatch", "client_mismatch", "client_request_diff", "runtime_diff"):
            if candidate in results:
                result = candidate
                break
        else:
            if results == {"match"}:
                result = "match"
    turn.update(calls=calls, request_efforts=req, response_efforts=res,
                verdict=result, has_difference=any(c["has_difference"] for c in calls))


def attach_evidence(session, evidence):
    session["unmatched"] = []
    by_turn = {t["id"]: t for t in session["turns"]}
    by_output = {}
    for turn in session["turns"]:
        turn["evidence"] = []
        for oid in turn.get("output_ids", []):
            by_output.setdefault(oid, set()).add(turn["id"])
    candidates = []
    capture_turns = {}
    capture_conflicts = set()
    for original in evidence:
        if original.get("thread_id") != session["id"]:
            continue
        item = dict(original)
        turn_id = item.get("turn_id")
        match = by_turn.get(turn_id)
        reason = "turn_id 精确匹配" if match else None
        matches = set().union(*(by_output.get(oid, set()) for oid in item.get("output_ids", [])))
        conflict = len(matches) > 1 or bool(turn_id and matches and matches != {turn_id})
        if conflict:
            match = None
            item["association_conflict"] = True
            reason = "关联冲突：turn_id 与 output_id 不一致，或 output_id 指向多个轮次"
        elif not match and len(matches) == 1:
            match = by_turn[next(iter(matches))]
            reason = "响应 output_id 与消息精确匹配"
        capture_id = item.get("capture_id")
        if capture_id:
            if conflict:
                capture_conflicts.add(capture_id)
            if match:
                capture_turns.setdefault(capture_id, set()).add(match["id"])
        candidates.append((item, match, reason))
    for item, match, reason in candidates:
        capture_id = item.get("capture_id")
        matched_turns = capture_turns.get(capture_id, set())
        if capture_id and (capture_id in capture_conflicts or len(matched_turns) > 1):
            match = None
            item["association_conflict"] = True
            reason = "关联冲突：同一 capture_id 的强标识不一致"
        elif not match and not item.get("association_conflict") and len(matched_turns) == 1:
            target_id = next(iter(matched_turns))
            if item.get("turn_id") and item["turn_id"] != target_id:
                item["association_conflict"] = True
                reason = "关联冲突：turn_id 与同次 capture_id 关联的轮次不一致"
            else:
                match = by_turn[target_id]
                reason = "capture_id 与已关联的同次调用精确匹配"
        if match:
            item["association"] = reason
            match["evidence"].append(item)
        else:
            item["association"] = reason or "仅匹配会话，尚不能归属具体消息"
            session["unmatched"].append(item)
    for turn in session["turns"]:
        summarize_turn(turn)
    return session


class Observatory:
    def __init__(self, data_dir=None, *, source_paths=None, archive_root=None, logs_path=None):
        self.data_dir = Path(data_dir or ROOT / "data")
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db_path = self.data_dir / "evidence.sqlite"
        with self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS evidence (id TEXT PRIMARY KEY, thread_id TEXT, observed_at TEXT, body TEXT NOT NULL)"
            )
            db.execute("CREATE INDEX IF NOT EXISTS evidence_thread_time ON evidence(thread_id, observed_at)")
            db.execute("CREATE INDEX IF NOT EXISTS evidence_event_id ON evidence(json_extract(body, '$.event_id'))")
        self.lock = threading.RLock()
        self.cache = {}
        self.summary_cache = {}
        self.call_metadata_cache = {}
        self.evidence_revisions = {}
        self.catalog = {}
        configured_sources = os.environ.get("OBSERVATORY_SOURCE_PATHS")
        self.source_paths = [Path(p) for p in (source_paths if source_paths is not None else
            configured_sources.split(os.pathsep) if configured_sources else DBS)]
        self.archive_root = Path(archive_root or os.environ.get("OBSERVATORY_ARCHIVE_ROOT", "/tmp"))
        self.logs_path = Path(logs_path or os.environ.get("OBSERVATORY_LOGS_PATH", "/root/.codex/sqlite/logs_2.sqlite"))
        self.sources = {}
        self.last_discovery = 0
        self.monitor = {
            "running": False,
            "scans": 0,
            "saved": 0,
            "processes": [],
            "error": None,
        }
        self.stop_event = threading.Event()
        self.import_archives()

    def connect(self):
        return sqlite3.connect(self.db_path, timeout=10)

    def add(self, record):
        record = redact(record)
        fingerprint = {
            k: v for k, v in record.items() if k not in ("observed_at", "address", "id")
        }
        identifier = hashlib.sha256(
            json.dumps(fingerprint, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()[:24]
        record["id"] = identifier
        record.setdefault("observed_at", now())
        enriched = False
        with self.connect() as db:
            # Replay may recover additional safe diagnostics for the same native
            # event. Keep its original ID/count and never overwrite core evidence.
            if record.get("source") == "codex_trace" and record.get("event_id"):
                db.execute("BEGIN IMMEDIATE")
                previous = db.execute("SELECT id,body FROM evidence WHERE json_extract(body,'$.event_id')=?",
                                      (record["event_id"],)).fetchall()
                for old_id, body in previous:
                    old = json.loads(body)
                    if any(old.get(key) != record.get(key) for key in
                           ("source", "capture_id", "thread_id", "turn_id", "kind", "event_type")):
                        continue
                    for key in ("http_status", "error_code", "error_message", "error_classification_source", "corroborating_log_ids"):
                        if record.get(key) is not None and old.get(key) != record[key]:
                            if key == "error_code" and record[key] == "unknown" and old.get(key):
                                continue
                            if key == "error_message" and record.get("error_code") == "unknown" and old.get("error_code") not in (None, "unknown"):
                                continue
                            old[key] = record[key]
                            enriched = True
                    if enriched:
                        db.execute("UPDATE evidence SET body=? WHERE id=?", (json.dumps(old, ensure_ascii=False), old_id))
                    record = old
                    identifier = old_id
                    break
            cursor = db.execute(
                "INSERT OR IGNORE INTO evidence VALUES (?,?,?,?)",
                (
                    identifier,
                    record.get("thread_id"),
                    record["observed_at"],
                    json.dumps(record, ensure_ascii=False),
                ),
            )
            inserted = cursor.rowcount
        if inserted or enriched:
            thread_id = record.get("thread_id")
            with self.lock:
                self.evidence_revisions[thread_id] = self.evidence_revisions.get(thread_id, 0) + 1
                metadata = self.catalog.get(thread_id, {})
                source_path = metadata.get("path")
                if not source_path or not Path(source_path).is_file():
                    self.cache.pop(thread_id, None)
        return inserted

    def all_evidence(self, thread_id=None, *, limit=None, offset=0):
        """Filter before pagination; detail and export callers default to complete results."""
        query = "SELECT body FROM evidence"
        parameters = []
        if thread_id is not None:
            query += " WHERE thread_id=?"
            parameters.append(thread_id)
        query += " ORDER BY observed_at DESC, id DESC"
        if limit is not None:
            query += " LIMIT ? OFFSET ?"
            parameters.extend((max(0, int(limit)), max(0, int(offset))))
        elif offset:
            query += " LIMIT -1 OFFSET ?"
            parameters.append(max(0, int(offset)))
        with self.connect() as db:
            return [json.loads(row[0]) for row in db.execute(query, parameters)]

    def count_evidence(self, thread_id=None):
        with self.connect() as db:
            if thread_id is None:
                return db.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]
            return db.execute("SELECT COUNT(*) FROM evidence WHERE thread_id=?", (thread_id,)).fetchone()[0]

    def evidence_item(self, identifier, thread_id=None):
        query, parameters = "SELECT body FROM evidence WHERE id=?", [identifier]
        if thread_id is not None:
            query += " AND thread_id=?"
            parameters.append(thread_id)
        with self.connect() as db:
            row = db.execute(query, parameters).fetchone()
        return json.loads(row[0]) if row else None

    def evidence_page(self, thread_id=None, *, limit=100, offset=0):
        limit, offset = max(1, min(int(limit), 500)), max(0, int(offset))
        return dict(items=self.all_evidence(thread_id, limit=limit, offset=offset),
                    total=self.count_evidence(thread_id), limit=limit, offset=offset)

    def call_metadata(self, records):
        """Read client identity from explicit rollout metadata, never path heuristics."""
        result = {}
        for sid in {r.get("thread_id") for r in records if r.get("thread_id")}:
            meta = dict(self.catalog.get(sid, {}))
            stamp = self._stamp(meta)
            verified_client = None
            if stamp:
                cached = self.call_metadata_cache.get(sid)
                if cached and cached[0] == stamp:
                    verified_client = cached[1]
                else:
                    try:
                        with Path(meta["path"]).open(encoding="utf-8") as stream:
                            for index, line in enumerate(stream):
                                if index >= 32:
                                    break
                                try:
                                    event = json.loads(line)
                                except ValueError:
                                    continue
                                if event.get("type") != "session_meta":
                                    continue
                                payload = event.get("payload", {})
                                originator = payload.get("originator")
                                if originator == "Codex Desktop":
                                    verified_client = "Codex App"
                                elif originator in {"codex-tui", "codex_cli_rs", "codex_exec"} or payload.get("source") in ("cli", "exec"):
                                    verified_client = "Codex CLI"
                                break
                    except (OSError, UnicodeError):
                        pass
                    self.call_metadata_cache[sid] = (stamp, verified_client)
            elif meta.get("archived_import") and meta.get("client") in ("Codex App", "Codex CLI"):
                verified_client = meta["client"]
            meta["verified_client"] = verified_client
            result[sid] = meta
        return result

    def calls(self, **filters):
        from call_log import calls_page
        from call_context import enrich_call_contexts

        self.discover()
        with self.lock:
            records = self.all_evidence()
            metadata = self.call_metadata(records)
            return calls_page(records, metadata, contexts=enrich_call_contexts(self, records, metadata), **filters)

    def call(self, identifier):
        from call_log import call_rows
        from call_context import enrich_call_contexts

        self.discover()
        with self.lock:
            records = self.all_evidence()
            metadata = self.call_metadata(records)
            return call_rows(records, metadata, detail_id=identifier,
                             contexts=enrich_call_contexts(self, records, metadata))

    def _source_status(self, path, kind, error=None):
        key = str(path)
        previous = self.sources.get(key, {})
        self.sources[key] = dict(path=key, kind=kind, state="unavailable" if error else "ready",
                                checked_at=now(), last_success=previous.get("last_success") if error else now(),
                                error=str(error) if error else None)

    def source_statuses(self):
        self.discover()
        return [dict(item) for item in self.sources.values()]

    def import_archives(self):
        for path in self.archive_root.glob("codex-app-capture-*/verified-result.json"):
            try:
                data = json.loads(path.read_text())
            except (OSError, ValueError) as error:
                self._source_status(path, "archive", error)
                continue
            self._source_status(path, "archive")
            sid = data.get("thread_id")
            turns = data.get("turn_context", [])
            self.catalog[sid] = dict(
                id=sid,
                title=data.get("user_message", "历史取证"),
                client="Codex App",
                path=data.get("session_file", ""),
                archived_import=True,
                archive_turns=turns,
            )
            for response in data.get("response_evidence", []):
                matches = response.get("verified_output_matches", [])
                output_ids = [x["id"] for x in matches if x.get("id")]
                # Preserve existing evidence IDs and bytes, including older bad associations.
                if any(e.get("source_file") == str(path) and e.get("kind") == "response_fragment"
                       and e.get("output_ids", []) == output_ids
                       and e.get("reasoning") == response.get("reasoning")
                       for e in self.all_evidence(sid)):
                    continue
                # The archived result contains an explicit, previously verified output-id match.
                self.add(
                    dict(
                        thread_id=sid,
                        turn_id=response.get("turn_id"),
                        kind="response_fragment",
                        source="archived_memory",
                        effort=response.get("reasoning", {}).get("effort"),
                        reasoning=response.get("reasoning"),
                        observed_at=response.get("captured_at"),
                        output_ids=output_ids,
                        event_time=None,
                        source_file=str(path),
                        note="历史内存片段；输出 ID 已核对。捕获时间不等于响应产生时间。",
                    )
                )
        for path in self.archive_root.glob("codex-app-request-check-*/request-fields.json"):
            try:
                records = json.loads(path.read_text())
            except (OSError, ValueError) as error:
                self._source_status(path, "archive", error)
                continue
            self._source_status(path, "archive")
            for r in records:
                self.add(
                    dict(
                        kind="request",
                        source="archived_memory",
                        thread_id=r.get("prompt_cache_key"),
                        turn_id=r.get("client_metadata", {}).get("turn_id"),
                        effort=r.get("reasoning", {}).get("effort"),
                        reasoning=r.get("reasoning"),
                        model=r.get("model"),
                        observed_at=r.get("captured_at"),
                        source_file=str(path),
                    )
                )

    def discover(self, force=False):
        with self.lock:
            if not force and time.monotonic() - self.last_discovery < 20:
                return
            for db_path in self.source_paths:
                if not db_path.exists():
                    self._source_status(db_path, "sessions", "文件不存在")
                    continue
                try:
                    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1)
                    db.row_factory = sqlite3.Row
                    rows = db.execute(
                        "SELECT id,title,rollout_path,updated_at,model,reasoning_effort FROM threads ORDER BY updated_at DESC LIMIT 45"
                    ).fetchall()
                    db.close()
                    self._source_status(db_path, "sessions")
                    for row in rows:
                        path = windows_path(row["rollout_path"])
                        if not path.is_file():
                            continue
                        self.catalog[row["id"]] = dict(
                            id=row["id"],
                            title=row["title"][:160],
                            path=str(path),
                            updated=unix_time(row["updated_at"]),
                            model=row["model"],
                            client_effort=row["reasoning_effort"],
                            client=(
                                "Codex App" if "/mnt/c/" in str(path) else "Codex CLI"
                            ),
                        )
                except sqlite3.Error as error:
                    self._source_status(db_path, "sessions", error)
                    continue
            self.last_discovery = time.monotonic()

    @staticmethod
    def _stamp(meta):
        path = Path(meta.get("path") or "/nonexistent")
        try:
            stat = path.stat()
            return (str(path), stat.st_mtime_ns, stat.st_size) if path.is_file() else None
        except OSError:
            return None

    @staticmethod
    def _fallback_meta(sid):
        return dict(id=sid, title="捕获到的会话 · 尚无本地消息记录", client="Codex App", path="")

    def _base_session(self, meta, evidence, summary_only=False):
        path = Path(meta.get("path") or "/nonexistent")
        if self._stamp(meta):
            parser = parse_summary if summary_only else parse_session
            session = parser(path, meta)
            self._source_status(path, "rollout", session.get("error"))
            return session
        if meta.get("path"):
            self._source_status(path, "rollout", "文件不存在或无法读取")
        session = dict(meta, available=False, turns=[])
        for turn in meta.get("archive_turns", []):
            if not turn.get("turn_id"):
                continue
            session["turns"].append(dict(
                id=turn["turn_id"], time=turn.get("timestamp"), model=turn.get("model"),
                client_effort=turn.get("effort"), output_ids=turn.get("output_ids", []),
                messages=[] if summary_only else [dict(role="user", text=meta["title"],
                    time=turn.get("timestamp"), id=None, line=None)],
                tools=[], evidence=[],
            ))
        if not session["turns"]:
            grouped = {}
            for record in evidence:
                tid = record.get("turn_id")
                if not tid:
                    continue
                if tid not in grouped:
                    grouped[tid] = dict(id=tid, time=record.get("observed_at"),
                        model=record.get("model"), client_effort=None,
                        messages=[], tools=[], evidence=[], time_source="捕获时间")
                if record.get("kind") == "request" and record.get("user_message") and not summary_only:
                    grouped[tid]["messages"] = [dict(role="user", text=record["user_message"],
                        time=record.get("observed_at"), id=None, line=None)]
            session["turns"] = sorted(grouped.values(), key=lambda turn: turn["time"] or "")
        return session

    def session(self, sid, with_logs=False):
        self.discover()
        with self.lock:
            evidence = self.all_evidence(sid)
            meta = self.catalog.get(sid)
            if not meta and not evidence:
                return None
            meta = meta or self._fallback_meta(sid)
            stamp = self._stamp(meta)
            # Captured-only turns can change when evidence is appended.
            key = (stamp, json.dumps(meta, sort_keys=True),
                   None if stamp else self.evidence_revisions.get(sid, 0))
            cached = self.cache.get(sid)
            if cached and cached[0] == key:
                session = json.loads(json.dumps(cached[1]))
            else:
                session = self._base_session(meta, evidence)
                self.cache[sid] = (key, json.loads(json.dumps(session)))
            session = attach_evidence(session, evidence)
            session["latest_verdict"] = session["turns"][-1]["verdict"] if session["turns"] else "unknown"
            session["has_difference"] = any(turn["has_difference"] for turn in session["turns"])
            if with_logs:
                self.attach_http_logs(session)
            return session

    def attach_http_logs(self, session):
        path = self.logs_path
        if not path.exists():
            self._source_status(path, "http_logs", "文件不存在")
            return
        turns = {t["id"]: t for t in session["turns"]}
        try:
            db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1)
            rows = db.execute(
                "SELECT id,ts,feedback_log_body FROM logs WHERE thread_id=? AND target='codex_http_client::client' ORDER BY id DESC LIMIT 100",
                (session["id"],),
            ).fetchall()
            db.close()
            self._source_status(path, "http_logs")
            for identifier, timestamp, text in rows:
                fields = {}
                for key in ("turn.id", "codex.turn.reasoning_effort", "url", "status"):
                    match = re.search(re.escape(key) + r'=("[^"]*"|[^\s}]+)', text)
                    if match:
                        fields[key] = match.group(1).strip('"')
                turn = turns.get(fields.get("turn.id"))
                if turn is not None:
                    turn.setdefault("http_requests", []).append(
                        dict(
                            log_id=identifier,
                            time=unix_time(timestamp),
                            runtime_effort=fields.get("codex.turn.reasoning_effort"),
                            endpoint=fields.get("url"),
                            status=fields.get("status"),
                        )
                    )
        except sqlite3.Error as error:
            self._source_status(path, "http_logs", error)

    def sessions(self):
        """Cache compact, complete turn summaries independently of detail navigation."""
        self.discover()
        with self.lock:
            with self.connect() as db:
                stats = {sid: (count, last_row) for sid, count, last_row in db.execute(
                    "SELECT thread_id, COUNT(*), MAX(rowid) FROM evidence GROUP BY thread_id"
                ) if sid}
            summaries = []
            for sid in set(self.catalog) | set(stats):
                meta = self.catalog.get(sid) or self._fallback_meta(sid)
                stamp = (self._stamp(meta), json.dumps(meta, sort_keys=True), stats.get(sid, (0, None)))
                cached = self.summary_cache.get(sid)
                if cached and cached[0] == stamp:
                    summaries.append(dict(cached[1]))
                    continue
                records = self.all_evidence(sid) if sid in stats else []
                session = attach_evidence(self._base_session(meta, records, summary_only=True), records)
                turns = session["turns"]
                latest = turns[-1] if turns else {}
                available = session.get("available", False)
                summary = dict(
                    id=sid, title=meta.get("title") or session.get("title") or "未命名会话",
                    client=session.get("client") or "Codex App", available=available,
                    updated=meta.get("updated") or latest.get("time") or "",
                    turns=len(turns) if available or turns else None,
                    tools=sum(len(t["tools"]) for t in turns) if available else None,
                    model=latest.get("model") or meta.get("model"),
                    client_effort=latest.get("client_effort") or meta.get("client_effort"),
                    verdict=latest.get("verdict", "unknown"),
                    latest_verdict=latest.get("verdict", "unknown"),
                    has_difference=any(t["has_difference"] for t in turns),
                    summary_state="ready" if available else "partial" if turns else "unavailable",
                    evidence=stats.get(sid, (0, None))[0],
                )
                self.summary_cache[sid] = (stamp, dict(summary))
                summaries.append(summary)
            return sorted(summaries, key=lambda item: item["updated"], reverse=True)
