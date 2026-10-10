"""Classify captured calls using explicit native thread/turn metadata only.

The return value is keyed by (thread_id, turn_id). No prompt, log body, parent
guess, model-name heuristic, or time-based association leaves this module.
"""

import json
from pathlib import Path
import re
import sqlite3
import time

MAX_TURNS = 160
MAX_THREADS = 48
MAX_LOG_ROWS = 50
LOG_PREFIX = 1024
LOG_SUFFIX = 31744
CACHE_SECONDS = 15
LABELS = {
    "conversation": "主对话",
    "title_generation": "标题生成",
    "memory_consolidation": "后台记忆合并",
    "unknown": "用途未确认",
}
_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_SUBMISSION = re.compile(
    r'^session_loop\{thread_id=([A-Za-z0-9_-]+)\}: Submission sub=Submission '
    r'\{ id: "([A-Za-z0-9_-]+)", op: TurnInput \{'
)
_START = re.compile(
    r'(?<!\\)start: TurnStartOptions \{ turn_trigger: '
    r'(?:Some\("([a-z_]+)"\)|None), final_output_json_schema: '
)


def _context(purpose, source):
    return {"purpose": purpose, "purpose_label": LABELS[purpose], "purpose_source": source}


def _object_end(text):
    """Find the balanced Rust JSON Object, respecting its quoted strings."""
    depth = 0
    quoted = escaped = False
    for index, char in enumerate(text):
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index + 1
    return None


def _title_schema(text):
    """Accept only a complete native schema whose only property is title."""
    if not text.startswith("Some(Object {"):
        return False
    text = text[len("Some(Object "):]
    end = _object_end(text[:8192])
    if end is None or not text[end:].startswith(")"):
        return False
    schema = text[:end]
    # Rust serde_json debug formatting. Its quoted JSON strings are never
    # evaluated; only the known wrappers are removed before json.loads.
    schema = re.sub(r'\bObject\s*(?=\{)', '', schema)
    schema = re.sub(r'\bArray\s*(?=\[)', '', schema)
    schema = re.sub(r'\bString\(("(?:[^"\\]|\\.)*")\)', r'\1', schema)
    schema = re.sub(r'\bNumber\((-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\)', r'\1', schema)
    schema = re.sub(r'\bBool\((true|false)\)', r'\1', schema)
    schema = re.sub(r'\bNull\b', 'null', schema)
    try:
        value = json.loads(schema)
    except (ValueError, TypeError):
        return False
    return (isinstance(value, dict) and value.get("type") == "object"
            and isinstance(value.get("properties"), dict)
            and set(value["properties"]) == {"title"}
            and value.get("required") == ["title"]
            and isinstance(value["properties"]["title"], dict)
            and value["properties"]["title"].get("type") == "string")


def classify_submission(thread_id, prefix, suffix=""):
    """Return (turn_id, context), or None for unverified diagnostic text."""
    submission = _SUBMISSION.match(prefix)
    if not submission or submission[1] != thread_id:
        return None
    # Long user text is omitted by SQL. Structural native fields are at the
    # tail, and quotes inside a user-text Rust string are escaped, so they
    # cannot match the unescaped TurnStartOptions enum values below.
    text = prefix if not suffix else suffix
    matches = list(_START.finditer(text))
    if len(matches) != 1:
        return None
    start = matches[0]
    trigger = start[1]
    if trigger == "memory_consolidation":
        return submission[2], _context("memory_consolidation", "native_turn_trigger")
    if trigger == "user":
        return submission[2], _context("conversation", "native_turn_trigger")
    if trigger is not None:
        return submission[2], _context("unknown", "unrecognized_native_trigger")
    if trigger is None and _title_schema(text[start.end():]):
        return submission[2], _context("title_generation", "native_title_schema")
    return None


