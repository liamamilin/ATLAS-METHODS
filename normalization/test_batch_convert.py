import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from batch_convert import OpenCodeClient, key_of, prepare_input, run_batch, write_json


def blob(data):
    return hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()


def response(path='skills/demo/SKILL.md', start=1, end=2):
    ev = [{'path': path, 'line_start': start, 'line_end': end}]
    return {'content': {'name': 'fixture', 'title': 'Offline fixture', 'summary': None,
                        'evidence': ev, 'tasks': [], 'rules': [
                            {'text': 'Fixture rule', 'when': None, 'rationale': None, 'evidence': ev}],
                        'resources': [], 'notes': []}, 'gaps': []}


class FakeClient:
    def __init__(self, replies=None):
        self.calls = []
        self.replies = list(replies or [])

    def call(self, prompt, attempt):
        self.calls.append(prompt)
        value = self.replies.pop(0) if self.replies else response()
        text = value if isinstance(value, str) else json.dumps(value)
        (attempt / 'response.txt').write_text(text)
        return {'text': text, 'session_id': f'fixture-{len(self.calls)}', 'finish': 'stop',
                'tool_events': [], 'usage': {'available': True, 'turns': [
                    {'tokens': {'input': 10, 'output': 5, 'cache': {'read': 2}}, 'cost': 0}]}}


