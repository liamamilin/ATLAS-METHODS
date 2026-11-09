"""Small quota-aware selector for the existing OpenCode batch client."""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path
import re
import subprocess
import threading
import time
import uuid


class ModelDeferred(Exception):
    def __init__(self, message, model, reply=None):
        super().__init__(message)
        self.model = model
        self.reply = reply or {}


def content_filter_signal(events):
    """A source-specific provider refusal must not stall or change models."""
    return any(e.get('type') == 'error' and isinstance(e.get('error'), dict) and
               (e['error'].get('type') in ('provider.content-filter', 'provider.content_filter') or
                e['error'].get('code') in ('content_filter', 'content_policy_violation'))
               for e in events)


def connection_reset_signal(events):
    return any(e.get('type') == 'error' and isinstance(e.get('error'), dict) and
               str(e['error'].get('message', '')).startswith('Transport: ECONNRESET:')
               for e in events)


def limit_signal(events):
    """Only provider quota/rate errors, never schema/auth/context errors."""
    for event in events:
        if event.get('type') != 'error':
            continue
        if content_filter_signal([event]):
            continue
        error = event.get('error', {})
        if not isinstance(error, dict):
            continue
        data = error.get('data', error)
        if not isinstance(data, dict):
            data = error
        message = str(data.get('message', error.get('message', '')))
        explicit = re.search(r'\[code=([\w.-]+)\]', message)
        code = data.get('code') or error.get('code') or (explicit.group(1) if explicit else '')
        lower = message.lower()
        if any(s in lower for s in ('context length', 'context window', 'maximum context', 'invalid api key')):
            continue
        status = data.get('statusCode', data.get('status', error.get('statusCode', error.get('status'))))
        codes = {'tier_limit_exceeded', 'quota_exceeded', 'insufficient_quota',
                 'rate_limit_exceeded', 'usage_limit_exceeded', 'insufficient_credits',
                 'credit_balance_exhausted', 'budget_exceeded', 'insufficient_balance'}
        typed_quota = error.get('type') == 'provider.quota'
        if code not in codes and status != 429 and not typed_quota and not any(s in lower for s in (
                'quota exhausted', 'quota is exhausted', 'quota exceeded', 'rate limit exceeded', 'too many requests',
                'membership concurrency limit', 'insufficient credits')):
            continue
        kind = 'concurrency' if 'concurrency' in lower else ('rate' if status == 429 or 'rate_limit' in code or 'rate limit' in lower else 'quota')
        result = {'kind': kind, 'code': code or str(status or 'provider_limit')}
        reset = re.search(r'resets?(?: at)?[:\s]+(\d{4}-\d{2}-\d{2}T[\d:.]+(?:Z|[+-]\d{2}:\d{2}))', message)
        if reset:
            try:
                result['retry_at'] = datetime.fromisoformat(reset.group(1).replace('Z', '+00:00')).timestamp()
            except ValueError:
                pass
        after = data.get('retryAfter', error.get('retryAfter'))
        if isinstance(after, (int, float)) and after > 0:
            result['retry_after_seconds'] = after
        return result
    return None


def verify_free_model(executable, model):
    """Read current OpenCode catalog; no credentials or workspace headers saved."""
    provider, name = model.split('/', 1)
    if provider != 'opencode-go' or not name.endswith('-free'):
        raise ValueError('Automatic fallback must be an explicit opencode-go/*-free model')
    result = subprocess.run([executable, 'api', 'model.list', '--standalone'],
                            stdin=subprocess.DEVNULL, capture_output=True, timeout=60, check=True)
    catalog = json.loads(result.stdout)['data']
    entry = next((m for m in catalog if m.get('providerID') == provider and m.get('id') == name), None)
    if not entry or not entry.get('enabled') or entry.get('status') != 'active':
        raise ValueError('Free fallback is not active/enabled in the current catalog')
    prices = entry.get('cost')
    if not isinstance(prices, list) or not prices:
        raise ValueError('Free fallback price is unknown; refusing automatic fallback')
    for price in prices:
        if (price.get('input') != 0 or price.get('output') != 0 or
                price.get('cache', {}).get('read') != 0 or price.get('cache', {}).get('write') != 0):
            raise ValueError('Fallback is no longer explicitly zero-priced; refusing it')
    return {'model': model, 'checked_at': datetime.now(timezone.utc).isoformat(),
            'source': 'opencode api model.list', 'cost': prices,
            'status': entry['status'], 'enabled': True, 'limit': entry.get('limit')}


