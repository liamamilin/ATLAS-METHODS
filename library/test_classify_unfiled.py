import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
from tempfile import TemporaryDirectory
import unittest

import classify_unfiled
from build import SCHEMA
from classification_overrides import static_topics
from classify_unfiled import (
    AGENT, EXAMPLE_PROPOSE, MODEL, MODEL_ID, PROVIDER, SCHEMA_VERSION, TOOL_DENIES, CostUnknown, PriceStop,
    Runner, combine_ledgers, cost_gate, evidence_error, export_usage, load_excluded, prompt_for, recheck_exports,
    review_retains,
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
                'events': reply.get('events', []),
                'exit_code': reply.get('exit_code', 0),
                'timed_out': reply.get('timed_out', False),
                'session_id': reply.get('session_id'),
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
            budget.get('clock'), cost_policy=budget.get('cost_policy', 'strict'),
            resume_missing_cost_halt=budget.get('resume_missing_cost_halt', False),
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
            # /var on macOS is a symlink. Put the client temp dir behind one so
            # PWD and getcwd must be the same physical path or the fake CLI stops
            # before it can emit the tool event.
            physical = root / 'physical-tmp'
            physical.mkdir()
            logical = root / 'logical-tmp'
            logical.symlink_to(physical, target_is_directory=True)
            previous_tempdir = tempfile.tempdir
            tempfile.tempdir = str(logical)
            try:
                reply = classify_unfiled.OpenCodeFreeClient(str(executable), timeout=10).request('prompt', 'title', attempt)
            finally:
                tempfile.tempdir = previous_tempdir
            stderr = (attempt / 'stderr.txt').read_text(encoding='utf-8')
            self.assertEqual(reply['exit_code'], 0, stderr)
            self.assertEqual(stderr, '')
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

    def test_error_turns_without_cost_stay_unknown_when_the_last_turn_is_zero(self):
        session = {
            'info': {'id': 'ses_recorded', 'cost': 0, 'outcome': 'succeeded',
                     'tokens': {'input': 688, 'output': 187}},
            'messages': [
                {'type': 'agent-switched', 'info': {'cost': 0}},
                {'type': 'user', 'info': {'cost': 0, 'tokens': {'input': 0, 'output': 0}}},
                {'type': 'synthetic'},
                {'type': 'idle', 'info': {'cost': 0}},
                {'id': 'msg-err-2', 'type': 'assistant', 'finish': 'error',
                 'model': {'providerID': PROVIDER, 'id': MODEL_ID},
                 'error': {'type': 'provider.invalid-output', 'status': 200,
                           'message': 'OpenAI Chat stream ended without finish_reason'},
                 'retry': {'attempt': 2}},
                {'id': 'msg-err-3', 'type': 'assistant', 'finish': 'error',
                 'model': {'providerID': PROVIDER, 'id': MODEL_ID},
                 'error': {'type': 'provider.invalid-output', 'status': 200,
                           'message': 'OpenAI Chat stream ended without finish_reason'},
                 'retry': {'attempt': 3}},
                {'id': 'msg-stop', 'type': 'assistant', 'finish': 'stop', 'cost': 0,
                 'tokens': {'input': 688, 'output': 187},
                 'model': {'providerID': PROVIDER, 'id': MODEL_ID}},
            ],
        }
        usage = export_usage(json.dumps(session).encode())
        self.assertEqual(usage['cost_status'], 'unknown')
        self.assertEqual(len(usage['turns']), 3)
        self.assertEqual([turn['cost_known'] for turn in usage['turns']], [False, False, True])
        self.assertEqual([turn['cli_retry_attempt'] for turn in usage['turns']], [2, 3, None])
        self.assertFalse(usage['available'])
        with self.assertRaises(CostUnknown):
            cost_gate(usage)
        kept, status = combine_ledgers(usage['turns'], usage['turns'][-1:])
        self.assertEqual(status, 'unknown')
        self.assertEqual(len(kept), 3)

    def test_unknown_cost_stops_new_runs_and_keeps_the_error_turn(self):
        with TemporaryDirectory() as temp:
            usage = export_usage(json.dumps({'messages': [
                {'id': 'msg-err', 'type': 'assistant', 'finish': 'error',
                 'model': {'providerID': PROVIDER, 'id': MODEL_ID},
                 'error': {'type': 'provider.invalid-output', 'status': 200,
                           'message': 'OpenAI Chat stream ended without finish_reason'}},
                {'id': 'msg-stop', 'type': 'assistant', 'finish': 'stop', 'cost': 0,
                 'tokens': {'input': 1, 'output': 1}, 'model': {'providerID': PROVIDER, 'id': MODEL_ID}},
            ]}).encode())
            client = FakeClient([
                {'json': good_decision(), 'usage': usage, 'session_id': 'ses_recorded', 'events': [
                    {'type': 'step_start'}, {'type': 'error', 'error': {
                        'type': 'provider.invalid-output', 'status': 200,
                        'message': 'OpenAI Chat stream ended without finish_reason'}},
                    {'type': 'step_start'},
                ]},
                {'json': good_decision(), 'session_id': 'ses_ok'},
            ])
            report, database = self.run_trial(Path(temp), client, ['b' * 32, 'c' * 32, 'd' * 32], workers=1)
            self.assertEqual(len(client.requests), 1)
            self.assertEqual(report['stopped'], 'cost_unknown')
            self.assertEqual(report['proposed'], 0)
            self.assertEqual(report['cost_unknown'], 1)
            state = json.loads((Path(temp) / 'out/state.json').read_text(encoding='utf-8'))
            unknown = state['completed']['b' * 32]
            self.assertEqual(unknown['error'], 'cost_unknown')
            self.assertEqual(unknown['diagnosis']['cli_steps'], 2)
            self.assertFalse(unknown['diagnosis']['fee_proven'])
            self.assertEqual(unknown['diagnosis']['turns'][0]['cost'], None)
            again = FakeClient()
            resumed = Runner(again, Path(temp) / 'out', database, Path(temp) / 'none.json', [], '', 'trial', 1).run()
            self.assertEqual(again.requests, [])
            self.assertEqual(resumed['stopped'], 'cost_unknown')
            self.assertEqual(resumed['calls'], report['calls'])

    def test_user_only_export_is_not_free_and_a_complete_export_is_not_reread(self):
        usage = export_usage(json.dumps({'info': {'cost': 0}, 'messages': [
            {'type': 'agent-switched', 'info': {'cost': 0, 'tokens': {'input': 0, 'output': 0}}},
            {'type': 'user', 'info': {'cost': 0, 'tokens': {'input': 0, 'output': 0}}},
        ]}).encode())
        self.assertEqual(usage['cost_status'], 'no_model_turn')
        with TemporaryDirectory() as temp:
            root = Path(temp)
            executable = root / 'opencode'
            executable.write_text('''#!/usr/bin/env python3
import json, sys
from pathlib import Path
log = Path(sys.argv[0]).with_name('calls.txt')
log.write_text((log.read_text() if log.exists() else '') + sys.argv[1] + '\\n')
if sys.argv[1:3] == ['api', 'config.get']:
    print('[]')
    raise SystemExit
if sys.argv[1] == 'run':
    print(json.dumps({'type': 'text', 'sessionID': 'ses_partial', 'part': {'text': '{}'}}) )
    raise SystemExit
print(json.dumps({'info': {'cost': 0, 'outcome': 'succeeded'}, 'messages': [
    {'type': 'user', 'info': {'cost': 0}},
    {'id': 'msg-err', 'type': 'assistant', 'finish': 'error', 'model': {'providerID': 'opencode-go', 'id': 'iceman-contributor-free'},
     'error': {'type': 'provider.invalid-output', 'status': 200, 'message': 'OpenAI Chat stream ended without finish_reason'},
     'retry': {'attempt': 2}},
    {'id': 'msg-stop', 'type': 'assistant', 'finish': 'stop', 'cost': 0, 'tokens': {'input': 1, 'output': 1},
     'model': {'providerID': 'opencode-go', 'id': 'iceman-contributor-free'}}]}))
''', encoding='utf-8')
            executable.chmod(0o755)
            attempt = root / 'attempt'
            attempt.mkdir()
            client = classify_unfiled.OpenCodeFreeClient(str(executable), timeout=10, sleeper=lambda _seconds: None)
            reply = client.request('prompt', 'title', attempt)
            self.assertEqual(reply['usage']['cost_status'], 'unknown')
            self.assertEqual(reply['usage']['exports'], 1)
            self.assertEqual((root / 'calls.txt').read_text(encoding='utf-8').count('run'), 1)
            self.assertEqual((root / 'calls.txt').read_text(encoding='utf-8').count('session'), 1)

    def test_reread_stops_when_the_export_arrives_and_recheck_does_not_run(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            database = build_db(root, ['b' * 32])
            out = root / 'out'
            out.mkdir()
            skill_prompt = good_decision()
            (out / 'attempts' / ('b' * 32) / '001').mkdir(parents=True)
            (out / 'attempts' / ('b' * 32) / '001' / 'response.txt').write_text(
                json.dumps(skill_prompt, ensure_ascii=False), encoding='utf-8')
            connection = sqlite3.connect(database)
            connection.row_factory = sqlite3.Row
            from classify_unfiled import allowed_vocabulary, load_skill
            skill = load_skill(connection, 'b' * 32, allowed_vocabulary(database))
            connection.close()
            state = {
                'schema_version': SCHEMA_VERSION, 'model': MODEL, 'completed': {
                    'b' * 32: {
                        'status': 'error', 'error': 'cost_unknown', 'fingerprint': skill['fingerprint'],
                        'session_id': 'ses_recorded',
                        'diagnosis': {'cost_status': 'unknown', 'fee_proven': False, 'turns': [
                            {'message_id': 'msg-err', 'cost_known': False, 'cost': None, 'finish': 'error'},
                            {'message_id': 'msg-stop', 'cost_known': True, 'cost': 0, 'finish': 'stop',
                             'providerID': PROVIDER, 'id': MODEL_ID},
                        ]},
                    }
                }
            }
            (out / 'state.json').write_text(json.dumps(state), encoding='utf-8')
            executable = root / 'opencode'
            executable.write_text('''#!/usr/bin/env python3
import json, sys
from pathlib import Path
log = Path(sys.argv[0]).with_name('calls.txt')
log.write_text((log.read_text() if log.exists() else '') + ' '.join(sys.argv[1:]) + '\\n')
if sys.argv[1] == 'run':
    raise SystemExit('run is not a recheck')
print(json.dumps({'messages': [
    {'id': 'msg-err', 'type': 'assistant', 'finish': 'error', 'model': {'providerID': 'opencode-go', 'id': 'iceman-contributor-free'},
     'error': {'type': 'provider.invalid-output', 'status': 200, 'message': 'OpenAI Chat stream ended without finish_reason'}},
    {'id': 'msg-stop', 'type': 'assistant', 'finish': 'stop', 'cost': 0, 'tokens': {'input': 1, 'output': 1},
     'model': {'providerID': 'opencode-go', 'id': 'iceman-contributor-free'}}]}))
''', encoding='utf-8')
            executable.chmod(0o755)
            report = recheck_exports(classify_unfiled.OpenCodeFreeClient(str(executable), timeout=10), out, database)
            self.assertEqual(report['runs'], 0)
            self.assertEqual(report['promoted'], [])
            self.assertEqual(report['still_unknown'], ['b' * 32])
            self.assertNotIn('run', (root / 'calls.txt').read_text(encoding='utf-8'))
            kept = json.loads((out / 'state.json').read_text(encoding='utf-8'))['completed']['b' * 32]
            self.assertEqual(kept['error'], 'cost_unknown')
            self.assertFalse(kept['diagnosis']['fee_proven'])
            self.assertIsNone(kept['diagnosis']['turns'][0]['cost'])

    def test_empty_export_is_reread_but_a_later_zero_does_not_drop_the_error_turn(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            executable = root / 'opencode'
            executable.write_text('''#!/usr/bin/env python3
import json, sys
from pathlib import Path
counter = Path(sys.argv[0]).with_name('exports.txt')
if sys.argv[1:3] == ['api', 'config.get']:
    print('[]')
    raise SystemExit
if sys.argv[1] == 'run':
    print(json.dumps({'type': 'step_start', 'sessionID': 'ses_late', 'part': {'type': 'step-start'}}))
    print(json.dumps({'type': 'text', 'sessionID': 'ses_late', 'part': {'text': '{}'}}) )
    raise SystemExit
seen = counter.read_text() if counter.exists() else ''
counter.write_text(seen + 'export\\n')
if seen.count('export') == 0:
    print('{}')
    raise SystemExit
print(json.dumps({'messages': [{'id': 'msg-stop', 'type': 'assistant', 'finish': 'stop', 'cost': 0,
    'tokens': {'input': 1, 'output': 1}, 'model': {'providerID': 'opencode-go', 'id': 'iceman-contributor-free'}}]}))
''', encoding='utf-8')
            executable.chmod(0o755)
            attempt = root / 'attempt'
            attempt.mkdir()
            sleeps = []
            reply = classify_unfiled.OpenCodeFreeClient(
                str(executable), timeout=10, sleeper=sleeps.append).request('prompt', 'title', attempt)
            self.assertEqual(reply['usage']['cost_status'], 'zero')
            self.assertEqual(reply['usage']['exports'], 2)
            self.assertEqual(sleeps, [1])
            self.assertEqual((root / 'exports.txt').read_text(encoding='utf-8').count('export'), 2)

    def test_ten_in_flight_are_not_followed_by_more_runs_after_unknown(self):
        with TemporaryDirectory() as temp:
            unknown = export_usage(json.dumps({'messages': [
                {'id': 'msg-err', 'type': 'assistant', 'finish': 'error',
                 'model': {'providerID': PROVIDER, 'id': MODEL_ID},
                 'error': {'type': 'provider.invalid-output', 'status': 200,
                           'message': 'OpenAI Chat stream ended without finish_reason'}},
                {'id': 'msg-stop', 'type': 'assistant', 'finish': 'stop', 'cost': 0,
                 'tokens': {'input': 1, 'output': 1}, 'model': {'providerID': PROVIDER, 'id': MODEL_ID}},
            ]}).encode())

            class Burst(FakeClient):
                def request(self, prompt, title, attempt):
                    with self.gate:
                        self.inflight += 1
                        self.max_inflight = max(self.max_inflight, self.inflight)
                        ordinal = len(self.requests) + 1
                        self.requests.append({'prompt': prompt, 'title': title})
                    try:
                        time.sleep(0.02 if ordinal == 1 else 0.08)
                        if ordinal == 1:
                            return {'text': json.dumps(good_decision()), 'usage': unknown, 'duration_seconds': 0.02,
                                    'tool_events': [], 'events': [{'type': 'step_start'}, {'type': 'step_start'}],
                                    'exit_code': 0, 'timed_out': False, 'session_id': 'ses_unknown'}
                        return {'text': json.dumps(good_decision()), 'usage': free_usage(), 'duration_seconds': 0.08,
                                'tool_events': [], 'events': [], 'exit_code': 0, 'timed_out': False,
                                'session_id': 'ses_ok'}
                    finally:
                        with self.gate:
                            self.inflight -= 1

            client = Burst()
            ids = [f'{index:032x}' for index in range(1, 21)]
            report, _ = self.run_trial(
                Path(temp), client, ids, mode='continuous', workers=10,
                max_candidates=100, max_requests=100, max_runtime_seconds=60,
            )
            self.assertLessEqual(len(client.requests), 10)
            self.assertLessEqual(client.max_inflight, 10)
            self.assertEqual(report['stopped'], 'cost_unknown')
            self.assertEqual(report['cost_unknown'], 1)
            self.assertEqual(report['completed'], len(client.requests))
            state = json.loads((Path(temp) / 'out/state.json').read_text(encoding='utf-8'))
            unknown_rows = [item for item in state['completed'].values() if item.get('error') == 'cost_unknown']
            self.assertEqual(len(unknown_rows), 1)
            self.assertIsNone(unknown_rows[0]['diagnosis']['turns'][0]['cost'])
            self.assertEqual(unknown_rows[0]['diagnosis']['turns'][1]['cost'], 0)
            self.assertFalse(unknown_rows[0]['diagnosis']['fee_proven'])

    def test_model_mismatch_and_tools_also_stop_later_runs(self):
        mismatch = {'available': False, 'cost_status': 'model_mismatch', 'turns': [
            {'providerID': 'other', 'id': 'paid', 'cost': 0, 'cost_known': True}]}
        with TemporaryDirectory() as temp:
            client = FakeClient([
                {'json': good_decision(), 'usage': mismatch},
                {'json': good_decision()},
            ])
            report, _ = self.run_trial(Path(temp), client, ['b' * 32, 'c' * 32], workers=1)
            self.assertEqual(len(client.requests), 1)
            self.assertIn('not opencode-go/iceman-contributor-free', report['stopped'])
        with TemporaryDirectory() as temp:
            client = FakeClient([
                {'json': good_decision(), 'tool_events': ['tool']},
                {'json': good_decision()},
            ])
            report, _ = self.run_trial(Path(temp), client, ['b' * 32, 'c' * 32], workers=1)
            self.assertEqual(len(client.requests), 1)
            self.assertIn('tool events are not accepted', report['stopped'])
            self.assertEqual(report['proposed'], 0)

    def test_allow_missing_cost_continues_without_calling_the_cost_zero(self):
        usage = export_usage(json.dumps({'info': {'cost': 0, 'outcome': 'succeeded'}, 'messages': [
            {'id': 'msg-err', 'type': 'assistant', 'finish': 'error',
             'model': {'providerID': PROVIDER, 'id': MODEL_ID},
             'error': {'type': 'provider.invalid-output', 'status': 200,
                       'message': 'OpenAI Chat stream ended without finish_reason'}},
            {'id': 'msg-stop', 'type': 'assistant', 'finish': 'stop', 'cost': 0,
             'tokens': {'input': 1, 'output': 1}, 'model': {'providerID': PROVIDER, 'id': MODEL_ID}},
        ]}).encode())
        with TemporaryDirectory() as temp:
            client = FakeClient([
                {'json': good_decision(), 'usage': usage, 'session_id': 'ses_missing'},
                {'json': good_decision(), 'session_id': 'ses_zero'},
            ])
            report, _ = self.run_trial(
                Path(temp), client, ['b' * 32, 'c' * 32], workers=1, cost_policy='allow-missing-cost')
            self.assertEqual(len(client.requests), 2)
            self.assertIsNone(report['stopped'])
            self.assertEqual(report['missing_cost_allowed'], 1)
            self.assertEqual(report['verified_zero'], 1)
            self.assertFalse(report['fee_proven_all'])
            state = json.loads((Path(temp) / 'out/state.json').read_text(encoding='utf-8'))
            missing = state['completed']['b' * 32]
            self.assertEqual(missing['cost_accounting'], 'missing_cost_allowed_by_user')
            self.assertFalse(missing['fee_proven'])
            self.assertIsNone(missing['diagnosis']['turns'][0]['cost'])
            self.assertEqual(missing['proposal']['cost_accounting'], 'missing_cost_allowed_by_user')
            self.assertFalse(missing['proposal']['fee_proven'])

    def test_paid_or_unverified_identity_still_stops_when_missing_cost_is_allowed(self):
        paid = export_usage(json.dumps({'messages': [
            {'id': 'msg-err', 'type': 'assistant', 'finish': 'error', 'cost': 1,
             'model': {'providerID': PROVIDER, 'id': MODEL_ID},
             'error': {'type': 'provider.invalid-output', 'status': 200, 'message': 'stream ended'}},
        ]}).encode())
        unnamed = export_usage(json.dumps({'messages': [
            {'id': 'msg-err', 'type': 'assistant', 'finish': 'error',
             'error': {'type': 'provider.invalid-output', 'status': 200, 'message': 'stream ended'}},
        ]}).encode())
        with TemporaryDirectory() as temp:
            client = FakeClient([{'json': good_decision(), 'usage': paid}, {'json': good_decision()}])
            report, _ = self.run_trial(
                Path(temp), client, ['b' * 32, 'c' * 32], workers=1, cost_policy='allow-missing-cost')
            self.assertEqual(len(client.requests), 1)
            self.assertIn('cost was 1', report['stopped'])
        with TemporaryDirectory() as temp:
            client = FakeClient([{'json': good_decision(), 'usage': unnamed}, {'json': good_decision()}])
            report, _ = self.run_trial(
                Path(temp), client, ['b' * 32, 'c' * 32], workers=1, cost_policy='allow-missing-cost')
            self.assertEqual(len(client.requests), 1)
            self.assertEqual(report['stopped'], 'identity_unverified')

    def test_bad_content_stays_rejected_and_resume_does_not_rerun_old_failures(self):
        usage = export_usage(json.dumps({'messages': [
            {'id': 'msg-err', 'type': 'assistant', 'finish': 'error',
             'model': {'providerID': PROVIDER, 'id': MODEL_ID},
             'error': {'type': 'provider.invalid-output', 'status': 200, 'message': 'stream ended'}},
            {'id': 'msg-stop', 'type': 'assistant', 'finish': 'stop', 'cost': 0,
             'tokens': {'input': 1, 'output': 1}, 'model': {'providerID': PROVIDER, 'id': MODEL_ID}},
        ]}).encode())
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeClient([
                {'text': 'not json', 'usage': usage},
                {'text': 'still not json', 'usage': usage},
                {'json': good_decision()},
            ])
            report, database = self.run_trial(
                root, client, ['b' * 32, 'c' * 32], workers=1, cost_policy='allow-missing-cost')
            self.assertIsNone(report['stopped'])
            self.assertEqual(report['proposed'], 1)
            self.assertEqual(report['errors'], 1)
            self.assertEqual(len(client.requests), 3)
            held = json.loads((root / 'out/state.json').read_text(encoding='utf-8'))
            held['stopped'] = 'tool events are not accepted: tool'
            (root / 'out/state.json').write_text(json.dumps(held), encoding='utf-8')
            again = FakeClient([{'json': good_decision()}])
            blocked = Runner(
                again, root / 'out', database, root / 'none.json', [], '', 'continuous', 1,
                10, 10, 60, cost_policy='allow-missing-cost', resume_missing_cost_halt=True,
            ).run()
            self.assertEqual(again.requests, [])
            self.assertIn('tool events', blocked['stopped'])

    def test_resume_missing_cost_halt_finalizes_saved_text_without_a_new_run(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            database = build_db(root, ['b' * 32, 'c' * 32])
            out = root / 'out'
            attempt = out / 'attempts' / ('b' * 32) / '001'
            attempt.mkdir(parents=True)
            (attempt / 'response.txt').write_text(json.dumps(good_decision(), ensure_ascii=False), encoding='utf-8')
            import sqlite3 as sqlite
            connection = sqlite.connect(database)
            connection.row_factory = sqlite.Row
            from classify_unfiled import allowed_vocabulary, load_skill
            skill = load_skill(connection, 'b' * 32, allowed_vocabulary(database))
            connection.close()
            state = {
                'schema_version': SCHEMA_VERSION, 'model': MODEL, 'stopped': 'cost_unknown',
                'completed': {'b' * 32: {
                    'status': 'error', 'error': 'cost_unknown', 'fingerprint': skill['fingerprint'],
                    'session_id': 'ses_old',
                    'diagnosis': {'fee_proven': False, 'turns': [
                        {'message_id': 'msg-err', 'providerID': PROVIDER, 'id': MODEL_ID,
                         'cost_known': False, 'cost': None, 'finish': 'error'},
                        {'message_id': 'msg-stop', 'providerID': PROVIDER, 'id': MODEL_ID,
                         'cost_known': True, 'cost': 0, 'finish': 'stop'},
                    ]},
                }},
            }
            (out / 'state.json').write_text(json.dumps(state), encoding='utf-8')
            client = FakeClient([{'json': good_decision()}])
            report = Runner(
                client, out, database, root / 'none.json', [], '', 'trial', 1,
                cost_policy='allow-missing-cost', resume_missing_cost_halt=True,
            ).run()
            self.assertEqual(len(client.requests), 1)
            self.assertIsNone(report['stopped'])
            self.assertFalse(report['fee_proven_all'])
            saved = json.loads((out / 'state.json').read_text(encoding='utf-8'))
            old = saved['completed']['b' * 32]
            self.assertEqual(old['prior_error'], 'cost_unknown')
            self.assertIsNone(old['diagnosis']['turns'][0]['cost'])
            self.assertEqual(old['cost_accounting'], 'missing_cost_allowed_by_user')
            self.assertFalse(old['fee_proven'])
            self.assertEqual(saved['previous_stop'], 'cost_unknown')
            self.assertNotIn('b' * 32, client.requests[0]['prompt'])
            self.assertIn('c' * 32, client.requests[0]['prompt'])


if __name__ == '__main__':
    unittest.main()