def _read_contexts(path, wanted):
    found = {}
    try:
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.3) as db:
            # Only exact indexed thread IDs are read. A long prompt is never
            # materialized in Python; prefix+suffix total at most 32KiB.
            for sid in sorted({key[0] for key in wanted}):
                rows = db.execute(
                    "SELECT substr(CAST(feedback_log_body AS BLOB),1,?), "
                    "CASE WHEN length(CAST(feedback_log_body AS BLOB))>? "
                    "THEN substr(CAST(feedback_log_body AS BLOB),-?) ELSE X'' END "
                    "FROM logs WHERE thread_id=? AND target='codex_core::session::handlers' "
                    "ORDER BY id DESC LIMIT ?",
                    (LOG_PREFIX, LOG_PREFIX, LOG_SUFFIX, sid, MAX_LOG_ROWS),
                )
                for prefix, suffix in rows:
                    prefix = (prefix or b"").decode("utf-8", errors="replace")
                    suffix = (suffix or b"").decode("utf-8", errors="replace")
                    result = classify_submission(sid, prefix, suffix)
                    if result and (sid, result[0]) in wanted:
                        key = (sid, result[0])
                        if key in found and found[key] != result[1]:
                            found[key] = _context("unknown", "conflicting_native_metadata")
                        else:
                            found[key] = result[1]
    except (OSError, sqlite3.Error, ValueError):
        pass
    return found


def _user_threads(paths, wanted, metadata):
    result = {sid for sid in wanted if metadata.get(sid, {}).get("thread_source") == "user"}
    placeholders = ",".join("?" for _ in wanted)
    for path in paths:
        if not path.is_file():
            continue
        try:
            with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.3) as db:
                rows = db.execute(
                    f"SELECT id,thread_source FROM threads WHERE id IN ({placeholders})", sorted(wanted))
                result.update(sid for sid, source in rows if source == "user")
        except (OSError, sqlite3.Error, ValueError):
            continue
    return result


def enrich_call_contexts(store, records, metadata=None):
    """Return safe per-turn purposes; unknown is explicit and never guessed.

    Only the most recent bounded set of recorded turns is inspected. Sources
    are the store's read-only state databases and their sibling logs database.
    A short cache avoids rescanning native diagnostics on each dashboard poll.
    """
    metadata = metadata or {}
    ordered = sorted(records, key=lambda r: r.get("observed_at") or r.get("event_time") or "", reverse=True)
    wanted, threads = set(), set()
    for record in ordered:
        sid, tid = record.get("thread_id"), record.get("turn_id")
        if not isinstance(sid, str) or not isinstance(tid, str) or not _ID.fullmatch(sid) or not _ID.fullmatch(tid):
            continue
        if sid not in threads and len(threads) >= MAX_THREADS:
            continue
        threads.add(sid)
        wanted.add((sid, tid))
        if len(wanted) >= MAX_TURNS:
            break
    if not wanted:
        return {}
    state_paths = [Path(path) for path in getattr(store, "source_paths", [])]
    log_paths = {path.with_name("logs_2.sqlite") for path in state_paths}
    if getattr(store, "logs_path", None):
        log_paths.add(Path(store.logs_path))
    signature = (tuple(sorted(wanted)), tuple(map(str, state_paths)), tuple(sorted(map(str, log_paths))),
                 tuple(sorted(sid for sid in threads if metadata.get(sid, {}).get("thread_source") == "user")))
    cached = getattr(store, "_call_context_cache", None)
    now = time.monotonic()
    if cached and cached[0] == signature and now < cached[1]:
        return {key: dict(value) for key, value in cached[2].items()}
    users = _user_threads(state_paths, threads, metadata)
    contexts = {key: _context("conversation", "native_thread_source") if key[0] in users
                else _context("unknown", "unavailable") for key in wanted}
    direct = {}
    for path in sorted(log_paths):
        if not path.is_file():
            continue
        for key, value in _read_contexts(path, wanted).items():
            if key in direct and direct[key] != value:
                direct[key] = _context("unknown", "conflicting_native_metadata")
            else:
                direct[key] = value
    contexts.update(direct)
    store._call_context_cache = (signature, now + CACHE_SECONDS, contexts)
    return {key: dict(value) for key, value in contexts.items()}
