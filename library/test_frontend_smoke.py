"""Render the local library against a synthetic catalogue and exercise the page."""
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import threading
import time
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import quote
from urllib.request import Request, urlopen
import unittest

import build
import serve
from classification_overrides import representative_key
from test_classification_overrides import patched_library, run_cli, write_proposal


CHROME = shutil.which('google-chrome') or shutil.which('chromium') or shutil.which('chromium-browser')
RAW = '# 合成原文\n只供页面冒烟。\n'.encode()
RAW_SHA = hashlib.sha256(RAW).hexdigest()


def skill_record(source, title, summary, tasks, rules=None, gaps=None):
    return {
        'schema_version': '0.1.1',
        'source': source,
        'content': {
            'name': Path(source['skill_path']).parent.name,
            'title': title,
            'summary': summary,
            'tasks': tasks,
            'rules': rules or [],
        },
        'extraction': {
            'generated_at': '2026-11-01T00:00:00+00:00',
            'review_status': 'unreviewed',
            'files_read': [],
            'gaps': gaps or [],
        },
    }


def source(collection, commit, repository, path, sha, blob):
    return {
        'collection_id': collection, 'repository': repository, 'commit_sha': commit,
        'skill_path': path, 'git_blob_sha': blob, 'entry_sha256': sha,
    }


