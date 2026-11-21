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


_FILTER_FINISH = {'content_filter', 'content-filter'}
_FINISH_KEYS = ('finish_reason', 'finishReason', 'rawFinish', 'raw_finish', 'finish')
_STATUS_KEYS = ('status', 'statusCode', 'status_code')
_LITERAL_FINISH = re.compile(
    r'(?:"finish_reason"|\'finish_reason\'|\bfinish_reason)\s*[:=]\s*["\']?(content_filter|content-filter)\b',
    re.IGNORECASE)
_HTTP_400 = re.compile(r'\bHTTP\s*400\b', re.IGNORECASE)


def _embedded_json(text):
    if not isinstance(text, str):
        return None
    start = text.find('{')
    if start < 0:
        return None
    try:
        parsed, _ = json.JSONDecoder().raw_decode(text[start:])
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _provider_objects(error):
    """Error fields and one decoded provider body. Do not walk echoed prompt text."""
    found, seen = [], set()

    def add(obj):
        if isinstance(obj, dict) and id(obj) not in seen:
            seen.add(id(obj))
            found.append(obj)

    data = error.get('data') if isinstance(error.get('data'), dict) else None
    add(error)
    add(data)
    for source in (error, data):
        if not isinstance(source, dict):
            continue
        for key in ('message', 'responseBody', 'response_body'):
            parsed = _embedded_json(source.get(key))
            add(parsed)
            if not isinstance(parsed, dict):
                continue
            nested = parsed.get('error')
            add(nested if isinstance(nested, dict) else None)
            choices = parsed.get('choices')
            if isinstance(choices, list):
                for choice in choices:
                    add(choice if isinstance(choice, dict) else None)
    return found


def _explicit_content_filter(error):
    data = error.get('data') if isinstance(error.get('data'), dict) else {}
    if (error.get('type') in ('provider.content-filter', 'provider.content_filter') or
            data.get('type') in ('provider.content-filter', 'provider.content_filter') or
            error.get('code') in ('content_filter', 'content_policy_violation') or
            data.get('code') in ('content_filter', 'content_policy_violation') or
            error.get('name') == 'ContentFilterError'):
        return True
    return (error.get('type') == 'provider.invalid-request' and error.get('status') == 400 and
            str(error.get('message', '')).startswith(
                'Upstream request failed: [invalid_request_error] Request blocked by content filter:'))


def _records_http_400(error):
    for obj in _provider_objects(error):
        if any(obj.get(key) == 400 for key in _STATUS_KEYS):
            return True
    data = error.get('data') if isinstance(error.get('data'), dict) else {}
    for source in (error, data):
        message = source.get('message')
        if isinstance(message, str) and _HTTP_400.search(message):
            return True
    return False


def _finish_in_tree(value, depth=0):
    """Match a finish field in decoded objects, never inside prompt strings."""
    if depth > 8:
        return False
    if isinstance(value, dict):
        if any(value.get(key) in _FILTER_FINISH for key in _FINISH_KEYS):
            return True
        return any(_finish_in_tree(item, depth + 1) for item in value.values())
    if isinstance(value, list):
        return any(_finish_in_tree(item, depth + 1) for item in value)
    return False


def _records_filter_finish(error):
    for obj in _provider_objects(error):
        if _finish_in_tree(obj):
            return True
    data = error.get('data') if isinstance(error.get('data'), dict) else {}
    for source in (error, data):
        for key in ('message', 'responseBody', 'response_body'):
            text = source.get(key)
            if not isinstance(text, str) or not _LITERAL_FINISH.search(text):
                continue
            # A decoded body was already checked field-by-field. Do not treat a
            # finish_reason mentioned inside echoed prompt text as the refusal.
            if text.find('{') >= 0 and isinstance(_embedded_json(text), dict):
                continue
            return True
    return False


def _step_filter_finish(event):
    part = event.get('part') if isinstance(event.get('part'), dict) else None
    if event.get('type') not in ('step_finish', 'step-finish') and not (part and part.get('type') == 'step-finish'):
        return False
    reason = part.get('reason') if part else None
    if reason not in _FILTER_FINISH:
        reason = event.get('reason')
    return reason in _FILTER_FINISH


