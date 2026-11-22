import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time
from tempfile import TemporaryDirectory
import unittest

import classify_unfiled
from build import SCHEMA
from classify_unfiled import MODEL, MODEL_ID, PROVIDER, PriceStop, Runner, evidence_error, prompt_for


SUMMARY = '根据目录摘要定位应用日志里的异常并给出检索范围。'
TOPIC = '观测、日志与告警'


def free_usage(cost=0):
    return {'available': True, 'turns': [{'providerID': PROVIDER, 'id': MODEL_ID, 'cost': cost}]}


def good_decision():
    return {
        'decision': 'propose_existing_category', 'category': 'operations', 'topic': TOPIC,
        'evidence': [{'field': 'summary', 'quote': '应用日志里的异常'}],
        'reason': '摘要说的是日志异常定位，对应既有观测专题。', 'retention_reason': None,
    }


class FakeClient:
    def __init__(self, replies=None, price=None):
        self.replies = list(replies or [])
        self.price = price or {'ok': True}
        self.requests = []
        self.sleeps = []
        self.sleeper = self.sleeps.append
        self.inflight = 0
        self.max_inflight = 0
        self.gate = threading.Lock()

    def check_price(self):
        if not self.price.get('ok', True):
            raise PriceStop(self.price.get('reason', 'not free'))
        return {'model': MODEL, 'cost': [{'input': 0, 'output': 0, 'cache': {'read': 0, 'write': 0}}]}

    def request(self, prompt, title, attempt):
        with self.gate:
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            if self.price.get('delay'):
                time.sleep(self.price['delay'])
            self.requests.append({'prompt': prompt, 'title': title})
            reply = self.replies.pop(0) if self.replies else {'json': good_decision()}
            text = reply.get('text', json.dumps(reply.get('json', good_decision()), ensure_ascii=False))
            return {
                'text': text, 'usage': reply.get('usage', free_usage()), 'duration_seconds': 0.01,
                'rate_limit_seconds': reply.get('rate_limit_seconds'),
                'quota_exhausted': reply.get('quota_exhausted', False),
            }
        finally:
            with self.gate:
                self.inflight -= 1


def build_db(root, ids):
    database = root / 'catalog.sqlite3'
    connection = sqlite3.connect(database)
    connection.executescript(SCHEMA)
    for index, skill_id in enumerate(ids):
        category = 'code' if skill_id.startswith('keep') else 'unfiled'
        connection.execute('INSERT INTO skills VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)', (
            skill_id, f'标题{index}', SUMMARY, category, 'unfiled' if category == 'unfiled' else 'rule',
            '线索不足' if category == 'unfiled' else '待细分', 'unreviewed',
            '2026-11-01T00:00:00+00:00', 'owner/example', '', 1, skill_id, SUMMARY,
        ))
        connection.execute('INSERT INTO sources VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', (
            skill_id, skill_id, 'c' + skill_id[:8], 'owner/example', f'skills/{index}/SKILL.md', 'a' * 40,
            'b' * 40, 'c' * 64, '[]', None, None, 'representative',
        ))
        connection.execute(
            'INSERT INTO catalog_tasks(skill, position, goal, applicability, outputs, tags) VALUES (?,?,?,?,?,?)',
            (skill_id, 1, '根据日志检索异常', '已有应用日志时', '[]', '{}'),
        )
    connection.commit()
    connection.close()
    return database


