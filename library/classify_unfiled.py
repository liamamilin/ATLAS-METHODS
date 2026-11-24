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
from taxonomy import LABELS, TOPIC_PATTERNS

MODEL = 'opencode-go/iceman-contributor-free'
PROVIDER = 'opencode-go'
MODEL_ID = 'iceman-contributor-free'
MAX_IN_FLIGHT = 10
TRIAL_LIMIT = 10
MAX_BATCH_CANDIDATES = 100
MAX_BATCH_REQUESTS = 600
MAX_BATCH_RUNTIME = 6 * 60 * 60
EXPORT_READS = 3
SCHEMA_VERSION = 'catalogue-classification-response/v2'
AGENT = 'atlas-catalogue'
# OpenCode v2.2 agent permissions: ordered rules, last match wins, unmatched asks.
# https://opencode.ai/docs/permissions/ and https://opencode.ai/docs/agents/
# steps=2 avoids the forced summary injected on the final step. Deny is project-local.
TOOL_DENIES = [
    {'action': action, 'resource': '*', 'effect': 'deny'}
    for action in (
        'read', 'glob', 'grep', 'edit', 'shell', 'webfetch', 'websearch', 'skill',
        'subagent', 'question', 'external_directory', 'execute', '*',
    )
]
EXAMPLE_PROPOSE = {
    'decision': 'propose_existing_category',
    'category': 'operations',
    'topic': '观测、日志与告警',
    'evidence': [{'field': 'summary', 'quote': '应用日志里的异常', 'task_index': None}],
    'reason': '摘要描述日志异常定位，对应既有专题。',
    'retention_reason': None,
}
EXAMPLE_RETAIN = {
    'decision': 'retain_unfiled',
    'category': None,
    'topic': None,
    'evidence': [],
    'reason': None,
    'retention_reason': '摘要和任务没有足够的连续原文来对应既有专题。',
}
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


def tool_event_types(events):
    found = []
    for event in events:
        if isinstance(event, dict) and 'tool' in str(event.get('type', '')).lower():
            found.append(str(event.get('type')))
    return found


def project_config(disabled_mcp):
    """Private project config. It does not write the user's global OpenCode config."""
    return {
        'mcp': {'servers': {
            name: {'type': 'local', 'command': ['false'], 'disabled': True} for name in disabled_mcp
        }},
        'agents': {AGENT: {
            'description': 'Classify one catalogue record from the prompt; no tools',
            'mode': 'primary',
            'system': '只根据用户消息里的目录摘要和任务返回一个 JSON 对象。不要读取文件，不要搜索，不要使用工具。',
            'permissions': TOOL_DENIES,
            'steps': 2,
        }},
    }


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


class CostUnknown(RuntimeError):
    def __init__(self, status):
        super().__init__(status)
        self.status = status


