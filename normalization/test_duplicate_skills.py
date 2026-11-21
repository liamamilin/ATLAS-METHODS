from concurrent.futures import ThreadPoolExecutor
import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from batch_convert import key_of, prepare_input, prepare_run, run_batch, write_json
from duplicate_skills import SkillDeduplicator
import test_batch_convert as fixtures
import test_corpus_run as corpus_fixtures
from run_corpus import run, snapshot


class DuplicateTests(unittest.TestCase):
    def setup_pair(self, root, variant=False, extra_reference=False):
        args, rows, items = fixtures.BatchTests().fixture(root)
        write_json(args.selection, {'items': items[:1]})
        run_batch(args, fixtures.FakeClient())
        record_path = args.out / 'records' / (key_of(items[0]) + '.json')
        representative = json.loads(record_path.read_text())
        for row in rows:
            row['declared_name'] = 'demo'
        if variant:
            data = b'alpha\nbeta\ngamma\ndelta\nepsilon\nzeta\n'
            new_raw = args.index.parent / 'raw/unique-path/variant.md'
            new_raw.write_bytes(data)
            rows[1].update(raw_path='raw/unique-path/variant.md',
                           sha256=hashlib.sha256(data).hexdigest(), git_blob_sha=fixtures.blob(data))
            manifest_path = args.index.parent / rows[1]['repository_manifest']
            manifest = json.loads(manifest_path.read_text())
            manifest['files'][0].update(raw_path=rows[1]['raw_path'], git_blob_sha=rows[1]['git_blob_sha'])
            write_json(manifest_path, manifest)
        with gzip.open(args.index, 'wt') as stream:
            stream.write('\n'.join(json.dumps(row) for row in rows))
        if extra_reference:
            items[1]['references'] = [{'path': 'guide.md'}]
        write_json(args.selection, {'items': items[1:]})
        args.out = root / 'next'
        context = prepare_run(args)
        lookup = {(row['id'], row['commit_sha']): row for row in rows}
        dedup = SkillDeduplicator(args.index, lookup, [(record_path, representative)])
        return args, rows, items, context, dedup, record_path

    def convert(self, fixture, client):
        args, rows, items, context, dedup, _ = fixture
        return dedup.convert(items[1], rows[1], args, client, context['schema'], context['contract'])

    def test_identical_sources_keep_their_identity_without_another_model_call(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = self.setup_pair(Path(temp))
            client = fixtures.FakeClient()
            state = self.convert(fixture, client)
            self.assertEqual(state['state'], 'skipped_duplicate')
            self.assertEqual(state['collection_id'], 'fixture-1')
            self.assertEqual(state['duplicate_of']['source']['collection_id'], 'fixture-0')
            self.assertEqual(state['duplicate_of']['match'], 'identical_supplied_text')
            self.assertEqual(client.calls, [])
            args = fixture[0]
            write_json(args.out / 'state.json', {key_of(fixture[2][1]): state})
            self.assertEqual(prepare_run(args)['todo'], [])
            args.retry_failed = True
            self.assertEqual(prepare_run(args)['todo'], [])
            stored = json.loads((args.out / 'state.json').read_text())[key_of(fixture[2][1])]
            self.assertEqual(stored['state'], 'skipped_duplicate')
            self.assertNotIn('failure_stage', stored)
            args.retry_failed = False
            fixture[-1].unlink()
            self.assertEqual(len(prepare_run(args)['todo']), 1)

    def test_reworded_skill_requires_an_equivalence_decision_and_retains_usage(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = self.setup_pair(Path(temp), variant=True)
            client = fixtures.FakeClient([{'equivalent': True, 'reason': 'Same steps and required conditions'}])
            state = self.convert(fixture, client)
            self.assertEqual(state['state'], 'skipped_duplicate')
            self.assertEqual(state['duplicate_of']['match'], 'model_equivalent')
            comparison = fixture[0].out / state['duplicate_of']['comparison']
            metadata = json.loads(comparison.read_text())
            self.assertTrue(metadata['decision']['equivalent'])
            self.assertEqual(metadata['usage']['turns'][0]['tokens']['input'], 10)
            self.assertEqual(len(client.calls), 1)
            self.assertFalse((fixture[0].out / 'records').exists())
            args = fixture[0]
            write_json(args.out / 'state.json', {key_of(fixture[2][1]): state})
            self.assertEqual(prepare_run(args)['todo'], [])
            (comparison.parent / 'response.txt').unlink()
            self.assertEqual(len(prepare_run(args)['todo']), 1)

    def test_same_entry_with_different_supplied_guides_is_not_automatically_skipped(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = self.setup_pair(Path(temp), extra_reference=True)
            client = fixtures.FakeClient([{'equivalent': False, 'reason': 'Different required reference procedure'}])
            state = self.convert(fixture, client)
            self.assertEqual(state['state'], 'succeeded')
            self.assertEqual(len(client.calls), 2)
            self.assertIn('guide.md', client.calls[0])

    def test_malformed_equivalence_response_falls_back_to_conversion(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = self.setup_pair(Path(temp), variant=True)
            client = fixtures.FakeClient(['bad JSON'])
            state = self.convert(fixture, client)
            self.assertEqual(state['state'], 'succeeded')
            self.assertEqual(len(client.calls), 2)

    def test_equivalence_decisions_are_reused_after_a_retry_or_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = self.setup_pair(Path(temp), variant=True)
            args, rows, items, context, dedup, representative = fixture
            client = fixtures.FakeClient([{'equivalent': False, 'reason': 'Different required steps'}])
            _, _, _, blocks = prepare_input(rows[1], items[1], args.index)
            candidate = key_of(items[0])
            self.assertIsNone(dedup.compare(blocks, None, candidate, rows[1], args, items[1], client))
            fresh = SkillDeduplicator(args.index, dedup.rows,
                                      [(representative, json.loads(representative.read_text()))])
            self.assertIsNone(fresh.compare(blocks, None, candidate, rows[1], args, items[1], client))
            self.assertEqual(len(client.calls), 1)

    def test_a_mirror_under_another_directory_skips_without_a_model_call(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = self.setup_pair(Path(temp))
            args, rows, _, _, _, _ = fixture
            rows[1]['skill_path'] = 'plugins/another/skills/demo/SKILL.md'
            path = args.index.parent / rows[1]['repository_manifest']
            manifest = json.loads(path.read_text())
            manifest['files'][0]['path'] = rows[1]['skill_path']
            write_json(path, manifest)
            client = fixtures.FakeClient()
            state = self.convert(fixture, client)
            self.assertEqual(state['state'], 'skipped_duplicate')
            self.assertEqual(state['source']['skill_path'], rows[1]['skill_path'])
            self.assertEqual(client.calls, [])

    def test_unread_package_files_with_different_hashes_keep_separate_conversions(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = self.setup_pair(Path(temp))
            args, rows, _, _, _, _ = fixture
            for row in rows:
                row['local_package_files'] = [row['skill_path'], 'guide.md']
            path = args.index.parent / rows[1]['repository_manifest']
            manifest = json.loads(path.read_text())
            manifest['files'][1]['git_blob_sha'] = 'f' * 40
            write_json(path, manifest)
            client = fixtures.FakeClient([{'equivalent': False, 'reason': 'Required unread guide differs'}])
            state = self.convert(fixture, client)
            self.assertEqual(state['state'], 'succeeded')
            self.assertEqual(len(client.calls), 2)

    def test_unread_ranges_remain_part_of_the_package_context(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = self.setup_pair(Path(temp))
            row = fixture[1][1]
            dedup = fixture[4]
            blocks = [{'path': 'guide.md', 'coverage': 'partial'}]
            context = dedup.unread_context(row, blocks)
            self.assertEqual(len(context), 1)
            self.assertEqual(context[0][0], 'guide.md')
            self.assertEqual(context[0][1], 'saved')

    def test_manifest_cache_refreshes_after_a_manifest_edit(self):
        with tempfile.TemporaryDirectory() as temp:
            args, rows, items = fixtures.BatchTests().fixture(Path(temp))
            prepare_input(rows[0], items[0], args.index)
            path = args.index.parent / rows[0]['repository_manifest']
            manifest = json.loads(path.read_text())
            manifest['commit_sha'] = 'f' * 40
            write_json(path, manifest)
            with self.assertRaisesRegex(ValueError, 'manifest identity mismatch'):
                prepare_input(rows[0], items[0], args.index)

    def test_concurrent_identical_packages_issue_one_conversion(self):
        with tempfile.TemporaryDirectory() as temp:
            args, rows, items = fixtures.BatchTests().fixture(Path(temp))
            context = prepare_run(args)
            dedup = SkillDeduplicator(args.index, {}, [])
            client = fixtures.FakeClient()
            with ThreadPoolExecutor(2) as pool:
                states = list(pool.map(lambda pair: dedup.convert(
                    pair[0], pair[1], args, client, context['schema'], context['contract']
                ), zip(items, rows)))
            self.assertEqual(sorted(state['state'] for state in states), ['skipped_duplicate', 'succeeded'])
            self.assertEqual(len(client.calls), 1)

    def test_rolling_resume_and_snapshot_count_duplicate_skips_separately(self):
        with tempfile.TemporaryDirectory() as temp:
            args = corpus_fixtures.CorpusTests().rolling_fixture(Path(temp))
            args.skip_similar = True
            client = fixtures.FakeClient()
            run(args, client)
            progress = json.loads((args.out / 'progress.json').read_text())
            self.assertEqual(progress['status'], 'first_pass_finished')
            self.assertEqual(progress['skipped_duplicate'], 6)
            self.assertEqual(progress['remaining'], 0)
            self.assertEqual(client.calls, [])
            run(args, client)
            args.snapshot = 'dedup'
            report = snapshot(args)
            self.assertEqual((report['total_succeeded'], report['skipped_duplicate'], report['remaining']), (1, 6, 0))
            checkpoint = args.out / 'checkpoints' / 'dedup'
            duplicates = [json.loads(line) for line in (checkpoint / 'duplicates.jsonl').read_text().splitlines()]
            self.assertEqual(len(duplicates), 6)
            for duplicate in duplicates:
                self.assertTrue((checkpoint / duplicate['duplicate_of']['record_path']).is_file())
                self.assertTrue((checkpoint / duplicate['detail_path']).is_file())
            self.assertEqual(client.calls, [])

    def test_duplicate_prepass_respects_the_existing_batch_limit(self):
        with tempfile.TemporaryDirectory() as temp:
            args = corpus_fixtures.CorpusTests().rolling_fixture(Path(temp))
            args.skip_similar = True
            args.max_batches = 1
            client = fixtures.FakeClient()
            run(args, client)
            progress = json.loads((args.out / 'progress.json').read_text())
            self.assertEqual(progress['status'], 'batch_limit_finished')
            self.assertEqual(progress['skipped_duplicate'], 2)
            self.assertEqual(progress['remaining'], 4)
            self.assertFalse((args.out / 'batches/0002').exists())
            self.assertEqual(client.calls, [])


if __name__ == '__main__':
    unittest.main()