class ClassificationRunnerTests(unittest.TestCase):
    def run_trial(self, root, client, ids, after='', exclude=None, mode='trial', workers=10):
        database = build_db(root, ids)
        runner = Runner(client, root / 'out', database, root / 'none.json', exclude or [], after, mode, workers)
        return runner.run(), database

    def test_rejects_slash_category_and_invented_topic_without_accepting_them(self):
        bad = good_decision()
        bad['category'] = 'operations/观测、日志与告警'
        bad['topic'] = '应用日志异常定位'
        skill = {'summary': SUMMARY, 'tasks': [{'goal': '根据日志检索异常', 'applicability': ''}],
                 'vocabulary': {'operations': [TOPIC]}}
        self.assertIn('category must be an existing category id', evidence_error(bad, skill))
        bad['category'] = 'operations'
        self.assertIn('topic must be an existing topic name', evidence_error(bad, skill))
        bad['topic'] = TOPIC
        bad['evidence'] = [{'field': 'title', 'quote': '只看标题不能当证据'}]
        self.assertIn('title alone', evidence_error(bad, skill))
        bad['evidence'] = [{'field': 'summary', 'quote': '这段不在摘要里的话'}]
        self.assertIn('not a substring', evidence_error(bad, skill))
        bad['evidence'] = [{'field': 'summary', 'quote': '应用日志里的异常'}]
        self.assertIsNone(evidence_error(bad, skill))

    def test_invalid_json_is_kept_and_not_turned_into_a_proposal(self):
        with TemporaryDirectory() as temp:
            client = FakeClient([{'text': 'category = operations/观测'}, {'text': 'still not json'}])
            report, _ = self.run_trial(Path(temp), client, ['b' * 32])
            self.assertEqual(report['errors'], 1)
            self.assertEqual(report['proposed'], 0)
            rejected = list((Path(temp) / 'out/attempts').glob('*/001/rejected.txt'))
            self.assertTrue(rejected and 'operations/观测' in rejected[0].read_text(encoding='utf-8'))
            proposals = json.loads((Path(temp) / 'out/proposals.json').read_text(encoding='utf-8'))
            self.assertEqual(proposals['records'], [])

    def test_paid_price_blocks_every_request_and_there_is_no_fallback_model(self):
        with TemporaryDirectory() as temp:
            client = FakeClient(price={'ok': False, 'reason': 'output price is no longer 0'})
            report, database = self.run_trial(Path(temp), client, ['b' * 32])
            self.assertEqual(client.requests, [])
            self.assertIn('no longer 0', report['stopped'])
            self.assertNotIn('open-design', json.dumps(report))
            command = classify_unfiled.OpenCodeFreeClient('opencode').command('ATLAS catalogue classification', 'prompt')
            self.assertEqual(command[:6], ['opencode', 'run', '--standalone', '--model', MODEL, '--format'])
            self.assertEqual(command[6:8], ['json', '--title'])
            self.assertEqual(hashlib.sha256(database.read_bytes()).hexdigest(), hashlib.sha256(database.read_bytes()).hexdigest())

    def test_nonzero_response_cost_stops_later_items(self):
        with TemporaryDirectory() as temp:
            client = FakeClient([
                {'json': good_decision(), 'usage': free_usage(1)},
                {'json': good_decision()},
            ])
            report, _ = self.run_trial(Path(temp), client, ['b' * 32, 'c' * 32], workers=1)
            self.assertEqual(len(client.requests), 1)
            self.assertIn('cost was 1', report['stopped'])
            self.assertEqual(report['proposed'], 0)

    def test_concurrency_never_exceeds_ten(self):
        with TemporaryDirectory() as temp:
            client = FakeClient(price={'ok': True, 'delay': 0.02})
            ids = [f'{index:032x}' for index in range(1, 13)]
            report, _ = self.run_trial(Path(temp), client, ids, mode='continuous', workers=30)
            self.assertLessEqual(report['max_in_flight'], 10)
            self.assertEqual(report['workers'], 10)
            self.assertLessEqual(client.max_inflight, 10)

    def test_rate_limit_backoff_is_recorded_and_then_succeeds(self):
        with TemporaryDirectory() as temp:
            client = FakeClient([
                {'text': '', 'rate_limit_seconds': 0.01},
                {'json': good_decision()},
            ])
            report, _ = self.run_trial(Path(temp), client, ['b' * 32], workers=1)
            self.assertEqual(client.sleeps, [0.01])
            self.assertEqual(report['proposed'], 1)
            self.assertTrue((Path(temp) / 'out/attempts' / ('b' * 32) / '001/backoff.json').is_file())

    def test_resume_does_not_repeat_a_completed_fingerprint(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeClient()
            _, database = self.run_trial(root, client, ['b' * 32])
            again = FakeClient()
            report = Runner(again, root / 'out', database, root / 'none.json', [], '', 'trial', 1).run()
            self.assertEqual(again.requests, [])
            self.assertEqual(report['calls'], 0)

    def test_fingerprint_conflict_does_not_call_the_model_or_replace_the_proposal(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeClient()
            self.run_trial(root, client, ['b' * 32])
            database = root / 'catalog.sqlite3'
            connection = sqlite3.connect(database)
            connection.execute('UPDATE skills SET summary = ? WHERE id = ?', ('完全不同的摘要内容，用来制造指纹冲突。', 'b' * 32))
            connection.commit()
            connection.close()
            before = json.loads((root / 'out/proposals.json').read_text(encoding='utf-8'))
            again = FakeClient()
            runner = Runner(again, root / 'out', database, root / 'none.json', [], '', 'trial', 1)
            report = runner.run()
            self.assertEqual(again.requests, [])
            self.assertEqual(report['conflicts'], 1)
            after = json.loads((root / 'out/proposals.json').read_text(encoding='utf-8'))
            self.assertEqual(after['records'][0]['summary'], before['records'][0]['summary'])

    def test_second_process_does_not_start_a_copy(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            database = build_db(root, ['b' * 32])
            out = root / 'out'
            out.mkdir()
            held = (out / 'runner.lock').open('a+')
            __import__('fcntl').flock(held, __import__('fcntl').LOCK_EX | __import__('fcntl').LOCK_NB)
            runner = Runner(FakeClient(), out, database, root / 'none.json', [], '', 'trial', 1)
            with self.assertRaises(SystemExit):
                runner.run()
            self.assertEqual(runner.client.requests, [])
            held.close()

    def test_catalogue_file_is_not_written(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeClient()
            _, database = self.run_trial(root, client, ['b' * 32])
            digest = hashlib.sha256(database.read_bytes()).hexdigest()
            overrides = root / 'overrides.json'
            self.assertFalse(overrides.exists())
            self.assertEqual(hashlib.sha256(database.read_bytes()).hexdigest(), digest)
            proposal = json.loads((root / 'out/proposals.json').read_text(encoding='utf-8'))
            self.assertEqual(proposal['records'][0]['review'], 'unreviewed')
            self.assertEqual(proposal['records'][0]['proposed_category'], 'operations')
            self.assertEqual(proposal['records'][0]['proposed_topic'], TOPIC)
            self.assertNotIn('batch_convert', Path(classify_unfiled.__file__).read_text(encoding='utf-8'))

    def test_prompt_lists_exact_topic_names(self):
        skill = {'id': 'b' * 32, 'title': '标题', 'summary': SUMMARY, 'tasks': [],
                 'original_classification': {'category': 'unfiled', 'topic': '线索不足', 'label_from': 'unfiled', 'review': 'unreviewed'},
                 'vocabulary': {'operations': [TOPIC]}}
        text = prompt_for(skill)
        self.assertIn(TOPIC, text)
        self.assertIn('不能写成', text)


if __name__ == '__main__':
    unittest.main()
