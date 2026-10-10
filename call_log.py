"""Call-oriented views of captured evidence, without inventing missing pairs."""

from collections import Counter, defaultdict
import hashlib
import json

from observatory import effort_state
from observation_fields import safe_failure_fields

CLIENTS = ("Codex App", "Codex CLI", "unknown")
RESPONSE_KINDS = {"response", "response_fragment"}
TERMINAL_STATUSES = {
    "completed", "failed", "incomplete", "cancelled", "http_error", "transport_error",
    "incomplete_stream",
}


def values(records, key):
    return sorted({r[key] for r in records if isinstance(r.get(key), str) and r[key]})


def single(items):
    return items[0] if len(items) == 1 else None


def identity(record):
    if record.get("id"):
        return record["id"]
    body = {k: v for k, v in record.items() if k not in {"observed_at", "address"}}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:24]


def group_records(records):
    """Only capture IDs join request/response; response IDs join lifecycle events."""
    groups = defaultdict(list)
    response_captures = defaultdict(set)
    for record in records:
        if record.get("capture_id"):
            key = "capture:" + record["capture_id"]
            groups[key].append(record)
            if record.get("kind") in RESPONSE_KINDS and record.get("response_id"):
                response_captures[record["response_id"]].add(key)
    for record in records:
        if record.get("capture_id"):
            continue
        response_id = record.get("response_id") if record.get("kind") in RESPONSE_KINDS else None
        if response_id:
            targets = response_captures.get(response_id, set())
            target = next(iter(targets)) if len(targets) == 1 else None
            # A duplicate response observed by another collector may join a capture,
            # but a conflicting session identifier must never contaminate it.
            thread_ids = values(groups[target], "thread_id") if target else []
            if target and (not record.get("thread_id") or not thread_ids or thread_ids == [record["thread_id"]]):
                key = target
            else:
                key = "response:" + response_id
        else:
            key = "observation:" + identity(record)
        groups[key].append(record)
    return groups


def safe_usage(record):
    usage = record.get("usage")
    if not isinstance(usage, dict):
        return None
    result = {k: usage[k] for k in ("input_tokens", "output_tokens", "total_tokens")
              if isinstance(usage.get(k), (int, float)) and not isinstance(usage[k], bool)}
    for key, allowed in {"input_tokens_details": ("cached_tokens",),
                         "output_tokens_details": ("reasoning_tokens",)}.items():
        nested = usage.get(key)
        if isinstance(nested, dict):
            result[key] = {k: nested[k] for k in allowed if isinstance(nested.get(k), (int, float))
                           and not isinstance(nested[k], bool)}
    return result or None


def evidence_view(record):
    allowed = {
        "id", "thread_id", "turn_id", "capture_id", "response_id", "kind", "source", "client",
        "effort", "model", "observed_at", "event_time", "output_ids", "status", "event_type",
        "stage", "http_status", "capture_error", "capture_truncated", "terminal_unknown",
        "effort_state", "effort_source", "status_source", "run_id", "event_id",
        "inference_call_id", "upstream_request_id", "transport", "transport_attempt", "attempt",
        "rollout_id", "trace_id", "seq", "error_code", "error_message", "error_classification_source", "corroborating_log_ids",
    }
    result = {k: record[k] for k in allowed if k in record}
    if isinstance(record.get("reasoning"), dict):
        result["reasoning"] = {"effort": record["reasoning"].get("effort")}
    usage = safe_usage(record)
    if usage is not None:
        result["usage"] = usage
    # Error text and arbitrary nested metadata can contain message bodies. Only
    # the documented classification fields belong in this compact evidence view.
    if isinstance(record.get("incomplete_details"), dict):
        reason = record["incomplete_details"].get("reason")
        if reason in {"max_output_tokens", "content_filter"}:
            result["incomplete_details"] = {"reason": reason}
    return result


def native_lifecycle(record):
    """Native completion summarizes the client lifecycle, not the response body."""
    return record.get("source") == "codex_trace" or record.get("status_source") == "client_lifecycle"


def response_field_records(records):
    actual = [r for r in records if not native_lifecycle(r)]
    direct = [r for r in actual if r.get("source") == "codex_event_log"]
    return direct or actual


