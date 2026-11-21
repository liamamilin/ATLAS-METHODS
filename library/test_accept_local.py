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


def write_catalogue(path, root, as_of, title, representatives=1):
    record = root / 'normalized/records/skill.json'
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps({
        'source': {'skill_path': 'skills/example/SKILL.md'},
        'content': {'title': title, 'summary': '合成摘要', 'tasks': [{'goal': '查看一条', 'when': '打开详情时'}]},
        'extraction': {'review_status': 'unreviewed'},
    }, ensure_ascii=False), encoding='utf-8')
    connection = __import__('sqlite3').connect(path)
    connection.executescript(SCHEMA)
    skill_id = 'ab' * 16
    connection.execute('INSERT INTO skills VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)', (
        skill_id, title, '合成摘要', 'code', 'catalogue', '前端与 Web 界面', 'unreviewed',
        '2026-11-01T00:00:00+00:00', 'owner/example', 'normalized/records/skill.json', 1, title, title,
    ))
    connection.execute('INSERT INTO sources VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', (
        skill_id, skill_id, 'collection', 'owner/example', 'skills/example/SKILL.md', 'a' * 40,
        'b' * 40, 'c' * 64, '["MIT"]', None, 'normalized/records/skill.json', 'representative',
    ))
    meta = {
        'as_of': as_of, 'representatives': representatives, 'sources': 1, 'normalized': 1,
        'same_package': 0, 'same_format': 0, 'categories': {'code': representatives},
    }
    connection.executemany('INSERT INTO meta VALUES (?,?)', ((key, json.dumps(value)) for key, value in meta.items()))
    connection.commit()
    connection.close()
    return skill_id


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
