from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from chrome_probe import APP_BINARIES, ChromePage, chrome_path


def touch(root, relative):
    path = Path(root) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'')
    return path


class ChromePathTests(unittest.TestCase):
    def test_macos_chrome_app_is_used_when_path_has_no_browser(self):
        with TemporaryDirectory() as temp:
            binary = touch(temp, APP_BINARIES[0])
            found = chrome_path(path_lookup=lambda name: None, roots=(temp,))
            self.assertEqual(found, str(binary))

    def test_path_entry_wins_over_an_app_bundle(self):
        with TemporaryDirectory() as temp:
            touch(temp, APP_BINARIES[0])
            found = chrome_path(
                path_lookup=lambda name: '/opt/google-chrome' if name == 'google-chrome' else None,
                roots=(temp,),
            )
            self.assertEqual(found, '/opt/google-chrome')

    def test_chromium_and_canary_follow_stable_chrome(self):
        with TemporaryDirectory() as temp:
            canary = touch(temp, APP_BINARIES[2])
            self.assertEqual(chrome_path(path_lookup=lambda name: None, roots=(temp,)), str(canary))
            chromium = touch(temp, APP_BINARIES[1])
            self.assertEqual(chrome_path(path_lookup=lambda name: None, roots=(temp,)), str(chromium))
            stable = touch(temp, APP_BINARIES[0])
            self.assertEqual(chrome_path(path_lookup=lambda name: None, roots=(temp,)), str(stable))

    def test_home_applications_is_searched_after_the_system_folder(self):
        with TemporaryDirectory() as system, TemporaryDirectory() as home:
            touch(home, APP_BINARIES[0])
            self.assertEqual(
                chrome_path(path_lookup=lambda name: None, roots=(system, home)),
                str(Path(home) / APP_BINARIES[0]),
            )
            system_chrome = touch(system, APP_BINARIES[0])
            self.assertEqual(chrome_path(path_lookup=lambda name: None, roots=(system, home)), str(system_chrome))


class ChromeRetryTests(unittest.TestCase):
    def page(self, alive=True):
        state = {'alive': alive}
        page = ChromePage(None, 9, None)
        page.proc = type('Proc', (), {'poll': lambda self: None if state['alive'] else 0})()
        page._launches = []

        def launch():
            page._launches.append(list(getattr(page, '_command', [])))
            state['alive'] = True
            page.proc = type('Proc', (), {'poll': lambda self: None})()

        page._launch = launch
        page._target = lambda url: {'webSocketDebuggerUrl': 'ws://127.0.0.1:9/devtools/page/1'}
        page._connect = lambda websocket_url: None
        return page, state

    def test_page_enable_disconnect_reconnects_once_without_a_new_window(self):
        page, state = self.page(alive=True)
        calls = []

        def call(method, params=None):
            calls.append(method)
            if method == 'Page.enable' and calls.count('Page.enable') == 1:
                raise RuntimeError('Chrome 调试连接中断')
            return {}

        page.call = call
        page._attach('http://127.0.0.1:1/')
        self.assertEqual(calls, ['Page.enable', 'Page.enable', 'Runtime.enable'])
        self.assertEqual(page._launches, [])
        self.assertTrue(state['alive'])

    def test_a_dead_browser_is_relaunched_once_with_the_same_command(self):
        page, state = self.page(alive=False)
        page._command = ['/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', '--headless=new']
        calls = []

        def call(method, params=None):
            calls.append((state['alive'], method))
            if method == 'Page.enable' and len(calls) == 1:
                state['alive'] = False
                page.proc = type('Proc', (), {'poll': lambda self: 0})()
                raise RuntimeError('Chrome 调试连接中断')
            return {}

        page.call = call
        page._attach('http://127.0.0.1:1/')
        self.assertEqual(page._launches, [page._command, page._command])
        self.assertEqual([method for _, method in calls], ['Page.enable', 'Page.enable', 'Runtime.enable'])

    def test_a_second_disconnect_is_not_retried_forever(self):
        page, _ = self.page()

        def call(method, params=None):
            raise RuntimeError('Chrome 调试连接中断')

        page.call = call
        with self.assertRaisesRegex(RuntimeError, '调试连接中断'):
            page._attach('http://127.0.0.1:1/')

    def test_other_runtime_errors_are_not_retried(self):
        page, _ = self.page()
        calls = []

        def call(method, params=None):
            calls.append(method)
            raise RuntimeError('别的错误')

        page.call = call
        with self.assertRaisesRegex(RuntimeError, '别的错误'):
            page._attach('http://127.0.0.1:1/')
        self.assertEqual(calls, ['Page.enable'])


if __name__ == '__main__':
    unittest.main()
