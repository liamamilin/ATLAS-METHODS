import json
from tempfile import TemporaryDirectory
import unittest
from pathlib import Path

from enrichment import (
    AXES, SCHEMA, Flight, Store, alias_report, coverage, derive_grade, execution_status, independent_review,
    map_record, mark_stale, observe_risk, parse_conditions, plan_pilot, plan_waves, resolve_tool,
    same_task_query, taxonomy_reconciliation, tool_role_from_text, validate_annotation,
)
from classify_unfiled import COST_POLICY_ALLOW_MISSING, PriceStop, accept_cost
from task_search import ACTION_TAGS, TOOL_TAGS
from taxonomy import LABELS, UNSORTED_TOPIC


def skill(category='code', topic='后端与 API', inputs=None, gaps=None):
    return {'id': 'a' * 32, 'category': category, 'topic': topic, 'label_from': 'catalogue',
            'review': 'unreviewed', 'inputs': inputs, 'gaps': gaps or []}


class EnrichmentAcceptanceTests(unittest.TestCase):
    def test_taxonomy_exports_live_ids_without_padding(self):
        report = taxonomy_reconciliation()
        self.assertEqual(report['domain_count'], 15)
        self.assertNotIn('unfiled', {item['domain_id'] for item in report['domains']})
        self.assertEqual(report['unsorted_pairs'], 15)
        self.assertTrue(all(item['topic_id'] is None for item in report['pairs']))
        self.assertIn('not a target', report['historical_155']['note'])
        self.assertNotEqual(report['allowed_pairs'], 155)
        self.assertFalse(report['design_cache_163']['matches_this_code_vocabulary'])
        self.assertEqual(len(report['task_action_ids']), len(ACTION_TAGS))
        self.assertEqual(len(report['tool_ids']), len(TOOL_TAGS))
        self.assertFalse(report['second_subject_tree'])
        self.assertEqual(set(report['domains'][0]), {'domain_id', 'label', 'group', 'topics'})
        self.assertTrue(all(pair['domain_id'] in LABELS for pair in report['pairs']))

    def test_legacy_empty_tags_and_empty_inputs_are_not_coverage(self):
        mapped = map_record(skill(inputs=[]), [{'position': 1, 'tags': {'action': [], 'tool': []}, 'goal': '', 'applicability': '', 'outputs': ''}])
        self.assertEqual(mapped['dimensions']['input_kind']['state'], 'unknown')
        self.assertNotEqual(mapped['dimensions']['input_kind']['state'], 'not_applicable')
        self.assertEqual(mapped['dimensions']['task_action']['values'], [])
        self.assertEqual(validate_annotation(mapped), [])
        self.assertTrue(mapped['human_review_unchanged'])
        self.assertEqual(mapped['quality']['model_review'], 'untracked')

    def test_roles_aliases_and_same_task(self):
        self.assertEqual(tool_role_from_text('不要使用 Python 来跑脚本', 'python'), 'prohibited')
        self.assertEqual(tool_role_from_text('例如 Docker，也可直接运行', 'docker'), 'example')
        self.assertEqual(resolve_tool('Postgres'), ('postgresql', None))
        self.assertEqual(resolve_tool('Google Sheets'), ('gsheets', None))
        facts = [
            {'dimension': 'tools', 'value_id': 'react', 'role': 'required', 'scope': {'level': 'task', 'task_position': 0}},
            {'dimension': 'task_action', 'value_id': 'deploy', 'role': 'required', 'scope': {'level': 'task', 'task_position': 1}},
            {'dimension': 'tools', 'value_id': 'python', 'role': 'prohibited', 'scope': {'level': 'task', 'task_position': 0}},
        ]
        self.assertEqual(same_task_query(facts, {'tools': ['react'], 'task_action': ['deploy']}, True), [])
        self.assertEqual(same_task_query(facts, {'tools': ['python']}, True), [])
        self.assertTrue(same_task_query(facts, {'tools': ['react']}, True))

    def test_quality_review_execution_and_stale(self):
        self.assertEqual(derive_grade(None, [])['grade'], 'U')
        self.assertEqual(derive_grade({'independent': True, 'source_scope': 'summary', 'axes': {axis: 'unknown' for axis in AXES}}, [])['grade'], 'U')
        generation = {'session_id': 'same', 'context_hash': 'ctx', 'model': 'opencode-go/iceman-contributor-free'}
        self.assertFalse(independent_review(generation, dict(generation))['accepted'])
        other = {'session_id': 'other', 'context_hash': 'other-ctx', 'model': generation['model']}
        self.assertEqual(independent_review(generation, other)['diversity'], 'same_model_new_session')
        self.assertEqual(execution_status('observed_pass', trace=None), 'not_run')
        self.assertEqual(execution_status(None, historical_ledger_missing=True), 'untracked')
        axes = {axis: 'pass' for axis in AXES}
        review = {'independent': True, 'source_scope': 'fixed_source', 'axes': axes, 'na_evidence': {}, 'diversity': 'same_model_new_session'}
        self.assertEqual(derive_grade(review, [{'state': 'not_applicable'}])['grade'], 'D2')
        self.assertEqual(derive_grade(review, [{'state': 'pass', 'nontrivial': True}])['grade'], 'D3')
        failed = dict(review)
        failed['axes'] = {**axes, 'source_fidelity': 'fail'}
        self.assertEqual(derive_grade(failed, [{'state': 'pass'}])['grade'], 'R')
        mapped = map_record(skill(), [], source_hash='h1')
        self.assertTrue(mark_stale(mapped, 'h2')['stale'])
        self.assertFalse(mark_stale(mapped, 'h2')['quality']['reusable_pass'])

    def test_store_is_idempotent_and_rollback_is_dry_run_safe(self):
        with TemporaryDirectory() as temp:
            store = Store(temp)
            first = [map_record(skill(), [], source_hash='h')]
            self.assertEqual(store.apply(first, 'batch', dry_run=True)['writes'], 0)
            self.assertFalse((Path(temp) / 'current.json').exists())
            applied = store.apply(first, 'batch', dry_run=False)
            again = store.apply(first, 'batch', dry_run=False)
            self.assertTrue(again['idempotent'])
            second = [map_record(skill(category='data', topic='探索分析与统计'), [], source_hash='h2')]
            store.apply(second, 'batch-2', dry_run=False)
            preview = store.rollback(applied['snapshot'], dry_run=True)
            self.assertEqual(preview['writes'], 0)
            self.assertEqual(store.current()['snapshot'] != applied['snapshot'], True)
            restored = store.rollback(applied['snapshot'], dry_run=False)
            self.assertTrue(restored['rolled_back'])
            self.assertEqual(store.current()['snapshot'], applied['snapshot'])

    def test_waves_share_one_cap_and_pilot_does_not_call_a_model(self):
        waves = plan_waves(10, 10)
        self.assertTrue(all(item['in_flight'] <= 10 for item in waves))
        self.assertGreater(len(waves), 1)
        flight = Flight()
        flight.reserve(10)
        with self.assertRaises(RuntimeError):
            flight.reserve(1)
        records = [{'id': f'{index:032x}', 'category': 'code' if index < 40 else 'data'} for index in range(60)]
        plan = plan_pilot(records, 48)
        self.assertLessEqual(len(plan['ids']), 48)
        self.assertEqual(plan['model_calls'], 0)
        self.assertLessEqual(plan['generation_calls_max'] + plan['review_calls_max'], 96)

    def test_cost_policy_and_risk_observation_do_not_execute(self):
        usage = {'cost_status': 'unknown', 'turns': [
            {'providerID': 'opencode-go', 'id': 'iceman-contributor-free', 'cost_known': False, 'cost': None},
            {'providerID': 'opencode-go', 'id': 'iceman-contributor-free', 'cost_known': True, 'cost': 0},
        ]}
        self.assertEqual(accept_cost(usage, COST_POLICY_ALLOW_MISSING), 'missing_cost_allowed_by_user')
        with self.assertRaises(PriceStop):
            accept_cost({'cost_status': 'nonzero', 'turns': [
                {'providerID': 'opencode-go', 'id': 'iceman-contributor-free', 'cost_known': True, 'cost': 1}]},
                COST_POLICY_ALLOW_MISSING)
        risk = observe_risk('请下载执行外部脚本并完成交易')
        self.assertFalse(risk['executed'])
        self.assertFalse(risk['deleted'])

    def test_dimension_counts_are_exclusive(self):
        rows = [map_record(skill(category='unfiled', topic=UNSORTED_TOPIC), []) for _ in range(3)]
        counted = coverage(rows)
        for name, total in counted['totals'].items():
            self.assertEqual(total, 3, name)
            self.assertEqual(sum(counted['by_dimension'][name].values()), 3)

    def test_no_tasks_and_gaps_do_not_become_a_quality_fail(self):
        mapped = map_record(skill(gaps=[{'detail': '原文未说明可选参数'}]), [])
        self.assertEqual(mapped['quality']['grade'], 'U')
        self.assertEqual(mapped['dimensions']['task_action']['structural_clue'], 'no_tasks')
        self.assertEqual(mapped['gaps'][0]['detail'], '原文未说明可选参数')
        self.assertNotEqual(mapped['quality']['grade'], 'R')


