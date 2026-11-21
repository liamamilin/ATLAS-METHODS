import hashlib
import json
from contextlib import contextmanager, redirect_stdout
from io import StringIO
from pathlib import Path
import shutil
import sqlite3
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

import build
import import_classifications
import serve
from batch_convert import key_of
from classification_overrides import SCHEMA, load_overrides, representative_key, snapshot_notice, topic_allowed
from export_pages import META_FIELDS


REAL = Path(build.__file__).resolve().parents[1]
TITLE = '备忘条目甲'
SUMMARY = '把零散事项记下来。'
GOAL = '记下一条备忘。'
WHEN = '需要留一条备忘时。'
REPO = 'owner/repo'
PATH = 'skills/note/SKILL.md'


def source(collection, commit, sha='entry-sha', path=PATH, blob='blob-sha'):
    return {
        'collection_id': collection, 'repository': REPO, 'commit_sha': commit, 'skill_path': path,
        'git_blob_sha': blob, 'entry_sha256': sha,
    }


def task(steps=None):
    item = {'goal': GOAL, 'when': WHEN, 'outputs': ['一条备忘']}
    if steps is not None:
        item['steps'] = steps
    return item


def write_record(root, item, tasks):
    key = representative_key(item['collection_id'], item['commit_sha'])
    record = {
        'schema_version': '0.1.1', 'source': item,
        'content': {'name': 'note', 'title': TITLE, 'summary': SUMMARY, 'tasks': tasks},
        'extraction': {'generated_at': '2026-11-01T00:00:00+00:00', 'review_status': 'unreviewed', 'files_read': []},
    }
    path = root / 'normalized/batch-001/records' / f'{key}.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False), encoding='utf-8')
    return key, path


def write_index(root, rows):
    lines = []
    for item, extra in rows:
        row = {
            'id': item['collection_id'], 'repository': item['repository'], 'commit_sha': item['commit_sha'],
            'skill_path': item['skill_path'], 'git_blob_sha': item['git_blob_sha'], 'sha256': item['entry_sha256'],
            'repository_manifest': 'manifests/unused.json', **extra,
        }
        lines.append(json.dumps(row, ensure_ascii=False))
    (root / 'index.jsonl').write_text('\n'.join(lines) + '\n', encoding='utf-8')


def write_manifest(root, name, commit):
    path = root / 'manifests' / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        'repository': REPO, 'commit_sha': commit,
        'files': [{'path': PATH, 'git_blob_sha': 'blob-sha', 'status': 'saved'}],
    }), encoding='utf-8')
    return f'manifests/{name}'


def proposal_record(skill_id, **updates):
    record = {
        'id': skill_id, 'title': TITLE, 'summary': SUMMARY, 'category': 'unfiled', 'topic': '线索不足',
        'label_from': 'unfiled', 'review': 'unreviewed', 'repo': REPO, 'ordinal': 1,
        'tasks': [{'goal': GOAL, 'applicability': WHEN}],
        'sources': [{'repo': REPO, 'path': PATH, 'relationship': 'representative'}],
        'decision': 'propose_existing_category', 'proposed_category': 'legal', 'proposed_topic': '合同',
        'original_classification': {
            'category': 'unfiled', 'topic': '线索不足', 'label_from': 'unfiled', 'review': 'unreviewed',
        },
        'quality_flags': ['classification_not_endorsement'], 'reason': '目录线索', 'retention_reason': None,
        'source_record_sha256': 'ab' * 32,
    }
    record.update(updates)
    return record


def write_proposal(path, records, stamp='2026-11-20 22:34:50 UTC+8'):
    path.write_text(json.dumps({
        'metadata': {
            'schema': 'atlas-material-classification-proposals/v1',
            'created_at_shanghai': '2026-11-21T21:23:23+08:00',
            'catalog_as_of_shanghai': stamp,
        },
        'records': records,
    }, ensure_ascii=False), encoding='utf-8')


def run_cli(args):
    buffer = StringIO()
    with redirect_stdout(buffer):
        code = import_classifications.run(args)
    return code, json.loads(buffer.getvalue())