class ChromePage:
    def __init__(self, proc, port, sock):
        self.proc = proc
        self.port = port
        self.sock = sock
        self.next_id = 0

    @classmethod
    def start(cls, user_data):
        if not CHROME:
            raise unittest.SkipTest('未找到 Chrome，跳过页面渲染')
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        proc = subprocess.Popen(
            [CHROME, '--headless=new', '--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu',
             f'--remote-debugging-port={port}', f'--user-data-dir={user_data}',
             '--window-size=1280,900', '--no-first-run', '--no-default-browser-check'],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                with urlopen(f'http://127.0.0.1:{port}/json/version', timeout=0.5) as response:
                    if response.status == 200:
                        break
            except OSError:
                if proc.poll() is not None:
                    raise RuntimeError(proc.stderr.read())
                time.sleep(0.1)
        else:
            proc.kill()
            raise RuntimeError('Chrome 调试端口没有起来')
        return cls(proc, port, None)

    def open(self, url):
        request = Request(f'http://127.0.0.1:{self.port}/json/new?{quote(url, safe="")}', method='PUT')
        try:
            with urlopen(request, timeout=5) as response:
                target = json.load(response)
        except OSError:
            request = Request(f'http://127.0.0.1:{self.port}/json/new?{quote(url, safe="")}')
            with urlopen(request, timeout=5) as response:
                target = json.load(response)
        self._connect(target['webSocketDebuggerUrl'])
        self.call('Page.enable')
        self.call('Runtime.enable')
        self.wait(lambda state: state['busy'] == 'false' and state['updated'] != '正在读取本机目录')
        return self

    def _connect(self, websocket_url):
        path = websocket_url.split(f'127.0.0.1:{self.port}', 1)[1]
        sock = socket.create_connection(('127.0.0.1', self.port), timeout=10)
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall(
            f'GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nUpgrade: websocket\r\n'
            f'Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n'.encode()
        )
        buffered = b''
        while b'\r\n\r\n' not in buffered:
            buffered += sock.recv(4096)
        if b' 101 ' not in buffered.split(b'\r\n', 1)[0]:
            raise RuntimeError(buffered.split(b'\r\n', 1)[0].decode())
        self.sock = sock
        self.pending = buffered.split(b'\r\n\r\n', 1)[1]

    def call(self, method, params=None):
        self.next_id += 1
        message = {'id': self.next_id, 'method': method, 'params': params or {}}
        self._send(json.dumps(message))
        deadline = time.time() + 15
        while time.time() < deadline:
            payload = json.loads(self._recv())
            if payload.get('id') == message['id']:
                if 'error' in payload:
                    raise RuntimeError(payload['error'])
                return payload.get('result', {})
        raise TimeoutError(method)

    def evaluate(self, expression):
        result = self.call('Runtime.evaluate', {
            'expression': expression, 'returnByValue': True, 'awaitPromise': True,
        })['result']
        if result.get('subtype') == 'error':
            raise RuntimeError(result.get('description'))
        return result.get('value')

    def state(self):
        return self.evaluate('''(() => {
            const dialog = document.getElementById('detail');
            const results = document.getElementById('results');
            return {
                title: document.getElementById('result-title')?.textContent || '',
                count: document.getElementById('result-count')?.textContent || '',
                busy: results?.getAttribute('aria-busy'),
                updated: document.getElementById('updated')?.textContent || '',
                cards: [...document.querySelectorAll('#results .card h3')].map((node) => node.textContent),
                dialogOpen: !!dialog?.open,
                dialogText: dialog?.innerText || '',
                links: [...document.querySelectorAll('#detail a')].map((node) => node.href),
                actionHidden: !!document.getElementById('action')?.parentElement.hidden,
                toolHidden: !!document.getElementById('tool')?.parentElement.hidden,
            };
        })()''')

    def wait(self, predicate):
        deadline = time.time() + 8
        last = None
        while time.time() < deadline:
            last = self.state()
            if predicate(last):
                return last
            time.sleep(0.05)
        raise AssertionError(last)

    def click_text(self, selector, text):
        clicked = self.evaluate(f'''(() => {{
            const node = [...document.querySelectorAll({json.dumps(selector)})].find((item) => item.textContent.includes({json.dumps(text)}));
            if (!node) return false;
            node.click();
            return true;
        }})()''')
        if not clicked:
            raise AssertionError(f'找不到 {selector} / {text}')

    def shot(self, path):
        if not path:
            return
        image = base64.b64decode(self.call('Page.captureScreenshot', {'format': 'png'})['data'])
        Path(path).write_bytes(image)

    def close(self):
        if self.sock:
            self.sock.close()
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=5)
        if self.proc and self.proc.stderr:
            self.proc.stderr.close()

    def _send(self, text):
        payload = text.encode()
        mask = os.urandom(4)
        header = bytearray([0x81])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack('!H', length)
        else:
            header.append(0x80 | 127)
            header += struct.pack('!Q', length)
        header += mask
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(header + masked)

    def _recv(self):
        chunks = []
        while True:
            opcode, data = self._frame()
            if opcode == 8:
                raise RuntimeError('Chrome 关闭了调试连接')
            if opcode == 9:
                self._pong(data)
                continue
            if opcode == 1:
                chunks.append(data)
            if opcode in (0, 1) and chunks:
                # Chrome sends these CDP payloads in one frame.
                return b''.join(chunks).decode()

    def _frame(self):
        header = self._read(2)
        opcode = header[0] & 15
        length = header[1] & 127
        if length == 126:
            length = struct.unpack('!H', self._read(2))[0]
        elif length == 127:
            length = struct.unpack('!Q', self._read(8))[0]
        if header[1] & 128:
            mask = self._read(4)
            data = bytes(byte ^ mask[index % 4] for index, byte in enumerate(self._read(length)))
        else:
            data = self._read(length)
        return opcode, data

    def _read(self, size):
        data = self.pending
        self.pending = b''
        while len(data) < size:
            piece = self.sock.recv(max(4096, size - len(data)))
            if not piece:
                raise RuntimeError('Chrome 调试连接中断')
            data += piece
        self.pending = data[size:]
        return data[:size]

    def _pong(self, data):
        mask = os.urandom(4)
        frame = bytearray([0x8A, 0x80 | len(data)]) + mask
        frame += bytes(byte ^ mask[index % 4] for index, byte in enumerate(data))
        self.sock.sendall(frame)


def api(port, path):
    with urlopen(f'http://127.0.0.1:{port}{path}') as response:
        return json.load(response)


