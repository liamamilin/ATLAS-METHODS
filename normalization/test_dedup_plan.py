from argparse import Namespace
import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest

from batch_convert import write_json
from deduplicate_remaining import run as deduplicate
from run_corpus import run, snapshot
from model_routing import ModelDeferred
import test_batch_convert as fixtures
import test_corpus_run as corpus_fixtures


class PlanTests(unittest.TestCase):
    def setup(self, root, semantic=False, count=3):
        args = corpus_fixtures.CorpusTests().rolling_fixture(root, count=count)
        with gzip.open(args.index, 'rt') as stream:
            rows = [json.loads(line) for line in stream]
        for n, row in enumerate(rows):
            row['declared_name'] = 'baseline' if n == 0 else 'same-procedure'
            if n == 0:
                continue
            data = b'alpha\nbeta\ngamma\ndelta\nepsilon\nzeta\n'
            if semantic and n >= 2:
                data = f'alternative{n}\nwording\nfor\nthe\nsame\nprocedure\n'.encode()
            raw = args.index.parent / f'raw/unique-path/pending-{n}.md'
            raw.write_bytes(data)
            row.update(raw_path=str(raw.relative_to(args.index.parent)),
                       sha256=hashlib.sha256(data).hexdigest(), git_blob_sha=fixtures.blob(data))
            manifest_path = args.index.parent / row['repository_manifest']
            manifest = json.loads(manifest_path.read_text())
            manifest['files'][0].update(raw_path=row['raw_path'], git_blob_sha=row['git_blob_sha'])
            write_json(manifest_path, manifest)
        with gzip.open(args.index, 'wt') as stream:
            stream.write('\n'.join(json.dumps(r) for r in rows))
        plan_args = Namespace(out=args.out, index=args.index, queue=args.queue, baseline=args.baseline,
                              name='dedup-test', workers=2, go_fallback='opencode-go/beam-free',
                              go_backup='opencode-go/iceman-contributor-free')
        return args, plan_args, rows

    def test_exact_pending_duplicates_are_planned_before_one_conversion(self):
        with tempfile.TemporaryDirectory() as temp:
            args, plan_args, rows = self.setup(Path(temp))
            comparison = fixtures.FakeClient()
            self.assertTrue(deduplicate(plan_args, comparison))
            self.assertEqual(comparison.calls, [])
            stage = args.out / plan_args.name
            report = json.loads((stage / 'report.json').read_text())
            self.assertEqual((report['unique_to_convert'], report['duplicates']), (1, 1))
            self.assertEqual(list(args.out.glob('batches/*/records/*.json')), [])
            args.dedup_plan = stage / 'plan.jsonl'
            conversion = fixtures.FakeClient()
            run(args, conversion)
            self.assertEqual(len(conversion.calls), 1)
            args.snapshot = 'planned'
            result = snapshot(args)
            self.assertEqual((result['total_succeeded'], result['skipped_duplicate'], result['remaining']), (2, 1, 0))

    def test_semantic_planning_uses_raw_sources_then_reuses_the_decision(self):
        with tempfile.TemporaryDirectory() as temp:
            args, plan_args, rows = self.setup(Path(temp), semantic=True)
            comparison = fixtures.FakeClient([{'equivalent': True, 'reason': 'Same required procedure and conditions'}])
            self.assertTrue(deduplicate(plan_args, comparison))
            self.assertEqual(len(comparison.calls), 1)
            self.assertTrue(deduplicate(plan_args, comparison))
            self.assertEqual(len(comparison.calls), 1)
            stage = args.out / plan_args.name
            profiles = list((stage / 'source-packages').glob('*.json'))
            self.assertEqual(len(profiles), 2)
            for path in profiles:
                profile = json.loads(path.read_text())
                self.assertEqual(profile['kind'], 'source_package')
                self.assertNotIn('content', profile)
                self.assertNotIn('schema_version', profile)
            args.dedup_plan = stage / 'plan.jsonl'
            conversion = fixtures.FakeClient()
            run(args, conversion)
            self.assertEqual(len(conversion.calls), 1)
            args.snapshot = 'semantic'
            result = snapshot(args)
            self.assertEqual((result['total_succeeded'], result['skipped_duplicate']), (2, 1))

    def test_failed_representative_keeps_its_duplicates_pending(self):
        with tempfile.TemporaryDirectory() as temp:
            args, plan_args, rows = self.setup(Path(temp))
            deduplicate(plan_args, fixtures.FakeClient())
            args.dedup_plan = args.out / plan_args.name / 'plan.jsonl'
            conversion = fixtures.FakeClient(['bad JSON', 'bad JSON'])
            run(args, conversion)
            self.assertEqual(len(conversion.calls), 2)
            args.snapshot = 'failed'
            report = snapshot(args)
            self.assertEqual((report['total_succeeded'], report['failed'], report['skipped_duplicate'], report['remaining']),
                             (1, 1, 0, 2))
            states = json.loads((args.out / 'batches/0001/state.json').read_text())
            self.assertIn('pending_duplicate', [state['state'] for state in states.values()])
            args.retry_failed = True
            retry = fixtures.FakeClient()
            run(args, retry)
            self.assertEqual(len(retry.calls), 1)
            args.snapshot = 'recovered'
            recovered = snapshot(args)
            self.assertEqual((recovered['total_succeeded'], recovered['skipped_duplicate'], recovered['remaining']), (2, 1, 0))

    def test_planning_against_an_existing_result_requires_no_conversion(self):
        with tempfile.TemporaryDirectory() as temp:
            args = corpus_fixtures.CorpusTests().rolling_fixture(Path(temp))
            plan_args = Namespace(out=args.out, index=args.index, queue=args.queue, baseline=args.baseline,
                                  name='dedup-test', workers=2, go_fallback='opencode-go/beam-free',
                                  go_backup='opencode-go/iceman-contributor-free')
            comparison = fixtures.FakeClient()
            deduplicate(plan_args, comparison)
            args.dedup_plan = args.out / plan_args.name / 'plan.jsonl'
            conversion = fixtures.FakeClient()
            run(args, conversion)
            self.assertEqual(conversion.calls, [])
            self.assertEqual(comparison.calls, [])
            progress = json.loads((args.out / 'progress.json').read_text())
            self.assertEqual(progress['skipped_duplicate'], 6)

    def test_model_unavailability_keeps_conversion_paused_and_can_resume(self):
        with tempfile.TemporaryDirectory() as temp:
            args, plan_args, rows = self.setup(Path(temp), semantic=True)
            stop = threading.Event()

            class Unavailable:
                def call(self, prompt, attempt):
                    stop.set()
                    raise ModelDeferred('fixture model unavailable', 'fixture')

            self.assertFalse(deduplicate(plan_args, Unavailable(), stop_event=stop, retry_delay=0))
            self.assertEqual(list(args.out.glob('batches/*/records/*.json')), [])
            self.assertFalse((args.out / plan_args.name / 'report.json').exists())
            recovered = fixtures.FakeClient([{'equivalent': True, 'reason': 'Same procedure'}])
            self.assertTrue(deduplicate(plan_args, recovered))
            self.assertEqual(len(recovered.calls), 1)

    def test_earlier_source_proofs_resolve_a_chain_to_one_real_record(self):
        with tempfile.TemporaryDirectory() as temp:
            args, plan_args, rows = self.setup(Path(temp), semantic=True, count=4)
            comparison = fixtures.FakeClient([
                {'equivalent': True, 'reason': 'Same steps and conditions'},
                {'equivalent': True, 'reason': 'Equivalent wording'},
            ])
            self.assertTrue(deduplicate(plan_args, comparison))
            decisions = [json.loads(p.read_text()) for p in
                         (args.out / plan_args.name / 'decisions').glob('*.json')]
            self.assertEqual(max(len(d['proofs']) for d in decisions), 2)
            args.dedup_plan = args.out / plan_args.name / 'plan.jsonl'
            conversion = fixtures.FakeClient()
            run(args, conversion)
            self.assertEqual(len(conversion.calls), 1)
            args.snapshot = 'chain'
            report = snapshot(args)
            self.assertEqual((report['total_succeeded'], report['skipped_duplicate'], report['remaining']), (2, 2, 0))


if __name__ == '__main__':
    unittest.main()
