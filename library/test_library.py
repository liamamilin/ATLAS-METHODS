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
from task_search import add_tasks
from taxonomy import classify, topic_for


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

    def test_code_topics_follow_the_primary_work(self):
        cases = (
            ('React/Next.js 视图过渡', 'vercel-react-view-transitions', '前端与 Web 界面'),
            ('React Native / Expo 最佳实践', 'react-native-patterns', '移动与桌面应用'),
            ('使用 FastAPI 构建异步 Python API', 'fastapi-expert', '后端与 API'),
            ('PostgreSQL 只读查询', 'postgresql-query', '数据库与持久化'),
            ('架构决策记录（ADR）的创建与维护', 'architecture-decision-records', '架构设计与重构'),
            ('使用 Git Worktree 建立隔离工作区', 'using-git-worktrees', '构建与开发工作流'),
            ('Azure Identity SDK for Python', 'azure-identity-py', 'SDK 与平台接入'),
            ('TypeScript Advanced Types', 'typescript-advanced-types', '编程语言与代码实践'),
            ('PubMed 数据库检索', 'pubmed-database', '待细分'),
            ('通用工具', 'miscellaneous', '待细分'),
        )
        for title, slug, expected in cases:
            with self.subTest(title=title):
                source = {'repository': 'owner/repo', 'skill_path': f'skills/{slug}/SKILL.md'}
                self.assertEqual(topic_for(source, 'code', title, '', ''), expected)
        source = {'repository': 'thomasmoreai/legal-skills-open', 'skill_path': 'de/contracts/skills/example/SKILL.md'}
        self.assertEqual(topic_for(source, 'legal', '合同示例', '', ''), '合同')

    def test_topics_cover_other_domains_and_unfiled_clues(self):
        cases = (
            ('ai', 'RAG Implementation', 'rag', '知识检索与记忆'),
            ('operations', 'Spring Boot 可观测性配置与审查', 'spring-observability', '观测、日志与告警'),
            ('testing', 'Test-Driven Development', 'tdd', '单元与集成测试'),
            ('security', '数字取证与事件响应工件整理', 'digital-forensics', '事件响应与数字取证'),
            ('data', 'ETL/ELT 数据管道的设计与实现', 'data-pipeline-builder', '数据管道与仓库'),
            ('integration', 'Mailchimp Automation via Rube MCP', 'mailchimp-automation', '营销、CRM 与表单'),
            ('integration', 'Docupost Automation via Rube MCP', 'docupost-automation', '通用 MCP 工具连接'),
            ('legal', '合同起草', 'contract-drafting', '合同'),
            ('finance', 'Monaco business profits tax', 'mc-corporate-income-tax', '企业所得税'),
            ('research', '文献综述与证据综合', 'literature-review', '文献检索与证据综述'),
            ('engineering', '机器人系统设计', 'robotics-software-principles', '机器人系统'),
            ('business', 'SEO 优化', 'seo', 'SEO 与搜索增长'),
            ('design', '图像生成与编辑', 'image-generation', '图像生成与编辑'),
            ('office', 'Microsoft Excel 工作簿', 'excel', '表格与结构化办公'),
            ('writing', 'Blog Rewriter', 'blog-rewrite', '编辑、润色与校对'),
            ('unfiled', 'Python 与 Excel', 'python-excel', '跨领域线索'),
            ('unfiled', '使用说明', 'guide', '线索不足'),
        )
        for category, title, slug, expected in cases:
            with self.subTest(category=category, title=title):
                source = {'repository': 'owner/repo', 'skill_path': f'skills/{slug}/SKILL.md'}
                self.assertEqual(topic_for(source, category, title, '', ''), expected)

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
            result = serve.list_skills(con, {'q': ['合同'], 'category': ['legal'], 'topic': ['合同'],
                                             'repo': ['owner/copy'], 'page': ['999']})
            self.assertEqual((result['total'], result['page'], result['items'][0]['id']), (1, 1, 'a'))
            self.assertEqual(serve.list_skills(con, {'category': ['legal'], 'topic': ['诉讼']})['total'], 0)
            con.execute("INSERT INTO meta VALUES ('categories', '{}')")
            self.assertEqual(serve.meta(con)['topics'], [{'category': 'legal', 'topic': '合同', 'count': 1}])
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

    def test_task_search_returns_one_match_per_skill_with_source_fields(self):
        with sqlite3.connect(':memory:') as con:
            con.row_factory = sqlite3.Row
            con.executescript(SCHEMA)
            for key, title, category in (('a', 'React View Transitions', 'code'), ('b', '独立手册', 'writing')):
                con.execute('INSERT INTO skills VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)', (
                    key, title, '', category, 'sample', '', 'unreviewed', 'today',
                    'owner/example', '', 1, search_text(title), search_text(title),
                ))
            add_tasks(con, 'a', 'React View Transitions', [
                {'goal': '读取 PostgreSQL 的只读查询结果'},
                {'goal': '用 React 为列表进入详情加上视图过渡', 'when': '在现有 React 应用内',
                 'outputs': ['CSS 配方']},
                {'goal': '处理 React 视图过渡的浏览器回退'},
            ])
            query = {'q': ['给 React 列表进入详情时加一个过渡动画'], 'category': ['code']}
            result = serve.list_skills(con, query)
            self.assertEqual(result['total'], 1)
            match = result['items'][0]['match']
            self.assertEqual((match['position'], match['when'], match['outputs']),
                             (2, '在现有 React 应用内', ['CSS 配方']))
            self.assertEqual(serve.list_skills(con, {**query, 'category': ['office']})['total'], 0)
            self.assertEqual(serve.list_skills(con, {'action': ['transition'], 'tool': ['react']})['total'], 1)
            self.assertEqual(serve.list_skills(con, {'action': ['transition'], 'tool': ['postgresql']})['total'], 0)
            fallback = serve.list_skills(con, {'q': ['独立手册']})
            self.assertEqual(fallback['items'][0]['id'], 'b')
            self.assertNotIn('match', fallback['items'][0])

    def test_task_search_prefers_producing_minutes_over_tasks_that_use_them(self):
        with sqlite3.connect(':memory:') as con:
            con.row_factory = sqlite3.Row
            con.executescript(SCHEMA)
            for key, title in (('a', '议题与记录'), ('b', '会后跟进'), ('c', '材料交接')):
                con.execute('INSERT INTO skills VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)', (
                    key, title, '', 'office', 'sample', '', 'unreviewed', 'today',
                    'owner/example', '', 1, search_text(title), search_text(title),
                ))
            add_tasks(con, 'a', '议题与记录', [{'goal': '根据原始记录起草会议纪要'}])
            add_tasks(con, 'b', '会后跟进', [{'goal': '从会议纪要生成行动计划'}])
            add_tasks(con, 'c', '材料交接', [{'goal': '确认文档权限', 'when': '已有会议纪要'}])
            result = serve.list_skills(con, {'q': ['准备一场会议并生成会议纪要']})
            self.assertEqual([item['id'] for item in result['items']], ['a', 'b'])

    def test_linked_facets_count_distinct_skills_and_require_tags_on_one_task(self):
        with sqlite3.connect(':memory:') as con:
            con.row_factory = sqlite3.Row
            con.executescript(SCHEMA)
            for key, title, topic in (('a', '视图实践', '前端'), ('b', '查询优化', '数据库'),
                                      ('c', '网页组件', '前端')):
                con.execute('INSERT INTO skills VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)', (
                    key, title, '', 'code', 'sample', topic, 'unreviewed', 'today',
                    'owner/example', '', 1, search_text(title), search_text(title),
                ))
            add_tasks(con, 'a', '视图实践', [
                {'goal': '为 React 加入视图过渡'}, {'goal': '为 React 检查视图过渡'},
                {'goal': '使用 PostgreSQL 排查慢查询'},
            ])
            add_tasks(con, 'b', '查询优化', [{'goal': '使用 PostgreSQL 排查慢查询'}])
            add_tasks(con, 'c', '网页组件', [{'goal': '为 Vue 加入视图过渡'}])
            selection = {'category': ['code'], 'topic': ['前端']}
            facets = serve.list_skills(con, selection)['facets']
            self.assertEqual(facets['action'], {'transition': 2, 'slowquery': 1})
            self.assertEqual(facets['tool'], {'react': 1, 'postgresql': 1})
            filtered = serve.list_skills(con, {**selection, 'action': ['transition'], 'tool': ['react']})
            self.assertEqual(filtered['total'], 1)
            self.assertEqual(filtered['facets']['action'], {'transition': 1})
            self.assertEqual(filtered['facets']['tool'], {'react': 1})
            self.assertEqual(filtered['facets']['topics'], [{'category': 'code', 'topic': '前端', 'count': 1}])
            alternatives = serve.list_skills(con, {**selection, 'action': ['transition'], 'tool': ['postgresql']})
            self.assertEqual(alternatives['total'], 0)
            self.assertEqual(alternatives['facets']['tool'], {'react': 1})
            self.assertEqual(alternatives['facets']['action'], {'slowquery': 1})
            same_action = serve.list_skills(con, {**selection, 'action': ['slowquery']})
            self.assertEqual({t['topic']: t['count'] for t in same_action['facets']['topics']},
                             {'数据库': 1, '前端': 1})
            searched = serve.list_skills(con, {**selection, 'q': ['怎么给 React 加一个过渡动画']})
            self.assertEqual(searched['total'], 1)
            self.assertEqual(searched['facets']['action'], {'transition': 1})

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