class OpenCodeFreeClient:
    def __init__(self, executable, timeout=180, sleeper=time.sleep):
        self.executable = executable
        self.timeout = timeout
        self.sleeper = sleeper
        self._mcp_lock = threading.Lock()
        self._mcp_servers = None

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
        return [self.executable, 'run', '--standalone', '--model', MODEL, '--format', 'json',
                '--agent', AGENT, '--title', title, prompt]

    def disabled_mcp_servers(self, work, env):
        # Read names only. Do not copy credentials or the user's global config.
        with self._mcp_lock:
            if self._mcp_servers is None:
                result = subprocess.run(
                    [self.executable, 'api', 'config.get', '--standalone'],
                    cwd=work, env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=30,
                )
                if result.returncode:
                    raise PriceStop('Cannot inspect MCP configuration; no classification request was sent')
                sources = json.loads(result.stdout.decode('utf-8'))
                if not isinstance(sources, list):
                    raise PriceStop('Unexpected OpenCode config.get format; no classification request was sent')
                names = set()
                for source in sources:
                    info = source.get('info') if isinstance(source, dict) else None
                    mcp = info.get('mcp') if isinstance(info, dict) else None
                    servers = mcp.get('servers') if isinstance(mcp, dict) else None
                    if isinstance(servers, dict):
                        names.update(servers)
                self._mcp_servers = sorted(names)
            return self._mcp_servers

    def request(self, prompt, title, attempt):
        started = time.monotonic()
        command = self.command(title, prompt)
        with tempfile.TemporaryDirectory(prefix='atlas-catalogue-') as temp:
            # macOS getcwd follows /var -> /private/var. PWD must be that same path,
            # or OpenCode reads a different project and the temp config is missed.
            work = Path(temp).resolve()
            env = os.environ.copy()
            env['PWD'] = str(work)
            disabled = self.disabled_mcp_servers(work, env)
            (work / 'opencode.json').write_text(
                json.dumps(project_config(disabled), ensure_ascii=False, indent=2) + '\n', encoding='utf-8',
            )
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
            text = response_text(events)
            (attempt / 'response.txt').write_text(text, encoding='utf-8')
            sessions = session_ids(events)
            session_id = sessions[0] if len(sessions) == 1 else None
            if len(sessions) > 1:
                usage = {'available': False, 'cost_status': 'session_ambiguous', 'turns': [], 'exports': 0}
            elif session_id:
                usage = self.read_exports(work, env, session_id, attempt)
            else:
                usage = {'available': False, 'cost_status': 'no_session', 'turns': [], 'exports': 0}
            usage['transport'] = transport_signal(events)
            usage['invalid_output'] = invalid_output_signal(events)
            duration = round(time.monotonic() - started, 3)
            return {
                'text': text, 'events': events, 'usage': usage,
                'exit_code': proc.returncode, 'timed_out': timed_out, 'duration_seconds': duration,
                'session_id': session_id,
                'rate_limit_seconds': rate_limit(events), 'quota_exhausted': quota_exhausted(events),
                'tool_events': tool_event_types(events),
                'command': command[:-1] + ['<prompt>'],
            }

    def read_exports(self, work, env, session_id, attempt):
        usage = {'available': False, 'cost_status': 'export_failed', 'turns': [], 'exports': 0}
        for index in range(1, EXPORT_READS + 1):
            usage = self.read_export(work, env, session_id, attempt, index)
            usage['exports'] = index
            # A parsed export that already lacks a turn cost will not grow that field
            # by being read again. Retry only when the export itself is missing.
            if usage.get('cost_status') in ('zero', 'nonzero', 'model_mismatch', 'unknown', 'no_model_turn'):
                return usage
            if index < EXPORT_READS:
                self.sleeper(1)
        return usage

    def read_export(self, work, env, session_id, attempt, index):
        # Read-only. This never calls `opencode run`.
        exported = subprocess.run(
            [self.executable, 'session', 'export', '--standalone', session_id],
            cwd=work, env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=60,
        )
        (attempt / f'session-export-{index:03}.json').write_bytes(exported.stdout)
        if exported.returncode != 0:
            return {'available': False, 'cost_status': 'export_failed', 'turns': [], 'reason': 'export_failed'}
        return export_usage(exported.stdout)

    def export_existing(self, session_id, attempt):
        with tempfile.TemporaryDirectory(prefix='atlas-catalogue-export-') as temp:
            work = Path(temp).resolve()
            env = os.environ.copy()
            env['PWD'] = str(work)
            return self.read_export(work, env, session_id, attempt, 1)


def message_parts(message):
    if not isinstance(message, dict):
        return None, None
    info = message.get('info', message)
    if not isinstance(info, dict):
        return message, None
    return message, info


def is_model_turn(message, info):
    """User and agent-switch rows can carry a default cost of 0. That is not a model turn."""
    kind = str(message.get('type') or info.get('role') or info.get('type') or '').lower().replace('_', '-')
    if kind in {'user', 'agent-switched', 'synthetic', 'idle'}:
        return False
    if kind == 'assistant':
        return True
    error = message.get('error') if isinstance(message.get('error'), dict) else info.get('error')
    if isinstance(error, dict) and (error.get('type') or error.get('message')):
        return True
    model = info.get('model') if isinstance(info.get('model'), dict) else {}
    if (model.get('providerID') or model.get('id')) and (
            isinstance(info.get('tokens'), dict) or info.get('finish') or 'cost' in info):
        return True
    return False


def turn_from(message, info):
    model = info.get('model') if isinstance(info.get('model'), dict) else {}
    error = message.get('error') if isinstance(message.get('error'), dict) else info.get('error')
    if not isinstance(error, dict):
        error = {}
    known = 'cost' in info and isinstance(info.get('cost'), (int, float)) and not isinstance(info.get('cost'), bool)
    retry = info.get('retry') if isinstance(info.get('retry'), dict) else message.get('retry')
    if not isinstance(retry, dict):
        retry = {}
    return {
        'message_id': message.get('id') or info.get('id'),
        'providerID': model.get('providerID'), 'id': model.get('id'),
        'cost': info.get('cost') if known else None, 'cost_known': known,
        'finish': info.get('finish'), 'error_type': error.get('type'),
        'message_type': message.get('type') or info.get('role') or info.get('type'),
        'cli_retry_attempt': retry.get('attempt') if isinstance(retry.get('attempt'), int) else None,
    }


