#!/usr/bin/env python3
import argparse
import copy
import hashlib
import http.server
import json
import os
import urllib.parse
import threading
from pathlib import Path

from observatory import ROOT, Observatory, now
from sanitization import redact
from capture import processes
from event_capture import start_event_monitor
from observed_launcher import EVENT_ROOT, instrumentation_status
from app_log_bridge import AppLogBridge
from coverage import coverage_status

def _desktop_log_root():
    override = os.environ.get("OBSERVATORY_DESKTOP_LOG_ROOT")
    if override:
        return Path(override)
    matches = sorted(Path("/mnt/c/Users").glob("*/AppData/Local/Packages/OpenAI.Codex_*/LocalCache/Local/Codex/Logs"))
    return matches[0] if matches else Path("/mnt/c/Users")


DESKTOP_LOG_ROOT = _desktop_log_root()


def start_desktop_bridge(store):
    bridge = AppLogBridge(DESKTOP_LOG_ROOT, EVENT_ROOT / 'app' / 'desktop-native')
    store.desktop_bridge_status = bridge.status
    # Establish the old-log boundary before a new desktop launch writes events.
    bridge.scan_once()
    def worker():
        try:
            while not store.event_stop_event.is_set():
                try:
                    bridge.scan_once()
                except Exception as exc:
                    bridge.status['error'] = 'Local desktop log read failed: ' + type(exc).__name__
                store.event_stop_event.wait(0.5)
        finally:
            bridge.status['running'] = False
    thread = threading.Thread(target=worker, daemon=True, name='desktop-log-bridge')
    thread.start()
    return thread
from relay import relay_request

EVIDENCE_FIELDS = {
    'id', 'thread_id', 'turn_id', 'capture_id', 'kind', 'source', 'effort',
    'reasoning', 'model', 'observed_at', 'event_time', 'output_ids', 'response_id',
    'status', 'usage', 'incomplete_details', 'finish_reason', 'reason', 'error_type', 'stage',
    'association', 'association_conflict', 'capture_error', 'capture_truncated',
}
PREVIEW_FIELDS = {
    'id', 'kind', 'source', 'effort', 'observed_at', 'event_time', 'association',
    'association_conflict', 'capture_id', 'status', 'note',
}


def tool_version(tool):
    content = {key: tool.get(key) for key in ('arguments', 'result', 'status', 'end_time')}
    return hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def session_page(session, limit, offset):
    """Page backwards from the latest turn; each page is chronological."""
    result = dict(session)
    turns = session['turns']
    end = max(0, len(turns) - offset)
    start = max(0, end - limit)
    result['turns'] = copy.deepcopy(turns[start:end])
    result['pagination'] = dict(offset=offset, limit=limit, total=len(turns),
                                has_more=start > 0)
    for index, turn in enumerate(result['turns'], start + 1):
        turn['position'] = index
        for tool in turn.get('tools', []):
            tool['content_version'] = tool_version(tool)
            tool.pop('arguments', None)
            tool.pop('result', None)
            tool['content_deferred'] = True
        turn['evidence'] = [evidence_preview(item) for item in turn.get('evidence', [])]
    result['unmatched'] = [evidence_preview(item) for item in session.get('unmatched', [])]
    result.pop('archive_turns', None)
    return result


def evidence_preview(item):
    return {**{key: item[key] for key in PREVIEW_FIELDS if key in item},
            'content_deferred': True}


def minimal_evidence(record):
    result = {key: record[key] for key in EVIDENCE_FIELDS if key in record}
    reasoning = record.get('reasoning')
    if isinstance(reasoning, dict):
        result['reasoning'] = {key: reasoning[key] for key in ('effort',) if key in reasoning}
    usage = record.get('usage')
    if isinstance(usage, dict):
        keys = ('input_tokens', 'output_tokens', 'total_tokens')
        result['usage'] = {key: usage[key] for key in keys if isinstance(usage.get(key), (int, float))}
        for detail, fields in {'input_tokens_details': ('cached_tokens',), 'output_tokens_details': ('reasoning_tokens',)}.items():
            if isinstance(usage.get(detail), dict):
                result['usage'][detail] = {key: usage[detail][key] for key in fields if isinstance(usage[detail].get(key), (int, float))}
    incomplete = record.get('incomplete_details')
    if isinstance(incomplete, dict):
        result['incomplete_details'] = {key: incomplete[key] for key in ('reason',) if key in incomplete}
    return result


