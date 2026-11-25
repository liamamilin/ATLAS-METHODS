import json
from tempfile import TemporaryDirectory
import unittest
from pathlib import Path

from enrichment import (
    AXES, Flight, Store, coverage, derive_grade, execution_status, independent_review, map_record,
    mark_stale, observe_risk, plan_pilot, plan_waves, resolve_tool, same_task_query, taxonomy_reconciliation,
    tool_role_from_text, validate_annotation,
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


if __name__ == '__main__':
    unittest.main()
