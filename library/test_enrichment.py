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
        exact_runtime_only = (
            ('legal', 'administrative'), ('legal', 'arbitration'), ('legal', 'aviation'),
            ('legal', 'bankruptcy'), ('legal', 'capital markets'), ('legal', 'commercial'),
            ('legal', 'constitutional'), ('legal', 'construction'), ('legal', 'consumer'),
            ('legal', 'cybersecurity'), ('legal', 'energy'), ('legal', 'entertainment'),
            ('legal', 'environmental'), ('legal', 'finance'), ('legal', 'gaming'),
            ('legal', 'general'), ('legal', 'government contracts'), ('legal', 'healthcare'),
            ('legal', 'immigration'), ('legal', 'insurance'), ('legal', 'investment funds'),
            ('legal', 'life sciences'), ('legal', 'maritime'), ('legal', 'military'),
            ('legal', 'nonprofit'), ('legal', 'personal injury'), ('legal', 'sanctions'),
            ('legal', 'securities'), ('legal', 'social security'), ('legal', 'sports'),
            ('legal', 'tmt'), ('legal', 'trade'), ('legal', 'transportation'),
            ('legal', 'trusts and estates'), ('legal', 'white collar'),
        )
        self.assertEqual(sorted((item['domain_id'], item['topic']) for item in merged['only_in_runtime']),
                         sorted(exact_runtime_only))
        code_pairs = {(item['domain_id'], item['topic']) for item in code['pairs']}
        self.assertTrue(code_pairs <= runtime)
        for pair in (('integration', '待细分'), ('legal', '税务')):
            self.assertIn(pair, runtime)
            self.assertIn(pair, code_pairs)
            self.assertNotIn(pair, exact_runtime_only)
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

    def test_manifest_selects_only_those_ids_and_rejects_a_stale_hash(self):
        from enrichment import manifest_digest, run_enrichment
        quote = '根据日志检索异常'
        skill_row = {'id': 'b' * 32, 'category': 'code', 'topic': '后端与 API', 'label_from': 'catalogue',
                     'review': 'unreviewed', 'summary': '目录摘要。',
                     'tasks': [{'position': 1, 'goal': quote, 'applicability': '', 'outputs': '', 'tags': {}}]}
        other = dict(skill_row, id='c' * 32)
        source_hash = __import__('enrichment').digest({'summary': skill_row['summary'], 'tasks': skill_row['tasks']})
        ids = [skill_row['id']]
        records = [{'id': skill_row['id'], 'source_hash': source_hash}]
        manifest = {'ids': ids, 'records': records, 'sha256': manifest_digest(ids, records)}

        class Fake:
            def __init__(self):
                self.calls = []
            def check_price(self):
                return {'cost': [{'input': 0, 'output': 0, 'cache': {'read': 0, 'write': 0}}]}
            def request(self, prompt, title, attempt):
                self.calls.append(title)
                return {'text': json.dumps({'facts': [{'dimension': 'task_action', 'value_id': 'debug',
                        'role': 'required', 'quote': quote, 'task_position': 1}]}),
                        'session_id': 'ses_gen', 'usage': {'cost_status': 'zero', 'turns': [
                            {'providerID': 'opencode-go', 'id': 'iceman-contributor-free', 'cost_known': True, 'cost': 0}]},
                        'tool_events': [], 'exit_code': 0, 'timed_out': False, 'events': []}

        with TemporaryDirectory() as temp:
            fake = Fake()
            report = run_enrichment(fake, temp, [other, skill_row], phase='generate', manifest=manifest,
                                    cost_policy='allow-missing-cost', limit=48)
            self.assertEqual(len(fake.calls), 1)
            self.assertIn(skill_row['id'], fake.calls[0])
            self.assertNotIn(other['id'], ''.join(fake.calls))
            saved = json.loads(Path(temp, 'phase-manifest.json').read_text(encoding='utf-8'))
            self.assertEqual(saved['selected_ids'], [skill_row['id']])
            self.assertTrue(saved['calls_model'])
            stale = dict(manifest)
            stale['records'] = [{'id': skill_row['id'], 'source_hash': '0' * 64}]
            stale['sha256'] = manifest_digest(stale['ids'], stale['records'])
            fake2 = Fake()
            refused = run_enrichment(fake2, temp + '-stale', [skill_row], phase='generate', manifest=stale,
                                     cost_policy='allow-missing-cost')
            self.assertEqual(fake2.calls, [])
            self.assertEqual(refused['applied']['records'] if isinstance(refused.get('applied'), dict) else 0, 0)

    def test_missing_sidecar_filter_does_not_return_the_whole_catalogue(self):
        import os
        import sqlite3
        import serve
        from build import SCHEMA
        with TemporaryDirectory() as temp:
            database = Path(temp) / 'catalog.sqlite3'
            connection = sqlite3.connect(database)
            connection.row_factory = sqlite3.Row
            connection.executescript(SCHEMA)
            connection.execute('INSERT INTO skills VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)', (
                'a' * 32, '标题', '摘要', 'code', 'catalogue', '后端与 API', 'unreviewed',
                '2026-11-01T00:00:00+00:00', 'owner/example', '', 1, 'a' * 32, '摘要'))
            connection.commit()
            os.environ['ATLAS_ENRICHMENT_DIR'] = str(Path(temp) / 'missing-enrichment')
            try:
                listed = serve.list_skills(connection, {'edim': ['tools'], 'evalue': ['python']})
                untouched = serve.list_skills(connection, {})
            finally:
                os.environ.pop('ATLAS_ENRICHMENT_DIR', None)
                connection.close()
            self.assertEqual(listed['items'], [])
            self.assertEqual(listed['total'], 0)
            self.assertEqual(listed['enrichment_filter'], 'index_missing')
            self.assertEqual(untouched['total'], 1)
            bare = map_record({'id': 'a' * 32, 'category': 'legal', 'topic': 'administrative',
                               'label_from': 'rule', 'review': 'unreviewed'}, [])
            self.assertEqual(bare['primary']['topic'], 'administrative')
            self.assertEqual(bare['primary']['topic_origin'], 'not_checked')
            self.assertNotEqual(bare['dimensions']['input_kind']['state'], 'not_applicable')
            self.assertEqual(bare['dimensions']['input_kind']['reason'], 'not_in_supplied_scope')
            explicit_empty = map_record(skill(inputs=[]), [])
            self.assertEqual(explicit_empty['dimensions']['input_kind']['reason'], 'not_in_source')

    def test_cursor_covers_classified_ids_and_does_not_retry_or_cover_a_failure(self):
        from classify_unfiled import CostHalt
        from enrichment import run_enrichment
        usage = {'cost_status': 'zero', 'turns': [
            {'providerID': 'opencode-go', 'id': 'iceman-contributor-free', 'cost_known': True, 'cost': 0}]}

        class Fake:
            def __init__(self):
                self.calls = []
            def check_price(self):
                return {'cost': [{'input': 0, 'output': 0, 'cache': {'read': 0, 'write': 0}}]}
            def request(self, prompt, title, attempt):
                self.calls.append(title)
                if title.endswith('a' * 32):
                    raise RuntimeError('schema')
                return {'text': '{}', 'session_id': 'ses_' + str(len(self.calls)), 'usage': usage,
                        'tool_events': [], 'exit_code': 0, 'timed_out': False, 'events': []}

        def row(record_id, category):
            return {'id': record_id, 'category': category, 'topic': '后端与 API', 'label_from': 'catalogue',
                    'review': 'unreviewed', 'summary': '目录摘要。',
                    'tasks': [{'position': 1, 'goal': '检索', 'applicability': '', 'outputs': '', 'tags': {}}]}

        classified = row('a' * 32, 'code')
        later = row('b' * 32, 'unfiled')
        with TemporaryDirectory() as temp:
            fake = Fake()
            report = run_enrichment(fake, temp, [later, classified], phase='generate', limit=2,
                                    cost_policy='allow-missing-cost', workers=1, after='')
            self.assertEqual(len(fake.calls), 2)
            self.assertIn('code', classified['category'])
            saved = json.loads(Path(temp, 'phase-manifest.json').read_text(encoding='utf-8'))
            self.assertEqual(saved['selection'], 'cursor')
            self.assertEqual(saved['covered_cursor'], '')
            self.assertIn(classified['id'], saved['skipped_errors'])
            self.assertIn(later['id'], saved['selected_ids'])
            fake2 = Fake()
            run_enrichment(fake2, temp, [classified, later], phase='generate', limit=2,
                           cost_policy='allow-missing-cost', workers=1, after='')
            self.assertEqual(fake2.calls, [])

        class Halt:
            def __init__(self):
                self.calls = 0
            def check_price(self):
                return {}
            def request(self, prompt, title, attempt):
                self.calls += 1
                raise CostHalt('identity_unverified')

        with TemporaryDirectory() as temp:
            halt = Halt()
            stopped = run_enrichment(halt, temp, [classified, later], phase='generate', limit=2,
                                     cost_policy='strict', workers=1)
            self.assertEqual(halt.calls, 1)
            self.assertIn('identity_unverified', stopped['stopped'])

    def test_same_task_accepts_skill_wide_evidence_and_rejects_example_roles(self):
        skill_wide = [
            {'dimension': 'tools', 'value_id': 'python', 'role': 'required', 'scope': {'level': 'skill'}},
            {'dimension': 'task_action', 'value_id': 'debug', 'role': 'required',
             'scope': {'level': 'task', 'task_position': 1}},
        ]
        self.assertTrue(same_task_query(skill_wide, {'tools': ['python'], 'task_action': ['debug']}, True))
        for role in ('example', 'prohibited'):
            self.assertEqual(same_task_query([
                {'dimension': 'side_effects', 'value_id': 'read_only', 'role': role,
                 'scope': {'level': 'task', 'task_position': 1}},
            ], {'side_effects': ['read_only']}, True), [])
        self.assertTrue(same_task_query([
            {'dimension': 'side_effects', 'value_id': 'read_only', 'role': 'required',
             'scope': {'level': 'task', 'task_position': 1}},
        ], {'side_effects': ['read_only']}, True))

    def test_runtime_allowlist_keeps_legal_topics_without_rejecting_defaults(self):
        from enrichment import merge_runtime_allowlist
        raw = Path('library/fixtures/taxonomy-reconciliation-c26.safe.json').read_bytes()
        merged = merge_runtime_allowlist(taxonomy_reconciliation(), json.loads(raw), 'abc')
        keys = {(item['domain_id'], item['topic']) for item in merged['only_in_runtime']}
        self.assertEqual(len(keys), 35)
        self.assertIn(('legal', 'administrative'), keys)
        marked = map_record({'id': 'd' * 32, 'category': 'legal', 'topic': 'administrative',
                             'label_from': 'rule', 'review': 'unreviewed'}, [], runtime_topics=keys)
        self.assertEqual(marked['primary']['topic_origin'], 'runtime_observed')
        self.assertEqual(marked['primary']['topic'], 'administrative')

    def test_help_accepts_manifest_and_ids(self):
        import subprocess
        import sys
        result = subprocess.run([sys.executable, 'library/enrichment.py', '--help'],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0)
        self.assertIn('--manifest', result.stdout)
        self.assertIn('--ids', result.stdout)
        self.assertIn('--after', result.stdout)
        self.assertIn('--runtime-allowlist', result.stdout)

    def test_full_scale_map_reads_tasks_once(self):
        import sqlite3
        from build import SCHEMA
        from enrichment import map_catalogue, map_record, source_digest
        representatives, sources, tasks = 46608, 55673, 130824
        goal = '根据目录中的任务说明完成这一步，并保留原文里的限制。' * 4
        with TemporaryDirectory() as temp:
            database = Path(temp) / 'catalog.sqlite3'
            connection = sqlite3.connect(database)
            connection.executescript(SCHEMA)
            connection.execute('PRAGMA synchronous=OFF')
            skill_rows = [(f'{index:032x}', '标题', '摘要', 'code', 'catalogue', '后端与 API', 'unreviewed',
                           '2026-11-01T00:00:00+00:00', 'owner/example', '', 1, f'{index:032x}', '摘要')
                          for index in range(representatives)]
            connection.executemany('INSERT INTO skills VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)', skill_rows)
            task_rows = []
            remaining = tasks
            for index in range(representatives):
                count = 3 if index < (tasks - representatives * 2) else 2
                for position in range(1, count + 1):
                    tags = {'action': [], 'tool': []}
                    if position == 1 and index < 1136:
                        tags = {'action': ['debug'], 'tool': []}
                    elif position == 1 and index < 1441:
                        tags = {'action': ['debug'], 'tool': ['python']}
                    elif position == 1 and index < 4554:
                        tags = {'action': [], 'tool': ['python']}
                    task_rows.append((f'{index:032x}', position, goal, '', '[]', json.dumps(tags, ensure_ascii=False)))
                remaining -= count
            self.assertEqual(remaining, 0)
            self.assertEqual(len(task_rows), tasks)
            connection.executemany(
                'INSERT INTO catalog_tasks(skill, position, goal, applicability, outputs, tags) VALUES (?,?,?,?,?,?)',
                task_rows)
            source_rows = []
            for index in range(sources):
                skill_index = index if index < representatives else index - representatives
                source_rows.append((
                    f'{index:032x}', f'{skill_index:032x}', 'collection', 'owner/example', 'SKILL.md',
                    'abc', 'blob', 'a' * 64, '', None, None, 'representative'))
            connection.executemany('INSERT INTO sources VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', source_rows)
            connection.commit()
            connection.close()
            started = __import__('time').perf_counter()
            report = map_catalogue(database)
            elapsed = __import__('time').perf_counter() - started
            self.assertLess(elapsed, 90, report['stages_seconds'])
            self.assertTrue(report['complete'])
            self.assertFalse(report['bounded_slice'])
            self.assertEqual(report['writes'], 0)
            self.assertEqual(report['processed'], representatives)
            self.assertEqual(report['queries']['task_selects'], 1)
            self.assertEqual(report['queries']['skill_selects'], 1)
            self.assertEqual(report['queries']['source_rows_loaded'], 0)
            self.assertEqual(report['catalogue_counts'], {
                'representatives': representatives, 'sources': sources, 'tasks': tasks})
            self.assertEqual(report['tasks_loaded'], tasks)
            self.assertEqual(report['legacy_action_value_covered_representatives'], 1441)
            self.assertEqual(report['legacy_tool_value_covered_representatives'], 3418)
            self.assertEqual(report['legacy_either_value_covered_representatives'], 4554)
            self.assertEqual(report['new_facet_value_coverage'], 'unknown_not_computed')
            self.assertEqual(report['input_kind_not_applicable'], 0)
            self.assertGreater(report['peak_rss_kib'], 0)
            sliced = map_catalogue(database, limit=100)
            self.assertFalse(sliced['complete'])
            self.assertEqual(sliced['processed'], 100)
            self.assertEqual(sliced['queries']['task_selects'], 1)
            loaded, stats = __import__('enrichment').load_catalog(database, ids=[f'{0:032x}'])
            direct = map_record(loaded[0], loaded[0]['tasks'], source_hash=source_digest(loaded[0]))
            self.assertEqual(stats['task_selects'], 1)
            self.assertEqual(direct['source_hash'], source_digest(loaded[0]))
            self.assertNotEqual(direct['dimensions']['input_kind']['state'], 'not_applicable')


if __name__ == '__main__':
    unittest.main()