def normalize_turn(turn):
    if not isinstance(turn, dict):
        return {'providerID': None, 'id': None, 'cost': None, 'cost_known': False, 'finish': None, 'error_type': None}
    if 'cost_known' in turn:
        return turn
    cost = turn.get('cost')
    known = isinstance(cost, (int, float)) and not isinstance(cost, bool)
    return {**turn, 'cost_known': known, 'cost': cost if known else None}


def judge_turns(turns):
    turns = [normalize_turn(turn) for turn in turns]
    if not turns:
        return 'no_model_turn'
    if any(turn['cost_known'] and turn['cost'] not in (0, 0.0) for turn in turns):
        return 'nonzero'
    if any(turn['cost_known'] and (turn.get('providerID') != PROVIDER or turn.get('id') != MODEL_ID) for turn in turns):
        return 'model_mismatch'
    if any(not turn['cost_known'] for turn in turns):
        return 'unknown'
    if any(turn.get('providerID') != PROVIDER or turn.get('id') != MODEL_ID for turn in turns):
        return 'model_mismatch'
    return 'zero'


def export_usage(raw):
    try:
        session = json.loads(raw.decode('utf-8'))
    except (UnicodeError, json.JSONDecodeError):
        return {'available': False, 'cost_status': 'corrupt_export', 'turns': [], 'reason': 'corrupt_export'}
    messages = session.get('messages') if isinstance(session, dict) else None
    if not isinstance(messages, list) or not messages:
        return {'available': False, 'cost_status': 'no_messages', 'turns': [], 'reason': 'session export has no messages'}
    turns = []
    for message in messages:
        message, info = message_parts(message)
        if info is None or not is_model_turn(message, info):
            continue
        turns.append(turn_from(message, info))
    status = judge_turns(turns)
    return {'available': status == 'zero', 'cost_status': status, 'turns': turns,
            'reason': None if status == 'zero' else status}


def cost_status(usage):
    if not isinstance(usage, dict):
        return 'unknown'
    if usage.get('cost_status'):
        return usage['cost_status']
    return judge_turns(usage.get('turns') or [])


def cost_gate(usage):
    status = cost_status(usage)
    turns = [normalize_turn(turn) for turn in (usage.get('turns') or [])]
    if status == 'nonzero':
        shown = next(turn.get('cost') for turn in turns if turn.get('cost_known') and turn.get('cost') not in (0, 0.0))
        raise PriceStop(f'response cost was {shown}, not 0; stopping without a fallback')
    if status == 'model_mismatch':
        bad = next((turn for turn in turns if turn.get('cost_known') and (
            turn.get('providerID') != PROVIDER or turn.get('id') != MODEL_ID)), turns[0] if turns else {})
        raise PriceStop(
            f"response provider/model was {bad.get('providerID')}/{bad.get('id')}, not {MODEL}; stopping"
        )
    if status != 'zero':
        raise CostUnknown(status)


def transport_signal(events):
    for event in events:
        if not isinstance(event, dict) or event.get('type') != 'error' or not isinstance(event.get('error'), dict):
            continue
        error = event['error']
        message = str(error.get('message', ''))
        if error.get('type') == 'provider.transport' or message.startswith('Transport: ECONNRESET:') or 'ECONNRESET' in message:
            return True
    return False


def invalid_output_signal(events):
    for event in events:
        if not isinstance(event, dict) or event.get('type') != 'error' or not isinstance(event.get('error'), dict):
            continue
        error = event['error']
        message = str(error.get('message', ''))
        if error.get('type') == 'provider.invalid-output' or 'stream ended without finish_reason' in message:
            return True
    return False


