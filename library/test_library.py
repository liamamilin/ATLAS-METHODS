import hashlib
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from build import SCHEMA, package_signature
from export_pages import source_row
from search import search_text
import serve
from taxonomy import classify


class LibraryTests(unittest.TestCase):
    def test_pages_source_export_only_contains_public_provenance(self):
        row = {
            'id': 'a' * 32, 'repo': 'owner/public', 'path': 'skills/example/SKILL.md',
            'revision': 'b' * 40, 'relationship': 'representative',
            'record_path': 'private-record.json', 'raw_path': 'private-original.md',
            'summary': 'private normalized text',
        }
        self.assertEqual(source_row(row, {'owner/public': 0}),
                         ['a' * 32, 0, 'skills/example/SKILL.md', 'b' * 40, 'representative'])
        with self.assertRaises(ValueError):
            source_row({**row, 'path': '../SKILL.md'}, {'owner/public': 0})

    def test_title_fallback_keeps_ambiguous_and_preclassified_entries(self):
        source = {'repository': 'owner/repo', 'skill_path': 'skills/example/SKILL.md'}
        connector = {'title': 'Asana Automation via Rube MCP'}
        self.assertEqual(classify(source, connector), ('integration', 'title'))
        self.assertEqual(classify(source, connector, 'office'), ('office', 'sample'))
        self.assertEqual(classify(source, {'title': 'Rube MCP Helper'}), ('unfiled', 'unfiled'))
        self.assertEqual(classify(source, {'title': 'SEO and PPTX with automation via Rube MCP'}),
                         ('unfiled', 'unfiled'))

    def test_dedup_uses_context_and_actual_coverage(self):
        def sample(base, reference_blob, coverage='full'):
            entry, ref = base + '/SKILL.md', base + '/guide.md'
            row = dict(repository='owner/repo', commit_sha='commit', skill_path=entry,
                       sha256='entry-text', git_blob_sha='entry-blob', repository_manifest='manifest.json',
                       local_package_files=[entry, ref], local_package_files_not_exported=[],
                       external_dependency_status='not_fetched_or_verified')
            record = SimpleNamespace(reads=[{'path': entry, 'coverage': 'full'},
                                            {'path': ref, 'coverage': coverage}])
            manifest = {'repository': row['repository'], 'commit_sha': row['commit_sha']}
            files = {name: {'git_blob_sha': blob, 'status': 'saved'}
                     for name, blob in ((entry, 'entry-blob'), (ref, reference_blob))}
            with patch('build.repository_manifest', return_value=(manifest, files)):
                return package_signature(record, row)

        self.assertEqual(sample('plugins/a', 'same'), sample('skills/copy', 'same'))
        self.assertNotEqual(sample('a', 'one'), sample('b', 'two'))
        self.assertNotEqual(sample('a', 'same'), sample('b', 'same', 'partial'))

    def test_search_includes_alias_repositories_and_short_chinese_queries(self):
        with sqlite3.connect(':memory:') as con:
            con.row_factory = sqlite3.Row
            con.executescript(SCHEMA)
            con.execute('INSERT INTO skills VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)', (
                'a', '合同审核', '核对合同', 'legal', 'sample', '合同', 'unreviewed', 'today',
                'owner/primary', '', 2, '合同审核', '合同审核 owner/primary owner/copy',
            ))
            for source_id, repo in (('a', 'owner/primary'), ('b', 'owner/copy')):
                con.execute('INSERT INTO sources VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', (
                    source_id, 'a', source_id, repo, 'SKILL.md', 'commit', 'blob', 'sha', '[]',
                    None, None, 'test',
                ))
            result = serve.list_skills(con, {'q': ['合同'], 'category': ['legal'], 'repo': ['owner/copy'], 'page': ['999']})
            self.assertEqual((result['total'], result['page'], result['items'][0]['id']), (1, 1, 'a'))
            self.assertEqual(serve.list_skills(con, {'repo': ["missing' OR 1=1"]})['total'], 0)

    def test_search_accepts_title_separators_and_full_width_input(self):
        with sqlite3.connect(':memory:') as con:
            con.row_factory = sqlite3.Row
            con.executescript(SCHEMA)
            title = 'KI-Sicherheitskomponente'
            con.execute('INSERT INTO skills VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)', (
                'a', title, '', 'legal', 'sample', '', 'unreviewed', 'today',
                'owner/public', '', 1, search_text(title),
                search_text(title + ' de/tmt/skills/sicherheitskomponente-mit-ki/SKILL.md'),
            ))
            for query in ('KI–Sicherheitskomponente', title, 'KI Sicherheitskomponente',
                          'ＫＩ＿Sicherheitskomponente', 'sicherheitskomponente-mit-ki'):
                with self.subTest(query=query):
                    self.assertEqual(serve.list_skills(con, {'q': [query]})['total'], 1)
            self.assertEqual(search_text('C++'), 'c++')

    def test_raw_requires_bytes_from_the_pinned_entry(self):
        with TemporaryDirectory() as temp, sqlite3.connect(':memory:') as con:
            root = Path(temp)
            con.row_factory = sqlite3.Row
            con.executescript(SCHEMA)
            body = b'# Original\n'
            raw = root / 'SKILL.md'
            raw.write_bytes(body)
            con.execute('INSERT INTO sources VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', (
                'a', 'rep', 'source', 'owner/repo', 'SKILL.md', 'commit', 'blob',
                hashlib.sha256(body).hexdigest(), '[]', raw.name, None, 'test',
            ))
            with patch.multiple(serve, ROOT=root, INDEX_DIR=root):
                self.assertEqual(serve.get_raw(con, 'a'), {'text': body.decode()})
                raw.write_bytes(b'modified')
                with self.assertRaisesRegex(ValueError, 'hash mismatch'):
                    serve.get_raw(con, 'a')


if __name__ == '__main__':
    unittest.main()
