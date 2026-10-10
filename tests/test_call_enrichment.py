import json
from pathlib import Path
import tempfile
import unittest

from call_log import calls_page, call_rows
from observatory import Observatory


class EnrichmentTests(unittest.TestCase):
    def test_native_enrichment_preserves_id_count_time_and_existing_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Observatory(directory, source_paths=[], archive_root=Path(directory) / 'none')
            record = dict(source='codex_trace', event_id='event-one', capture_id='call-one',
                          kind='response', event_type='inference_failed', status='failed',
                          thread_id='thread', turn_id='turn', observed_at='2026-10-08T01:45:00Z')
            self.assertEqual(store.add(record), 1)
            old = store.all_evidence()[0]
            self.assertEqual(store.add(dict(record, http_status=404, error_code='model_not_supported',
                                           error_message='上游不支持请求的模型')), 0)
            store.add(dict(record, error_code='unknown', error_message='请求失败，未识别到具体原因'))
            store.add(record)
            current = store.all_evidence()[0]
            self.assertEqual(store.count_evidence(), 1)
            for key in ('id', 'observed_at', 'capture_id', 'status'):
                self.assertEqual(current[key], old[key])
            self.assertEqual(current['error_code'], 'model_not_supported')
            self.assertEqual(current['error_message'], '上游不支持请求的模型')
            self.assertEqual(current['http_status'], 404)

    def test_same_event_id_cannot_replace_other_call_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Observatory(directory, source_paths=[], archive_root=Path(directory) / 'none')
            record = dict(source='codex_trace', event_id='event-one', kind='response',
                          capture_id='first', event_type='inference_failed', status='failed')
            store.add(record)
            store.add(dict(record, capture_id='second', http_status=404))
            self.assertEqual(store.count_evidence(), 2)
            first = next(r for r in store.all_evidence() if r['capture_id'] == 'first')
            self.assertNotIn('http_status', first)

    def test_purpose_filter_keeps_main_success_separate_from_auxiliary_failures(self):
        records = []
        def call(capture, thread, model, status, stamp, **extra):
            records.extend([
                dict(id=capture+'-req', kind='request', client='Codex CLI', thread_id=thread, turn_id='t',
                     capture_id=capture, model=model, effort='high', observed_at=stamp),
                dict(id=capture+'-res', kind='response', source='codex_trace' if status == 'failed' else 'codex_event_log',
                     client='Codex CLI', thread_id=thread, turn_id='t', capture_id=capture,
                     status=status, observed_at=stamp, **extra),
            ])
        call('main','main-thread','model-main','completed','2026-10-08T01:45:00Z',effort='high')
        for index in range(6):
            call(str(index),'background','model-other','failed',f'2026-10-08T01:45:0{index+1}Z',
                 http_status=404,error_code='model_not_supported',error_message='SECRET arbitrary text')
        contexts = {
            ('main-thread','t'):dict(purpose='conversation',purpose_label='主对话',purpose_source='native_thread_source'),
            ('background','t'):dict(purpose='memory_consolidation',purpose_label='后台记忆合并',purpose_source='native_turn_trigger'),
        }
        main = calls_page(records, {}, client='Codex CLI', purpose='conversation', contexts=contexts)
        self.assertEqual(main['total'], 1)
        self.assertEqual(main['items'][0]['result'], 'match')
        back = calls_page(records, {}, purpose='memory_consolidation', contexts=contexts)
        self.assertEqual(back['total'], 6)
        self.assertEqual({r['attempt_index'] for r in back['items']}, set(range(1,7)))
        self.assertTrue(all(r['attempt_count']==6 and r['auxiliary'] for r in back['items']))
        self.assertTrue(all(r['response']['error_message']=='上游不支持请求的模型' for r in back['items']))
        self.assertNotIn('SECRET', json.dumps(back))
        self.assertTrue(all(r['session_title']=='后台记忆合并 · 独立辅助会话' for r in back['items']))
        unknown = call_rows(records, {})
        self.assertTrue(all(r['purpose']=='unknown' for r in unknown))


if __name__ == '__main__':
    unittest.main()