def content_filter_signal(events):
    """A source-specific provider refusal must not be resent or moved to another model.

    Explicit provider content-filter errors count on their own. A generic HTTP 400
    counts only together with a recorded finish reason of content_filter or
    content-filter, so ordinary request failures stay retryable.
    """
    http_400 = False
    filter_finish = False
    for event in events:
        if not isinstance(event, dict):
            continue
        error = event.get('error')
        if event.get('type') == 'error' and isinstance(error, dict):
            if _explicit_content_filter(error):
                return True
            # Finish text on a different event must not combine with an unrelated 400.
            if _records_http_400(error) and _records_filter_finish(error):
                return True
            http_400 = http_400 or _records_http_400(error)
        elif _step_filter_finish(event):
            filter_finish = True
        if http_400 and filter_finish:
            return True
    return False


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
                 fallback_override=None, free_override=None, free_backup=None, go_only=False):
        self.primary = config['primary']
        self.go_only = go_only
        if go_only and fallback_override:
            raise ValueError('Go-only routing cannot use a Luna fallback')
        self.configured_free_fallback = config['free_fallback']
        self.free_fallback = free_override or self.configured_free_fallback
        if free_backup == self.free_fallback:
            raise ValueError('Go backup must differ from the first Go model')
        self.free_models = (self.free_fallback, free_backup) if free_backup else (self.free_fallback,)
        self.luna = fallback_override
        self.max_model_redirects = len(self.free_models) + int(bool(self.luna))
        self.cooldown = config.get('primary_probe_seconds', 300)
        self.transient_cooldown = config.get('rate_probe_seconds', 30)
        if self.cooldown < 1 or self.transient_cooldown < 1:
            raise ValueError('Routing probe intervals must be positive')
        self.state_path = Path(state_path)
        self.factory, self.free_check, self.clock = factory, free_check, clock
        self.probe_timeout = probe_timeout
        self.lock = threading.Lock()
        self.free_catalog = {}

    @contextmanager
    def state(self):
        with self.lock:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            with self.state_path.with_suffix('.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                if self.state_path.exists():
                    state = json.loads(self.state_path.read_text())
                    if (state.get('primary'), state.get('free_fallback')) != (self.primary, self.configured_free_fallback):
                        raise ValueError('Routing state belongs to a different model pair; use a separate state path')
                else:
                    state = {'primary': self.primary, 'free_fallback': self.configured_free_fallback,
                             'primary_retry_at': 0, 'probe': None, 'events': []}
                yield state
                temp = self.state_path.with_suffix('.tmp')
                temp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n')
                temp.replace(self.state_path)

    def choose(self):
        with self.state() as state:
            t = self.clock()
            candidates = [] if self.go_only else [(self.primary, 'primary_retry_at', 'probe')]
            if self.luna:
                candidates.append((self.luna, 'luna_retry_at', 'luna_probe'))
            for model, retry_key, probe_key in candidates:
                retry_at = state.get(retry_key, 0)
                if not retry_at:
                    return model, None
                probe = state.get(probe_key)
                if t >= retry_at and (not probe or probe['expires'] <= t):
                    token = uuid.uuid4().hex
                    state[probe_key] = {'token': token, 'expires': t + self.probe_timeout}
                    return model, token
            cooling = state.get('go_transport_retry_at', {}) if len(self.free_models) > 1 else {}
            leases = state.get('go_transport_probes', {})
            for model in self.free_models:
                if model not in cooling:
                    return model, None
                if t >= cooling[model] and t >= leases.get(model, {}).get('expires', 0):
                    token = uuid.uuid4().hex
                    state.setdefault('go_transport_probes', {})[model] = {
                        'token': token, 'expires': t + self.probe_timeout,
                    }
                    return model, token
            raise ModelDeferred('Go connections are in cooldown', self.free_fallback)

    def observe_go_connection(self, model, probe, reply, reset):
        with self.state() as state:
            retry_times = state.setdefault('go_transport_retry_at', {})
            leases = state.setdefault('go_transport_probes', {})
            t = self.clock()
            matching_probe = probe and leases.get(model, {}).get('token') == probe
            if matching_probe:
                leases.pop(model)
                if not reply.get('transport_error') and not reply.get('limit'):
                    retry_times.pop(model, None)
                    self.event(state, 'go_connection_restored', model=model)
                else:
                    retry_times[model] = t + self.cooldown
            elif reset and t >= retry_times.get(model, 0) and model not in leases:
                retry_times[model] = t + self.cooldown
                self.event(state, 'go_connection_reset', model=model, retry_at=retry_times[model])
            return bool(reset and any(
                other != model and (other not in retry_times or (
                    t >= retry_times[other] and t >= leases.get(other, {}).get('expires', 0)
                ))
                for other in self.free_models
            ))

    def event(self, state, action, **fields):
        state['events'] = (state['events'] + [{'at': self.clock(), 'action': action, **fields}])[-500:]

    def observe(self, model, probe, reply):
        if model == self.primary:
            tier, probe_key = 'primary', 'probe'
        elif model == self.luna:
            tier, probe_key = 'luna', 'luna_probe'
        else:
            return
        retry_key = tier + '_retry_at'
        signal = reply.get('limit')
        with self.state() as state:
            if signal:
                delay = self.transient_cooldown if signal['kind'] in ('rate', 'concurrency') else self.cooldown
                retry_at = signal.get('retry_at', self.clock() + delay)
                if 'retry_after_seconds' in signal:
                    retry_at = self.clock() + signal['retry_after_seconds']
                if signal['kind'] == 'concurrency':
                    retry_at = min(retry_at, self.clock() + delay)
                state[retry_key] = max(self.clock() + 1, retry_at)
                state[probe_key] = None
                self.event(state, tier + '_limited', limit=signal, retry_at=state[retry_key])
            elif probe and (state.get(probe_key) or {}).get('token') == probe:
                state[probe_key] = None
                if not reply.get('transport_error'):
                    state[retry_key] = 0
                    self.event(state, tier + '_restored')
                else:
                    state[retry_key] = self.clock() + self.cooldown
                    self.event(state, tier + '_probe_failed', retry_at=state[retry_key])
            # Only a leased probe can restore a model after a quota rejection.

    def call(self, prompt, attempt):
        model, probe = self.choose()
        evidence = None
        if model in self.free_models:
            with self.lock:
                until, evidence = self.free_catalog.get(model, (0, None))
                if self.clock() >= until:
                    try:
                        evidence = self.free_check(model)
                    except Exception as exc:
                        raise ModelDeferred(f'Free fallback unavailable: {type(exc).__name__}: {exc}', model) from exc
                    self.free_catalog[model] = (self.clock() + 300, evidence)
        try:
            reply = self.factory(model).call(prompt, attempt)
        except Exception:
            self.observe(model, probe, {'transport_error': 'client exception'})
            raise
        self.observe(model, probe, reply)
        unavailable = (reply.get('transport_error') and not reply.get('limit') and
                       (not reply.get('timed_out') or reply.get('connection_reset')) and
                       not reply.get('input_error') and not reply.get('content_filtered'))
        redirect = model not in self.free_models and bool(reply.get('limit'))
        if model == self.primary:
            selected = 'primary_probe' if probe else 'primary'
        elif model == self.luna:
            selected = 'luna_probe' if probe else 'luna_fallback'
            redirect = redirect or bool(probe and unavailable)
        else:
            selected = 'go_only' if self.go_only else 'free_fallback'
            reset = bool(unavailable and reply.get('connection_reset'))
            if len(self.free_models) > 1 and (reset or probe):
                redirect = self.observe_go_connection(model, probe, reply, reset)
        reply = {**reply, 'model': reply.get('model') or model, 'routing': {
            'selected': selected, 'primary': None if self.go_only else self.primary,
            'free_fallback': self.free_fallback,
            'free_catalog_check': evidence},
            'retry_on_fallback': redirect}
        if model in self.free_models and reply.get('limit'):
            raise ModelDeferred('Fallback limited; waiting for model availability', model, reply)
        if unavailable and not redirect:
            # A provider/connection failure is not evidence that the Skill is
            # invalid. Pause the queue rather than fail thousands of sources.
            raise ModelDeferred('Model request unavailable; see retained events/stderr', model, reply)
        return reply
