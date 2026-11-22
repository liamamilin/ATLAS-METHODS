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
from classification_overrides import static_topics
from classify_unfiled import (
    AGENT, EXAMPLE_PROPOSE, MODEL, MODEL_ID, PROVIDER, SCHEMA_VERSION, TOOL_DENIES, PriceStop, Runner,
    evidence_error, load_excluded, prompt_for, review_retains,
)


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
                'tool_events': reply.get('tool_events', []),
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
    def run_trial(self, root, client, ids, after='', exclude=None, mode='trial', workers=10, **budget):
        database = build_db(root, ids)
        runner = Runner(
            client, root / 'out', database, root / 'none.json', exclude or [], after, mode, workers,
            budget.get('max_candidates'), budget.get('max_requests'), budget.get('max_runtime_seconds'),
            budget.get('clock'),
        )
        return runner.run(), database

    def test_rejects_slash_category_and_invented_topic_without_accepting_them(self):
        bad = good_decision()
        bad['category'] = 'operations/观测、日志与告警'
        bad['topic'] = '应用日志异常定位'
        skill = {'summary': SUMMARY, 'tasks': [{'goal': '根据日志检索异常', 'applicability': ''}],
                 'vocabulary': {'operations': [TOPIC]}}
        self.assertIn('category 必须是 allowed', evidence_error(bad, skill))
        bad['category'] = 'operations'
        self.assertIn('topic 必须与', evidence_error(bad, skill))
        bad['topic'] = TOPIC
        bad['evidence'] = [{'field': 'title', 'quote': '只看标题不能当证据', 'task_index': None}]
        self.assertIn('title', evidence_error(bad, skill))
        bad['evidence'] = [{'field': 'summary', 'quote': '这段不在摘要里的话', 'task_index': None}]
        self.assertIn('连续原文', evidence_error(bad, skill))
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
            self.assertEqual(command[:8], ['opencode', 'run', '--standalone', '--model', MODEL, '--format', 'json', '--agent'])
            self.assertEqual(command[8:10], [AGENT, '--title'])
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
            report, _ = self.run_trial(
                Path(temp), client, ids, mode='continuous', workers=30,
                max_candidates=100, max_requests=50, max_runtime_seconds=60,
            )
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
            self.assertEqual(report['calls'], 1)

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
        self.assertIn(json.dumps(EXAMPLE_PROPOSE, ensure_ascii=False), text)
        self.assertIn('propose_existing_category', text)
        self.assertIn('retain_unfiled', text)

    def test_prompt_example_passes_the_same_validator_and_object_evidence_does_not(self):
        skill = {
            'id': 'b' * 32, 'title': '标题', 'summary': SUMMARY, 'tasks': [{'goal': '根据日志检索异常', 'applicability': ''}],
            'original_classification': {'category': 'unfiled', 'topic': '线索不足', 'label_from': 'unfiled', 'review': 'unreviewed'},
            'vocabulary': {
                'operations': sorted(static_topics('operations')),
                'code': sorted(static_topics('code')),
                'business': sorted(static_topics('business')),
                'integration': sorted(static_topics('integration')),
            },
        }
        text = prompt_for(skill)
        self.assertIn(json.dumps(EXAMPLE_PROPOSE, ensure_ascii=False), text)
        self.assertIn('后端与 API', text)
        self.assertIn('前端与 Web 界面', text)
        self.assertIn('SEO 与搜索增长', text)
        self.assertIn('营销、CRM 与表单', text)
        self.assertIsNone(evidence_error(EXAMPLE_PROPOSE, skill))
        observed = dict(EXAMPLE_PROPOSE)
        observed['evidence'] = {'field': 'summary', 'quote': '应用日志里的异常'}
        message = evidence_error(observed, skill)
        self.assertIn('必须是数组', message)
        self.assertIn('object', message)
        spaced = dict(EXAMPLE_PROPOSE)
        spaced['category'] = 'integration'
        spaced['topic'] = '营销、CRM与表单'
        self.assertIn('逐字相同', evidence_error(spaced, skill))
        spaced['topic'] = '营销、CRM 与表单'
        self.assertIsNone(evidence_error(spaced, skill))
        classified = dict(EXAMPLE_PROPOSE)
        classified['decision'] = 'classify'
        self.assertIn('propose_existing_category', evidence_error(classified, skill))

    def test_correction_names_the_array_mismatch(self):
        with TemporaryDirectory() as temp:
            bad = dict(EXAMPLE_PROPOSE)
            bad['evidence'] = {'field': 'summary', 'quote': '应用日志里的异常'}
            client = FakeClient([{'json': bad}, {'json': good_decision()}])
            report, _ = self.run_trial(Path(temp), client, ['b' * 32], workers=1)
            self.assertEqual(report['proposed'], 1)
            self.assertIn('必须是数组', client.requests[1]['prompt'])
            self.assertIn('收到的是 object', client.requests[1]['prompt'])
            self.assertIn('{"field": "summary"', (Path(temp) / 'out/attempts' / ('b' * 32) / '001/rejected.txt').read_text(encoding='utf-8'))

    def test_old_schema_directory_is_not_rewritten_as_success(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            database = build_db(root, ['b' * 32])
            out = root / 'out'
            out.mkdir()
            state = {'schema_version': 'catalogue-classification-response/v1', 'model': MODEL,
                     'completed': {'b' * 32: {'status': 'error', 'fingerprint': 'old'}}}
            (out / 'state.json').write_text(json.dumps(state), encoding='utf-8')
            (out / 'attempts').mkdir()
            before = (out / 'state.json').read_text(encoding='utf-8')
            client = FakeClient()
            runner = Runner(client, out, database, root / 'none.json', [], '', 'trial', 1)
            with self.assertRaises(SystemExit) as caught:
                runner.run()
            self.assertIn(SCHEMA_VERSION, str(caught.exception))
            self.assertEqual((out / 'state.json').read_text(encoding='utf-8'), before)
            self.assertEqual(client.requests, [])

    def test_tool_events_are_isolated_and_not_accepted(self):
        with TemporaryDirectory() as temp:
            client = FakeClient([{'json': good_decision(), 'tool_events': ['tool']}])
            report, _ = self.run_trial(Path(temp), client, ['b' * 32], workers=1)
            self.assertEqual(report['proposed'], 0)
            self.assertEqual(report['errors'], 1)
            self.assertEqual(len(client.requests), 1)
            self.assertIn('tool events are not accepted', json.loads(
                (Path(temp) / 'out/attempts' / ('b' * 32) / 'receipt.json').read_text(encoding='utf-8'))['error'])

    def test_private_agent_denies_tools_without_writing_agents_md(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            executable = root / 'opencode'
            executable.write_text('''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
log = Path(sys.argv[0]).with_name('calls.txt')
log.write_text((log.read_text() if log.exists() else '') + ' '.join(sys.argv[1:]) + '\\n')
if sys.argv[1:3] == ['api', 'config.get']:
    print('[]')
    raise SystemExit
if sys.argv[1:3] == ['session', 'export']:
    print(json.dumps({'messages': [{'model': {'providerID': 'opencode-go', 'id': 'iceman-contributor-free'},
                                    'tokens': {'input': 1, 'output': 1}, 'cost': 0, 'finish': 'stop'}]}))
    raise SystemExit
cfg = json.loads(Path('opencode.json').read_text())
assert {'action': 'read', 'resource': '*', 'effect': 'deny'} in cfg['agents']['atlas-catalogue']['permissions']
assert {'action': '*', 'resource': '*', 'effect': 'deny'} in cfg['agents']['atlas-catalogue']['permissions']
assert not Path('AGENTS.md').exists()
assert os.environ['PWD'] == os.getcwd()
print(json.dumps({'type': 'tool', 'sessionID': 's', 'tool': 'read'}))
print(json.dumps({'type': 'text', 'sessionID': 's', 'part': {'text': '{}'}}) )
''', encoding='utf-8')
            executable.chmod(0o755)
            attempt = root / 'attempt'
            attempt.mkdir()
            reply = classify_unfiled.OpenCodeFreeClient(str(executable), timeout=10).request('prompt', 'title', attempt)
            self.assertEqual(reply['tool_events'], ['tool'])
            self.assertIn('--agent', reply['command'])
            self.assertIn(AGENT, reply['command'])
            self.assertNotIn('open-design', ' '.join(reply['command']))
            self.assertEqual(TOOL_DENIES[-1]['effect'], 'deny')

    def test_continuous_budgets_stop_without_a_second_wave(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaises(SystemExit):
                Runner(FakeClient(), root / 'out', root / 'missing.sqlite3', root / 'none.json', [], '', 'continuous', 10)
            client = FakeClient()
            report, _ = self.run_trial(
                root, client, ['b' * 32, 'c' * 32, 'd' * 32], mode='continuous', workers=1,
                max_candidates=2, max_requests=10, max_runtime_seconds=60,
            )
            self.assertEqual(len(client.requests), 2)
            self.assertTrue(report['candidate_budget_reached'])
            self.assertLessEqual(report['max_in_flight'], 10)

    def test_request_and_runtime_budgets_are_persistent(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeClient()
            report, database = self.run_trial(
                root, client, ['b' * 32, 'c' * 32], mode='continuous', workers=1,
                max_candidates=10, max_requests=1, max_runtime_seconds=60,
            )
            self.assertEqual(len(client.requests), 1)
            self.assertEqual(report['stopped'], 'request budget reached')
            again = FakeClient()
            resumed = Runner(again, root / 'out', database, root / 'none.json', [], '', 'continuous', 1, 10, 1, 60).run()
            self.assertEqual(again.requests, [])
            self.assertEqual(resumed['stopped'], 'request budget reached')

    def test_retain_is_not_a_permanent_exclude_and_review_ignores_title(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            proposals = {
                'records': [
                    {'id': 'a' * 32, 'decision': 'retain_unfiled', 'title': 'vite frontend DeepSpeed',
                     'summary': '这份摘要没有对应专题用词。', 'tasks': []},
                    {'id': 'b' * 32, 'decision': 'retain_unfiled', 'title': '无关标题',
                     'summary': '使用 fine tuning 训练模型。', 'tasks': [{'goal': '', 'applicability': ''}]},
                    {'id': 'c' * 32, 'decision': 'propose_existing_category'},
                ]
            }
            path = root / 'proposals.json'
            path.write_text(json.dumps(proposals), encoding='utf-8')
            excluded = load_excluded([path], root / 'none.json')
            self.assertIn('c' * 32, excluded)
            self.assertNotIn('a' * 32, excluded)
            self.assertNotIn('b' * 32, excluded)
            report = review_retains(path, root / 'review')
            by_id = {item['id']: item for item in report['records']}
            self.assertEqual(by_id['a' * 32]['taxonomy_hits_in_summary_or_tasks'], [])
            self.assertFalse(by_id['a' * 32]['permanent_exclude'])
            self.assertFalse(by_id['a' * 32]['title_used'])
            topics = {item['topic'] for item in by_id['b' * 32]['taxonomy_hits_in_summary_or_tasks']}
            self.assertIn('模型训练与微调', topics)
            self.assertFalse((root / 'catalog.sqlite3').exists())


if __name__ == '__main__':
    unittest.main()
