import hashlib
import json
from pathlib import Path
import threading
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

import accept_local
import serve
from build import SCHEMA
from chrome_probe import chrome_path


def write_record(root, name, title):
    path = root / 'normalized/records' / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        'source': {'skill_path': f'skills/{name}'},
        'content': {'title': title, 'summary': '合成摘要', 'tasks': [{'goal': '查看一条', 'when': '打开详情时'}]},
        'extraction': {'review_status': 'unreviewed'},
    }, ensure_ascii=False), encoding='utf-8')
    return f'normalized/records/{name}'


def write_catalogue(path, root, as_of, title='视图过渡条目'):
    classified_path = write_record(root, 'classified.json', title)
    unfiled_path = write_record(root, 'unfiled.json', '待归条目')
    connection = __import__('sqlite3').connect(path)
    connection.executescript(SCHEMA)
    rows = (
        ('ab' * 16, title, 'code', 'catalogue', '前端与 Web 界面', classified_path, 'skills/classified/SKILL.md'),
        ('cd' * 16, '待归条目', 'unfiled', 'unfiled', '线索不足', unfiled_path, 'skills/unfiled/SKILL.md'),
    )
    for skill_id, label, category, label_from, topic, record_path, skill_path in rows:
        searchable = f'{label} 视图过渡' if category == 'code' else '待归条目'
        connection.execute('INSERT INTO skills VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)', (
            skill_id, label, '合成摘要', category, label_from, topic, 'unreviewed',
            '2026-11-01T00:00:00+00:00', 'owner/example', record_path, 1, searchable, searchable,
        ))
        connection.execute('INSERT INTO sources VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', (
            skill_id, skill_id, 'collection-' + category, 'owner/example', skill_path, 'a' * 40,
            'b' * 40, 'c' * 64, '["MIT"]', None, record_path, 'representative',
        ))
    meta = {
        'as_of': as_of, 'representatives': 2, 'sources': 2, 'normalized': 2,
        'same_package': 0, 'same_format': 0, 'categories': {'code': 1, 'unfiled': 1},
    }
    connection.executemany('INSERT INTO meta VALUES (?,?)', ((key, json.dumps(value)) for key, value in meta.items()))
    connection.commit()
    connection.close()
    return 'ab' * 16


class ScriptedPage:
    def __init__(self, states, evaluations):
        self.states = list(states)
        self.evaluations = list(evaluations)
        self.clicks = []
        self.shots = []

    def wait(self, predicate):
        if not self.states:
            raise AssertionError('没有更多页面状态')
        state = self.states.pop(0)
        if not predicate(state):
            raise AssertionError(state)
        return state

    def evaluate(self, script):
        if not self.evaluations:
            raise AssertionError(script)
        return self.evaluations.pop(0)

    def click_text(self, selector, text):
        self.clicks.append((selector, text))

    def shot(self, path):
        self.shots.append(path)


def page_state(**updates):
    state = {
        'busy': 'false', 'updated': '目录更新', 'count': '2 条', 'metric': '2',
        'cards': ['视图过渡条目', '待归条目'], 'dialogOpen': False, 'dialogText': '', 'links': [], 'error': '',
        'title': '全部 Skill',
    }
    state.update(updates)
    return state


class PageWalkTests(unittest.TestCase):
    def success_page(self):
        states = [
            page_state(),
            page_state(count='1 条', cards=['视图过渡条目'], title='全部 Skill'),
            page_state(),
            page_state(count='1 条', cards=['视图过渡条目'], title='软件开发与架构'),
            page_state(dialogOpen=True, dialogText='未人工审阅 · 目录归类，未审源文\n软件开发与架构 · 前端与 Web 界面'),
            page_state(dialogOpen=True, dialogText='固定提交 aaaa', links=['https://github.com/owner/example']),
            page_state(dialogOpen=False),
            page_state(),
            page_state(count='1 条', cards=['待归条目'], title='待归类'),
            page_state(dialogOpen=True, dialogText='未人工审阅 · 待归类'),
            page_state(dialogOpen=True, dialogText='未人工审阅 · 待归类\n固定提交 bbbb'),
        ]
        evaluations = [None, None, {'category': 'code', 'label': '软件开发与架构'}, None, None, True]
        return ScriptedPage(states, evaluations)

    def test_page_walk_records_search_classified_and_unfiled(self):
        page = self.success_page()
        report = accept_local.inspect_page(page, query='视图过渡')
        self.assertTrue(report['ok'], report.get('error'))
        self.assertEqual(report['search'], {'query': '视图过渡', 'count': '1 条', 'cards': ['视图过渡条目']})
        self.assertEqual(report['classified']['category'], 'code')
        self.assertTrue(report['classified']['catalogue_or_domain_visible'])
        self.assertFalse(report['unfiled']['classified_override'])
        self.assertEqual([text for _, text in page.clicks], ['视图过渡条目', '来源', '待归条目', '来源'])

    def test_search_that_does_not_narrow_the_list_fails(self):
        page = ScriptedPage([
            page_state(),
            page_state(),
        ], [None])
        report = accept_local.inspect_page(page, query='视图过渡')
        self.assertFalse(report['ok'])
        self.assertIn('没有改变列表', report['error'])

    def test_empty_search_fails(self):
        page = ScriptedPage([
            page_state(),
            page_state(count='0 条', cards=[]),
        ], [None])
        report = accept_local.inspect_page(page, query='视图过渡')
        self.assertFalse(report['ok'])
        self.assertIn('没有结果', report['error'])

    def test_unfiled_detail_marked_as_catalogue_override_fails(self):
        page = self.success_page()
        page.states[9] = page_state(dialogOpen=True, dialogText='未人工审阅 · 目录归类，未审源文\n待归类')
        report = accept_local.inspect_page(page, query='视图过渡')
        self.assertFalse(report['ok'])
        self.assertIn('目录归类覆盖', report['error'])

    def test_missing_classified_domain_fails(self):
        page = ScriptedPage([page_state(), page_state(count='1 条', cards=['视图过渡条目']), page_state()], [None, None, None])
        report = accept_local.inspect_page(page, query='视图过渡')
        self.assertFalse(report['ok'])
        self.assertIn('已归类领域', report['error'])


