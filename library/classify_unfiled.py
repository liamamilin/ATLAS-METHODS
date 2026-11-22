#!/usr/bin/env python3
"""Propose catalogue labels for unfiled skills through the free OpenCode Go model.

This is not the SkillRecord converter. It only writes proposal files. It does
not update the catalogue database or the classification override layer.
"""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from classification_overrides import (
    DATABASE, OVERRIDES, catalogue_topics, connect_readonly, load_overrides, static_topics, topic_allowed,
)
from taxonomy import LABELS

MODEL = 'opencode-go/iceman-contributor-free'
PROVIDER = 'opencode-go'
MODEL_ID = 'iceman-contributor-free'
MAX_IN_FLIGHT = 10
TRIAL_LIMIT = 10
# Event lines are the JSON objects printed by `opencode run --format json`.
# Text is events with type "text" and part.text. Errors use type "error".
# Cost and the actual model come from `opencode session export`, whose assistant
# messages carry model.providerID, model.id, and cost. The catalogue prices come
# from `opencode api model.list` field data[].cost.


def now():
    return datetime.now(timezone.utc).isoformat()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def fingerprint(skill):
    return hashlib.sha256(canonical(skill['identity']).encode()).hexdigest()


def allowed_vocabulary(database):
    known = catalogue_topics(database)
    vocabulary = {}
    for category in LABELS:
        if category == 'unfiled':
            continue
        names = set(static_topics(category))
        names.update(known.get(category, ()))
        vocabulary[category] = sorted(names)
    return vocabulary


def parse_events(stdout):
    events = []
    for line in stdout.decode('utf-8', errors='replace').splitlines():
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            events.append({'type': 'unparsed', 'line': line})
    return events


def response_text(events):
    return ''.join(
        event.get('part', {}).get('text', '')
        for event in events
        if isinstance(event, dict) and event.get('type') == 'text' and isinstance(event.get('part'), dict)
    )


def session_ids(events):
    return sorted({event.get('sessionID') for event in events if isinstance(event, dict) and event.get('sessionID')})


def rate_limit(events):
    """Return retry seconds for an HTTP 429, without treating other errors as rate limits."""
    for event in events:
        if not isinstance(event, dict) or event.get('type') != 'error':
            continue
        error = event.get('error')
        if not isinstance(error, dict):
            continue
        data = error.get('data') if isinstance(error.get('data'), dict) else {}
        status = data.get('status', error.get('status', error.get('statusCode')))
        message = str(data.get('message', error.get('message', '')))
        if status != 429 and 'rate limit' not in message.lower() and 'too many requests' not in message.lower():
            continue
        after = data.get('retryAfter', error.get('retryAfter'))
        if not isinstance(after, (int, float)) or after <= 0:
            match = re.search(r'retry after (\d+)', message.lower())
            after = int(match.group(1)) if match else 2
        return min(60, float(after))
    return None


def quota_exhausted(events):
    for event in events:
        if not isinstance(event, dict) or event.get('type') != 'error':
            continue
        error = event.get('error')
        if not isinstance(error, dict):
            continue
        message = json.dumps(error, ensure_ascii=False).lower()
        if any(token in message for token in ('quota', 'insufficient_balance', 'insufficient credits', 'credit_balance')):
            if rate_limit([event]) is None:
                return True
    return False


def zero_prices(entry):
    prices = entry.get('cost')
    if not isinstance(prices, list) or not prices:
        return False
    for price in prices:
        cache = price.get('cache') if isinstance(price.get('cache'), dict) else {}
        if price.get('input') != 0 or price.get('output') != 0 or cache.get('read') != 0 or cache.get('write') != 0:
            return False
    return True


class PriceStop(RuntimeError):
    pass