def diagnosis_of(reply):
    usage = reply.get('usage') if isinstance(reply.get('usage'), dict) else {}
    turns = usage.get('turns') or []
    status = cost_status(usage)
    events = reply.get('events') or []
    return {
        'cost_status': status, 'fee_proven': status == 'zero', 'turns': turns,
        'exports': usage.get('exports', 0), 'transport': bool(usage.get('transport')),
        'invalid_output': bool(usage.get('invalid_output')), 'timed_out': bool(reply.get('timed_out')),
        'exit_code': reply.get('exit_code'), 'session_id': reply.get('session_id'),
        'cli_steps': sum(1 for event in events if isinstance(event, dict) and event.get('type') in ('step_start', 'step-start')),
        'cli_retry_attempts': [turn.get('cli_retry_attempt') for turn in turns if turn.get('cli_retry_attempt')],
        'session_info_cost_ignored': True,
    }


def combine_ledgers(previous, new_turns):
    """A later export must not delete an earlier turn whose cost was absent."""
    previous = [normalize_turn(turn) for turn in previous or []]
    new_turns = [normalize_turn(turn) for turn in new_turns or []]
    if previous and len(new_turns) < len(previous):
        return previous, 'unknown'
    previous_ids = {turn.get('message_id') for turn in previous if turn.get('message_id')}
    new_ids = {turn.get('message_id') for turn in new_turns if turn.get('message_id')}
    if previous_ids and not previous_ids <= new_ids:
        return previous, 'unknown'
    return new_turns, judge_turns(new_turns)


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
    """Validate against the same object the prompt shows. Name the broken field."""
    if not isinstance(decision, dict):
        return '输出必须是 JSON 对象。decision 只能是 propose_existing_category 或 retain_unfiled。'
    name = decision.get('decision')
    if name not in ('propose_existing_category', 'retain_unfiled'):
        return f'decision 必须是 propose_existing_category 或 retain_unfiled，收到 {name!r}。'
    evidence = decision.get('evidence')
    if not isinstance(evidence, list):
        kind = 'object' if isinstance(evidence, dict) else type(evidence).__name__
        return (
            f'evidence 必须是数组，收到的是 {kind}。'
            '每个元素都要有 field、quote、task_index。'
            'field 只能是 summary 或 task。summary 的 task_index 为 null；'
            'task 的 task_index 是 tasks 数组下标。'
            '不能把 evidence 写成单个对象。'
        )
    if name == 'propose_existing_category' and not evidence:
        return 'propose_existing_category 的 evidence 必须是至少包含一项的数组。'
    for item in evidence:
        if not isinstance(item, dict):
            return 'evidence 数组里的每一项都必须是对象，包含 field、quote、task_index。'
        quote = item.get('quote')
        field = item.get('field')
        if field not in ('summary', 'task'):
            return 'evidence.field 只能是 summary 或 task；title 不能作为 field。'
        if not isinstance(quote, str) or len(quote.strip()) < 8:
            return 'evidence.quote 必须是所给字段里的连续原文，且至少 8 个字符。'
        if field == 'summary':
            if item.get('task_index') not in (None,):
                return 'field 为 summary 时 task_index 必须是 null。'
            source = skill['summary']
        else:
            if not isinstance(item.get('task_index'), int):
                return 'field 为 task 时 task_index 必须是整数下标。'
            try:
                task = skill['tasks'][item['task_index']]
            except (IndexError, KeyError, TypeError):
                return 'evidence.task_index 超出了本次提供的 tasks。'
            source = f"{task.get('goal') or ''}\n{task.get('applicability') or ''}"
        if quote not in source:
            return 'evidence.quote 不是所给 summary 或 task 字段里的连续原文。'
    if name == 'retain_unfiled':
        if decision.get('category') is not None or decision.get('topic') is not None:
            return 'retain_unfiled 的 category 和 topic 必须是 null。'
        if not isinstance(decision.get('retention_reason'), str) or not decision['retention_reason'].strip():
            return 'retain_unfiled 必须提供非空 retention_reason。'
        return None
    category, topic = decision.get('category'), decision.get('topic')
    if not isinstance(category, str) or category not in skill['vocabulary'] or '/' in category:
        return f'category 必须是 allowed 里的领域 id，不能写成“领域/专题”，收到 {category!r}。'
    if topic not in skill['vocabulary'][category]:
        return f'topic 必须与 {category} 的既有专题名逐字相同，收到 {topic!r}。'
    if not isinstance(decision.get('reason'), str) or not decision['reason'].strip():
        return 'propose_existing_category 必须提供非空 reason。'
    return None