class AcceptLocalTests(unittest.TestCase):
    def test_missing_catalogue_is_reported_without_creating_files(self):
        with TemporaryDirectory() as temp:
            database = Path(temp) / 'missing.sqlite3'
            overrides = Path(temp) / 'overrides.json'
            code = accept_local.run(['--database', str(database), '--overrides', str(overrides), '--base', 'http://127.0.0.1:9'])
            self.assertEqual(code, 2)
            self.assertFalse(database.exists())
            self.assertFalse(overrides.exists())

    def test_status_matches_a_running_server_and_does_not_write(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            database = root / 'catalog.sqlite3'
            write_catalogue(database, root, '2026-11-21T00:00:00+00:00', '只读条目')
            before = hashlib.sha256(database.read_bytes()).hexdigest()
            server = serve.bind(0)
            port = server.server_address[1]
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            with patch.multiple(serve, ROOT=root, NORMALIZED=root / 'normalized', DATABASE=database, INDEX_DIR=root):
                thread.start()
                try:
                    absent = accept_local.run([
                        '--database', str(database), '--overrides', str(root / 'none.json'),
                        '--base', 'http://127.0.0.1:9', '--require-server',
                    ])
                    self.assertEqual(absent, 1)
                    code = accept_local.run([
                        '--database', str(database), '--overrides', str(root / 'none.json'),
                        '--base', f'http://127.0.0.1:{port}', '--require-server',
                    ])
                    self.assertEqual(code, 0)
                    if chrome_path():
                        shots = root / 'shots'
                        viewed = accept_local.run([
                            '--database', str(database), '--overrides', str(root / 'none.json'),
                            '--base', f'http://127.0.0.1:{port}', '--require-server', '--page',
                            '--shots', str(shots),
                        ])
                        self.assertEqual(viewed, 0)
                        self.assertTrue((shots / 'accept-detail.png').is_file())
                        self.assertTrue((shots / 'accept-sources.png').is_file())
                        self.assertTrue((shots / 'accept-search.png').is_file())
                        self.assertTrue((shots / 'accept-unfiled.png').is_file())
                finally:
                    server.shutdown()
            self.assertEqual(hashlib.sha256(database.read_bytes()).hexdigest(), before)
            self.assertFalse((root / 'none.json').exists())

    def test_running_server_with_a_different_catalogue_is_a_mismatch(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            current = root / 'current.sqlite3'
            other = root / 'other.sqlite3'
            write_catalogue(current, root, '2026-11-21T00:00:00+00:00', '当前')
            write_catalogue(other, root / 'other', '2026-11-20T00:00:00+00:00', '另一份')
            server = serve.bind(0)
            port = server.server_address[1]
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            with patch.multiple(serve, ROOT=root / 'other', NORMALIZED=root / 'other/normalized', DATABASE=other, INDEX_DIR=root):
                thread.start()
                try:
                    code = accept_local.run([
                        '--database', str(current), '--overrides', str(root / 'none.json'),
                        '--base', f'http://127.0.0.1:{port}', '--require-server',
                    ])
                finally:
                    server.shutdown()
            self.assertEqual(code, 1)

    def test_second_server_on_the_same_port_explains_the_existing_one(self):
        first = serve.bind(0)
        try:
            with self.assertRaises(SystemExit) as caught:
                serve.bind(first.server_address[1])
        finally:
            first.server_close()
        self.assertIn('已有进程在监听', str(caught.exception))
        self.assertIn('accept_local.py', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