class OpenCodeFreeClient:
    def __init__(self, executable, timeout=180, sleeper=time.sleep):
        self.executable = executable
        self.timeout = timeout
        self.sleeper = sleeper

    def check_price(self):
        result = subprocess.run(
            [self.executable, 'api', 'model.list', '--standalone'],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=60,
        )
        if result.returncode != 0:
            raise PriceStop('model.list failed; no classification request was sent')
        payload = json.loads(result.stdout)
        catalog = payload.get('data') if isinstance(payload, dict) else None
        if not isinstance(catalog, list):
            raise PriceStop('model.list did not return data[]; no classification request was sent')
        entry = next((item for item in catalog
                      if item.get('providerID') == PROVIDER and item.get('id') == MODEL_ID), None)
        if not entry or entry.get('enabled') is not True or entry.get('status') != 'active' or not zero_prices(entry):
            raise PriceStop(f'{MODEL} is not an active zero-price model; stopping without a fallback')
        return {'model': MODEL, 'checked_at': now(), 'cost': entry['cost'], 'status': 'active', 'enabled': True}

    def command(self, title, prompt):
        return [self.executable, 'run', '--standalone', '--model', MODEL, '--format', 'json', '--title', title, prompt]

    def request(self, prompt, title, attempt):
        started = time.monotonic()
        command = self.command(title, prompt)
        with tempfile.TemporaryDirectory(prefix='atlas-catalogue-') as temp:
            work = Path(temp)
            env = os.environ.copy()
            env['PWD'] = str(work)
            proc = subprocess.Popen(
                command, cwd=work, env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
            )
            timed_out = False
            try:
                stdout, stderr = proc.communicate(timeout=self.timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                os.killpg(proc.pid, signal.SIGTERM)
                stdout, stderr = proc.communicate()
            (attempt / 'events.jsonl').write_bytes(stdout)
            (attempt / 'stderr.txt').write_bytes(stderr)
            (attempt / 'request.txt').write_text(prompt, encoding='utf-8')
            events = parse_events(stdout)
            sessions = session_ids(events)
            usage = {'available': False}
            if len(sessions) == 1 and not timed_out and proc.returncode == 0:
                exported = subprocess.run(
                    [self.executable, 'session', 'export', '--standalone', sessions[0]],
                    cwd=work, env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=60,
                )
                (attempt / 'session-export.json').write_bytes(exported.stdout)
                if exported.returncode == 0:
                    usage = export_usage(exported.stdout)
            duration = round(time.monotonic() - started, 3)
            return {
                'text': response_text(events), 'events': events, 'usage': usage,
                'exit_code': proc.returncode, 'timed_out': timed_out, 'duration_seconds': duration,
                'session_id': sessions[0] if len(sessions) == 1 else None,
                'rate_limit_seconds': rate_limit(events), 'quota_exhausted': quota_exhausted(events),
                'command': command[:-1] + ['<prompt>'],
            }


def export_usage(raw):
    session = json.loads(raw.decode('utf-8'))
    messages = session.get('messages') if isinstance(session, dict) else None
    if not isinstance(messages, list):
        return {'available': False, 'reason': 'session export has no messages'}
    turns = []
    for message in messages:
        info = message.get('info', message) if isinstance(message, dict) else None
        if not isinstance(info, dict) or not isinstance(info.get('tokens'), dict):
            continue
        model = info.get('model') if isinstance(info.get('model'), dict) else {}
        turns.append({
            'providerID': model.get('providerID'), 'id': model.get('id'),
            'cost': info.get('cost'), 'finish': info.get('finish'),
        })
    if not turns:
        return {'available': False, 'reason': 'session export has no model turn'}
    return {'available': True, 'turns': turns}


def assert_free_turn(usage):
    if not usage.get('available'):
        raise PriceStop('actual model cost could not be read; stopping without another request')
    for turn in usage['turns']:
        if turn.get('providerID') != PROVIDER or turn.get('id') != MODEL_ID:
            raise PriceStop(
                f"response provider/model was {turn.get('providerID')}/{turn.get('id')}, not {MODEL}; stopping"
            )
        if turn.get('cost') not in (0, 0.0):
            raise PriceStop(f"response cost was {turn.get('cost')}, not 0; stopping without a fallback")


def parse_decision(text):
    body = text.strip()
    if body.startswith('```'):
        lines = [line for line in body.splitlines() if not line.startswith('```')]
        body = '\n'.join(lines).strip()
    try:
        value = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ValueError(f'invalid JSON: {exc.msg}') from exc
    if not isinstance(value, dict):
        raise ValueError('model output is not a JSON object')
    return value


def evidence_error(decision, skill):
    if decision.get('decision') == 'retain_unfiled':
        if not isinstance(decision.get('retention_reason'), str) or not decision['retention_reason'].strip():
            return 'retain_unfiled requires retention_reason'
        return None
    if decision.get('decision') != 'propose_existing_category':
        return 'decision must be propose_existing_category or retain_unfiled'
    category, topic = decision.get('category'), decision.get('topic')
    if not isinstance(category, str) or category not in skill['vocabulary'] or '/' in category:
        return f'category must be an existing category id, not {category!r}'
    if topic not in skill['vocabulary'][category]:
        return f'topic must be an existing topic name for {category}, not {topic!r}'
    evidence = decision.get('evidence')
    if not isinstance(evidence, list) or not evidence:
        return 'propose requires evidence quotes from the supplied summary or tasks'
    for item in evidence:
        if not isinstance(item, dict):
            return 'evidence entries must be objects'
        quote = item.get('quote')
        field = item.get('field')
        if field not in ('summary', 'task'):
            return 'evidence field must be summary or task; title alone is not accepted'
        if not isinstance(quote, str) or len(quote.strip()) < 8:
            return 'evidence quote is missing or too short'
        if field == 'summary':
            source = skill['summary']
        elif field == 'task':
            try:
                task = skill['tasks'][item.get('task_index')]
            except (IndexError, KeyError, TypeError):
                return 'evidence task_index is outside the supplied tasks'
            source = f"{task.get('goal') or ''}\n{task.get('applicability') or ''}"
        if quote not in source:
            return 'evidence quote is not a substring of the supplied field'
    if not isinstance(decision.get('reason'), str) or not decision['reason'].strip():
        return 'propose requires reason'
    return None


def prompt_for(skill, feedback=''):
    listed = {key: skill['vocabulary'][key] for key in skill['vocabulary']}
    payload = {
        'id': skill['id'], 'title_context_not_sufficient': skill['title'],
        'summary': skill['summary'], 'tasks': skill['tasks'],
        'current': skill['original_classification'],
        'allowed': listed,
    }
    rules = (
        '为这条待归类目录材料选择一个既有领域 id 和该领域下的既有专题名。'
        'category 只能是 allowed 的键，不能写成“领域/专题”。topic 必须与 allowed 里的字符串完全相同。'
        'evidence.quote 必须是 summary 或某个 task 的 goal/applicability 里的连续原文，不能只依据 title。'
        '材料不足以区分时 decision 用 retain_unfiled，并写 retention_reason，不要发明专题。'
        '只返回一个 JSON 对象，字段为 decision, category, topic, evidence, reason, retention_reason。'
        '这是目录归类提案，不是源文审阅，不要把 review 改成已审。'
    )
    if feedback:
        rules += '\n上一次输出未通过检查：' + feedback
    return rules + '\n' + json.dumps(payload, ensure_ascii=False)


def load_skill(connection, skill_id, vocabulary):
    row = connection.execute('SELECT * FROM skills WHERE id = ?', (skill_id,)).fetchone()
    if row is None or row['category'] != 'unfiled':
        return None
    tasks = [dict(item) for item in connection.execute(
        'SELECT goal, applicability FROM catalog_tasks WHERE skill = ? ORDER BY position, id', (skill_id,)
    )]
    source = connection.execute(
        '''SELECT repo, path, revision, relationship FROM sources
           WHERE skill = ? AND relationship = ? ORDER BY id LIMIT 1''',
        (skill_id, 'representative'),
    ).fetchone()
    if source is None:
        return None
    skill = {
        'id': skill_id, 'title': row['title'], 'summary': row['summary'], 'tasks': tasks,
        'original_classification': {
            'category': row['category'], 'topic': row['topic'],
            'label_from': row['label_from'], 'review': row['review'],
        },
        'representative': {'repo': source['repo'], 'path': source['path'], 'revision': source['revision']},
        'vocabulary': vocabulary,
    }
    skill['identity'] = {
        'id': skill_id, 'title': skill['title'], 'summary': skill['summary'], 'tasks': tasks,
        'representative': skill['representative'], 'original_classification': skill['original_classification'],
    }
    skill['fingerprint'] = fingerprint(skill)
    return skill


def select_ids(connection, after, excluded, limit):
    rows = connection.execute(
        '''SELECT id FROM skills WHERE category = ? AND id > ? ORDER BY id''', ('unfiled', after),
    )
    chosen = []
    for (skill_id,) in rows:
        if skill_id in excluded:
            continue
        chosen.append(skill_id)
        if limit is not None and len(chosen) >= limit:
            break
    return chosen


def load_excluded(paths, overrides):
    excluded = set()
    if Path(overrides).is_file():
        try:
            excluded.update(load_overrides(overrides).get('entries', {}))
        except ValueError:
            pass
    for path in paths:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        records = data.get('records') if isinstance(data, dict) else data
        if isinstance(records, list):
            excluded.update(item.get('id') for item in records if isinstance(item, dict) and item.get('id'))
        elif isinstance(records, dict):
            excluded.update(records)
    return {item for item in excluded if isinstance(item, str)}


def proposal_from(skill, decision):
    record = {
        'id': skill['id'], 'title': skill['title'], 'summary': skill['summary'], 'tasks': skill['tasks'],
        'repo': skill['representative']['repo'],
        'sources': [{**skill['representative'], 'relationship': 'representative'}],
        'original_classification': skill['original_classification'],
        'original_category': skill['original_classification']['category'],
        'original_topic': skill['original_classification']['topic'],
        'label_from': skill['original_classification']['label_from'],
        'review': 'unreviewed', 'decision': decision['decision'],
        'proposed_category': decision.get('category') if decision['decision'] != 'retain_unfiled' else None,
        'proposed_topic': decision.get('topic') if decision['decision'] != 'retain_unfiled' else None,
        'evidence': decision.get('evidence') or [],
        'reason': decision.get('reason'), 'retention_reason': decision.get('retention_reason'),
        'quality_flags': [], 'source_record_sha256': skill['fingerprint'],
        'review_scope': 'catalog_content_only', 'not_original_source_review': True,
        'proposal_status': 'internal_proposal_not_applied',
    }
    return record


class Runner:
    def __init__(self, client, out, database, overrides, exclude, after, mode, workers=MAX_IN_FLIGHT):
        self.client = client
        self.out = Path(out)
        self.database = database
        self.overrides = overrides
        self.exclude = list(exclude)
        self.after = after
        self.mode = mode
        self.workers = min(MAX_IN_FLIGHT, max(1, workers))
        self.stop = threading.Event()
        self.stop_reason = None
        self.lock = threading.Lock()
        self.inflight = 0
        self.max_inflight = 0
        self.calls = 0

    def state_path(self):
        return self.out / 'state.json'

    def load_state(self):
        path = self.state_path()
        if not path.exists():
            return {'model': MODEL, 'after': self.after, 'completed': {}, 'stopped': None}
        state = json.loads(path.read_text(encoding='utf-8'))
        if state.get('model') != MODEL:
            raise ValueError('run state belongs to a different model')
        return state

    def save_state(self, state):
        path = self.state_path()
        temporary = path.with_suffix('.json.tmp')
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        temporary.replace(path)

    def classify_one(self, skill):
        if not (skill['summary'].strip() or any((task.get('goal') or '').strip() for task in skill['tasks'])):
            return {'status': 'retained', 'decision': {
                'decision': 'retain_unfiled', 'retention_reason': '目录摘要和任务都为空，不足以归类',
            }, 'calls': 0}
        feedback = ''
        last = None
        for attempt_no in range(2):
            if self.stop.is_set():
                return {'status': 'not_sent', 'reason': self.stop_reason}
            attempt = self.out / 'attempts' / skill['id'] / f'{attempt_no + 1:03}'
            attempt.mkdir(parents=True, exist_ok=True)
            prompt = prompt_for(skill, feedback)
            reply = self._request(prompt, f"ATLAS catalogue classification {skill['id']}", attempt)
            with self.lock:
                self.calls += 1
            last = reply
            if reply.get('stop'):
                self.stop.set()
                self.stop_reason = reply['stop']
                return {'status': 'stopped', 'reason': reply['stop'], 'calls': attempt_no + 1}
            if reply.get('retry'):
                return {'status': 'retry', 'reason': reply['retry'], 'calls': attempt_no + 1}
            try:
                assert_free_turn(reply['usage'])
                decision = parse_decision(reply['text'])
                error = evidence_error(decision, skill)
                if error:
                    raise ValueError(error)
                return {'status': 'proposed' if decision['decision'] != 'retain_unfiled' else 'retained',
                        'decision': decision, 'calls': attempt_no + 1, 'duration_seconds': reply['duration_seconds']}
            except PriceStop as exc:
                self.stop.set()
                self.stop_reason = str(exc)
                return {'status': 'stopped', 'reason': str(exc), 'calls': attempt_no + 1}
            except ValueError as exc:
                feedback = str(exc)
                (attempt / 'rejected.txt').write_text(reply.get('text') or '', encoding='utf-8')
        return {'status': 'error', 'error': feedback, 'calls': 2, 'text': None if last is None else last.get('text')}

    def _request(self, prompt, title, attempt):
        delays = []
        for _ in range(3):
            if self.stop.is_set():
                return {'stop': self.stop_reason}
            reply = self.client.request(prompt, title, attempt)
            if reply.get('quota_exhausted'):
                return {'stop': 'provider quota is exhausted; not switching models'}
            delay = reply.get('rate_limit_seconds')
            if delay:
                delays.append(delay)
                (attempt / 'backoff.json').write_text(json.dumps({'retry_after_seconds': delay}) + '\n', encoding='utf-8')
                self.client.sleeper(delay)
                continue
            reply['backoffs'] = delays
            return reply
        return {'retry': 'rate limit persisted', 'backoffs': delays}

    def run(self):
        self.out.mkdir(parents=True, exist_ok=True)
        lock_file = (self.out / 'runner.lock').open('a+')
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            lock_file.close()
            raise SystemExit('分类运行器已有进程持有锁，未启动副本') from exc
        try:
            return self._run_locked()
        finally:
            lock_file.close()

    def _run_locked(self):
        try:
            price = self.client.check_price()
        except (PriceStop, OSError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
            receipt = {'stopped': str(exc), 'calls': 0, 'writes_catalogue': False}
            (self.out / 'report.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            return receipt
        (self.out / 'price.json').write_text(json.dumps(price, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        state = self.load_state()
        vocabulary = allowed_vocabulary(self.database)
        excluded = load_excluded(self.exclude, self.overrides)
        connection = connect_readonly(self.database)
        try:
            pending = []
            for skill_id in select_ids(connection, self.after, excluded, None):
                skill = load_skill(connection, skill_id, vocabulary)
                if skill is None:
                    continue
                previous = state['completed'].get(skill_id)
                if previous and previous.get('fingerprint') == skill['fingerprint']:
                    continue
                if previous and previous.get('fingerprint') != skill['fingerprint']:
                    state['completed'][skill_id] = {**previous, 'status': 'fingerprint_conflict'}
                    continue
                if not (skill['summary'].strip() or any((task.get('goal') or '').strip() for task in skill['tasks'])):
                    self._record(state, skill, self.classify_one(skill))
                    continue
                pending.append(skill)
                if self.mode == 'trial' and len(pending) >= TRIAL_LIMIT:
                    break
        finally:
            connection.close()
        self._work(pending, state)
        proposals = [item['proposal'] for item in state['completed'].values() if item.get('proposal')]
        document = {
            'metadata': {
                'schema': 'atlas-material-classification-proposals/v1',
                'created_at': now(), 'model': MODEL, 'mode': self.mode,
                'not_applied': True, 'production_review_unchanged': 'unreviewed',
            },
            'records': proposals,
        }
        (self.out / 'proposals.json').write_text(json.dumps(document, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        report = {
            'model': MODEL, 'mode': self.mode, 'workers': self.workers, 'max_in_flight': self.max_inflight,
            'calls': self.calls, 'stopped': self.stop_reason, 'completed': len(state['completed']),
            'proposed': sum(item.get('status') == 'proposed' for item in state['completed'].values()),
            'retained': sum(item.get('status') == 'retained' for item in state['completed'].values()),
            'errors': sum(item.get('status') == 'error' for item in state['completed'].values()),
            'conflicts': sum(item.get('status') == 'fingerprint_conflict' for item in state['completed'].values()),
            'writes_catalogue': False,
        }
        (self.out / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        self.save_state(state)
        return report

    def _work(self, pending, state):
        if not pending:
            return

        def one(skill):
            if self.stop.is_set():
                return
            with self.lock:
                self.inflight += 1
                self.max_inflight = max(self.max_inflight, self.inflight)
            try:
                result = self.classify_one(skill)
            finally:
                with self.lock:
                    self.inflight -= 1
            if result.get('status') == 'retry':
                result = {'status': 'error', 'error': result.get('reason') or 'rate limit', 'calls': result.get('calls', 0)}
            if result.get('status') == 'not_sent':
                return
            self._record(state, skill, result)

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            tuple(pool.map(one, pending))

    def _record(self, skill_result_state, skill, result):
        state, = (skill_result_state,)
        entry = {'status': result['status'], 'fingerprint': skill['fingerprint'], 'at': now()}
        if result['status'] in ('proposed', 'retained'):
            entry['proposal'] = proposal_from(skill, result['decision'])
        else:
            entry['error'] = result.get('error') or result.get('reason')
        attempt = self.out / 'attempts' / skill['id']
        attempt.mkdir(parents=True, exist_ok=True)
        (attempt / 'receipt.json').write_text(json.dumps({**entry, 'calls': result.get('calls', 0)}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        with self.lock:
            state['completed'][skill['id']] = entry
            if result['status'] == 'stopped':
                state['stopped'] = result.get('reason')
            self.save_state(state)


def run(argv=None):
    parser = argparse.ArgumentParser(description='用零价 OpenCode Go 模型为待归类条目生成目录分类提案。不写生产目录。')
    parser.add_argument('--database', type=Path, default=DATABASE)
    parser.add_argument('--overrides', type=Path, default=OVERRIDES)
    parser.add_argument('--exclude', action='append', default=[], type=Path)
    parser.add_argument('--after', required=True, help='只处理 id 严格大于这个游标的待归类条目')
    parser.add_argument('--mode', choices=('trial', 'continuous'), required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--opencode', default=shutil.which('opencode') or 'opencode')
    parser.add_argument('--workers', type=int, default=MAX_IN_FLIGHT)
    args = parser.parse_args(argv)
    if args.workers > MAX_IN_FLIGHT:
        args.workers = MAX_IN_FLIGHT
    client = OpenCodeFreeClient(args.opencode)
    runner = Runner(client, args.out, args.database, args.overrides, args.exclude, args.after, args.mode, args.workers)
    report = runner.run()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 2 if report.get('stopped') else 0


if __name__ == '__main__':
    raise SystemExit(run())