class QuotaRouter:
    uses_fallback = True

    def __init__(self, config, state_path, factory, free_check, clock=time.time, probe_timeout=300,
                 fallback_override=None):
        self.primary = config['primary']
        self.free_fallback = config['free_fallback']
        self.fallback = fallback_override or self.free_fallback
        self.cooldown = config.get('primary_probe_seconds', 300)
        self.transient_cooldown = config.get('rate_probe_seconds', 30)
        if self.cooldown < 1 or self.transient_cooldown < 1:
            raise ValueError('Routing probe intervals must be positive')
        self.state_path = Path(state_path)
        self.factory, self.free_check, self.clock = factory, free_check, clock
        self.probe_timeout = probe_timeout
        self.lock = threading.Lock()
        self.free_until, self.free_evidence = 0, None

    @contextmanager
    def state(self):
        with self.lock:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            with self.state_path.with_suffix('.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                if self.state_path.exists():
                    state = json.loads(self.state_path.read_text())
                    if (state.get('primary'), state.get('free_fallback')) != (self.primary, self.free_fallback):
                        raise ValueError('Routing state belongs to a different model pair; use a separate state path')
                else:
                    state = {'primary': self.primary, 'free_fallback': self.free_fallback,
                             'primary_retry_at': 0, 'probe': None, 'events': []}
                yield state
                temp = self.state_path.with_suffix('.tmp')
                temp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n')
                temp.replace(self.state_path)

    def choose(self):
        with self.state() as state:
            t = self.clock()
            if not state['primary_retry_at']:
                return self.primary, None
            probe = state.get('probe')
            if t >= state['primary_retry_at'] and (not probe or probe['expires'] <= t):
                token = uuid.uuid4().hex
                state['probe'] = {'token': token, 'expires': t + self.probe_timeout}
                return self.primary, token
            return self.fallback, None

    def event(self, state, action, **fields):
        state['events'] = (state['events'] + [{'at': self.clock(), 'action': action, **fields}])[-500:]

    def observe(self, model, probe, reply):
        signal = reply.get('limit')
        with self.state() as state:
            if model != self.primary:
                return
            if signal:
                delay = self.transient_cooldown if signal['kind'] in ('rate', 'concurrency') else self.cooldown
                retry_at = signal.get('retry_at', self.clock() + delay)
                if 'retry_after_seconds' in signal:
                    retry_at = self.clock() + signal['retry_after_seconds']
                if signal['kind'] == 'concurrency':
                    retry_at = min(retry_at, self.clock() + delay)
                state['primary_retry_at'] = max(self.clock() + 1, retry_at)
                state['probe'] = None
                self.event(state, 'primary_limited', limit=signal, retry_at=state['primary_retry_at'])
            elif probe and (state.get('probe') or {}).get('token') == probe:
                state['probe'] = None
                if not reply.get('transport_error'):
                    state['primary_retry_at'] = 0
                    self.event(state, 'primary_restored')
                else:
                    state['primary_retry_at'] = self.clock() + self.cooldown
                    self.event(state, 'primary_probe_failed', retry_at=state['primary_retry_at'])
            # A request already in flight before quota failed cannot prove that
            # quota recovered. Only the later leased probe may restore primary.

    def call(self, prompt, attempt):
        model, probe = self.choose()
        evidence = None
        if model == self.free_fallback:
            with self.lock:
                if self.clock() >= self.free_until:
                    try:
                        self.free_evidence = self.free_check(model)
                    except Exception as exc:
                        raise ModelDeferred(f'Free fallback unavailable: {type(exc).__name__}: {exc}', model) from exc
                    self.free_until = self.clock() + 300
                evidence = self.free_evidence
        try:
            reply = self.factory(model).call(prompt, attempt)
        except Exception:
            self.observe(model, probe, {'transport_error': 'client exception'})
            raise
        self.observe(model, probe, reply)
        selected = 'primary_probe' if probe else 'primary'
        if model != self.primary:
            selected = 'free_fallback' if model == self.free_fallback else 'luna_fallback'
        reply = {**reply, 'model': reply.get('model') or model, 'routing': {
            'selected': selected, 'primary': self.primary, 'free_fallback': self.free_fallback,
            'free_catalog_check': evidence},
            'retry_on_fallback': model == self.primary and bool(reply.get('limit'))}
        if model == self.fallback and reply.get('limit'):
            raise ModelDeferred('Fallback limited; waiting for model availability', model, reply)
        if (reply.get('transport_error') and not reply.get('limit') and
                (not reply.get('timed_out') or reply.get('connection_reset')) and
                not reply.get('input_error') and not reply.get('content_filtered')):
            # A provider/connection failure is not evidence that the Skill is
            # invalid. Pause the queue rather than fail thousands of sources.
            raise ModelDeferred('Model request unavailable; see retained events/stderr', model, reply)
        return reply