def prompt_for(skill, feedback=''):
    listed = {key: skill['vocabulary'][key] for key in skill['vocabulary']}
    payload = {
        'id': skill['id'], 'title_context_not_sufficient': skill['title'],
        'summary': skill['summary'], 'tasks': skill['tasks'],
        'current': skill['original_classification'],
        'allowed': listed,
        'response_schema': SCHEMA_VERSION,
    }
    rules = (
        f'响应契约 {SCHEMA_VERSION}。只返回一个 JSON 对象，不要 markdown，不要调用工具。'
        'decision 只能是 propose_existing_category 或 retain_unfiled。'
        'propose_existing_category：category 是 allowed 的领域 id，topic 与 allowed 中的专题名逐字相同（空格也要相同），'
        'evidence 是至少一项的数组，reason 为非空字符串，retention_reason 为 null。'
        'retain_unfiled：category 和 topic 为 null，evidence 是数组且可以为空，reason 为 null，retention_reason 为非空字符串。'
        'evidence 的每个元素必须含 field、quote、task_index。field 只能是 summary 或 task。'
        'summary 的 task_index 为 null；task 的 task_index 是 tasks 的整数下标。'
        'quote 必须是该字段中的连续原文。title 不能作为 field，也不能只依据 title 归类。'
        '正例只说明结构，不要照抄其中的领域，除非当前摘要或任务确实包含对应原文且专题逐字匹配。\n'
        '正例：' + json.dumps(EXAMPLE_PROPOSE, ensure_ascii=False) + '\n'
        '保留例：' + json.dumps(EXAMPLE_RETAIN, ensure_ascii=False)
    )
    if feedback:
        rules += '\n上一次输出未通过同一契约。结构性错误如下，请按正例整份重写，不要只改其中一个字段：\n' + feedback
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
            for item in records:
                if not isinstance(item, dict) or not item.get('id'):
                    continue
                if item.get('decision') == 'retain_unfiled':
                    continue
                excluded.add(item['id'])
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
        'quality_flags': ['provisional_not_final'] if decision['decision'] == 'retain_unfiled' else [],
        'source_record_sha256': skill['fingerprint'],
        'response_schema': SCHEMA_VERSION,
        'review_scope': 'catalog_content_only', 'not_original_source_review': True,
        'proposal_status': 'internal_proposal_not_applied',
    }
    return record