class FrontendSmokeTests(unittest.TestCase):
    def test_rendered_page_filters_detail_classification_and_retained_item(self):
        shots = os.environ.get('ATLAS_SMOKE_ARTIFACTS')
        if shots:
            Path(shots).mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory() as temp, TemporaryDirectory(ignore_cleanup_errors=True) as chrome_dir:
            root = Path(temp)
            catalogue, retained = self.build_catalogue(root)
            server = serve.ThreadingHTTPServer(('127.0.0.1', 0), serve.Handler)
            port = server.server_address[1]
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            page = None
            try:
                with patch.multiple(serve, ROOT=root, NORMALIZED=root / 'normalized', INDEX_DIR=root,
                                    DATABASE=root / 'library/catalog.sqlite3'):
                    thread.start()
                    self.check_api(port, catalogue, retained)
                    if not CHROME:
                        self.skipTest('未找到 Chrome，接口已核对，页面渲染未执行')
                    page = ChromePage.start(chrome_dir)
                    page.open(f'http://127.0.0.1:{port}/')
                    self.check_browser(page, catalogue, retained, shots)
            finally:
                server.shutdown()
                if page:
                    page.close()

    def build_catalogue(self, root):
        catalogue_source = source(
            'collection-catalogue', 'a' * 40, 'owner/frontend', 'skills/list-transition/SKILL.md', RAW_SHA, 'b' * 40,
        )
        retained_source = source(
            'collection-retained', 'c' * 40, 'owner/notes', 'skills/note/SKILL.md', 'd' * 64, 'e' * 40,
        )
        catalogue_tasks = [{
            'goal': '为列表进入详情加上视图过渡',
            'when': '在现有页面内',
            'inputs': ['现有列表页'],
            'steps': [
                {'action': '给列表项设置过渡名称', 'when': '浏览器支持该过渡'},
                {'action': '在详情路由包一层过渡'},
            ],
            'outputs': ['React 配方'],
            'checks': [{'text': '退场动画不挡住点击'}],
        }]
        retained_tasks = [{'goal': '记下一条备忘。', 'when': '需要留一条备忘时。', 'outputs': ['一条备忘']}]
        records = [
            (catalogue_source, skill_record(
                catalogue_source, '条目甲', '把零散事项记下来。', catalogue_tasks,
                rules=[{'text': '不改数据请求'}], gaps=[{'detail': '未覆盖旧浏览器回退'}],
            )),
            (retained_source, skill_record(retained_source, '条目乙', '把零散事项记下来。', retained_tasks)),
        ]
        lines = []
        for item, record in records:
            row = {
                'id': item['collection_id'], 'repository': item['repository'], 'commit_sha': item['commit_sha'],
                'skill_path': item['skill_path'], 'git_blob_sha': item['git_blob_sha'], 'sha256': item['entry_sha256'],
                'repository_manifest': 'manifests/unused.json', 'license_candidates': ['MIT'],
            }
            if item is catalogue_source:
                raw = root / 'originals/entry.md'
                raw.parent.mkdir(parents=True)
                raw.write_bytes(RAW)
                row['raw_path'] = 'originals/entry.md'
            lines.append(json.dumps(row, ensure_ascii=False))
            key = representative_key(item['collection_id'], item['commit_sha'])
            path = root / 'normalized/batch-001/records' / f'{key}.json'
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(record, ensure_ascii=False), encoding='utf-8')
        (root / 'index.jsonl').write_text('\n'.join(lines) + '\n', encoding='utf-8')
        with patched_library(root):
            first = build.build()
            self.assertEqual(first['representatives'], 2, first['excluded'])
            catalogue_id = representative_key(catalogue_source['collection_id'], catalogue_source['commit_sha'])
            retained_id = representative_key(retained_source['collection_id'], retained_source['commit_sha'])
            import sqlite3
            with sqlite3.connect(root / 'library/catalog.sqlite3') as con:
                con.row_factory = sqlite3.Row
                current = dict(con.execute(
                    'SELECT category, topic, label_from, review FROM skills WHERE id = ?', (catalogue_id,)
                ).fetchone())
            self.assertEqual(current['label_from'], 'unfiled', current)
            self.assertEqual(current['review'], 'unreviewed')
            proposal = root / 'proposal.json'
            record = {
                'id': catalogue_id, 'title': '条目甲', 'summary': '把零散事项记下来。',
                'repo': 'owner/frontend', 'ordinal': 1, 'decision': 'propose_existing_category',
                'proposed_category': 'code', 'proposed_topic': '前端与 Web 界面',
                'tasks': [{'goal': '为列表进入详情加上视图过渡', 'applicability': '在现有页面内'}],
                'sources': [{'repo': 'owner/frontend', 'path': 'skills/list-transition/SKILL.md', 'relationship': 'representative'}],
                'original_classification': current, 'quality_flags': [], 'reason': '目录线索',
                'source_record_sha256': 'a' * 64,
            }
            write_proposal(proposal, [record])
            code, report = run_cli([
                '--proposals', str(proposal), '--database', str(root / 'library/catalog.sqlite3'),
                '--overrides', str(root / 'library/classification-overrides.json'), '--apply',
            ])
            self.assertEqual(code, 0, report)
            rebuilt = build.build()
            self.assertEqual(rebuilt['classification_overrides']['applied'], 1, rebuilt['classification_overrides'])
        return catalogue_id, retained_id

    def check_api(self, port, catalogue, retained):
        meta = api(port, '/api/meta')
        self.assertEqual(meta['representatives'], 2)
        self.assertTrue(meta['task_search'])
        listed = api(port, '/api/skills?q=' + quote('为列表进入详情加上视图过渡'))
        self.assertEqual([item['id'] for item in listed['items']], [catalogue])
        self.assertIn('视图过渡', listed['items'][0]['match']['goal'])
        scoped = api(port, '/api/skills?category=code&topic=' + quote('前端与 Web 界面') + '&action=transition&tool=react')
        self.assertEqual([item['id'] for item in scoped['items']], [catalogue])
        self.assertEqual(api(port, '/api/skills?category=unfiled')['items'][0]['id'], retained)
        detail = api(port, '/api/skill/' + catalogue)
        self.assertEqual((detail['category'], detail['topic'], detail['label_from']), ('code', '前端与 Web 界面', 'catalogue'))
        self.assertEqual(detail['record']['extraction']['review_status'], 'unreviewed')
        self.assertEqual(detail['record']['content']['tasks'][0]['steps'][0]['action'], '给列表项设置过渡名称')
        self.assertEqual(detail['record']['content']['rules'][0]['text'], '不改数据请求')
        self.assertEqual(detail['sources'][0]['revision'], 'a' * 40)
        self.assertIn('github.com/owner/frontend', detail['sources'][0]['url'])
        self.assertTrue(detail['sources'][0]['has_raw'])
        self.assertEqual(api(port, '/api/source/' + catalogue)['text'], RAW.decode())
        retained_detail = api(port, '/api/skill/' + retained)
        self.assertEqual(retained_detail['label_from'], 'unfiled')
        self.assertEqual(retained_detail['record']['extraction']['review_status'], 'unreviewed')
        self.assertNotEqual(retained_detail['category'], 'code')

    def check_browser(self, page, catalogue, retained, shots):
        state = page.wait(lambda item: '条目甲' in item['cards'] and '条目乙' in item['cards'])
        self.assertIn('2 条', state['count'])
        self.assertFalse(state['actionHidden'])
        self.assertFalse(state['toolHidden'])
        page.shot(str(Path(shots) / 'smoke-list.png') if shots else None)
        page.evaluate('''(() => {
            document.getElementById('query').value = '为列表进入详情加上视图过渡';
            document.getElementById('search-form').requestSubmit();
        })()''')
        state = page.wait(lambda item: item['cards'] == ['条目甲'] and '视图过渡' in item['dialogText'] or item['cards'] == ['条目甲'])
        self.assertEqual(state['cards'], ['条目甲'])
        page.shot(str(Path(shots) / 'smoke-search.png') if shots else None)
        page.click_text('#results .card', '条目甲')
        state = page.wait(lambda item: item['dialogOpen'] and '目录归类，未审源文' in item['dialogText'])
        self.assertIn('未人工审阅', state['dialogText'])
        self.assertIn('软件开发与架构 · 前端与 Web 界面', state['dialogText'])
        self.assertIn('给列表项设置过渡名称', state['dialogText'])
        self.assertIn('不改数据请求', state['dialogText'])
        self.assertIn('退场动画不挡住点击', state['dialogText'])
        self.assertIn('未覆盖旧浏览器回退', state['dialogText'])
        self.assertNotIn('已审阅', state['dialogText'])
        page.shot(str(Path(shots) / 'smoke-detail-method.png') if shots else None)
        page.evaluate('''(() => {
            const heading = [...document.querySelectorAll('#detail h4')].find((node) => node.textContent === '规则与限制');
            heading.scrollIntoView({block: 'center'});
        })()''')
        page.shot(str(Path(shots) / 'smoke-detail-limits.png') if shots else None)
        page.click_text('#detail .tabs button', '来源')
        state = page.wait(lambda item: any('github.com/owner/frontend' in link for link in item['links']))
        self.assertIn('skills/list-transition/SKILL.md', state['dialogText'])
        self.assertIn('固定提交 ' + 'a' * 40, state['dialogText'])
        self.assertIn('MIT', state['dialogText'])
        page.evaluate('''(() => {
            [...document.querySelectorAll('#detail button')].find((node) => node.textContent === '查看本机原文').click();
        })()''')
        state = page.wait(lambda item: '只供页面冒烟' in item['dialogText'])
        page.shot(str(Path(shots) / 'smoke-detail-sources.png') if shots else None)
        page.evaluate('document.getElementById("close-detail").click()')
        page.wait(lambda item: not item['dialogOpen'])
        page.evaluate('document.getElementById("clear").click()')
        page.wait(lambda item: len(item['cards']) == 2)
        page.click_text('.nav-button:not([data-topic])', '软件开发与架构')
        page.wait(lambda item: item['title'].startswith('软件开发与架构'))
        page.click_text('.nav-button[data-topic]', '前端与 Web 界面')
        page.evaluate('''(() => {
            const action = document.getElementById('action');
            const tool = document.getElementById('tool');
            action.value = 'transition';
            action.dispatchEvent(new Event('change'));
            tool.value = 'react';
            tool.dispatchEvent(new Event('change'));
        })()''')
        state = page.wait(lambda item: item['cards'] == ['条目甲'] and '前端与 Web 界面' in item['title'])
        self.assertIn('前端与 Web 界面', state['title'])
        page.evaluate('document.getElementById("clear").click()')
        page.wait(lambda item: len(item['cards']) == 2)
        page.click_text('.nav-button:not([data-topic])', '待归类')
        state = page.wait(lambda item: item['cards'] == ['条目乙'])
        page.click_text('#results .card', '条目乙')
        state = page.wait(lambda item: item['dialogOpen'] and '未人工审阅' in item['dialogText'])
        self.assertIn('待归类', state['dialogText'])
        self.assertNotIn('目录归类，未审源文', state['dialogText'])
        page.shot(str(Path(shots) / 'smoke-retained.png') if shots else None)
        page.evaluate('document.getElementById("close-detail").click()')
        page.call('Emulation.setDeviceMetricsOverride', {
            'width': 390, 'height': 844, 'deviceScaleFactor': 1, 'mobile': True,
        })
        self.assertEqual(page.evaluate('getComputedStyle(document.querySelector(".sidebar")).display'), 'none')
        self.assertNotEqual(page.evaluate('getComputedStyle(document.querySelector(".mobile-category")).display'), 'none')
        page.evaluate('''(() => {
            const select = document.getElementById('category-select');
            select.value = 'unfiled';
            select.dispatchEvent(new Event('change'));
        })()''')
        state = page.wait(lambda item: item['cards'] == ['条目乙'])
        self.assertEqual(state['title'], '待归类')
        page.shot(str(Path(shots) / 'smoke-mobile-unfiled.png') if shots else None)


if __name__ == '__main__':
    unittest.main()