def evidence_export(session, records):
    """Explicit allowlist: no chat text, commands, paths or arbitrary metadata."""
    associated = {record['id']: record for turn in session['turns'] for record in turn.get('evidence', []) if record.get('id')}
    associated.update({record['id']: record for record in session.get('unmatched', []) if record.get('id')})
    turns = []
    for turn in session['turns']:
        turns.append({key: turn[key] for key in (
            'id', 'time', 'end_time', 'model', 'client_effort', 'request_efforts',
            'response_efforts', 'verdict', 'has_difference', 'calls',
        ) if key in turn})
    return dict(
        schema_version=1, scope='evidence', exported_at=now(),
        thread_id=session['id'], turns=turns,
        evidence=[minimal_evidence(associated.get(record.get('id'), record)) for record in records],
        contents='仅包含档位、调用关联、时间、状态和用量；不含消息及工具正文。',
        redaction='已按规则隐藏常见凭据；无法保证识别任意自由文本中的秘密。',
    )


def make_handler(store):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, fmt, *args):
            pass

        def trusted(self):
            host = self.headers.get('Host', '').split(':')[0]
            origin = self.headers.get('Origin')
            return host in ('127.0.0.1', 'localhost') and (
                not origin or urllib.parse.urlparse(origin).netloc == self.headers.get('Host')
            )

        def respond(self, value, status=200, conditional=False, download=None):
            value = redact(value)
            data = json.dumps(value, ensure_ascii=False).encode()
            etag = None
            if conditional and status == 200:
                stable = {key: item for key, item in value.items() if key != 'time'} if isinstance(value, dict) else value
                etag = '"' + hashlib.sha256(json.dumps(stable, sort_keys=True, ensure_ascii=False).encode()).hexdigest() + '"'
                if self.headers.get('If-None-Match') == etag:
                    self.send_response(304)
                    self.send_header('ETag', etag)
                    self.send_header('Cache-Control', 'private, no-cache')
                    self.end_headers()
                    return
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Cache-Control', 'private, no-cache' if conditional else 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            if etag:
                self.send_header('ETag', etag)
            if download:
                self.send_header('Content-Disposition', 'attachment; filename="' + download + '"')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if not self.trusted():
                return self.respond({'error': '仅允许本机同源访问'}, 403)
            route = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(route.query)
            field = lambda name, default='': query.get(name, [default])[0]
            try:
                if route.path == '/api/calls':
                    limit, offset = self.pagination(query, default=50, maximum=500)
                    client, result = field('client'), field('result')
                    if client not in ('', 'Codex App', 'Codex CLI', 'unknown'):
                        raise ValueError('client 仅支持 Codex App、Codex CLI 或 unknown')
                    if result not in ('', 'match', 'difference', 'pending'):
                        raise ValueError('result 仅支持 match、difference 或 pending')
                    purpose = field('purpose')
                    if purpose not in ('', 'conversation', 'title_generation', 'memory_consolidation', 'unknown'):
                        raise ValueError('不支持的调用用途')
                    return self.respond(store.calls(client=client, q=field('q'), result=result,
                                        thread_id=field('thread_id'), purpose=purpose, limit=limit, offset=offset), conditional=True)
                if route.path == '/api/call':
                    identifier = field('id')
                    if not identifier:
                        raise ValueError('缺少调用 id')
                    item = store.call(identifier)
                    return self.respond(item if item is not None else {'error': '未找到调用观测'},
                                        status=200 if item is not None else 404, conditional=True)
                if route.path == '/api/sessions':
                    return self.respond({'sessions': store.sessions(), 'time': now()}, conditional=True)
                if route.path == '/api/status':
                    return self.respond(dict(
                        monitor=store.monitor, processes=processes(), time=now(),
                        evidence_count=store.count_evidence(), sources=store.source_statuses(),
                        event_monitor=getattr(store, 'event_status', {'running': False}),
                        instrumentation=instrumentation_status(),
                        desktop_log_bridge=getattr(store, 'desktop_bridge_status', {'running': False}),
                        coverage=coverage_status(store),
                    ))
                if route.path in ('/api/session', '/api/export', '/api/tool'):
                    sid = field('id')
                    if not sid:
                        raise ValueError('缺少会话 id')
                    scope = field('scope', 'evidence')
                    if route.path == '/api/export' and scope not in ('evidence', 'conversation'):
                        raise ValueError('导出范围仅支持 evidence 或 conversation')
                    session = store.session(sid, with_logs=route.path != '/api/tool')
                    if session is None:
                        return self.respond({'error': '未找到会话'}, 404)
                    if route.path == '/api/session':
                        limit, offset = self.pagination(query, default=20, maximum=100)
                        return self.respond(session_page(session, limit, offset), conditional=True)
                    if route.path == '/api/tool':
                        turn_id, tool_id = field('turn_id'), field('tool_id')
                        for turn in session['turns']:
                            if turn['id'] == turn_id:
                                for tool in turn.get('tools', []):
                                    if tool['id'] == tool_id:
                                        return self.respond(tool, conditional=True)
                        return self.respond({'error': '未找到工具记录'}, 404)
                    if scope == 'evidence':
                        result = evidence_export(session, store.all_evidence(thread_id=sid))
                    else:
                        result = dict(session, schema_version=1, scope='conversation', exported_at=now(),
                                      contents='当前解析的消息、工具参数与返回、关联证据；不是原始日志备份。长消息和工具返回最多保留 16000 字符。',
                                      redaction='已按规则隐藏常见凭据；自由文本仍需在分享前检查。')
                    return self.respond(result, download='observatory-' + scope + '.json')
                if route.path == '/api/evidence/item':
                    identifier, thread_id = field('id'), field('thread_id')
                    if not identifier or not thread_id:
                        raise ValueError('缺少证据 id 或 thread_id')
                    with store.connect() as db:
                        row = db.execute('SELECT body FROM evidence WHERE id=? AND thread_id=?', (identifier, thread_id)).fetchone()
                    return self.respond(json.loads(row[0]) if row else {'error': '未找到证据'},
                                        status=200 if row else 404, conditional=True)
                if route.path == '/api/evidence':
                    limit, offset = self.pagination(query, default=100, maximum=500)
                    return self.respond(store.evidence_page(field('thread_id') or None, limit=limit, offset=offset), conditional=True)
            except ValueError as error:
                return self.respond({'error': str(error)}, 400)
            name = {
                '/': 'dashboard.html', '/sessions': 'index.html',
                '/dashboard.js': 'dashboard.js', '/dashboard-views.js': 'dashboard-views.js',
                '/dashboard.css': 'dashboard.css', '/app.js': 'app.js', '/views.js': 'views.js',
                '/format.js': 'format.js', '/dom.js': 'dom.js', '/style.css': 'style.css',
            }.get(route.path)
            if not name:
                return self.respond({'error': 'Not found'}, 404)
            data = (ROOT / 'web' / name).read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', {
                'html': 'text/html; charset=utf-8', 'js': 'text/javascript; charset=utf-8',
                'css': 'text/css; charset=utf-8',
            }[name.split('.')[-1]])
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        @staticmethod
        def pagination(query, default, maximum):
            try:
                limit = int(query.get('limit', [default])[0])
                offset = int(query.get('offset', [0])[0])
            except (TypeError, ValueError):
                raise ValueError('分页参数必须是整数') from None
            if limit < 1 or limit > maximum or offset < 0:
                raise ValueError(f'limit 须为 1–{maximum}，offset 须非负')
            return limit, offset

        def do_POST(self):
            if not self.trusted():
                return self.respond({'error': '仅允许本机同源访问'}, 403)
            if self.path in ('/api/capture/start', '/api/capture/stop'):
                return self.respond({'error': '已改为客户端事件常驻采集，无需限时内存捕获。'}, 410)
            if self.path == '/api/refresh':
                store.discover(force=True)
                return self.respond({'ok': True})
            if self.path.startswith('/relay/'):
                return relay_request(self, store)
            return self.respond({'error': 'Not found'}, 404)

    return Handler


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8766)
    args = parser.parse_args()
    store = Observatory()
    start_event_monitor(store, EVENT_ROOT)
    start_desktop_bridge(store)
    server = http.server.ThreadingHTTPServer(('127.0.0.1', args.port), make_handler(store))
    server.daemon_threads = True
    print(f'Effort Observatory http://localhost:{args.port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        store.stop_event.set()
        store.event_stop_event.set()
        server.server_close()