class BatchTests(unittest.TestCase):
    def fixture(self, root, count=2):
        collection = root / 'collection'
        collection.mkdir()
        data = b'first\nsecond\nthird\nfourth\nfifth\nsixth\n'
        raw = collection / 'raw/unique-path/source.md'
        raw.parent.mkdir(parents=True)
        raw.write_bytes(data)
        ref = collection / 'raw/unique-path/guide.md'
        ref.write_bytes(data)
        path = 'skills/demo/SKILL.md'
        files = [{'path': path, 'status': 'saved', 'git_blob_sha': blob(data),
                  'raw_path': 'raw/unique-path/source.md'},
                 {'path': 'guide.md', 'status': 'saved', 'git_blob_sha': blob(data),
                  'raw_path': 'raw/unique-path/guide.md'}]
        rows, items = [], []
        for i in range(count):
            sha = str(i) * 40
            manifest = f'manifest-{i}.json'
            write_json(collection / manifest, {'repository': f'fixture/repo{i}', 'commit_sha': sha, 'files': files})
            rows.append({'id': f'fixture-{i}', 'repository': f'fixture/repo{i}', 'commit_sha': sha,
                         'skill_path': path, 'git_blob_sha': blob(data), 'sha256': hashlib.sha256(data).hexdigest(),
                         'state': 'collected', 'raw_path': 'raw/unique-path/source.md',
                         'repository_manifest': manifest, 'collection_root': '.'})
            items.append({'collection_id': f'fixture-{i}', 'commit_sha': sha})
        index = collection / 'skills.jsonl.gz'
        with gzip.open(index, 'wt') as f:
            f.write('\n'.join(json.dumps(r) for r in rows))
        selection = root / 'selection.json'
        write_json(selection, {'items': items})
        args = argparse.Namespace(index=index, selection=selection, out=root / 'normalized',
                                  language='zh-CN', model='open-design/fixture', opencode='unused',
                                  timeout=5, workers=1, limit=None, max_attempts=2, retry_failed=False)
        return args, rows, items

    def test_same_body_distinct_sources_resume_and_crash_recovery(self):
        with tempfile.TemporaryDirectory() as t:
            args, rows, items = self.fixture(Path(t))
            client = FakeClient()
            report = run_batch(args, client)
            self.assertEqual(report['succeeded'], 2)
            self.assertEqual(report['usage']['tokens']['input'], 20)
            records = [json.loads(s) for s in (args.out / 'records.jsonl').read_text().splitlines()]
            self.assertEqual([r['source']['collection_id'] for r in records], ['fixture-0', 'fixture-1'])
            self.assertEqual(records[0]['source']['entry_sha256'], rows[0]['sha256'])
            self.assertEqual(records[0]['extraction']['review_status'], 'unreviewed')
            # The original Git path differs from the storage path on purpose.
            self.assertEqual(records[0]['source']['skill_path'], 'skills/demo/SKILL.md')
            previous = (args.out / 'records.jsonl').read_bytes()
            (args.out / 'state.json').unlink()
            no_call = FakeClient()
            second = run_batch(args, no_call)
            self.assertEqual(no_call.calls, [])
            self.assertEqual(second['invocation']['skipped_success'], 2)
            self.assertEqual(previous, (args.out / 'records.jsonl').read_bytes())

    def test_failure_does_not_block_and_explicit_retry_preserves_success(self):
        with tempfile.TemporaryDirectory() as t:
            args, rows, items = self.fixture(Path(t))
            client = FakeClient(['bad JSON', 'bad JSON', response()])
            first = run_batch(args, client)
            self.assertEqual((first['succeeded'], first['failed']), (1, 1))
            self.assertEqual(first['usage']['tokens']['input'], 30)  # Failed calls included.
            good = args.out / 'records' / (key_of(items[1]) + '.json')
            original = good.read_bytes()
            no_call = FakeClient()
            second = run_batch(args, no_call)
            self.assertEqual(no_call.calls, [])
            self.assertEqual(second['invocation']['skipped_failed'], 1)
            state_path = args.out / 'state.json'
            states = json.loads(state_path.read_text())
            states[key_of(items[0])]['review_feedback'] = 'Fixture semantic correction: preserve the exception.'
            write_json(state_path, states)
            args.retry_failed = True
            retried = FakeClient()
            final = run_batch(args, retried)
            self.assertEqual((final['succeeded'], final['failed']), (2, 0))
            self.assertIn('Fixture semantic correction', retried.calls[0])
            self.assertEqual(good.read_bytes(), original)
            self.assertEqual((args.out / 'failures.jsonl').read_text(), '')

    def test_retry_format_error_without_inventing_content(self):
        with tempfile.TemporaryDirectory() as t:
            args, rows, items = self.fixture(Path(t), 1)
            client = FakeClient(['```json\n{}\n```', response()])
            r = run_batch(args, client)
            self.assertEqual(r['succeeded'], 1)
            self.assertEqual(len(client.calls), 2)
            self.assertIn('上一次结果未通过检查', client.calls[1])
            self.assertTrue((args.out / 'attempts' / key_of(items[0]) / '001/response.txt').exists())

    def test_actual_ranges_and_core_reference_not_memory_reads(self):
        with tempfile.TemporaryDirectory() as t:
            args, rows, items = self.fixture(Path(t), 1)
            items[0].update(entry_ranges=[{'line_start': 2, 'line_end': 3}],
                            references=[{'path': 'guide.md', 'ranges': [{'line_start': 5, 'line_end': 6}]}])
            write_json(args.selection, {'items': items})
            texts, reads, metadata, blocks = prepare_input(rows[0], items[0], args.index)
            self.assertEqual(blocks[0]['numbered_source'], '2|second\n3|third')
            self.assertEqual(blocks[1]['numbered_source'], '5|fifth\n6|sixth')
            self.assertNotIn('1|first', blocks[0]['numbered_source'])
            client = FakeClient([response(start=2, end=3)])
            r = run_batch(args, client)
            self.assertEqual(r['succeeded'], 1)
            record = json.loads((args.out / 'records.jsonl').read_text())
            self.assertEqual(record['extraction']['scope'], 'entry_and_references')
            self.assertEqual(record['extraction']['files_read'], reads)

    def test_unread_or_outside_range_evidence_is_failed(self):
        with tempfile.TemporaryDirectory() as t:
            args, rows, items = self.fixture(Path(t), 1)
            args.max_attempts = 1
            r = run_batch(args, FakeClient([response(path='unread.md')]))
            self.assertEqual(r['failed'], 1)
            self.assertIn('evidence_unread', (args.out / 'failures.jsonl').read_text())
            self.assertEqual(list((args.out / 'records').glob('*.json')), [])

    def test_corrupt_bytes_not_sent_to_model_and_pending_limit(self):
        with tempfile.TemporaryDirectory() as t:
            args, rows, items = self.fixture(Path(t))
            args.limit = 1
            (args.index.parent / rows[0]['raw_path']).write_bytes(b'changed')
            client = FakeClient()
            r = run_batch(args, client)
            self.assertEqual(client.calls, [])
            self.assertEqual((r['failed'], r['pending']), (1, 1))
            remaining = json.loads((args.out / 'remaining.selection.json').read_text())
            self.assertEqual(len(remaining['items']), 2)

    def test_cli_location_permissions_response_and_exported_usage(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            executable = root / 'fake-opencode'
            executable.write_text('''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
assert Path(os.environ['PWD']).resolve() == Path.cwd().resolve()
assert sys.stdin.read() == ''
c=json.loads(Path('opencode.json').read_text())
assert c['agents']['atlas-normalizer']['permissions'][-1] == {'action':'*','resource':'*','effect':'deny'}
assert c['agents']['atlas-normalizer']['steps'] == 2
if sys.argv[1]=='run':
    print(json.dumps({'type':'text','sessionID':'fixture-session','part':{'text':'{"content":{},"gaps":[]}'}}))
else:
    assert sys.argv[1:3]==['session','export']
    print(json.dumps({'messages':[{'type':'assistant','finish':'stop','tokens':{'input':17,'output':9},'cost':0}]}))
''')
            executable.chmod(0o755)
            attempt = root / 'attempt'
            attempt.mkdir()
            saved = os.dup(0)
            reader, writer = os.pipe()
            os.write(writer, b'parent heredoc must not reach the model\n')
            os.close(writer)
            try:
                os.dup2(reader, 0)
                os.close(reader)
                with patch.dict('os.environ', {'PWD': '/a/different/project'}):
                    reply = OpenCodeClient(str(executable), 'open-design/fixture', 10).call('fixture prompt', attempt)
            finally:
                os.dup2(saved, 0)
                os.close(saved)
            self.assertEqual(reply['text'], '{"content":{},"gaps":[]}')
            self.assertEqual(reply['usage']['turns'][0]['tokens']['input'], 17)
            self.assertEqual(reply['finish'], 'stop')
            self.assertNotIn('transport_error', reply)
            self.assertTrue((attempt / 'events.jsonl').exists())

    def test_unexpected_single_item_error_does_not_stop_later_items(self):
        class OnceBroken(FakeClient):
            def call(self, prompt, attempt):
                if not self.calls:
                    self.calls.append(prompt)
                    raise RuntimeError('fixture client failure')
                return super().call(prompt, attempt)
        with tempfile.TemporaryDirectory() as t:
            args, rows, items = self.fixture(Path(t))
            r = run_batch(args, OnceBroken())
            self.assertEqual((r['succeeded'], r['failed']), (1, 1))


if __name__ == '__main__':
    unittest.main()