def response_status_records(records):
    # A raw response.incomplete can map to Completed inside Codex. Prefer the
    # actual terminal status; the native summary is only a fallback when the
    # terminal wire event was not captured (e.g. transport failure).
    actual = [r for r in records if not native_lifecycle(r)]
    terminal = [r for r in actual if r.get("status") in TERMINAL_STATUSES]
    direct_terminal = [r for r in terminal if r.get("source") == "codex_event_log"]
    if direct_terminal or terminal:
        return direct_terminal or terminal
    native_terminal = [r for r in records if native_lifecycle(r) and r.get("status") in TERMINAL_STATUSES]
    return native_terminal or response_field_records(records) or records


def side_summary(records, kind, detail=False):
    fields = response_field_records(records) if kind == "response" else records
    effort_records = [r for r in fields if r.get("effort_state") != "not_captured"]
    efforts, state = effort_state(effort_records, "not_carried" if kind == "request" else "not_echoed")
    models = values(fields, "model")
    result = dict(effort=single(efforts), efforts=efforts, state=state,
                  count=len(records), evidence_ids=values(records, "id"), model=single(models), models=models)
    if kind == "response":
        status_records = response_status_records(records)
        statuses = values(status_records, "status")
        terminal = [r for r in status_records if r.get("status") in TERMINAL_STATUSES]
        terminal_statuses = values(terminal, "status")
        chosen = sorted(terminal or status_records,
                        key=lambda r: (r.get("event_time") or "", r.get("observed_at") or "", identity(r)))
        status = "conflicting" if len(terminal_statuses) > 1 else (
            single(terminal_statuses) or (chosen[-1].get("status") if chosen else None))
        # Use one terminal observation's counters. Never add created/completed or
        # duplicate object/SSE usage together, and do not carry interim usage into
        # a terminal event that did not provide it.
        usages = [r for r in chosen if safe_usage(r) is not None]
        usage = safe_usage(usages[-1]) if usages and status != "conflicting" else None
        result.update(status=status, statuses=statuses, response_ids=values(records, "response_id"), usage=usage)
        if status in {"failed", "http_error", "transport_error"}:
            result.update(safe_failure_fields(*(r for r in chosen if r.get("error_code") or r.get("http_status"))))
    if detail:
        result["evidence"] = [evidence_view(r) for r in sorted(records, key=lambda r: (r.get("observed_at") or "", identity(r)))]
    return result


def summarize_call(identifier, records, metadata, detail=False, contexts=None):
    requests = [r for r in records if r.get("kind") == "request"]
    responses = [r for r in records if r.get("kind") in RESPONSE_KINDS]
    diagnostics = [r for r in records if r.get("kind") not in RESPONSE_KINDS | {"request"}]
    request, response = side_summary(requests, "request", detail), side_summary(responses, "response", detail)
    threads, turns = values(records, "thread_id"), values(records, "turn_id")
    clients = values(records, "client")
    clients = [client for client in clients if client in CLIENTS[:2]]
    meta = metadata.get(single(threads), {})
    context = (contexts or {}).get((single(threads), single(turns)), {})
    purpose = context.get("purpose", "unknown")
    purpose_label = context.get("purpose_label", "用途未确认")
    auxiliary = purpose in {"title_generation", "memory_consolidation"}
    client_state = "recorded" if clients else "session" if meta.get("verified_client") else "unknown"
    client = single(clients) or meta.get("verified_client") or "unknown"
    conflicts = []
    if len(clients) > 1:
        client, client_state = "unknown", "conflicting"
        conflicts.append("client")
    if len(threads) > 1:
        conflicts.append("thread_id")
    if len(turns) > 1:
        conflicts.append("turn_id")
    if len(response["response_ids"]) > 1:
        conflicts.append("response_id")
    if request["state"] == "conflicting":
        conflicts.append("request_effort")
    if response["state"] == "conflicting":
        conflicts.append("response_effort")
    if response["status"] == "conflicting":
        conflicts.append("response_status")
    if len(request["models"]) > 1:
        conflicts.append("request_model")
    if len(response["models"]) > 1:
        conflicts.append("response_model")
    paired = identifier.startswith("capture:") and bool(requests and responses) and not conflicts
    verdict = "mixed" if conflicts else "unknown"
    if paired and request["effort"] and response["effort"]:
        verdict = "match" if request["effort"] == response["effort"] else "mismatch"
    result = "difference" if conflicts or verdict == "mismatch" else "match" if verdict == "match" else "pending"
    observed = values(records, "observed_at")
    event_times = values(responses, "event_time")
    request_times = values(requests, "observed_at")
    # Memory scan times are observation times, not HTTP request start times.
    time = (request_times[0] if identifier.startswith("capture:") and request_times else
            event_times[-1] if event_times else observed[0] if observed else None)
    time_source = "event_time" if event_times and not (identifier.startswith("capture:") and request_times) else "observed_at"
    models = sorted(set(request["models"] + response["models"]))
    row = dict(
        id=identifier, capture_id=single(values(records, "capture_id")), client=client, client_state=client_state,
        thread_id=single(threads), thread_ids=threads, turn_id=single(turns), turn_ids=turns,
        session_title=meta.get("title") or ("会话记录不可用" if threads else "未提供会话标识"),
        model=single(models), models=models, time=time, time_source=time_source,
        observed_at=observed[0] if observed else None, last_observed_at=observed[-1] if observed else None,
        event_time=event_times[-1] if event_times else None,
        paired=paired, pairing="capture_id" if identifier.startswith("capture:") else
            "response_id" if identifier.startswith("response:") else "observation",
        result=result, verdict=verdict, conflict=bool(conflicts), conflicts=conflicts,
        request=request, response=response, sources=values(records, "source"),
        observation_count=len(records), diagnostic_count=len(diagnostics),
        purpose=purpose, purpose_label=purpose_label,
        purpose_source=context.get("purpose_source", "unavailable"), auxiliary=auxiliary,
    )
    if not meta.get("title") and auxiliary:
        row["session_title"] = purpose_label + " · 独立辅助会话"
    if detail:
        row["diagnostics"] = [evidence_view(r) for r in diagnostics]
    return row


