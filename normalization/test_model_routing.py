import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_batch_convert as fixtures
from batch_convert import run_batch
from model_routing import ModelDeferred, QuotaRouter, limit_signal, verify_free_model


PRIMARY = 'open-design/deepseek-v4.1-flash'
FREE = 'opencode-go/iceman-contributor-free'
CONFIG = {'primary': PRIMARY, 'free_fallback': FREE,
          'primary_probe_seconds': 40, 'rate_probe_seconds': 10}


def limited():
    return {'text': '', 'transport_error': 'quota exhausted',
            'limit': {'kind': 'quota', 'code': 'quota_exceeded'},
            'session_id': 'limited', 'usage': {'available': False}}


def healthy():
    return {'text': json.dumps(fixtures.response()), 'finish': 'stop', 'tool_events': [],
            'session_id': 'success', 'usage': {'available': True, 'turns': [
                {'tokens': {'input': 11, 'output': 7}, 'cost': 0}]}}


class RoutingTests(unittest.TestCase):
    def router(self, root, queue=None, clock=None, check=None):
        self.calls = []
        queue = queue or {}
        clock = clock or [100]

        def factory(model):
            def call(prompt, attempt):
                self.calls.append(model)
                reply = queue[model].pop(0) if queue.get(model) else healthy()
                if attempt.exists():
                    (attempt / 'response.txt').write_text(reply.get('text', ''))
                return reply
            return SimpleNamespace(call=call)

        return QuotaRouter(CONFIG, root / 'route.json', factory,
                           check or (lambda model: {'model': model, 'cost': [{'input': 0, 'output': 0}]}),
                           clock=lambda: clock[0], probe_timeout=30)

    def test_only_quota_rate_or_concurrency_errors_trigger(self):
        q = limit_signal([{'type': 'error', 'error': {'message': '[code=tier_limit_exceeded] membership concurrency limit exceeded: 11/10 resets 2026-11-07T15:19:00Z'}}])
        self.assertEqual(q['kind'], 'concurrency')
        self.assertIn('retry_at', q)
        self.assertEqual(limit_signal([{'type': 'error', 'error': {'statusCode': 429}}])['kind'], 'rate')
        for message in ['invalid API key', 'maximum context length exceeded', 'JSON schema validation failed']:
            self.assertIsNone(limit_signal([{'type': 'error', 'error': {'message': message}}]))

    def test_fallback_then_one_real_task_probe_and_restore(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t);clock = [100]
            router = self.router(root, {PRIMARY: [limited(), healthy()]}, clock)
            self.assertTrue(router.call('fixture', root)['retry_on_fallback'])
            self.assertEqual(router.call('fixture', root)['model'], FREE)
            clock[0] = 139
            self.assertEqual(router.call('fixture', root)['model'], FREE)
            clock[0] = 141
            reply = router.call('fixture', root)
            self.assertEqual(reply['routing']['selected'], 'primary_probe')
            self.assertEqual(router.call('fixture', root)['model'], PRIMARY)
            state = json.loads((root / 'route.json').read_text())
            self.assertEqual(state['primary_retry_at'], 0)
            self.assertEqual([e['action'] for e in state['events']], ['primary_limited', 'primary_restored'])

    def test_still_limited_probe_and_stale_inflight_success_do_not_restore(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t);clock = [100]
            router = self.router(root, {PRIMARY: [limited(), limited()]}, clock)
            router.call('fixture', root)
            router.observe(PRIMARY, None, healthy())
            self.assertEqual(router.choose()[0], FREE)
            clock[0] = 141
            self.assertTrue(router.call('fixture', root)['retry_on_fallback'])
            self.assertEqual(router.choose()[0], FREE)

    def test_probe_lease_persists_across_restart_and_expires_after_crash(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t);clock = [100]
            a = self.router(root, {PRIMARY: [limited()]}, clock)
            a.call('fixture', root);clock[0] = 141
            model, old_probe = a.choose()
            b = self.router(root, clock=clock)
            self.assertEqual(b.choose()[0], FREE)
            clock[0] = 172
            _, new_probe = b.choose()
            self.assertNotEqual(old_probe, new_probe)
            a.observe(PRIMARY, old_probe, healthy())
            self.assertNotEqual(json.loads((root / 'route.json').read_text())['primary_retry_at'], 0)
            b.observe(PRIMARY, new_probe, healthy())
            self.assertEqual(b.choose()[0], PRIMARY)

    def test_auth_or_other_transport_error_does_not_enable_fallback(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            router = self.router(root, {PRIMARY: [{'transport_error': 'authentication failed'}]})
            self.assertNotIn('limit', router.call('fixture', root))
            self.assertEqual(router.call('fixture', root)['model'], PRIMARY)
            self.assertNotIn(FREE, self.calls)

    def test_known_reset_time_is_respected(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t);clock = [100]
            reply = limited();reply['limit']['retry_at'] = 1000
            router = self.router(root, {PRIMARY: [reply]}, clock)
            router.call('fixture', root)
            clock[0] = 999
            self.assertEqual(router.choose()[0], FREE)
            clock[0] = 1001
            self.assertEqual(router.choose()[0], PRIMARY)

    def test_expired_free_price_check_is_repeated_and_repricing_blocks_call(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t);clock = [100];checks = []
            def check(model):
                checks.append(model)
                if len(checks) > 1:
                    raise ValueError('price changed')
                return {'model': model, 'cost': [{'input': 0, 'output': 0}]}
            router = self.router(root, {PRIMARY: [limited(), limited()]}, clock, check)
            router.call('fixture', root)
            router.call('fixture', root)
            clock[0] = 401
            router.call('fixture', root)  # still-limited primary probe
            with self.assertRaises(ModelDeferred):
                router.call('fixture', root)
            self.assertEqual(checks, [FREE, FREE])
            self.assertEqual(self.calls.count(FREE), 1)

    def test_free_catalog_guard_rejects_paid_unknown_or_disabled_models(self):
        entry = {'id': 'iceman-contributor-free', 'providerID': 'opencode-go', 'enabled': True,
                 'status': 'active', 'cost': [{'input': 0, 'output': 0, 'cache': {'read': 0, 'write': 0}}]}
        def validate(value):
            with patch('model_routing.subprocess.run', return_value=SimpleNamespace(stdout=json.dumps({'data': [value]}).encode())):
                return verify_free_model('fixture-cli', FREE)
        self.assertEqual(validate(entry)['model'], FREE)
        for value in [{**entry, 'cost': []}, {**entry, 'enabled': False},
                      {**entry, 'cost': [{'input': 0, 'output': 1, 'cache': {'read': 0, 'write': 0}}]}]:
            with self.assertRaises(ValueError):
                validate(value)
        with self.assertRaises(ValueError):
            verify_free_model('unused', 'opencode-go/paid-model')

    def test_converter_preserves_both_attempts_and_actual_model_usage(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            args, rows, items = fixtures.BatchTests().fixture(root, 2)
            args.max_attempts = 1
            router = self.router(root, {PRIMARY: [limited()]})
            report = run_batch(args, router)
            self.assertEqual((report['succeeded'], report['failed']), (2, 0))
            self.assertEqual(report['record_models'], {FREE: 2})
            self.assertEqual(report['usage']['by_model'][PRIMARY]['sessions'], 1)
            self.assertEqual(report['usage']['by_model'][FREE]['sessions'], 2)
            self.assertEqual(report['usage']['tokens']['input'], 22)
            self.assertEqual(len(list(args.out.glob('attempts/*/*/metadata.json'))), 3)

    def test_unavailable_or_repriced_free_model_defers_without_charging_other_model(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            args, rows, items = fixtures.BatchTests().fixture(root, 2)
            def refuse(model):
                raise ValueError('no longer free')
            router = self.router(root, {PRIMARY: [limited()]}, check=refuse)
            report = run_batch(args, router)
            self.assertEqual((report['succeeded'], report['failed'], report['pending'], report['deferred']), (0, 0, 2, 2))
            self.assertEqual(self.calls, [PRIMARY])


if __name__ == '__main__':
    unittest.main()
