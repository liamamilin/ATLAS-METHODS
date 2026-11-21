"""Drive the installed Chrome against an already running local page. No network publish."""
import base64
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import time
from urllib.request import Request, urlopen


PATH_NAMES = ('google-chrome', 'chromium', 'chromium-browser')
APP_BINARIES = (
    'Google Chrome.app/Contents/MacOS/Google Chrome',
    'Chromium.app/Contents/MacOS/Chromium',
    'Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary',
)


def chrome_path(path_lookup=None, roots=None):
    """Find Chrome without requiring the macOS app bundle to be on PATH."""
    lookup = shutil.which if path_lookup is None else path_lookup
    for name in PATH_NAMES:
        found = lookup(name)
        if found:
            return found
    if roots is None:
        roots = (Path('/Applications'), Path.home() / 'Applications')
    for relative in APP_BINARIES:
        for root in roots:
            candidate = Path(root) / relative
            if candidate.is_file():
                return str(candidate)
    return None


class ChromePage:
    def __init__(self, proc, port, sock):
        self.proc = proc
        self.port = port
        self.sock = sock
        self.next_id = 0
        self.pending = b''

    @classmethod
    def start(cls, user_data):
        binary = chrome_path()
        if not binary:
            raise RuntimeError('未找到 Chrome')
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        page = cls(None, port, None)
        page._command = [
            binary, '--headless=new', '--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu',
            f'--remote-debugging-port={port}', f'--user-data-dir={user_data}',
            '--window-size=1280,900', '--no-first-run', '--no-default-browser-check',
        ]
        page._launch()
        return page

    def _launch(self):
        self.proc = subprocess.Popen(
            self._command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                with urlopen(f'http://127.0.0.1:{self.port}/json/version', timeout=0.5) as response:
                    if response.status == 200:
                        return
            except OSError:
                if self.proc.poll() is not None:
                    raise RuntimeError(self.proc.stderr.read())
                time.sleep(0.1)
        self.proc.kill()
        raise RuntimeError('Chrome 调试端口没有起来')

    def open(self, url):
        self._attach(url)
        self.wait(lambda state: state['busy'] == 'false' and state['updated'] != '正在读取本机目录')
        return self

    def _attach(self, url):
        """Reconnect once if DevTools drops while the page target is enabled."""
        error = None
        for attempt in range(2):
            try:
                if self.proc is None or self.proc.poll() is not None:
                    self._launch()
                self._connect(self._target(url)['webSocketDebuggerUrl'])
                self.call('Page.enable')
                self.call('Runtime.enable')
                return
            except RuntimeError as exc:
                error = exc
                self._drop_socket()
                if attempt or '调试连接' not in str(exc):
                    raise
        raise error

    def _target(self, url):
        request = Request(f'http://127.0.0.1:{self.port}/json/new?{quote(url)}', method='PUT')
        try:
            with urlopen(request, timeout=5) as response:
                return json.load(response)
        except OSError:
            request = Request(f'http://127.0.0.1:{self.port}/json/new?{quote(url)}')
            with urlopen(request, timeout=5) as response:
                return json.load(response)

    def _drop_socket(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None
        self.pending = b''

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
            const metric = document.querySelector('#metrics dd');
            return {
                title: document.getElementById('result-title')?.textContent || '',
                count: document.getElementById('result-count')?.textContent || '',
                busy: results?.getAttribute('aria-busy'),
                updated: document.getElementById('updated')?.textContent || '',
                metric: metric?.textContent || '',
                cards: [...document.querySelectorAll('#results .card h3')].map((node) => node.textContent),
                dialogOpen: !!dialog?.open,
                dialogText: dialog?.innerText || '',
                links: [...document.querySelectorAll('#detail a')].map((node) => node.href),
                actionHidden: !!document.getElementById('action')?.parentElement.hidden,
                toolHidden: !!document.getElementById('tool')?.parentElement.hidden,
                error: document.querySelector('#results .notice')?.textContent || '',
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
        clicked = self.evaluate(
            '(() => { const node = [...document.querySelectorAll(' + json.dumps(selector) + ')]'
            '.find((item) => item.textContent.includes(' + json.dumps(text) + '));'
            ' if (!node) return false; node.click(); return true; })()'
        )
        if not clicked:
            raise AssertionError(f'找不到 {selector} / {text}')

    def shot(self, path):
        if not path:
            return
        image = base64.b64decode(self.call('Page.captureScreenshot', {'format': 'png'})['data'])
        Path(path).parent.mkdir(parents=True, exist_ok=True)
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
        while True:
            opcode, data = self._frame()
            if opcode == 8:
                raise RuntimeError('Chrome 关闭了调试连接')
            if opcode == 9:
                self._pong(data)
                continue
            if opcode == 1:
                return data.decode()

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


def quote(url):
    from urllib.parse import quote as encode
    return encode(url, safe='')
