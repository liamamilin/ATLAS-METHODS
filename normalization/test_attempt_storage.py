from concurrent.futures import ThreadPoolExecutor
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from attempt_storage import archive_session, release_session, save_request


class AttemptStorageTests(unittest.TestCase):
    def test_concurrent_retries_share_bytes_not_source_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            attempts = [root/'attempts'/str(i)/'001' for i in range(12)]
            for p in attempts: p.mkdir(parents=True)
            with ThreadPoolExecutor(max_workers=6) as pool:
                list(pool.map(lambda p: save_request(p, '完整请求\n' * 100), attempts))
            files = [p/'request.txt' for p in attempts]
            self.assertEqual(len({p.stat().st_ino for p in files}), 1)
            self.assertEqual(len(list((root/'.requests').glob('*.txt'))), 1)
            self.assertEqual(files[0].stat().st_mode & 0o222, 0)
            retry = root/'attempts/0/002'; retry.mkdir()
            save_request(retry, 'different feedback')
            self.assertNotEqual(files[0].stat().st_ino, (retry/'request.txt').stat().st_ino)
            self.assertEqual(files[0].read_text(), '完整请求\n' * 100)

    def test_corrupted_pool_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); a = root/'attempts/key/001'; a.mkdir(parents=True)
            pool = root/'.requests'; pool.mkdir()
            (pool/(hashlib.sha256(b'prompt').hexdigest()+'.txt')).write_text('corrupted')
            with self.assertRaisesRegex(ValueError, 'integrity'): save_request(a, 'prompt')
            self.assertFalse((a/'request.txt').exists())

    def session(self, work, **changes):
        info = dict(id='fixture', title='ATLAS SkillRecord conversion',
                    location={'directory': str(work)}, time={'idle': 12, 'busy': 11})
        info.update(changes)
        return {'info': info, 'messages': [{'text': '完整会话'}]}

    def test_verified_archive_precedes_session_deletion(self):
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp); session = self.session(work); raw = json.dumps(session).encode()
            archive = archive_session(work, raw)
            def delete(command, **kwargs):
                self.assertEqual(gzip.decompress((work/'session.json.gz').read_bytes()), raw)
                receipt = json.loads((work/'session-storage.json').read_text())
                self.assertEqual(receipt['database_copy'], 'retained')
                self.assertEqual(receipt['archive']['sha256'], hashlib.sha256(raw).hexdigest())
                self.assertEqual(command, ['opencode', 'session', 'delete', '--standalone', 'fixture'])
                return SimpleNamespace(returncode=0)
            with patch('attempt_storage.subprocess.run', side_effect=delete):
                receipt = release_session(work, session, 'fixture', work, 'opencode', {}, archive)
            self.assertEqual(receipt['database_copy'], 'deleted')

    def test_unproven_identity_busy_or_foreign_session_is_never_deleted(self):
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp)
            for changes in [{'id':'other'}, {'title':'interactive'}, {'parentID':'parent'},
                            {'location':{'directory':'/other'}}, {'time':{'busy':12,'idle':11}}, {'time':{}}]:
                session = self.session(work, **changes)
                archive = archive_session(work, json.dumps(session).encode())
                with patch('attempt_storage.subprocess.run') as call:
                    result = release_session(work, session, 'fixture', work, 'opencode', {}, archive)
                call.assert_not_called(); self.assertEqual(result['database_copy'], 'retained')

    def test_delete_failure_keeps_archive_and_reports_retention(self):
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp); session = self.session(work); raw = json.dumps(session).encode()
            archive = archive_session(work, raw)
            for response in [SimpleNamespace(returncode=1), subprocess.TimeoutExpired('opencode', 60)]:
                with patch('attempt_storage.subprocess.run', side_effect=([response] if not isinstance(response, Exception) else response)):
                    result = release_session(work, session, 'fixture', work, 'opencode', {}, archive)
                self.assertEqual(result['database_copy'], 'unknown' if isinstance(response, Exception) else 'retained')
                self.assertEqual(gzip.decompress((work/'session.json.gz').read_bytes()), raw)


if __name__ == '__main__': unittest.main()
