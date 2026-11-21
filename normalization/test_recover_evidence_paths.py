import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from check_record import check_record
from recover_evidence_paths import (
    apply_plan, evaluate, evidence_journal_status, load_attempt_response, load_plan,
)


def sha256(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def request(source, numbered, wrong_note=''):
    payload = {'source_identity': source, 'sources': [{
        'path': source['skill_path'], 'coverage': 'full', 'source_line_count': 2,
        'delivered_ranges': [{'line_start': 1, 'line_end': 2}], 'numbered_source': numbered,
    }]}
    return '本次输出语言：zh-CN\n' + json.dumps(payload, ensure_ascii=False) + wrong_note


def response(path, extra_path=None):
    evidence = [{'path': path, 'line_start': 1, 'line_end': 2}]
    if extra_path:
        evidence.append({'path': extra_path, 'line_start': 1, 'line_end': 1})
    return {'content': {'name': 'fixture', 'title': 'Offline fixture', 'summary': None,
                        'evidence': evidence, 'tasks': [], 'rules': [{
                            'text': 'Fixture rule', 'when': None, 'rationale': None, 'evidence': evidence[:1]}],
                        'resources': [], 'notes': []}, 'gaps': []}


class RecoveryTests(unittest.TestCase):
    def build(self, root, wrong='skills/demo/WRONG.md', right='skills/demo/SKILL.md', extra=None):
        text = 'alpha\nbeta\n'
        source = {'collection_id': 'fixture-0', 'repository': 'fixture/repo', 'commit_sha': 'a' * 40,
                  'skill_path': right, 'git_blob_sha': 'b' * 40, 'entry_sha256': sha256(text)}
        key = 'a' * 32
        batch = root / 'batches' / '0001'
        attempt = batch / 'attempts' / key / '004'
        attempt.mkdir(parents=True)
        (attempt.parent / '003').mkdir()
        body = request(source, '1|alpha\n2|beta')
        (attempt.parent / '003' / 'request.txt').write_text(body, encoding='utf-8')
        (attempt / 'request.txt').write_text(body, encoding='utf-8')
        (attempt / 'response.txt').write_text(json.dumps(response(wrong, extra), ensure_ascii=False), encoding='utf-8')
        (attempt / 'metadata.json').write_text(json.dumps({'model': 'fixture-model', 'source': source, 'state': 'failed'}), encoding='utf-8')
        inputs = {'source': source, 'files': [{
            'path': right, 'coverage': 'full', 'sha256': sha256(text), 'bytes': len(text.encode()),
            'source_lines': 2,
        }]}
        (batch / 'inputs').mkdir()
        (batch / 'inputs' / (key + '.json')).write_text(json.dumps(inputs), encoding='utf-8')
        state = {key: {
            'collection_id': source['collection_id'], 'commit_sha': source['commit_sha'], 'key': key,
            'source': source, 'state': 'failed', 'failure_stage': 'model_or_validation',
            'error': 'evidence_unread: /content -> ' + wrong, 'attempt': f'attempts/{key}/004',
        }, 'filter': {
            'collection_id': 'filter-1', 'commit_sha': 'c' * 40, 'state': 'failed',
            'failure_stage': 'content_filter', 'error': 'blocked',
        }, 'skip': {
            'collection_id': 'skip-1', 'commit_sha': 'd' * 40, 'state': 'skipped_duplicate',
        }}
        (batch / 'state.json').write_text(json.dumps(state), encoding='utf-8')
        plan = {'attempt': '004', 'records': [{'batch': '0001', 'key': key, 'replacements': [{'from': wrong, 'to': right}]}]}
        return batch, key, plan, source

    def test_preview_is_read_only_and_apply_preserves_attempt_and_neighbors(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            batch, key, plan, source = self.build(root, extra='skills/demo/SKILL.md')
            before = {path: path.read_bytes() for path in (batch / 'attempts').rglob('*') if path.is_file()}
            preview = evaluate(root, plan)
            self.assertEqual(preview[0]['status'], 'ready')
            self.assertEqual(preview[0]['check_errors'], [])
            self.assertEqual(preview[0]['evidence_paths_replaced'], 2)
            self.assertEqual(preview[0]['evidence_paths_already_correct'], 1)
            self.assertFalse((batch / 'records').exists())
            self.assertEqual({path: path.read_bytes() for path in (batch / 'attempts').rglob('*') if path.is_file()}, before)
            neighbors = json.loads((batch / 'state.json').read_text())
            applied = apply_plan(root, plan)
            self.assertEqual(applied[0]['status'], 'applied')
            self.assertEqual({path: path.read_bytes() for path in (batch / 'attempts').rglob('*') if path.is_file()}, before)
            saved = json.loads((batch / 'records' / (key + '.json')).read_text())
            schema = json.loads((Path(__file__).resolve().parent / 'skill-record.v0.1.1.schema.json').read_text())
            checked = check_record(saved, schema, {'skills/demo/SKILL.md': 'alpha\nbeta\n'})
            self.assertEqual(checked['errors'], [])
            self.assertEqual(saved['extraction']['review_status'], 'unreviewed')
            self.assertEqual(load_attempt_response(batch / 'attempts' / key / '004')['content'], saved['content'])
            state = json.loads((batch / 'state.json').read_text())
            self.assertEqual(state[key]['state'], 'succeeded')
            self.assertEqual(state['filter'], neighbors['filter'])
            self.assertEqual(state['skip'], neighbors['skip'])
            journal = json.loads((batch / 'recoveries' / 'evidence-paths' / key / 'journal.json').read_text())
            self.assertEqual(journal['original_response_sha256'], hashlib.sha256(before[batch / 'attempts' / key / '004' / 'response.txt']).hexdigest())
            self.assertEqual(journal['record_sha256'], hashlib.sha256((batch / 'records' / (key + '.json')).read_bytes()).hexdigest())
            self.assertEqual(journal['source'], source)
            frozen = {path: path.read_bytes() for path in batch.rglob('*') if path.is_file()}
            again = apply_plan(root, plan)
            self.assertEqual(again[0]['status'], 'already_applied')
            self.assertFalse(again[0]['wrote'])
            self.assertEqual({path: path.read_bytes() for path in batch.rglob('*') if path.is_file()}, frozen)

    def test_existing_record_journal_mismatch_and_unapproved_records_write_nothing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            batch, key, plan, _source = self.build(root)
            (batch / 'records').mkdir()
            (batch / 'records' / (key + '.json')).write_text('{"schema_version":"0.1.1"}\n', encoding='utf-8')
            before = {path: path.read_bytes() for path in batch.rglob('*') if path.is_file()}
            with self.assertRaises(ValueError):
                apply_plan(root, plan)
            self.assertEqual({path: path.read_bytes() for path in batch.rglob('*') if path.is_file()}, before)
            (batch / 'records' / (key + '.json')).unlink()
            blocked = {'attempt': '004', 'records': [{'batch': '0001', 'key': 'filter', 'replacements': plan['records'][0]['replacements']}]}
            self.assertEqual(evaluate(root, blocked)[0]['status'], 'refused')
            before = {path: path.read_bytes() for path in batch.rglob('*') if path.is_file()}
            with self.assertRaises(ValueError):
                apply_plan(root, {'attempt': '004', 'records': [plan['records'][0], blocked['records'][0]]})
            self.assertEqual({path: path.read_bytes() for path in batch.rglob('*') if path.is_file()}, before)

    def test_rejects_other_paths_source_drift_and_non_path_journal_edits(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _batch, _key, plan, _source = self.build(root, extra='skills/demo/OTHER.md')
            self.assertEqual(evaluate(root, plan)[0]['status'], 'refused')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            batch, key, plan, source = self.build(root)
            request_path = batch / 'attempts' / key / '003' / 'request.txt'
            request_path.write_text(request(source, '1|alpha\n2|changed'), encoding='utf-8')
            self.assertIn('same source', evaluate(root, plan)[0]['reason'])
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            batch, key, plan, _source = self.build(root)
            apply_plan(root, plan)
            attempt = batch / 'attempts' / key / '004'
            corrected = json.loads((batch / 'recoveries' / 'evidence-paths' / key / 'response.json').read_text())
            corrected['content']['title'] = 'Changed title'
            (batch / 'recoveries' / 'evidence-paths' / key / 'response.json').write_text(json.dumps(corrected), encoding='utf-8')
            with self.assertRaises(ValueError):
                evidence_journal_status(attempt)
            original = (attempt / 'response.txt').read_bytes()
            (attempt / 'response.txt').write_bytes(original + b' ')
            with self.assertRaises(ValueError):
                load_attempt_response(attempt)

    def test_missing_metadata_writes_nothing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            batch, key, plan, _source = self.build(root)
            (batch / 'attempts' / key / '004' / 'metadata.json').unlink()
            before = {path: path.read_bytes() for path in batch.rglob('*') if path.is_file()}
            self.assertEqual(evaluate(root, plan)[0]['status'], 'ready')
            with self.assertRaises(ValueError):
                apply_plan(root, plan)
            self.assertEqual({path: path.read_bytes() for path in batch.rglob('*') if path.is_file()}, before)

    def test_plan_requires_one_explicit_replacement(self):
        with self.assertRaises(ValueError):
            load_plan_text({'attempt': '004', 'records': [{'batch': '0001', 'key': 'a' * 32, 'replacements': [
                {'from': 'a', 'to': 'b'}, {'from': 'c', 'to': 'd'}]}]})
        with self.assertRaises(ValueError):
            load_plan_text({'attempt': '003', 'records': [{'batch': '0001', 'key': 'a' * 32, 'replacements': [
                {'from': 'a', 'to': 'b'}]}]})


def load_plan_text(plan):
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / 'plan.json'
        path.write_text(json.dumps(plan), encoding='utf-8')
        return load_plan(path)


if __name__ == '__main__':
    unittest.main()