def call_rows(records, metadata, detail_id=None, contexts=None):
    groups = group_records(records)
    rows = [summarize_call(key, group, metadata, detail=key == detail_id, contexts=contexts) for key, group in groups.items()]
    attempts = defaultdict(list)
    for row in rows:
        if row['auxiliary'] and row['thread_id'] and row['turn_id']:
            attempts[(row['thread_id'], row['turn_id'], row['purpose'])].append(row)
    for group in attempts.values():
        for index, row in enumerate(sorted(group, key=lambda r: (r['time'] or '', r['id'])), 1):
            row.update(attempt_index=index, attempt_count=len(group))
    if detail_id is not None:
        return next((row for row in rows if row['id'] == detail_id), None)
    return sorted(rows, key=lambda row: (row["time"] or "", row["id"]), reverse=True)


def calls_page(records, metadata, *, client="", q="", result="", thread_id="", purpose="", limit=50, offset=0, contexts=None):
    rows = call_rows(records, metadata, contexts=contexts)
    sessions = {}
    for row in rows:
        for sid in row["thread_ids"]:
            meta = metadata.get(sid, {})
            sessions.setdefault(sid, dict(id=sid, title=meta.get("title") or "会话记录不可用", client=row["client"]))
    query = q.casefold().strip()
    searched = [row for row in rows if (not thread_id or thread_id in row["thread_ids"])
               and (not purpose or row['purpose'] == purpose)
               and (not query or query in " ".join(str(x) for x in (
                   row["id"], row["session_title"], *row["models"], *row["thread_ids"], *row["turn_ids"],
                   *row["request"]["efforts"], *row["response"]["efforts"], *row["sources"],
                   row["response"]["status"] or "", row['purpose_label'], row['response'].get('error_message', ''),
                   row['response'].get('http_status', ''))).casefold())]
    matched = [row for row in searched if not result or row["result"] == result]
    counts = Counter(row["client"] for row in matched)
    selected = [row for row in searched if not client or row["client"] == client]
    filtered = [row for row in selected if not result or row["result"] == result]
    outcomes = Counter(row["result"] for row in selected)
    total = len(filtered)
    stats = dict(total=len(selected), paired=sum(row["paired"] for row in selected),
                 **{key: outcomes[key] for key in ("match", "difference", "pending")},
                 request_only=sum(bool(row["request"]["count"]) and not row["response"]["count"] for row in selected),
                 response_only=sum(bool(row["response"]["count"]) and not row["request"]["count"] for row in selected),
                 diagnostic_only=sum(not row["request"]["count"] and not row["response"]["count"] for row in selected),
                 observations=sum(row["observation_count"] for row in selected))
    return dict(items=filtered[offset:offset + limit], total=total, stats=stats,
                client_counts={key: counts[key] for key in CLIENTS}, sessions=list(sessions.values()),
                pagination=dict(limit=limit, offset=offset, total=total, has_more=offset + limit < total))