class Runner:
    def __init__(self, client, out, database, overrides, exclude, after, mode, workers=MAX_IN_FLIGHT,
                 max_candidates=None, max_requests=None, max_runtime_seconds=None, clock=None):
        self.client = client
        self.out = Path(out)
        self.database = database
        self.overrides = overrides
        self.exclude = list(exclude)
        self.after = after
        self.mode = mode
        self.workers = min(MAX_IN_FLIGHT, max(1, workers))
        self.clock = clock or time.monotonic
        if mode == 'trial':
            self.max_candidates = TRIAL_LIMIT
            self.max_requests = 40 if max_requests is None else max_requests
            self.max_runtime = 900 if max_runtime_seconds is None else max_runtime_seconds
        else:
            self.max_candidates = max_candidates
            self.max_requests = max_requests
            self.max_runtime = max_runtime_seconds
        self._check_budgets()
        self.stop = threading.Event()
        self.stop_reason = None
        self.lock = threading.Lock()
        self.inflight = 0
        self.max_inflight = 0
        self.calls = 0
        self.started = None
        self.state = None
        self.candidate_budget_reached = False

    def _check_budgets(self):
        if self.mode == 'continuous' and None in (self.max_candidates, self.max_requests, self.max_runtime):
            raise SystemExit('continuous 必须同时给出 --max-candidates、--max-requests 和 --max-runtime-seconds')
        if not isinstance(self.max_candidates, int) or not 1 <= self.max_candidates <= MAX_BATCH_CANDIDATES:
            raise SystemExit(f'每批候选必须是 1 到 {MAX_BATCH_CANDIDATES} 的整数')
        if not isinstance(self.max_requests, int) or not 1 <= self.max_requests <= MAX_BATCH_REQUESTS:
            raise SystemExit(f'每批请求必须是 1 到 {MAX_BATCH_REQUESTS} 的整数')
        if not isinstance(self.max_runtime, (int, float)) or not 0 < self.max_runtime <= MAX_BATCH_RUNTIME:
            raise SystemExit(f'每批运行秒数必须大于 0 且不超过 {MAX_BATCH_RUNTIME}')

    def state_path(self):
        return self.out / 'state.json'

    def load_state(self):
        path = self.state_path()
        if not path.exists():
            return {'schema_version': SCHEMA_VERSION, 'model': MODEL, 'after': self.after,
                    'completed': {}, 'stopped': None, 'calls': 0}
        state = json.loads(path.read_text(encoding='utf-8'))
        if state.get('schema_version') != SCHEMA_VERSION:
            raise SystemExit(
                '这个目录不是响应契约 '
                f'{SCHEMA_VERSION}。不会把其中的旧失败改成成功，也不会改写原始 attempts。请换一个新的 --out。'
            )
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
            last = reply
            if reply.get('stop'):
                self.stop.set()
                self.stop_reason = reply['stop']
                return {'status': 'stopped', 'reason': reply['stop'], 'calls': attempt_no + 1}
            if reply.get('retry'):
                return {'status': 'retry', 'reason': reply['retry'], 'calls': attempt_no + 1}
            try:
                cost_gate(reply['usage'])
                if reply.get('timed_out'):
                    return self._closed(reply, attempt_no, 'runner_timeout')
                if reply.get('exit_code') not in (0, None):
                    return self._closed(reply, attempt_no, 'cli_exit')
                tools = reply.get('tool_events') or []
                if tools:
                    message = 'tool events are not accepted: ' + ', '.join(tools)
                    (attempt / 'rejected.txt').write_text(reply.get('text') or '', encoding='utf-8')
                    return {**self._closed(reply, attempt_no, message), 'tool_events': tools}
                decision = parse_decision(reply['text'])
                error = evidence_error(decision, skill)
                if error:
                    raise ValueError(error)
                return {'status': 'proposed' if decision['decision'] != 'retain_unfiled' else 'retained',
                        'decision': decision, 'calls': attempt_no + 1, 'duration_seconds': reply['duration_seconds'],
                        'session_id': reply.get('session_id'), 'diagnosis': diagnosis_of(reply)}
            except PriceStop as exc:
                self.stop.set()
                self.stop_reason = str(exc)
                return {**self._closed(reply, attempt_no, str(exc)), 'status': 'stopped', 'reason': str(exc)}
            except CostUnknown:
                return self._closed(reply, attempt_no, 'cost_unknown')
            except ValueError as exc:
                feedback = str(exc)
                (attempt / 'rejected.txt').write_text(reply.get('text') or '', encoding='utf-8')
        return {'status': 'error', 'error': feedback, 'calls': 2, 'text': None if last is None else last.get('text')}

    def _closed(self, reply, attempt_no, reason):
        return {
            'status': 'error', 'error': reason, 'calls': attempt_no + 1,
            'session_id': reply.get('session_id'), 'diagnosis': diagnosis_of(reply),
        }

    def reserve_request(self):
        with self.lock:
            if self.started is not None and self.clock() - self.started >= self.max_runtime:
                self.stop.set()
                self.stop_reason = 'runtime budget reached'
                return False
            calls = self.state.get('calls', 0) if self.state else self.calls
            if calls >= self.max_requests:
                self.stop.set()
                self.stop_reason = 'request budget reached'
                return False
            calls += 1
            self.calls = calls
            if self.state is not None:
                self.state['calls'] = calls
                self.save_state(self.state)
            return True

    def _request(self, prompt, title, attempt):
        delays = []
        for _ in range(3):
            if self.stop.is_set():
                return {'stop': self.stop_reason}
            if not self.reserve_request():
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
        if self.state_path().exists():
            self.load_state()
        self.started = self.clock()
        try:
            price = self.client.check_price()
        except (PriceStop, OSError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
            receipt = {'stopped': str(exc), 'calls': 0, 'writes_catalogue': False}
            (self.out / 'report.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            return receipt
        (self.out / 'price.json').write_text(json.dumps(price, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        state = self.load_state()
        self.state = state
        self.calls = state.get('calls', 0)
        if self.calls >= self.max_requests:
            self.stop.set()
            self.stop_reason = 'request budget reached'
        elif self.clock() - self.started >= self.max_runtime:
            self.stop.set()
            self.stop_reason = 'runtime budget reached'
        vocabulary = allowed_vocabulary(self.database)
        excluded = load_excluded(self.exclude, self.overrides)
        counted = sum(item.get('status') in ('proposed', 'retained', 'error', 'stopped')
                      for item in state['completed'].values())
        room = self.max_candidates - counted
        connection = connect_readonly(self.database)
        try:
            pending = []
            for skill_id in select_ids(connection, self.after, excluded, None):
                if self.stop.is_set():
                    break
                if room <= 0:
                    self.candidate_budget_reached = True
                    self.stop_reason = self.stop_reason or 'candidate budget reached'
                    break
                skill = load_skill(connection, skill_id, vocabulary)
                if skill is None:
                    continue
                previous = state['completed'].get(skill_id)
                if previous and previous.get('fingerprint') == skill['fingerprint']:
                    continue
                if previous and previous.get('fingerprint') != skill['fingerprint']:
                    state['completed'][skill_id] = {**previous, 'status': 'fingerprint_conflict'}
                    continue
                room -= 1
                if not (skill['summary'].strip() or any((task.get('goal') or '').strip() for task in skill['tasks'])):
                    self._record(state, skill, self.classify_one(skill))
                    continue
                pending.append(skill)
        finally:
            connection.close()
        self._work(pending, state)
        proposals = [item['proposal'] for item in state['completed'].values() if item.get('proposal')]
        document = {
            'metadata': {
                'schema': 'atlas-material-classification-proposals/v1',
                'response_schema': SCHEMA_VERSION,
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
            'cost_unknown': sum(item.get('error') == 'cost_unknown' for item in state['completed'].values()),
            'conflicts': sum(item.get('status') == 'fingerprint_conflict' for item in state['completed'].values()),
            'writes_catalogue': False, 'response_schema': SCHEMA_VERSION,
            'max_candidates': self.max_candidates, 'max_requests': self.max_requests,
            'max_runtime_seconds': self.max_runtime,
            'candidate_budget_reached': self.candidate_budget_reached,
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
        entry = {'status': result['status'], 'fingerprint': skill['fingerprint'], 'at': now(),
                 'session_id': result.get('session_id'), 'diagnosis': result.get('diagnosis')}
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


def review_retains(path, out):
    """Report whether provisional retains mention existing topic terms. Not a new label."""
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    records = data.get('records') if isinstance(data, dict) else data
    if not isinstance(records, list):
        raise SystemExit('retain review 需要提案 records 数组')
    reviewed = []
    for record in records:
        if not isinstance(record, dict) or record.get('decision') != 'retain_unfiled':
            continue
        pieces = [record.get('summary') or '']
        for task in record.get('tasks') or []:
            if isinstance(task, dict):
                pieces.append(task.get('goal') or '')
                pieces.append(task.get('applicability') or '')
        text = '\n'.join(pieces)
        hits = []
        for category, patterns in TOPIC_PATTERNS.items():
            for topic, pattern in patterns.items():
                if pattern.search(text):
                    hits.append({'category': category, 'topic': topic})
        reviewed.append({
            'id': record.get('id'), 'permanent_exclude': False, 'final_review': False,
            'title_used': False, 'taxonomy_hits_in_summary_or_tasks': hits,
            'note': '暂保留不是终审，也不加入永久排除。命中只说明摘要或任务里出现了既有专题用词，不是新的归类决定。',
        })
    destination = Path(out)
    destination.mkdir(parents=True, exist_ok=True)
    report = {'response_schema': SCHEMA_VERSION, 'records': reviewed, 'writes_catalogue': False}
    (destination / 'retain-review.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return report


def recheck_exports(client, out, database):
    """Read saved sessions again. This does not call `opencode run` and does not invent a cost."""
    out = Path(out)
    lock_file = (out / 'runner.lock').open('a+')
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        lock_file.close()
        raise SystemExit('分类运行器已有进程持有锁，未启动副本') from exc
    try:
        state = json.loads((out / 'state.json').read_text(encoding='utf-8'))
        if state.get('schema_version') != SCHEMA_VERSION or state.get('model') != MODEL:
            raise SystemExit('补查只接受同一契约和同一免费模型的目录')
        vocabulary = allowed_vocabulary(database)
        connection = connect_readonly(database)
        promoted, still_unknown = [], []
        try:
            for skill_id, entry in state.get('completed', {}).items():
                if entry.get('status') in ('proposed', 'retained') or entry.get('proposal'):
                    continue
                if entry.get('error') != 'cost_unknown' or not entry.get('session_id'):
                    continue
                if (entry.get('diagnosis') or {}).get('timed_out'):
                    still_unknown.append(skill_id)
                    continue
                attempt = out / 'attempts' / skill_id / 'recheck'
                attempt.mkdir(parents=True, exist_ok=True)
                usage = client.export_existing(entry['session_id'], attempt)
                previous = (entry.get('diagnosis') or {}).get('turns') or []
                turns, status = combine_ledgers(previous, usage.get('turns') or [])
                note = {
                    'read': 'session export only', 'runs': 0, 'cost_status': status,
                    'turns': turns, 'fee_proven': status == 'zero',
                    'original_diagnosis_kept': True,
                }
                (attempt / 'recheck-diagnosis.json').write_text(
                    json.dumps(note, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
                if status in ('nonzero', 'model_mismatch'):
                    try:
                        cost_gate({'cost_status': status, 'turns': turns})
                    except PriceStop as exc:
                        state['stopped'] = str(exc)
                        break
                if status != 'zero':
                    still_unknown.append(skill_id)
                    continue
                response_path = out / 'attempts' / skill_id / '001' / 'response.txt'
                skill = load_skill(connection, skill_id, vocabulary)
                if skill is None or skill['fingerprint'] != entry.get('fingerprint') or not response_path.is_file():
                    still_unknown.append(skill_id)
                    continue
                try:
                    decision = parse_decision(response_path.read_text(encoding='utf-8'))
                    error = evidence_error(decision, skill)
                    if error:
                        raise ValueError(error)
                except ValueError:
                    still_unknown.append(skill_id)
                    continue
                entry['status'] = 'proposed' if decision['decision'] != 'retain_unfiled' else 'retained'
                entry['proposal'] = proposal_from(skill, decision)
                entry['error'] = None
                promoted.append(skill_id)
        finally:
            connection.close()
        records, seen = [], set()
        for item in state['completed'].values():
            proposal = item.get('proposal')
            if proposal and proposal['id'] not in seen:
                seen.add(proposal['id'])
                records.append(proposal)
        document = {'metadata': {
            'schema': 'atlas-material-classification-proposals/v1', 'response_schema': SCHEMA_VERSION,
            'created_at': now(), 'model': MODEL, 'not_applied': True,
            'production_review_unchanged': 'unreviewed',
        }, 'records': records}
        (out / 'proposals.json').write_text(json.dumps(document, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        temporary = (out / 'state.json').with_suffix('.json.tmp')
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        temporary.replace(out / 'state.json')
        return {'promoted': promoted, 'still_unknown': still_unknown, 'stopped': state.get('stopped'),
                'runs': 0, 'writes_catalogue': False}
    finally:
        lock_file.close()


def run(argv=None):
    parser = argparse.ArgumentParser(description='用零价 OpenCode Go 模型为待归类条目生成目录分类提案。不写生产目录。')
    parser.add_argument('--database', type=Path, default=DATABASE)
    parser.add_argument('--overrides', type=Path, default=OVERRIDES)
    parser.add_argument('--exclude', action='append', default=[], type=Path)
    parser.add_argument('--after', help='只处理 id 严格大于这个游标的待归类条目')
    parser.add_argument('--mode', choices=('trial', 'continuous'))
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--opencode', default=shutil.which('opencode') or 'opencode')
    parser.add_argument('--workers', type=int, default=MAX_IN_FLIGHT)
    parser.add_argument('--max-candidates', type=int)
    parser.add_argument('--max-requests', type=int)
    parser.add_argument('--max-runtime-seconds', type=float)
    parser.add_argument('--review-retains', type=Path)
    parser.add_argument('--recheck-exports', action='store_true')
    args = parser.parse_args(argv)
    if args.recheck_exports:
        report = recheck_exports(OpenCodeFreeClient(args.opencode), args.out, args.database)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2 if report.get('stopped') else 0
    if args.review_retains:
        report = review_retains(args.review_retains, args.out)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if not args.mode or args.after is None:
        raise SystemExit('分类运行需要 --mode 和 --after')
    if args.workers > MAX_IN_FLIGHT:
        args.workers = MAX_IN_FLIGHT
    client = OpenCodeFreeClient(args.opencode)
    runner = Runner(
        client, args.out, args.database, args.overrides, args.exclude, args.after, args.mode, args.workers,
        args.max_candidates, args.max_requests, args.max_runtime_seconds,
    )
    report = runner.run()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 2 if report.get('stopped') else 0


if __name__ == '__main__':
    raise SystemExit(run())