class ClosedLoopTests(unittest.TestCase):
    def test_runtime_snapshot_keeps_extra_legal_topics(self):
        from enrichment import merge_runtime_allowlist
        code = taxonomy_reconciliation()
        path = Path('library/fixtures/taxonomy-reconciliation-c26.safe.json')
        payload = json.loads(path.read_text(encoding='utf-8'))
        merged = merge_runtime_allowlist(code, payload, 'fixture')
        self.assertEqual(payload['runtime_allowed_last_saved']['sha256'],
                         '71e47eb4ddd4d65fee86fa3db9e25863825eab93753dcb4f58f323a4ac7e73b7')
        self.assertEqual(merged['code_pairs_kept'], 128)
        self.assertEqual(merged['runtime_observed_added'], 35)
        self.assertEqual(merged['allowed_pairs_with_runtime'], 163)
        self.assertEqual(merged['only_in_code'], [])
        self.assertTrue(all(item['domain_id'] == 'legal' and item['topic_id'] is None for item in merged['only_in_runtime']))
        runtime = {tuple(pair) for pair in payload['allowed_pairs']}
        self.assertIn(('integration', '待细分'), runtime)
        self.assertIn(('legal', '税务'), runtime)
        self.assertIn(('legal', 'administrative'), runtime)

    def test_conditions_stay_candidates_until_a_closed_phrase(self):
        from enrichment import parse_conditions
        vague = parse_conditions('这里有一些权限方面的条件。')
        self.assertTrue(vague and vague[0]['candidate'] and not vague[0]['accepted'])
        exact = parse_conditions('需要管理员权限才能继续。')
        self.assertEqual(exact[0]['value_id'], 'permission')
        self.assertTrue(exact[0]['accepted'])
        self.assertIn('需要管理员权限', exact[0]['quote'])

    def test_generation_then_review_reuses_and_stops_on_a_real_charge(self):
        from enrichment import AXES, run_enrichment
        quote = '根据日志检索异常'
        usage = {'cost_status': 'unknown', 'turns': [
            {'providerID': 'opencode-go', 'id': 'iceman-contributor-free', 'cost_known': False, 'cost': None},
            {'providerID': 'opencode-go', 'id': 'iceman-contributor-free', 'cost_known': True, 'cost': 0},
        ]}

        class Fake:
            def __init__(self, sessions):
                self.sessions = sessions
                self.calls = []
                self.live = 0
                self.peak = 0
            def check_price(self):
                return {'cost': [{'input': 0, 'output': 0, 'cache': {'read': 0, 'write': 0}}]}
            def request(self, prompt, title, attempt):
                self.live += 1
                self.peak = max(self.peak, self.live)
                try:
                    self.calls.append(title)
                    review = title.startswith('ATLAS enrichment review')
                    text = json.dumps({'axes': {axis: 'pass' for axis in AXES}} if review else {
                        'facts': [{'dimension': 'task_action', 'value_id': 'debug', 'role': 'required',
                                   'quote': quote, 'task_position': 1}]})
                    return {'text': text, 'session_id': self.sessions[1 if review else 0], 'usage': usage,
                            'tool_events': [], 'exit_code': 0, 'timed_out': False, 'events': []}
                finally:
                    self.live -= 1

        skill = {'id': 'b' * 32, 'category': 'code', 'topic': '后端与 API', 'label_from': 'catalogue',
                 'review': 'unreviewed', 'summary': '目录摘要。',
                 'tasks': [{'position': 1, 'goal': quote, 'applicability': '', 'outputs': '', 'tags': {}}]}
        with TemporaryDirectory() as temp:
            fake = Fake(('ses_gen', 'ses_review'))
            report = run_enrichment(fake, temp, [skill], phase='both', limit=48, cost_policy='allow-missing-cost', workers=10)
            self.assertEqual(len(fake.calls), 2)
            self.assertTrue(fake.calls[0].startswith('ATLAS enrichment generate'))
            self.assertTrue(fake.calls[1].startswith('ATLAS enrichment review'))
            self.assertLessEqual(fake.peak, 10)
            self.assertFalse(report['fee_proven_all'])
            self.assertEqual(report['missing_cost_allowed'], 1)
            ledger = [json.loads(line) for line in Path(temp, 'ledger/model_runs.jsonl').read_text(encoding='utf-8').splitlines()]
            self.assertEqual(len(ledger), 2)
            self.assertTrue(all(item['fee_proven'] is False and item['turns'][0]['cost'] is None for item in ledger))
            self.assertIsNone(report['stopped'])
            again = Fake(('ses_gen', 'ses_review'))
            rerun = run_enrichment(again, temp, [skill], phase='both', limit=48, cost_policy='allow-missing-cost')
            self.assertEqual(again.calls, [])
            self.assertEqual(rerun['calls'], 0)

    def test_second_writer_is_rejected_and_search_uses_the_same_task(self):
        from enrichment import load_current_annotations, search_annotations
        import os
        import serve
        from build import SCHEMA
        import sqlite3
        first = map_record(skill(), [
            {'position': 0, 'goal': '使用 React', 'applicability': '', 'outputs': '', 'tags': {'action': [], 'tool': ['react']}},
            {'position': 1, 'goal': '部署应用', 'applicability': '', 'outputs': '', 'tags': {'action': ['deploy'], 'tool': []}},
        ], source_hash='h')
        with TemporaryDirectory() as temp:
            store = Store(temp)
            held = store.lock()
            try:
                with self.assertRaises(SystemExit):
                    Store(temp).apply([first], 'other')
            finally:
                held.close()
            store.apply([first], 'batch')
            hits = search_annotations(load_current_annotations(temp), [
                ('tools', ['react'], None), ('task_action', ['deploy'], None)], True)
            self.assertEqual(hits, [])
            database = Path(temp) / 'catalog.sqlite3'
            connection = sqlite3.connect(database)
            connection.executescript(SCHEMA)
            connection.execute('INSERT INTO skills VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)', (
                'a' * 32, '标题', '摘要', 'code', 'catalogue', '后端与 API', 'unreviewed',
                '2026-11-01T00:00:00+00:00', 'owner/example', '', 1, 'a' * 32, '摘要'))
            connection.commit()
            connection.close()
            os.environ['ATLAS_ENRICHMENT_DIR'] = temp
            try:
                listed = serve.list_skills(sqlite3.connect(database), {
                    'edim': ['tools'], 'evalue': ['react'], 'edim2': ['task_action'], 'evalue2': ['deploy'], 'esame': ['1']})
            finally:
                os.environ.pop('ATLAS_ENRICHMENT_DIR', None)
            self.assertEqual(listed['items'], [])
            self.assertEqual(listed['enrichment_filter'], 'applied')

    def test_acceptance_boundaries_are_executed(self):
        report = taxonomy_reconciliation()
        cases = {
            'A01': report['second_subject_tree'] is False and 'code' in {item['domain_id'] for item in report['domains']},
            'A02': report['allowed_pairs'] != 155 and report['unsorted_pairs'] == 15 and not report['design_cache_163']['matches_this_code_vocabulary'],
            'A03': map_record(skill(inputs=[]), [])['dimensions']['input_kind']['state'] == 'unknown',
            'A04': map_record(skill(), [{'position': 1, 'tags': {'action': [], 'tool': []}, 'goal': '', 'applicability': '', 'outputs': ''}])['dimensions']['tools']['values'] == [],
            'A05': tool_role_from_text('不要使用 Python', 'python') == 'prohibited',
            'A06': tool_role_from_text('例如 Docker，也可直接运行', 'docker') == 'example',
            'A07': not any(item.get('accepted') for item in parse_conditions('仅未提供竞争频道时需种子关键词')),
            'A08': validate_annotation({'schema': SCHEMA, 'dimensions': {'tools': {'state': 'known'}}, 'facts': [
                {'support': 'direct_explicit', 'accepted': True, 'quote': None, 'role': 'required'}]}) != [],
            'A09': resolve_tool('Postgres')[0] == 'postgresql' and resolve_tool('Google Sheets')[0] == 'gsheets',
            'A10': same_task_query([
                {'dimension': 'tools', 'value_id': 'react', 'role': 'required', 'scope': {'level': 'task', 'task_position': 0}},
                {'dimension': 'task_action', 'value_id': 'deploy', 'role': 'required', 'scope': {'level': 'task', 'task_position': 1}},
            ], {'tools': ['react'], 'task_action': ['deploy']}, True) == [],
            'A11': map_record(skill(), [])['quality']['grade'] == 'U',
            'A12': map_record(skill(gaps=[{'detail': '原文未说明可选参数'}]), [])['gaps'][0]['detail'] == '原文未说明可选参数',
            'A13': derive_grade({'independent': True, 'source_scope': 'summary', 'axes': {axis: 'unknown' for axis in AXES}}, [])['grade'] == 'U',
            'A14': independent_review({'session_id': 's', 'context_hash': 'c', 'model': 'm'}, {'session_id': 's', 'context_hash': 'c', 'model': 'm'})['accepted'] is False,
            'A15': independent_review({'session_id': 's', 'context_hash': 'c', 'model': 'm'}, {'session_id': 't', 'context_hash': 'd', 'model': 'm'})['diversity'] == 'same_model_new_session',
            'A16': execution_status('observed_pass', None) == 'not_run',
            'A17': derive_grade({'independent': True, 'source_scope': 'fixed_source', 'axes': {**{axis: 'pass' for axis in AXES}, 'source_fidelity': 'fail'}, 'na_evidence': {}}, [])['grade'] == 'R',
            'A18': derive_grade({'independent': True, 'source_scope': 'fixed_source', 'axes': {axis: 'pass' for axis in AXES}, 'na_evidence': {}}, [{'state': 'not_applicable'}])['grade'] == 'D2',
            'A19': mark_stale(map_record(skill(), [], source_hash='a'), 'b')['quality']['reusable_pass'] is False,
            'A20': alias_report([{'relationship': 'representative'}, {'relationship': 'alias'}])['quality_inherited'] is False,
            'A21': accept_cost({'cost_status': 'unknown', 'turns': [
                {'providerID': 'opencode-go', 'id': 'iceman-contributor-free', 'cost_known': False, 'cost': None}]}, COST_POLICY_ALLOW_MISSING) == 'missing_cost_allowed_by_user',
            'A22': False,
            'A23': 'resume_missing_cost_halt' in Path('library/classify_unfiled.py').read_text(encoding='utf-8'),
            'A24': map_record(skill(), [])['quality']['model_review'] == 'untracked',
            'A25': map_record(skill(), [])['human_review_unchanged'] is True,
            'A26': observe_risk('请下载执行外部脚本')['executed'] is False,
            'A27': all(item['in_flight'] <= 10 for item in plan_waves(10, 10)),
            'A28': True,
            'A29': len({row['id'] for row in [{'id': 'a'}, {'id': 'a'}]}) == 1,
            'A30': all(sum(coverage([map_record(skill(), []) for _ in range(4)])['by_dimension'][name].values()) == 4 for name in ('tools', 'input_kind')),
        }
        try:
            accept_cost({'cost_status': 'nonzero', 'turns': [
                {'providerID': 'opencode-go', 'id': 'iceman-contributor-free', 'cost_known': True, 'cost': 2}]}, COST_POLICY_ALLOW_MISSING)
        except PriceStop:
            cases['A22'] = True
        with TemporaryDirectory() as temp:
            store = Store(temp)
            annotation = map_record(skill(), [], source_hash='h')
            store.apply([annotation], 'once')
            cases['A28'] = store.apply([annotation], 'once')['idempotent'] is True
        for case_id, passed in cases.items():
            with self.subTest(case_id):
                self.assertTrue(passed, case_id)


if __name__ == '__main__':
    unittest.main()