@contextmanager
def patched_library(root, sample=None):
    (root / 'docs').mkdir(exist_ok=True)
    (root / 'normalization').mkdir(exist_ok=True)
    (root / 'normalized/full-001').mkdir(parents=True, exist_ok=True)
    (root / 'library').mkdir(exist_ok=True)
    shutil.copy(REAL / 'normalization/skill-record.v0.1.1.schema.json', root / 'normalization/skill-record.v0.1.1.schema.json')
    (root / 'docs/content-distribution-20261118.sample.json').write_text(
        json.dumps(sample or {'sample': []}, ensure_ascii=False), encoding='utf-8',
    )
    with patch.multiple(
        build, ROOT=root, NORMALIZED=root / 'normalized', RUN=root / 'normalized/full-001',
        INDEX=root / 'index.jsonl', DATABASE=root / 'library/catalog.sqlite3',
        OVERRIDES=root / 'library/classification-overrides.json',
    ):
        yield


def connect(path):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    return con


def insert_skill(con, skill_id, item, category='unfiled', topic='线索不足', label='unfiled',
                 review='unreviewed', relationship='representative', skill=None, title=TITLE, summary=SUMMARY):
    owner = skill or skill_id
    if relationship == 'representative':
        con.execute('INSERT INTO skills VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)', (
            skill_id, title, summary, category, label, topic, review, '2026-11-01T00:00:00+00:00',
            item['repository'], '', 1, title, title,
        ))
    con.execute('INSERT INTO sources VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', (
        skill_id, owner, item['collection_id'], item['repository'], item['skill_path'], item['commit_sha'],
        item['git_blob_sha'], item['entry_sha256'], '[]', None, None, relationship,
    ))
    if relationship == 'representative':
        con.execute(
            'INSERT INTO catalog_tasks(skill, position, goal, applicability, outputs, tags) VALUES (?,?,?,?,?,?)',
            (skill_id, 1, GOAL, WHEN, '[]', '{}'),
        )


class ClassificationOverrideTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / 'catalog.sqlite3'
        self.overrides = self.root / 'overrides.json'
        self.con = sqlite3.connect(self.db)
        self.con.row_factory = sqlite3.Row
        self.con.executescript(build.SCHEMA)
        self.item = source('collection-alpha', 'a' * 40)
        self.skill_id = representative_key(self.item['collection_id'], self.item['commit_sha'])
        insert_skill(self.con, self.skill_id, self.item)
        self.con.execute("INSERT INTO meta VALUES ('as_of', ?)", (json.dumps('2026-11-20T14:34:50+00:00'),))
        self.con.commit()

    def tearDown(self):
        self.con.close()
        self.temp.cleanup()

    def test_representative_key_matches_catalogue_identity(self):
        self.assertEqual(representative_key('collection-alpha', 'a' * 40), key_of(self.item))
        self.assertEqual(len(self.skill_id), 32)

    def test_path_topics_require_an_existing_catalogue_label(self):
        self.assertFalse(topic_allowed('legal', 'general', {}))
        self.assertTrue(topic_allowed('legal', 'general', {'legal': {'general'}}))
        self.assertTrue(topic_allowed('legal', '合同', {}))
        self.assertTrue(topic_allowed('legal', '待细分', {}))
        self.assertFalse(topic_allowed('medicine', '合同', {'legal': {'合同'}}))

    def test_snapshot_time_difference_is_visible_and_not_a_forced_pass(self):
        self.assertIsNone(snapshot_notice('2026-11-20 22:34:50 UTC+8', '2026-11-20T14:34:50+00:00'))
        self.assertIn('不一致则跳过', snapshot_notice('2026-11-20 22:34:50 UTC+8', '2026-11-21T00:00:00+00:00'))

    def test_dry_run_reports_the_diff_without_writing(self):
        proposal = self.root / 'proposal.json'
        write_proposal(proposal, [proposal_record(self.skill_id)])
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        code, report = run_cli(['--proposals', str(proposal), '--database', str(self.db), '--overrides', str(self.overrides)])
        self.assertEqual(code, 0)
        self.assertEqual(report['mode'], 'dry-run')
        self.assertFalse(report['wrote'])
        self.assertFalse(self.overrides.exists())
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), before)
        self.assertEqual(report['counts'], {'apply': 1, 'unchanged': 0, 'pending_rebuild': 0, 'conflicts': 0, 'retained': 0})
        change = report['changes'][0]
        self.assertEqual(change['from']['review'], 'unreviewed')
        self.assertEqual(change['to'], {'category': 'legal', 'topic': '合同', 'label_from': 'catalogue', 'review': 'unreviewed'})
        self.assertIsNone(report['snapshot_notice'])
        self.assertNotIn('export_pages', Path(import_classifications.__file__).read_text(encoding='utf-8'))
        self.assertNotIn('classification_overrides', META_FIELDS)

    def test_apply_is_idempotent_and_keeps_flags_without_marking_reviewed(self):
        proposal = self.root / 'proposal.json'
        write_proposal(proposal, [proposal_record(self.skill_id, ordinal=7)])
        code, report = run_cli(['--proposals', str(proposal), '--database', str(self.db), '--overrides', str(self.overrides), '--apply'])
        self.assertEqual((code, report['wrote']), (0, True))
        stored = load_overrides(self.overrides)
        entry = stored['entries'][self.skill_id]
        self.assertEqual(entry['quality_flags'], ['classification_not_endorsement'])
        self.assertEqual(entry['label_from'], 'catalogue')
        self.assertNotIn('review', entry)
        self.assertEqual(self.con.execute('SELECT review, category FROM skills').fetchone()['review'], 'unreviewed')
        first = self.overrides.read_bytes()
        again, second = run_cli(['--proposals', str(proposal), '--database', str(self.db), '--overrides', str(self.overrides), '--apply'])
        self.assertEqual(again, 0)
        self.assertFalse(second['wrote'])
        self.assertEqual(second['changes'][0]['status'], 'unchanged')
        self.assertEqual(second['pending_rebuild'][0]['id'], self.skill_id)
        self.assertEqual(self.overrides.read_bytes(), first)

    def test_retained_rows_and_invalid_categories_are_not_imported(self):
        retained = proposal_record('b' * 32, decision='retain_unfiled', proposed_category='legal', proposed_topic='合同',
                                   retention_reason='没有合适的既有类别')
        invalid = proposal_record(self.skill_id, proposed_category='medicine', proposed_topic='合同', ordinal=2)
        bad_topic = proposal_record(self.skill_id, id=self.skill_id, proposed_topic='不存在的专题')
        proposal = self.root / 'proposal.json'
        write_proposal(proposal, [retained, invalid])
        code, report = run_cli(['--proposals', str(proposal), '--database', str(self.db), '--overrides', str(self.overrides), '--apply'])
        self.assertEqual(code, 1)
        self.assertFalse(self.overrides.exists())
        self.assertEqual(report['counts']['retained'], 1)
        self.assertEqual(report['conflicts'][0]['reason'], 'invalid_category')
        self.assertEqual(report['retained'][0]['retention_reason'], '没有合适的既有类别')
        write_proposal(proposal, [bad_topic])
        code, report = run_cli(['--proposals', str(proposal), '--database', str(self.db), '--overrides', str(self.overrides), '--apply'])
        self.assertEqual((code, report['conflicts'][0]['reason']), (1, 'invalid_topic'))
        self.assertFalse(self.overrides.exists())

    def test_malformed_input_writes_nothing(self):
        proposal = self.root / 'proposal.json'
        proposal.write_text('{', encoding='utf-8')
        code, report = run_cli(['--proposals', str(proposal), '--database', str(self.db), '--overrides', str(self.overrides), '--apply'])
        self.assertEqual(code, 2)
        self.assertIn('error', report)
        self.assertFalse(self.overrides.exists())
        write_proposal(proposal, [proposal_record(self.skill_id), proposal_record(self.skill_id)])
        code, report = run_cli(['--proposals', str(proposal), '--database', str(self.db), '--overrides', str(self.overrides), '--apply'])
        self.assertEqual(code, 2)
        self.assertIn('重复', report['error'])
        proposal.write_text(json.dumps({'metadata': {'schema': 'atlas-material-classification-proposals/v1'}, 'records': {}}), encoding='utf-8')
        code, _ = run_cli(['--proposals', str(proposal), '--database', str(self.db), '--overrides', str(self.overrides)])
        self.assertEqual(code, 2)
        self.assertFalse(self.overrides.exists())

    def test_expected_original_conflict_skips_sample_rule_and_source_labels(self):
        for label in ('sample', 'rule', 'source'):
            with self.subTest(label=label):
                self.con.execute('UPDATE skills SET label_from = ? WHERE id = ?', (label, self.skill_id))
                self.con.commit()
                proposal = self.root / f'{label}.json'
                write_proposal(proposal, [proposal_record(self.skill_id)])
                code, report = run_cli(['--proposals', str(proposal), '--database', str(self.db), '--overrides', str(self.overrides), '--apply'])
                self.assertEqual(code, 1)
                self.assertEqual(report['conflicts'][0]['reason'], 'expected_original_mismatch')
                self.assertFalse(self.overrides.exists())
        self.con.execute("UPDATE skills SET label_from = 'sample', category = 'legal', topic = '合同' WHERE id = ?", (self.skill_id,))
        self.con.commit()
        matching = proposal_record(self.skill_id, proposed_category='business', proposed_topic='组织经营与人力')
        matching['original_classification'] = {'category': 'legal', 'topic': '合同', 'label_from': 'sample', 'review': 'unreviewed'}
        proposal = self.root / 'match.json'
        write_proposal(proposal, [matching])
        code, report = run_cli(['--proposals', str(proposal), '--database', str(self.db), '--overrides', str(self.overrides), '--apply'])
        self.assertEqual(code, 0)
        self.assertEqual(report['changes'][0]['to']['label_from'], 'sample')
        self.assertTrue(load_overrides(self.overrides)['entries'][self.skill_id]['preserve_label'])

    def test_content_and_expected_classification_conflicts_do_not_overwrite(self):
        drifted = proposal_record(self.skill_id, summary=SUMMARY + '新增内容')
        shifted = proposal_record(self.skill_id)
        shifted['original_classification']['topic'] = '跨领域线索'
        proposal = self.root / 'proposal.json'
        write_proposal(proposal, [drifted])
        code, report = run_cli(['--proposals', str(proposal), '--database', str(self.db), '--overrides', str(self.overrides), '--apply'])
        self.assertEqual((code, report['conflicts'][0]['reason']), (1, 'content_drift'))
        write_proposal(proposal, [shifted])
        code, report = run_cli(['--proposals', str(proposal), '--database', str(self.db), '--overrides', str(self.overrides), '--apply'])
        self.assertEqual(report['conflicts'][0]['reason'], 'expected_original_mismatch')
        self.assertIn('跨领域线索', report['conflicts'][0]['detail'])
        self.assertFalse(self.overrides.exists())

    def test_identity_and_version_mismatches_are_skipped(self):
        renamed = proposal_record(self.skill_id)
        renamed['sources'] = [{'repo': REPO, 'path': 'skills/other/SKILL.md', 'relationship': 'representative'}]
        proposal = self.root / 'proposal.json'
        write_proposal(proposal, [renamed])
        _, report = run_cli(['--proposals', str(proposal), '--database', str(self.db), '--overrides', str(self.overrides), '--apply'])
        self.assertEqual(report['conflicts'][0]['reason'], 'identity_mismatch')
        other = source('collection-beta', 'b' * 40, path='skills/versioned/SKILL.md', sha='version-sha')
        other_id = representative_key(other['collection_id'], other['commit_sha'])
        insert_skill(self.con, other_id, other, title='另一版本')
        self.con.commit()
        missing = proposal_record('c' * 32)
        missing['sources'] = [{'repo': REPO, 'path': 'skills/versioned/SKILL.md', 'relationship': 'representative'}]
        write_proposal(proposal, [missing])
        _, report = run_cli(['--proposals', str(proposal), '--database', str(self.db), '--overrides', str(self.overrides), '--apply'])
        self.assertEqual(report['conflicts'][0]['reason'], 'version_mismatch')
        self.assertIn(other_id, report['conflicts'][0]['detail'])
        former = source('collection-former', 'd' * 40)
        former_id = representative_key(former['collection_id'], former['commit_sha'])
        insert_skill(self.con, former_id, former, relationship='same_package', skill=self.skill_id)
        self.con.commit()
        write_proposal(proposal, [proposal_record(former_id)])
        _, report = run_cli(['--proposals', str(proposal), '--database', str(self.db), '--overrides', str(self.overrides), '--apply'])
        self.assertEqual(report['conflicts'][0]['reason'], 'not_current_representative')
        self.assertFalse(self.overrides.exists())

    def test_partial_apply_skips_conflicts_and_rollback_preserves_unrelated_edits(self):
        second = source('collection-beta', 'b' * 40, path='skills/other/SKILL.md', sha='other-sha')
        second_id = representative_key(second['collection_id'], second['commit_sha'])
        insert_skill(self.con, second_id, second)
        self.con.commit()
        good = proposal_record(self.skill_id, ordinal=1)
        kept = proposal_record(second_id, ordinal=2)
        kept['sources'] = [{'repo': REPO, 'path': 'skills/other/SKILL.md', 'relationship': 'representative'}]
        bad = proposal_record('c' * 32, ordinal=3, summary='已经变了')
        bad['sources'] = [{'repo': REPO, 'path': 'skills/missing/SKILL.md', 'relationship': 'representative'}]
        retained = proposal_record('e' * 32, ordinal=4, decision='retain_unfiled', retention_reason='保留')
        proposal = self.root / 'proposal.json'
        write_proposal(proposal, [good, kept, bad, retained])
        code, report = run_cli(['--proposals', str(proposal), '--database', str(self.db), '--overrides', str(self.overrides), '--apply'])
        self.assertEqual(code, 1)
        self.assertEqual(set(load_overrides(self.overrides)['entries']), {self.skill_id, second_id})
        self.assertEqual(report['counts']['retained'], 1)
        self.assertEqual(report['conflicts'][0]['reason'], 'not_in_catalogue')
        stored = load_overrides(self.overrides)
        stored['entries'][second_id]['topic'] = '诉讼'
        foreign = 'ab' * 16
        stored['entries'][foreign] = {
            'category': 'research', 'topic': '文献检索与证据综述', 'label_from': 'catalogue',
            'imported_target': {'category': 'research', 'topic': '文献检索与证据综述', 'label_from': 'catalogue'},
            'batch_id': 'f' * 64, 'edited': False, 'preserve_label': False,
        }
        self.overrides.write_text(json.dumps(stored, ensure_ascii=False, indent=2, sort_keys=True) + '\n', encoding='utf-8')
        before = self.overrides.read_bytes()
        code, preview = run_cli(['--rollback', '--batch', report['batch_id'][:12], '--overrides', str(self.overrides)])
        self.assertEqual(code, 0)
        self.assertEqual(preview['remove'], [self.skill_id])
        self.assertEqual([item['id'] for item in preview['preserve']], [second_id])
        self.assertEqual(self.overrides.read_bytes(), before)
        code, applied = run_cli(['--rollback', '--batch', report['batch_id'], '--overrides', str(self.overrides), '--apply'])
        self.assertEqual(code, 0, applied)
        remaining = load_overrides(self.overrides)['entries']
        self.assertNotIn(self.skill_id, remaining)
        self.assertEqual(remaining[second_id]['topic'], '诉讼')
        self.assertEqual(remaining[foreign]['topic'], '文献检索与证据综述')
        self.assertEqual(applied['untouched'], [foreign])

    def test_corrupt_override_file_is_rejected(self):
        self.overrides.write_text('{', encoding='utf-8')
        code, report = run_cli(['--rollback', '--batch', 'a' * 12, '--overrides', str(self.overrides)])
        self.assertEqual(code, 2)
        self.assertIn('无法读取', report['error'])
        self.assertEqual(self.overrides.read_text(encoding='utf-8'), '{')

    def test_existing_override_conflict_is_not_replaced(self):
        proposal = self.root / 'proposal.json'
        write_proposal(proposal, [proposal_record(self.skill_id)])
        run_cli(['--proposals', str(proposal), '--database', str(self.db), '--overrides', str(self.overrides), '--apply'])
        replacement = proposal_record(self.skill_id, proposed_topic='诉讼')
        write_proposal(self.root / 'other.json', [replacement])
        code, report = run_cli(['--proposals', str(self.root / 'other.json'), '--database', str(self.db), '--overrides', str(self.overrides), '--apply'])
        self.assertEqual(report['conflicts'][0]['reason'], 'override_conflict')
        self.assertEqual(load_overrides(self.overrides)['entries'][self.skill_id]['topic'], '合同')
        self.assertEqual(code, 1)


