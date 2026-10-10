import copy
import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from observatory import Observatory, attach_evidence
from sanitization import redact
from server import make_handler, evidence_export
from test_http import LocalServer


class RedactionTests(unittest.TestCase):
    def test_common_free_text_forms_and_nested_json(self):
        secrets = ['env-synthetic', 'json-synthetic', 'query-synthetic', 'flag-synthetic', 'auth-synthetic']
        values = [
            'DB_PASSWORD="env-synthetic" command',
            'prefix {"password":"json-synthetic"} suffix',
            'https://example.invalid/a?access_token=query-synthetic&format=json',
            'tool --api-key flag-synthetic --format json',
            'https://user:auth-synthetic@example.invalid/',
        ]
        value = {'title': ' '.join(values), 'body': json.dumps({'nested': json.dumps({'password': 'json-synthetic'})})}
        clean = redact(value)
        for secret in secrets:
            self.assertNotIn(secret, json.dumps(clean))
        self.assertIn('format=json', clean['title'])
        self.assertEqual(redact(clean), clean)

    def test_minimum_export_preserves_conflicts_and_limits_nested_fields(self):
        record = dict(id='e', thread_id='s', turn_id='b', kind='response', output_ids=['msg-a'],
                      reasoning={'effort':'high','metadata':{'text':'private-reasoning-canary'}},
                      usage={'output_tokens':10,'debug':{'prompt':'private-usage-canary'}},
                      incomplete_details={'reason':'max_output_tokens','private':'private-incomplete-canary'})
        session = attach_evidence(dict(id='s', turns=[dict(id='a',output_ids=['msg-a']),dict(id='b')]), [record])
        result = evidence_export(session, [record])
        self.assertTrue(result['evidence'][0]['association_conflict'])
        self.assertIn('冲突', result['evidence'][0]['association'])
        for canary in ('private-reasoning-canary','private-usage-canary','private-incomplete-canary'):
            self.assertNotIn(canary, json.dumps(result))
        self.assertEqual(result['evidence'][0]['usage']['output_tokens'], 10)

    def test_unquoted_assignments_remain_idempotent(self):
        clean = redact('DB_PASSWORD=synthetic-secret --token token-canary')
        self.assertNotIn('synthetic-secret', clean)
        self.assertNotIn('token-canary', clean)
        self.assertEqual(redact(clean), clean)


class APIRegressionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        with patch.object(Observatory, 'import_archives'):
            self.store = Observatory(self.directory.name)
        self.store.discover = lambda **kwargs: None
        self.store.source_statuses = lambda: []
        self.session = {
            'id': 'synthetic-thread', 'title': 'DB_PASSWORD=title-canary',
            'available': True, 'unmatched': [], 'turns': [
                {'id': f'turn-{i}', 'messages': [{'role': 'user', 'text': 'private conversation canary'}],
                 'tools': [{'id': f'call-{i}', 'arguments': {'password': 'tool-canary'},
                            'result': 'private tool result', 'status': 'returned'}],
                 'evidence': [], 'client_effort': 'high', 'verdict': 'unknown'}
                for i in range(25)
            ],
        }
        self.store.session = lambda sid, with_logs=False: copy.deepcopy(self.session) if sid == self.session['id'] else None
        self.store.sessions = lambda: [{'id': self.session['id'], 'title': self.session['title']}]
        self.store.add({'id': 'ignored', 'thread_id': self.session['id'], 'turn_id': 'turn-24',
                        'kind': 'request', 'effort': 'high', 'source': 'test',
                        'user_message': 'private evidence message', 'endpoint': 'https://example.invalid/?access_token=query-canary'})
        self.evidence = self.store.all_evidence()[0]
        self.session['turns'][-1]['evidence'] = [self.evidence]
        self.local = LocalServer(make_handler(self.store))
        self.local.__enter__()
        self.addCleanup(self.local.__exit__)
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def get(self, path, headers=None):
        response = self.opener.open(urllib.request.Request(self.local.url + path, headers=headers or {}), timeout=5)
        with response:
            return json.load(response), response.headers

    def test_latest_turn_paging_and_lazy_tool(self):
        page, _ = self.get('/api/session?id=synthetic-thread&limit=20')
        self.assertEqual([t['id'] for t in page['turns']], [f'turn-{i}' for i in range(5, 25)])
        self.assertTrue(page['pagination']['has_more'])
        self.assertNotIn('arguments', page['turns'][0]['tools'][0])
        self.assertTrue(page['turns'][0]['tools'][0]['content_deferred'])
        self.assertNotIn('endpoint', page['turns'][-1]['evidence'][0])
        older, _ = self.get('/api/session?id=synthetic-thread&limit=20&offset=20')
        self.assertEqual(len(older['turns']), 5)
        self.assertFalse(older['pagination']['has_more'])
        tool, _ = self.get('/api/tool?id=synthetic-thread&turn_id=turn-5&tool_id=call-5')
        self.assertEqual(tool['result'], 'private tool result')
        self.assertNotIn('tool-canary', json.dumps(tool))

    def test_etag_304_and_changed_data(self):
        _, headers = self.get('/api/session?id=synthetic-thread')
        with self.assertRaises(urllib.error.HTTPError) as failure:
            self.get('/api/session?id=synthetic-thread', {'If-None-Match': headers['ETag']})
        self.assertEqual(failure.exception.code, 304)
        self.assertEqual(failure.exception.read(), b'')
        self.session['turns'][-1]['messages'].append({'text': 'a new message'})
        _, changed_headers = self.get('/api/session?id=synthetic-thread', {'If-None-Match': headers['ETag']})
        self.assertNotEqual(changed_headers['ETag'], headers['ETag'])

    def test_list_etag_does_not_change_with_server_clock(self):
        _, headers = self.get('/api/sessions')
        with patch('server.now', return_value='a different clock'):
            with self.assertRaises(urllib.error.HTTPError) as failure:
                self.get('/api/sessions', {'If-None-Match': headers['ETag']})
        self.assertEqual(failure.exception.code, 304)

    def test_minimum_export_excludes_conversation_and_tools(self):
        result, headers = self.get('/api/export?id=synthetic-thread')
        self.assertEqual(result['scope'], 'evidence')
        self.assertIn('attachment', headers['Content-Disposition'])
        text = json.dumps(result)
        for absent in ('private conversation canary', 'private tool result', 'private evidence message', 'title-canary'):
            self.assertNotIn(absent, text)
        full, _ = self.get('/api/export?id=synthetic-thread&scope=conversation')
        self.assertIn('private conversation canary', json.dumps(full))
        self.assertNotIn('title-canary', json.dumps(full))
        self.assertNotIn('query-canary', json.dumps(full))

    def test_evidence_pagination_and_scoped_raw_record(self):
        result, _ = self.get('/api/evidence?thread_id=synthetic-thread&limit=1')
        self.assertEqual(result['total'], 1)
        raw, _ = self.get('/api/evidence/item?id=' + self.evidence['id'] + '&thread_id=synthetic-thread')
        self.assertEqual(raw['id'], self.evidence['id'])
        self.assertNotIn('query-canary', json.dumps(raw))
        with self.assertRaises(urllib.error.HTTPError) as failure:
            self.get('/api/evidence/item?id=' + self.evidence['id'] + '&thread_id=other')
        self.assertEqual(failure.exception.code, 404)

    def test_bad_pagination_and_scope_are_rejected(self):
        for query in ('limit=0', 'limit=101', 'offset=-1', 'limit=oops'):
            with self.subTest(query=query), self.assertRaises(urllib.error.HTTPError) as failure:
                self.get('/api/session?id=synthetic-thread&' + query)
            self.assertEqual(failure.exception.code, 400)
        with self.assertRaises(urllib.error.HTTPError) as failure:
            self.get('/api/export?id=synthetic-thread&scope=typo')
        self.assertEqual(failure.exception.code, 400)


if __name__ == '__main__':
    unittest.main()