class RebuildPersistenceTests(unittest.TestCase):
    def build_one(self, root, item=None, tasks=None, sample=None):
        item = item or source('collection-alpha', 'a' * 40)
        key, record_path = write_record(root, item, tasks or [task()])
        write_index(root, [(item, {})])
        with patched_library(root, sample):
            report = build.build()
        return key, record_path, report

    def test_valid_import_survives_rebuild_without_changing_record_or_review(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            key, record_path, report = self.build_one(root)
            self.assertEqual(report['representatives'], 1)
            self.assertNotIn('unreadable_record', report['excluded'])
            with connect(root / 'library/catalog.sqlite3') as con:
                row = con.execute('SELECT category, topic, label_from, review FROM skills').fetchone()
                original = dict(row)
            record_hash = hashlib.sha256(record_path.read_bytes()).hexdigest()
            proposal = proposal_record(key)
            proposal['original_classification'] = {**original}
            target = ('research', '文献检索与证据综述') if (original['category'], original['topic']) == ('legal', '合同') else ('legal', '合同')
            proposal['proposed_category'], proposal['proposed_topic'] = target
            path = root / 'proposal.json'
            write_proposal(path, [proposal], stamp='2020-01-01 00:00:00 UTC+8')
            overrides = root / 'library/classification-overrides.json'
            code, preview = run_cli(['--proposals', str(path), '--database', str(root / 'library/catalog.sqlite3'), '--overrides', str(overrides)])
            self.assertEqual(code, 0, preview)
            self.assertFalse(overrides.exists())
            self.assertTrue(preview['snapshot_notice'])
            code, applied = run_cli(['--proposals', str(path), '--database', str(root / 'library/catalog.sqlite3'), '--overrides', str(overrides), '--apply'])
            self.assertEqual(code, 0, applied)
            with connect(root / 'library/catalog.sqlite3') as con:
                self.assertEqual(dict(con.execute('SELECT category, topic, label_from, review FROM skills').fetchone()), original)
            with patched_library(root):
                rebuilt = build.build()
            self.assertEqual(rebuilt['classification_overrides']['applied'], 1)
            self.assertEqual(rebuilt['classification_overrides']['suspended'], [])
            with connect(root / 'library/catalog.sqlite3') as con:
                stored = dict(con.execute('SELECT id, category, topic, label_from, review FROM skills').fetchone())
                goal = con.execute('SELECT goal, applicability FROM catalog_tasks').fetchone()
                self.assertEqual((stored['category'], stored['topic'], stored['label_from'], stored['review']), (*target, 'catalogue', 'unreviewed'))
                self.assertEqual((goal['goal'], goal['applicability']), (GOAL, WHEN))
                listed = serve.list_skills(con, {'category': [target[0]], 'topic': [target[1]]})
                self.assertEqual(listed['items'][0]['id'], key)
                with patch.multiple(serve, ROOT=root, NORMALIZED=root / 'normalized', INDEX_DIR=root):
                    detail = serve.get_skill(con, key, None)
            self.assertEqual(detail['record']['extraction']['review_status'], 'unreviewed')
            self.assertEqual(detail['label_from'], 'catalogue')
            self.assertEqual(hashlib.sha256(record_path.read_bytes()).hexdigest(), record_hash)
            record = json.loads(record_path.read_text(encoding='utf-8'))
            record['content']['summary'] = SUMMARY + '后续句子。'
            record_path.write_text(json.dumps(record, ensure_ascii=False), encoding='utf-8')
            with patched_library(root):
                drifted = build.build()
            self.assertEqual(drifted['classification_overrides']['suspended'], [{'id': key, 'reason': 'content_drift'}])
            with connect(root / 'library/catalog.sqlite3') as con:
                self.assertNotEqual(con.execute('SELECT category, topic FROM skills').fetchone()['topic'], target[1])
            self.assertIn(key, load_overrides(overrides)['entries'])

    def test_rollback_then_rebuild_restores_only_unedited_imports(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            first = source('collection-alpha', 'a' * 40)
            second = source('collection-beta', 'b' * 40, path='skills/other/SKILL.md', sha='other-sha')
            first_id, _ = write_record(root, first, [task()])
            second_id, _ = write_record(root, second, [task()])
            write_index(root, [(first, {}), (second, {})])
            with patched_library(root):
                built = build.build()
            self.assertEqual(built['representatives'], 2, built['excluded'])
            with connect(root / 'library/catalog.sqlite3') as con:
                originals = {row['id']: dict(row) for row in con.execute('SELECT id, category, topic, label_from, review FROM skills')}
            records = []
            for skill_id, item in ((first_id, first), (second_id, second)):
                current = originals[skill_id]
                record = proposal_record(skill_id)
                record['sources'] = [{'repo': item['repository'], 'path': item['skill_path'], 'relationship': 'representative'}]
                record['original_classification'] = current
                record['proposed_category'], record['proposed_topic'] = ('legal', '合同')
                records.append(record)
            path = root / 'proposal.json'
            write_proposal(path, records)
            overrides = root / 'library/classification-overrides.json'
            code, report = run_cli(['--proposals', str(path), '--database', str(root / 'library/catalog.sqlite3'), '--overrides', str(overrides), '--apply'])
            self.assertEqual(code, 0, report)
            stored = load_overrides(overrides)
            stored['entries'][second_id]['topic'] = '诉讼'
            stored['entries']['ab' * 16] = {
                'category': 'office', 'topic': '文档、知识库与协作', 'label_from': 'catalogue',
                'imported_target': {'category': 'office', 'topic': '文档、知识库与协作', 'label_from': 'catalogue'},
                'batch_id': '9' * 64, 'edited': False,
            }
            overrides.write_text(json.dumps(stored, ensure_ascii=False, indent=2, sort_keys=True) + '\n', encoding='utf-8')
            code, rollback = run_cli(['--rollback', '--batch', report['batch_id'], '--overrides', str(overrides), '--apply'])
            self.assertEqual(code, 0, rollback)
            remaining = load_overrides(overrides)['entries']
            self.assertNotIn(first_id, remaining)
            self.assertEqual(remaining[second_id]['topic'], '诉讼')
            self.assertIn('ab' * 16, remaining)
            with patched_library(root):
                rebuilt = build.build()
            suspended = {item['id']: item['reason'] for item in rebuilt['classification_overrides']['suspended']}
            self.assertEqual(suspended.get('ab' * 16), 'not_in_catalogue')
            with connect(root / 'library/catalog.sqlite3') as con:
                rows = {row['id']: dict(row) for row in con.execute('SELECT id, category, topic, label_from, review FROM skills')}
            self.assertEqual(rows[first_id], originals[first_id])
            self.assertEqual((rows[second_id]['category'], rows[second_id]['topic'], rows[second_id]['label_from'], rows[second_id]['review']),
                             ('legal', '诉讼', 'catalogue', 'unreviewed'))

    def test_future_representative_does_not_inherit_the_override(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            loser = source('collection-loser', '1' * 40, sha='same-sha')
            winner = source('collection-winner', '2' * 40, sha='same-sha')
            loser_id, _ = write_record(root, loser, [task(steps=[])])
            winner_id, _ = write_record(root, winner, [task(steps=[{'action': '甲'}, {'action': '乙'}])])
            extra = {
                'local_package_files': [PATH], 'local_package_files_not_exported': [],
                'external_dependency_status': 'not_fetched_or_verified',
            }
            write_index(root, [
                (loser, {**extra, 'repository_manifest': write_manifest(root, 'loser.json', loser['commit_sha'])}),
                (winner, {**extra, 'repository_manifest': write_manifest(root, 'winner.json', winner['commit_sha'])}),
            ])
            overrides = {
                'schema': SCHEMA,
                'entries': {loser_id: {
                    'category': 'legal', 'topic': '合同', 'label_from': 'catalogue', 'preserve_label': False,
                    'identity': {
                        'collection_id': loser['collection_id'], 'commit_sha': loser['commit_sha'],
                        'repository': loser['repository'], 'skill_path': loser['skill_path'],
                        'git_blob_sha': loser['git_blob_sha'], 'entry_sha256': loser['entry_sha256'],
                    },
                    'content': {'title': TITLE, 'summary': SUMMARY, 'tasks': [{'goal': GOAL, 'applicability': WHEN}]},
                    'imported_target': {'category': 'legal', 'topic': '合同', 'label_from': 'catalogue'},
                    'batch_id': '1' * 64, 'edited': False,
                }},
            }
            with patched_library(root):
                (root / 'library/classification-overrides.json').write_text(json.dumps(overrides), encoding='utf-8')
                report = build.build()
            self.assertEqual(report['representatives'], 1, report['excluded'])
            self.assertEqual(report['classification_overrides']['suspended'], [{'id': loser_id, 'reason': 'not_representative'}])
            with connect(root / 'library/catalog.sqlite3') as con:
                row = con.execute('SELECT id, category, label_from FROM skills').fetchone()
                relationships = {item['id']: item['relationship'] for item in con.execute('SELECT id, relationship FROM sources')}
            self.assertEqual(row['id'], winner_id)
            self.assertNotEqual(row['category'], 'legal')
            self.assertEqual(relationships[loser_id], 'same_package')
            self.assertEqual(relationships[winner_id], 'representative')

    def test_matching_sample_label_is_kept_and_review_stays_unreviewed(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            item = source('collection-sample', 'e' * 40)
            sample = {'sample': [{'source': {'collection_id': item['collection_id'], 'commit_sha': item['commit_sha']},
                                  'primary_category': '法律实务与合规'}]}
            key, record_path, _ = self.build_one(root, item, sample=sample)
            with connect(root / 'library/catalog.sqlite3') as con:
                original = dict(con.execute('SELECT category, topic, label_from, review FROM skills').fetchone())
            self.assertEqual((original['category'], original['label_from'], original['review']), ('legal', 'sample', 'unreviewed'))
            proposal = proposal_record(key, proposed_category='business', proposed_topic='组织经营与人力')
            proposal['original_classification'] = original
            path = root / 'proposal.json'
            write_proposal(path, [proposal])
            overrides = root / 'library/classification-overrides.json'
            code, report = run_cli(['--proposals', str(path), '--database', str(root / 'library/catalog.sqlite3'), '--overrides', str(overrides), '--apply'])
            self.assertEqual(code, 0, report)
            with patched_library(root, sample):
                rebuilt = build.build()
            self.assertEqual(rebuilt['classification_overrides']['applied'], 1)
            with connect(root / 'library/catalog.sqlite3') as con:
                stored = dict(con.execute('SELECT category, topic, label_from, review FROM skills').fetchone())
            self.assertEqual(stored, {'category': 'business', 'topic': '组织经营与人力', 'label_from': 'sample', 'review': 'unreviewed'})
            self.assertEqual(json.loads(record_path.read_text(encoding='utf-8'))['extraction']['review_status'], 'unreviewed')

    def test_corrupt_override_file_does_not_replace_the_catalogue(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            self.build_one(root)
            database = root / 'library/catalog.sqlite3'
            with connect(database) as con:
                before = dict(con.execute('SELECT id, category, topic, label_from, review FROM skills').fetchone())
            (root / 'library/classification-overrides.json').write_text('{', encoding='utf-8')
            with patched_library(root):
                with self.assertRaises(ValueError):
                    build.build()
            with connect(database) as con:
                self.assertEqual(dict(con.execute('SELECT id, category, topic, label_from, review FROM skills').fetchone()), before)


if __name__ == '__main__':
    unittest.main()
