#!/usr/bin/env python3
"""Versioned skill enrichment sidecar. It does not alter classification v2 records."""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
from pathlib import Path
import resource
import sqlite3
import sys
import time

from classification_overrides import static_topics
from task_search import ACTION_TAGS, TOOL_TAGS
from taxonomy import CATEGORIES, LABELS, UNSORTED_TOPIC

SCHEMA = 'atlas-skill-enrichment/v0.1'
DIMENSIONS = (
    'task_action', 'tools', 'input_kind', 'output_kind', 'scenario',
    'requirements', 'side_effects', 'kind',
)
STATES = ('known', 'partial', 'unknown', 'not_applicable', 'conflict')
TOOL_ROLES = (
    'required', 'conditional', 'optional', 'alternative', 'example',
    'target_platform', 'mentioned', 'prohibited', 'unknown',
)
SUPPORT = ('direct_explicit', 'deterministic_alias', 'legacy_unverified', 'bounded_inference')
AXES = (
    'source_fidelity', 'structural_completeness', 'actionability',
    'limitations_and_risks', 'classification_and_tag_fit',
)
STRONG_ROLES = {'required', 'conditional'}
ALIASES = {
    'postgres': 'postgresql', 'postgresql': 'postgresql', 'google sheets': 'gsheets',
    'google sheet': 'gsheets', '谷歌表格': 'gsheets',
}
INPUT_KINDS = (
    'text', 'document', 'url', 'repository', 'structured_data', 'image', 'audio',
    'video', 'database', 'api_payload', 'user_constraints',
)
OUTPUT_KINDS = (
    'report', 'document', 'code', 'dataset', 'visualization', 'image', 'audio',
    'video', 'configuration', 'decision_or_plan', 'remote_state_change',
)
KIND_VALUES = (
    'reference_guide', 'execution_workflow', 'connector_integration', 'document_template',
    'router_or_orchestrator', 'scaffold', 'placeholder', 'deprecated_redirect',
)
RULE_VERSION = 'canonical-value/v1'


def global_writer_lock():
    """One lock shared with the catalogue classifier. Held for a whole run, then released."""
    path = Path(__file__).resolve().parents[1] / 'data' / 'library' / 'atlas-writer.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open('a+')
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise SystemExit('atlas writer lock is already held') from exc
    return handle


def now():
    return datetime.now(timezone.utc).isoformat()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def taxonomy_reconciliation():
    """Export the IDs this repository actually defines. Do not pad them to 155 or 163."""
    domains = []
    pairs = []
    for key, label, group, _vocabulary in CATEGORIES:
        if key == 'unfiled':
            continue
        topics = sorted(static_topics(key))
        domains.append({'domain_id': key, 'label': label, 'group': group, 'topics': topics})
        for topic in topics:
            pairs.append({
                'domain_id': key, 'topic': topic, 'topic_id': None,
                'topic_resolved': topic != UNSORTED_TOPIC,
            })
    unsorted = [item for item in pairs if item['topic'] == UNSORTED_TOPIC]
    unique_topic_strings = sorted({item['topic'] for item in pairs})
    return {
        'schema': SCHEMA,
        'generated_at': now(),
        'domain_count': len(domains),
        'allowed_pairs': len(pairs),
        'unsorted_pairs': len(unsorted),
        'pairs_excluding_unsorted': len(pairs) - len(unsorted),
        'unique_topic_strings': len(unique_topic_strings),
        'historical_155': {
            'matches_runtime_pairs': len(pairs) == 155,
            'note': '155 is an older published count. It is not the runtime allowed-pair count and is not a target to add or delete topics toward.',
        },
        'design_cache_163': {
            'matches_this_code_vocabulary': len(pairs) == 163,
            'note': 'A design cache counted 163 runtime pairs, including catalogue-observed topics. This export lists the code vocabulary only and does not add topics to reach 163.',
        },
        'topic_id_null_means': 'a separate topic snapshot id was not obtained; the topic string itself is present',
        'domains': domains,
        'pairs': pairs,
        'task_action_ids': list(ACTION_TAGS),
        'tool_ids': list(TOOL_TAGS),
        'second_subject_tree': False,
    }


def empty_tags(tags):
    if not isinstance(tags, dict):
        return True
    return not tags.get('action') and not tags.get('tool')


def tool_role_from_text(text, tool_id):
    folded = text.casefold()
    label = TOOL_TAGS[tool_id][0].casefold()
    aliases = TOOL_TAGS[tool_id][1].casefold().split('|')
    if not any(alias in folded or label in folded for alias in aliases):
        return None
    if any(phrase in folded for phrase in ('不要使用', '不要用', '禁止使用', 'do not use', "don't use")):
        return 'prohibited'
    if any(phrase in folded for phrase in ('例如', '比如', 'for example', 'e.g.')):
        return 'example'
    if any(phrase in folded for phrase in ('也可以', '或者直接', 'alternatively')):
        return 'alternative'
    if any(phrase in folded for phrase in ('必须', '需要使用', 'required')):
        return 'required'
    return 'mentioned'


def resolve_tool(name):
    key = name.casefold().strip()
    if key in ALIASES:
        return ALIASES[key], None
    if key in TOOL_TAGS:
        return key, None
    hits = [tool_id for tool_id, (_label, aliases) in TOOL_TAGS.items()
            if key in aliases.casefold().split('|') or key == _label.casefold()]
    if len(hits) > 1:
        return None, 'conflict'
    if len(hits) == 1:
        return hits[0], None
    return None, 'candidate'


def fact(record_id, scope, dimension, value_id, role, support, quote, locator, source_hash, method):
    body = {
        'record_id': record_id, 'scope': scope, 'dimension': dimension, 'value_id': value_id,
        'role': role, 'support': support, 'quote': quote, 'locator': locator,
        'source_hash': source_hash, 'method': method, 'schema': SCHEMA,
    }
    body['fact_id'] = digest(body)[:32]
    body['accepted'] = bool(quote) and support in ('direct_explicit', 'deterministic_alias')
    body['candidate'] = not body['accepted']
    return body


def map_record(skill, tasks, gaps=None, source_hash='', runtime_topics=None):
    """Copy the existing category and only mark legacy task tags as unverified."""
    record_id = skill['id']
    dimensions = {name: {'state': 'unknown', 'reason': 'not_yet_annotated', 'values': []} for name in DIMENSIONS}
    facts = []
    domain_id = skill.get('category') if skill.get('category') in LABELS else 'unfiled'
    topic = skill.get('topic')
    if runtime_topics is None:
        topic_origin = 'not_checked'
    elif (domain_id, topic) in runtime_topics:
        topic_origin = 'runtime_observed'
    else:
        topic_origin = 'code_canonical_or_other'
    primary = {
        'domain_id': domain_id,
        'topic': topic,
        'topic_origin': topic_origin,
        'label_from': skill.get('label_from'),
        'topic_resolved': bool(topic) and topic != UNSORTED_TOPIC and skill.get('category') != 'unfiled',
        'domain_resolved': skill.get('category') not in (None, '', 'unfiled'),
    }
    if not tasks:
        dimensions['task_action']['reason'] = 'not_in_source'
        dimensions['task_action']['structural_clue'] = 'no_tasks'
    for task in tasks:
        scope = {'level': 'task', 'task_position': task.get('position'), 'task_id': task.get('id')}
        tags = task.get('tags') if isinstance(task.get('tags'), dict) else {}
        if isinstance(task.get('tags'), str):
            try:
                tags = json.loads(task['tags'])
            except json.JSONDecodeError:
                tags = {}
        if empty_tags(tags):
            continue
        for action in tags.get('action') or []:
            if action not in ACTION_TAGS:
                continue
            dimensions['task_action']['state'] = 'partial'
            dimensions['task_action']['reason'] = None
            dimensions['task_action']['values'].append(action)
            facts.append(fact(
                record_id, scope, 'task_action', action, 'unknown', 'legacy_unverified', None,
                'catalog_tasks.tags.action', source_hash, 'deterministic_legacy_map'))
        for tool in tags.get('tool') or []:
            if tool not in TOOL_TAGS:
                continue
            dimensions['tools']['state'] = 'partial'
            dimensions['tools']['reason'] = None
            dimensions['tools']['values'].append({'id': tool, 'role': 'unknown'})
            facts.append(fact(
                record_id, scope, 'tools', tool, 'unknown', 'legacy_unverified', None,
                'catalog_tasks.tags.tool', source_hash, 'deterministic_legacy_map'))
        text = '\n'.join(str(task.get(key) or '') for key in ('goal', 'applicability', 'outputs'))
        for tool_id in TOOL_TAGS:
            role = tool_role_from_text(text, tool_id)
            if not role:
                continue
            quote = next((piece for piece in text.splitlines() if piece and (
                TOOL_TAGS[tool_id][0].casefold() in piece.casefold()
                or any(alias in piece.casefold() for alias in TOOL_TAGS[tool_id][1].casefold().split('|'))
            )), None)
            facts.append(fact(
                record_id, scope, 'tools', tool_id, role,
                'direct_explicit' if quote else 'bounded_inference', quote,
                'catalog_tasks.text', source_hash, 'deterministic_role_phrase'))
    inputs = skill.get('inputs')
    if inputs == []:
        dimensions['input_kind'] = {'state': 'unknown', 'reason': 'not_in_source', 'values': []}
    elif inputs is None:
        dimensions['input_kind'] = {'state': 'unknown', 'reason': 'not_in_supplied_scope', 'values': []}
    stated_gaps = list(gaps or skill.get('gaps') or [])
    annotation = {
        'schema': SCHEMA,
        'record_id': record_id,
        'primary': primary,
        'dimensions': dimensions,
        'facts': facts,
        'gaps': stated_gaps,
        'quality': {'grade': 'U', 'model_review': 'untracked', 'execution': 'untracked'},
        'source_hash': source_hash,
        'fingerprint': None,
        'legacy_unverified': any(item['support'] == 'legacy_unverified' for item in facts),
        'human_review_unchanged': True,
    }
    annotation['fingerprint'] = digest({
        'record_id': record_id, 'primary': primary, 'facts': [
            {key: item[key] for key in ('scope', 'dimension', 'value_id', 'role', 'support', 'quote')}
            for item in facts
        ], 'source_hash': source_hash,
    })
    return annotation


def validate_annotation(annotation):
    errors = []
    if annotation.get('schema') != SCHEMA:
        errors.append('schema')
    for name, facet in annotation.get('dimensions', {}).items():
        if name not in DIMENSIONS or facet.get('state') not in STATES:
            errors.append('dimension:' + str(name))
    for item in annotation.get('facts', []):
        if item.get('support') not in SUPPORT:
            errors.append('support')
        if item.get('accepted') and not item.get('quote'):
            errors.append('accepted_without_quote')
        if item.get('role') in ('example', 'prohibited', 'alternative') and item.get('role') == 'required':
            errors.append('role')
    return errors


def scope_key(fact_item):
    scope = fact_item.get('scope') or {}
    if scope.get('level') == 'skill':
        return ('skill',)
    return ('task', scope.get('task_position'), scope.get('task_id'))


def satisfies(fact_item, dimension, values, role):
    if fact_item.get('accepted') is not True:
        return False
    if fact_item.get('dimension') != dimension or fact_item.get('value_id') not in values:
        return False
    if role == 'required' and fact_item.get('role') not in STRONG_ROLES:
        return False
    if fact_item.get('role') in ('prohibited', 'example', 'alternative', 'mentioned', 'target_platform'):
        return role not in ('required', 'conditional')
    return True


def same_task_query(facts, filters, same_task=True):
    """filters is dimension -> values. Across dimensions AND, within one dimension OR."""
    grouped = {}
    skill_wide = []
    for item in facts:
        if (item.get('scope') or {}).get('level') == 'skill':
            skill_wide.append(item)
        grouped.setdefault(scope_key(item), []).append(item)
    scopes = []
    if same_task:
        for key, bucket in grouped.items():
            scopes.append(bucket if key == ('skill',) else bucket + skill_wide)
        if not scopes:
            scopes = [skill_wide]
    else:
        scopes = [facts]
    hits = []
    for bucket in scopes:
        if all(any(satisfies(item, dimension, set(values), 'required') for item in bucket)
               for dimension, values in filters.items()):
            hits.append(bucket)
    return hits


def derive_grade(review, static_checks, execution_claim=None):
    if execution_claim == 'observed_pass':
        return {'grade': 'U', 'error': 'observed_pass_requires_trace'}
    if not review or not review.get('independent'):
        return {'grade': 'U', 'model_review': review.get('status') if review else 'untracked'}
    axes = review.get('axes') or {}
    if any(axes.get(axis) == 'fail' for axis in AXES):
        return {'grade': 'R', 'diversity': review.get('diversity')}
    if review.get('source_scope') != 'fixed_source':
        return {'grade': 'U' if axes.get('source_fidelity') == 'unknown' else 'D1',
                'source_fidelity': axes.get('source_fidelity', 'unknown')}
    applicable = [item for item in static_checks or [] if item.get('state') != 'not_applicable']
    all_na = bool(static_checks) and not applicable
    applicable_axes_pass = all(axes.get(axis) == 'pass' or (
        axes.get(axis) == 'not_applicable' and review.get('na_evidence', {}).get(axis)) for axis in AXES)
    if not applicable_axes_pass or any(axes.get(axis) in ('unknown', 'limited') for axis in AXES):
        return {'grade': 'D1', 'diversity': review.get('diversity')}
    if all_na or not applicable:
        return {'grade': 'D2', 'diversity': review.get('diversity'), 'd3_blocked': 'no_nontrivial_static_check'}
    if all(item.get('state') == 'pass' for item in applicable):
        return {'grade': 'D3', 'diversity': review.get('diversity')}
    return {'grade': 'D2', 'diversity': review.get('diversity')}


def independent_review(generation, review):
    if not generation or not review:
        return {'accepted': False, 'reason': 'missing_ledger'}
    if review.get('session_id') == generation.get('session_id') or review.get('context_hash') == generation.get('context_hash'):
        return {'accepted': False, 'reason': 'same_session_or_context', 'counts_as_completed_review': False}
    if review.get('model') != generation.get('model'):
        return {'accepted': False, 'reason': 'model_mismatch'}
    return {'accepted': True, 'diversity': 'same_model_new_session', 'counts_as_completed_review': True}


def execution_status(claim, trace=None, historical_ledger_missing=False):
    if historical_ledger_missing and not claim:
        return 'untracked'
    if claim == 'observed_pass' and not trace:
        return 'not_run'
    if claim in ('not_run', 'static_only', 'observed_pass', 'observed_fail'):
        return claim if claim != 'observed_pass' or trace else 'not_run'
    return 'untracked'


def mark_stale(annotation, source_hash):
    if annotation.get('source_hash') == source_hash:
        return annotation
    stale = dict(annotation)
    stale['stale'] = True
    stale['quality'] = dict(annotation.get('quality') or {})
    stale['quality']['reusable_pass'] = False
    return stale


def coverage(annotations):
    counts = {name: {state: 0 for state in STATES} for name in DIMENSIONS}
    for annotation in annotations:
        for name, facet in annotation['dimensions'].items():
            counts[name][facet['state']] += 1
    totals = {name: sum(states.values()) for name, states in counts.items()}
    return {'by_dimension': counts, 'totals': totals, 'records': len(annotations)}


def value_coverage(annotation):
    """A non-empty tags object with empty arrays is not coverage."""
    return any(item.get('value_id') and item.get('accepted') for item in annotation.get('facts', [])) or any(
        facet.get('values') for facet in annotation['dimensions'].values())


class Store:
    def __init__(self, out):
        self.out = Path(out)

    def lock(self):
        self.out.mkdir(parents=True, exist_ok=True)
        handle = (self.out / 'writer.lock').open('a+')
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise SystemExit('enrichment writer already holds the lock') from exc
        return handle

    def current(self):
        path = self.out / 'current.json'
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding='utf-8'))

    def apply(self, annotations, batch_id, dry_run=False):
        handle = self.lock()
        try:
            return self.apply_holding_lock(annotations, batch_id, dry_run)
        finally:
            handle.close()

    def apply_holding_lock(self, annotations, batch_id, dry_run=False):
        previous = self.current()
        fingerprint = digest([item['fingerprint'] for item in annotations])
        if previous and previous.get('fingerprint') == fingerprint and previous.get('batch_id') == batch_id:
            return {'applied': False, 'idempotent': True, 'snapshot': previous.get('snapshot'), 'writes': 0}
        snapshot = digest({'batch_id': batch_id, 'fingerprint': fingerprint})[:16]
        plan = {'snapshot': snapshot, 'records': len(annotations), 'batch_id': batch_id, 'dry_run': dry_run}
        if dry_run:
            plan['writes'] = 0
            return plan
        folder = self.out / 'snapshots' / snapshot
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'annotations.jsonl').write_text(
            ''.join(json.dumps(item, ensure_ascii=False) + '\n' for item in annotations), encoding='utf-8')
        pointer = {'schema': SCHEMA, 'snapshot': snapshot, 'batch_id': batch_id, 'fingerprint': fingerprint,
                   'previous': None if not previous else previous.get('snapshot'), 'records': len(annotations)}
        temporary = self.out / 'current.json.tmp'
        temporary.write_text(json.dumps(pointer, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        temporary.replace(self.out / 'current.json')
        plan['writes'] = 1
        plan['applied'] = True
        return plan

    def rollback(self, snapshot, dry_run=False):
        handle = self.lock()
        try:
            current = self.current()
            if not current or current.get('previous') != snapshot:
                return {'rolled_back': False, 'reason': 'snapshot_is_not_the_previous_pointer'}
            target = self.out / 'snapshots' / snapshot / 'annotations.jsonl'
            if not target.is_file():
                return {'rolled_back': False, 'reason': 'snapshot_missing'}
            if dry_run:
                return {'rolled_back': False, 'dry_run': True, 'would_restore': snapshot, 'writes': 0}
            pointer = dict(current)
            pointer['snapshot'] = snapshot
            pointer['rolled_back_from'] = current.get('snapshot')
            temporary = self.out / 'current.json.tmp'
            temporary.write_text(json.dumps(pointer, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            temporary.replace(self.out / 'current.json')
            return {'rolled_back': True, 'snapshot': snapshot}
        finally:
            handle.close()

    def append_ledger(self, kind, entry):
        handle = self.lock()
        try:
            return self.append_holding_lock(kind, entry)
        finally:
            handle.close()

    def append_holding_lock(self, kind, entry):
        folder = self.out / 'ledger'
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (kind + '.jsonl')
        line = json.dumps(entry, ensure_ascii=False)
        existing = path.read_text(encoding='utf-8') if path.is_file() else ''
        if line in existing.splitlines():
            return {'appended': False, 'idempotent': True}
        with path.open('a', encoding='utf-8') as handle_out:
            handle_out.write(line + '\n')
        return {'appended': True}


class Flight:
    def __init__(self, cap=10):
        self.cap = cap
        self.used = 0

    def reserve(self, count):
        if count < 0 or self.used + count > self.cap:
            raise RuntimeError('global in-flight cap is 10')
        self.used += count
        return self.used

    def release(self, count):
        self.used = max(0, self.used - count)


def plan_waves(generation, review, cap=10):
    if generation < 0 or review < 0:
        raise ValueError('negative wave')
    waves = []
    left_g, left_r = generation, review
    while left_g or left_r:
        take_g = min(left_g, cap)
        take_r = min(left_r, cap - take_g)
        waves.append({'generation': take_g, 'review': take_r, 'in_flight': take_g + take_r})
        left_g -= take_g
        left_r -= take_r
    return waves


def plan_pilot(records, limit=48):
    chosen = []
    seen = set()
    by_domain = {}
    for record in records:
        by_domain.setdefault(record.get('category') or 'unfiled', []).append(record)
    for _domain, bucket in sorted(by_domain.items()):
        for record in bucket[:2]:
            if record['id'] not in seen and len(chosen) < limit:
                chosen.append(record['id'])
                seen.add(record['id'])
    for record in records:
        if len(chosen) >= limit:
            break
        if record['id'] not in seen:
            chosen.append(record['id'])
            seen.add(record['id'])
    return {'ids': chosen[:limit], 'generation_calls_max': min(limit, 48), 'review_calls_max': min(limit, 48),
            'model_calls': 0}


def connect_readonly(database):
    uri = Path(database).resolve().as_uri() + '?mode=ro'
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def parse_tags(value):
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def attach_tasks(connection, skills, stats):
    """One catalog_tasks read for the selected ids. Never one query per skill."""
    started = time.perf_counter()
    for skill in skills:
        skill['tasks'] = []
    if not skills:
        stats['task_selects'] = 0
        stats['tasks_loaded'] = 0
        stats['load_tasks_seconds'] = 0
        return
    by_id = {skill['id']: skill for skill in skills}
    ids = list(by_id)
    if len(ids) <= 900:
        marks = ','.join('?' * len(ids))
        rows = connection.execute(
            'SELECT id, skill, position, goal, applicability, outputs, tags FROM catalog_tasks '
            f'WHERE skill IN ({marks}) ORDER BY skill, position, id',
            ids)
    else:
        rows = connection.execute(
            'SELECT id, skill, position, goal, applicability, outputs, tags FROM catalog_tasks '
            'ORDER BY skill, position, id')
    stats['task_selects'] = 1
    loaded = 0
    for row in rows:
        skill = by_id.get(row['skill'])
        if skill is None:
            continue
        skill['tasks'].append({
            'id': row['id'], 'position': row['position'], 'goal': row['goal'],
            'applicability': row['applicability'], 'outputs': row['outputs'],
            'tags': parse_tags(row['tags']),
        })
        loaded += 1
    stats['tasks_loaded'] = loaded
    stats['load_tasks_seconds'] = round(time.perf_counter() - started, 3)


def load_catalog(database, limit=None, ids=None, after='', with_tasks=True):
    """Read representatives once. Tasks are one scan or one IN-list, not N+1."""
    started = time.perf_counter()
    connection = connect_readonly(database)
    stats = {
        'skill_selects': 0, 'task_selects': 0, 'source_selects': 0, 'source_rows_loaded': 0,
        'tasks_loaded': 0, 'load_skills_seconds': 0, 'load_tasks_seconds': 0, 'catalogue_counts': {},
    }
    try:
        counts = {}
        for table in ('skills', 'sources', 'catalog_tasks'):
            counts[table] = connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
        stats['source_selects'] = 1
        stats['catalogue_counts'] = {
            'representatives': counts['skills'], 'sources': counts['sources'], 'tasks': counts['catalog_tasks'],
        }
        if ids is not None:
            if not ids:
                stats['load_skills_seconds'] = round(time.perf_counter() - started, 3)
                return [], stats
            marks = ','.join('?' * len(ids))
            rows = connection.execute(
                'SELECT id, title, summary, category, topic, label_from, review FROM skills '
                f'WHERE id IN ({marks})', list(ids))
        else:
            sql = 'SELECT id, title, summary, category, topic, label_from, review FROM skills'
            params = []
            if after:
                sql += ' WHERE id > ?'
                params.append(after)
            sql += ' ORDER BY id'
            if limit:
                sql += ' LIMIT ?'
                params.append(int(limit))
            rows = connection.execute(sql, params)
        stats['skill_selects'] = 1
        skills = [dict(row) for row in rows]
        if not ids:
            skills.sort(key=lambda item: item['id'])
        stats['load_skills_seconds'] = round(time.perf_counter() - started, 3)
        if with_tasks:
            attach_tasks(connection, skills, stats)
        return skills, stats
    finally:
        connection.close()


def peak_rss_kib():
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == 'darwin':
        return int(value / 1024)
    return int(value)


def map_catalogue(database, out=None, *, after='', limit=None, runtime_topics=None, apply=False, progress=None):
    """Deterministic map. Dry-run keeps no sidecar. Source hash is summary plus loaded tasks."""
    skills, stats = load_catalog(database, limit=limit, after=after, with_tasks=True)
    if progress:
        progress({'stage': 'loaded', 'skills': len(skills), 'tasks_loaded': stats['tasks_loaded'],
                  'task_selects': stats['task_selects'], 'catalogue_counts': stats['catalogue_counts']})
    started = time.perf_counter()
    fingerprints = []
    annotations = [] if apply else None
    action = tool = either = supplied_scope = 0
    for index, skill in enumerate(skills, 1):
        annotation = map_record(
            skill, skill['tasks'], source_hash=source_digest(skill), runtime_topics=runtime_topics)
        fingerprints.append(annotation['fingerprint'])
        has_action = bool(annotation['dimensions']['task_action']['values'])
        has_tool = bool(annotation['dimensions']['tools']['values'])
        action += bool(has_action)
        tool += bool(has_tool)
        either += bool(has_action or has_tool)
        if annotation['dimensions']['input_kind'].get('reason') == 'not_in_supplied_scope':
            supplied_scope += 1
        if annotations is not None:
            annotations.append(annotation)
        if progress and index % 5000 == 0:
            progress({'stage': 'map', 'processed': index, 'of': len(skills),
                      'elapsed_seconds': round(time.perf_counter() - started, 3)})
    catalogue_total = stats['catalogue_counts']['representatives']
    complete = not after and len(skills) == catalogue_total
    result = {
        'command': 'map', 'dry_run': not apply, 'writes': 0, 'complete': complete,
        'processed': len(skills), 'after': after or '',
        'next_after': skills[-1]['id'] if skills else (after or ''),
        'bounded_slice': not complete,
        'legacy_action_value_covered_representatives': action,
        'legacy_tool_value_covered_representatives': tool,
        'legacy_either_value_covered_representatives': either,
        'new_facet_value_coverage': 'unknown_not_computed',
        'input_kind_not_in_supplied_scope': supplied_scope,
        'input_kind_not_applicable': 0,
        'source_hash': 'sha256 of summary and the tasks loaded for that record',
        'fingerprint': digest(fingerprints) if fingerprints else digest([]),
        'map_seconds': round(time.perf_counter() - started, 3),
        'peak_rss_kib': peak_rss_kib(),
        'queries': {
            'skill_selects': stats['skill_selects'], 'task_selects': stats['task_selects'],
            'source_selects': stats['source_selects'], 'source_rows_loaded': 0,
        },
        'stages_seconds': {
            'load_skills': stats['load_skills_seconds'], 'load_tasks': stats['load_tasks_seconds'],
            'map': round(time.perf_counter() - started, 3),
        },
        'catalogue_counts': stats['catalogue_counts'], 'tasks_loaded': stats['tasks_loaded'],
        'complexity': 'one skills read and one catalog_tasks read; sources are counted, not loaded per record',
    }
    if apply:
        applied = Store(out).apply(annotations, 'deterministic-map', dry_run=False)
        result['writes'] = applied.get('writes', 0)
        result['dry_run'] = False
        result['applied'] = applied
    if progress:
        progress({'stage': 'done', 'complete': complete, 'processed': len(skills),
                  'stages_seconds': result['stages_seconds']})
    return result


def observe_risk(text):
    """Record a risk flag. This function does not download, execute, pay, or send messages."""
    flagged = any(piece in text for piece in ('下载执行', '交易', '发消息', '安装'))
    return {'executed': False, 'deleted': False, 'risk': 'unbounded_or_unauthorized_action_claim' if flagged else None}


def detail_for(out, skill_id):
    store = Store(out)
    current = store.current()
    if not current:
        return None
    path = store.out / 'snapshots' / current['snapshot'] / 'annotations.jsonl'
    if not path.is_file():
        return None
    for line in path.read_text(encoding='utf-8').splitlines():
        item = json.loads(line)
        if item.get('record_id') == skill_id:
            return {
                'schema': SCHEMA, 'grade': item['quality']['grade'],
                'model_review': item['quality']['model_review'],
                'primary': item['primary'], 'dimensions': item['dimensions'],
                'legacy_unverified': item.get('legacy_unverified', False),
            }
    return None


CONDITION_RULES = (
    ('requirements', 'authentication', 'required', ('需要登录', '必须登录')),
    ('requirements', 'network', 'required', ('需要网络',)),
    ('requirements', 'permission', 'required', ('需要管理员权限', '需要管理员')),
    ('side_effects', 'secrets_handling', 'prohibited', ('不要泄露密钥', '不要发送密钥')),
)


def parse_conditions(text):
    """Keep the original line. Accept only a closed phrase that appears inside it."""
    found = []
    for line in (text or '').splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        matched = False
        for dimension, value_id, role, phrases in CONDITION_RULES:
            if any(phrase in stripped for phrase in phrases):
                found.append({
                    'dimension': dimension, 'value_id': value_id, 'role': role,
                    'quote': stripped, 'accepted': True, 'candidate': False,
                    'support': 'direct_explicit',
                })
                matched = True
        if not matched and any(word in stripped for word in ('条件', '权限', '密钥', '登录', '网络')):
            found.append({
                'dimension': 'requirements', 'value_id': None, 'role': 'unknown',
                'quote': stripped, 'accepted': False, 'candidate': True,
                'support': 'bounded_inference',
            })
    return found


def alias_report(rows):
    representatives = [row for row in rows if row.get('relationship') == 'representative']
    aliases = [row for row in rows if row.get('relationship') != 'representative']
    return {
        'representatives': len(representatives),
        'aliases': len(aliases),
        'aliases_with_inherited_grade': sum(1 for row in aliases if row.get('quality_grade')),
        'quality_inherited': False,
    }


def runtime_pairs(payload):
    """Read either an explicit {pairs:[{domain_id,topic}]} file or the c26 reconciliation list."""
    raw_pairs = payload.get('allowed_pairs')
    if not isinstance(raw_pairs, list):
        raise ValueError('runtime allowlist pairs must be a list')
    stated = payload.get('runtime_allowed_last_saved') if isinstance(payload.get('runtime_allowed_last_saved'), dict) else {}
    source = stated.get('source') or payload.get('source')
    version = stated.get('sha256') or payload.get('version') or payload.get('sha256')
    parsed = []
    for pair in raw_pairs:
        if isinstance(pair, dict):
            domain, topic = pair.get('domain_id'), pair.get('topic')
        elif isinstance(pair, (list, tuple)) and len(pair) == 2:
            domain, topic = pair
        else:
            domain, topic = None, None
        parsed.append((domain, topic))
    return parsed, source, version


def merge_runtime_allowlist(code, payload, raw_hash):
    parsed, source, version = runtime_pairs(payload)
    code_keys = {(item['domain_id'], item['topic']) for item in code['pairs']}
    runtime_keys = set()
    skipped = 0
    extra = []
    for domain, topic in parsed:
        if domain not in LABELS or domain == 'unfiled' or not isinstance(topic, str) or not topic:
            skipped += 1
            continue
        runtime_keys.add((domain, topic))
        if (domain, topic) in code_keys:
            continue
        extra.append({
            'domain_id': domain, 'topic': topic, 'topic_id': None,
            'topic_resolved': topic != UNSORTED_TOPIC, 'origin': 'runtime_observed',
        })
    only_code = sorted(code_keys - runtime_keys)
    merged = dict(code)
    merged['pairs'] = [{**item, 'origin': item.get('origin', 'code_canonical')} for item in code['pairs']] + extra
    merged['runtime_observed_added'] = len(extra)
    merged['runtime_skipped'] = skipped
    merged['allowed_pairs_with_runtime'] = len(code_keys | runtime_keys)
    merged['only_in_runtime'] = [
        {'domain_id': item['domain_id'], 'topic': item['topic'], 'topic_id': None} for item in extra]
    merged['only_in_code'] = [{'domain_id': domain, 'topic': topic, 'topic_id': None} for domain, topic in only_code]
    merged['code_pairs_kept'] = len(code_keys)
    merged['runtime_source'] = source
    merged['runtime_vocabulary_sha256'] = version
    merged['snapshot_file_sha256'] = raw_hash
    merged['difference_source'] = (
        'Code export is static_topics only. Runtime pairs add path-derived legal labels '
        'whose folder slug is not one of the Chinese LEGAL_TOPICS names. Those pairs stay '
        'allowed and are not conflicts. Nothing is added or removed to force 155 or 163.'
    )
    return merged


def load_current_annotations(out):
    current = Store(out).current()
    if not current:
        return None
    path = Path(out) / 'snapshots' / current['snapshot'] / 'annotations.jsonl'
    if not path.is_file():
        return None
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]


def search_annotations(annotations, filters, same_task=True):
    """filters: list of (dimension, values, state or None)."""
    hits = []
    for annotation in annotations:
        facts = annotation.get('facts') or []
        ok = True
        for dimension, values, state in filters:
            facet = (annotation.get('dimensions') or {}).get(dimension) or {}
            if state and facet.get('state') != state:
                ok = False
                break
            if values and not same_task_query(facts, {dimension: values}, same_task):
                ok = False
                break
        if ok and filters:
            # Cross-dimension AND must hold inside one scope when same_task is set.
            active = [(dimension, values) for dimension, values, _state in filters if values]
            if same_task and len(active) > 1:
                if not same_task_query(facts, dict(active), True):
                    ok = False
        if ok:
            hits.append(annotation['record_id'])
    return hits


def supplied_text(skill):
    parts = [skill.get('summary') or '']
    for task in skill.get('tasks') or []:
        parts.extend(str(task.get(key) or '') for key in ('goal', 'applicability', 'outputs'))
    return '\n'.join(parts)


def value_catalog():
    """The same ids the prompt shows and the validator accepts."""
    return {
        'task_action': list(ACTION_TAGS),
        'tools': list(TOOL_TAGS),
        'input_kind': list(INPUT_KINDS),
        'output_kind': list(OUTPUT_KINDS),
        'kind': list(KIND_VALUES),
        'requirements': [value for dimension, value, _role, _phrases in CONDITION_RULES if dimension == 'requirements'],
        'side_effects': [value for dimension, value, _role, _phrases in CONDITION_RULES if dimension == 'side_effects'],
        'scenario': [],
    }


def value_aliases(dimension, value_id):
    if dimension == 'task_action' and value_id in ACTION_TAGS:
        label, extra = ACTION_TAGS[value_id]
        return [label, *extra.split('|')]
    if dimension == 'tools' and value_id in TOOL_TAGS:
        label, extra = TOOL_TAGS[value_id]
        return [label, *extra.split('|')]
    for rule_dimension, rule_value, _role, phrases in CONDITION_RULES:
        if rule_dimension == dimension and rule_value == value_id:
            return list(phrases)
    return [value_id]


def quote_mentions(dimension, value_id, quote):
    folded = (quote or '').casefold()
    return any(alias and alias.casefold() in folded for alias in value_aliases(dimension, value_id))


def resolve_unique(dimension, quote):
    """One catalog id, or none when the quote matches zero or several."""
    folded = (quote or '').casefold()
    scored = []
    for value_id in value_catalog().get(dimension) or []:
        lengths = [len(alias) for alias in value_aliases(dimension, value_id) if alias and alias.casefold() in folded]
        if lengths:
            scored.append((value_id, max(lengths)))
    kept = []
    for value_id, length in scored:
        aliases = [alias for alias in value_aliases(dimension, value_id) if alias and alias.casefold() in folded]
        shadowed = False
        for other, other_length in scored:
            if other == value_id or other_length <= length:
                continue
            longer = [alias for alias in value_aliases(dimension, other) if alias and alias.casefold() in folded]
            if any(alias.casefold() in big.casefold() for alias in aliases for big in longer):
                shadowed = True
        if not shadowed:
            kept.append(value_id)
    if len(kept) == 1:
        return kept[0]
    return None


def quote_positions(skill, quote):
    found = []
    for task in skill.get('tasks') or []:
        blob = '\n'.join(str(task.get(key) or '') for key in ('goal', 'applicability', 'outputs'))
        if quote and quote in blob:
            found.append(task.get('position'))
    return found


def scope_supports(skill, quote, task_position):
    positions = quote_positions(skill, quote)
    in_summary = bool(quote) and quote in (skill.get('summary') or '')
    if task_position is None:
        return in_summary or bool(positions)
    return task_position in positions


def program_evidence(skill, dimension, value_id, quote, task_position):
    text = supplied_text(skill)
    if not quote or quote not in text or value_id not in (value_catalog().get(dimension) or []):
        return False
    if not scope_supports(skill, quote, task_position):
        return False
    if dimension in ('task_action', 'tools', 'requirements', 'side_effects') and not quote_mentions(dimension, value_id, quote):
        return False
    if dimension == 'tools':
        detected = tool_role_from_text(quote, value_id)
        if detected in ('prohibited', 'example', 'alternative'):
            return True
    if any(phrase in quote for phrase in ('不要', '不是这一步', 'do not', "don't")):
        return False
    return True


def absorb_facts(annotation, payload, skill):
    """Keep every returned fact. A missing value_id is resolved only when one catalog id matches the quote."""
    text = supplied_text(skill) if isinstance(skill, dict) else skill
    skill_row = skill if isinstance(skill, dict) else {'summary': text, 'tasks': []}
    for raw in payload.get('facts') or []:
        if not isinstance(raw, dict):
            continue
        dimension = raw.get('dimension')
        if dimension not in DIMENSIONS:
            annotation.setdefault('dropped_facts', []).append({'reason': 'dimension', 'dimension': dimension})
            continue
        quote = raw.get('quote') if isinstance(raw.get('quote'), str) else ''
        quote_ok = bool(quote) and quote in text
        received_value = raw.get('value_id')
        value = received_value if isinstance(received_value, str) and received_value else None
        value_status = 'model'
        if value is None:
            resolved = resolve_unique(dimension, quote) if quote_ok else None
            if resolved:
                value = resolved
                value_status = 'resolved_from_quote'
            else:
                value_status = 'missing'
        elif value not in (value_catalog().get(dimension) or []):
            value_status = 'not_in_vocabulary'
            value = None
        received_role = raw.get('role')
        if received_role in TOOL_ROLES:
            role = received_role
            role_status = 'model'
        elif received_role in (None, ''):
            role = 'unknown'
            role_status = 'missing'
        else:
            role = 'unknown'
            role_status = 'invalid'
        if dimension == 'tools' and quote_ok and (value in TOOL_TAGS or received_value in TOOL_TAGS):
            detected = tool_role_from_text(quote, value or received_value)
            if detected in ('prohibited', 'example', 'alternative'):
                role = detected
                role_status = 'corrected_from_quote'
        task_position = raw.get('task_position')
        evidenced = program_evidence(skill_row, dimension, value, quote, task_position) if value else False
        item = fact(
            annotation['record_id'],
            {'level': 'task', 'task_position': task_position} if task_position is not None else {'level': 'skill'},
            dimension, value, role,
            'direct_explicit' if evidenced else 'bounded_inference',
            quote if quote_ok else None, 'model_quote', annotation.get('source_hash') or '', 'model_generation')
        item['value_received'] = received_value
        item['value_status'] = value_status
        item['role_received'] = received_role
        item['role_status'] = role_status
        item['proposed_value'] = raw.get('proposed_value')
        item['program_evidence'] = evidenced
        item['rule_version'] = RULE_VERSION
        item['accepted'] = False
        item['candidate'] = True
        annotation['facts'].append(item)
    annotation['fingerprint'] = digest({
        'record_id': annotation['record_id'], 'rule_version': RULE_VERSION,
        'facts': [item['fact_id'] for item in annotation['facts']],
        'source_hash': annotation.get('source_hash'),
    })
    return annotation


def guard_tag_fit(axes, facts, text):
    """A pass on tag fit cannot stand when the source has one clear catalog term and every value id is empty."""
    if axes.get('classification_and_tag_fit') != 'pass':
        return axes
    if any(item.get('value_id') for item in facts):
        return axes
    for dimension in ('task_action', 'tools'):
        if resolve_unique(dimension, text):
            axes = dict(axes)
            axes['classification_and_tag_fit'] = 'fail'
            axes['tag_fit_reason'] = 'mappable_term_present_but_no_canonical_value'
            return axes
    return axes


def settle_review(annotation, payload, skill):
    """Accept a fact only from program evidence plus a review that did not reject that fact."""
    text = supplied_text(skill)
    axes = dict(payload.get('axes') if isinstance(payload.get('axes'), dict) else {})
    if axes.get('source_fidelity') == 'pass':
        axes['source_fidelity'] = 'limited'
        axes['source_fidelity_reason'] = 'supply_is_summary_and_tasks_only'
    decisions = {}
    for row in payload.get('fact_decisions') or []:
        if isinstance(row, dict):
            decisions[(row.get('dimension'), row.get('value_id'), row.get('quote'))] = row.get('decision')
    for item in annotation.get('facts') or []:
        if item.get('support') == 'legacy_unverified':
            continue
        decision = decisions.get((item.get('dimension'), item.get('value_id'), item.get('quote')))
        item['review_decision'] = decision or 'unspecified'
        if decision == 'reject' or not item.get('program_evidence'):
            item['accepted'] = False
            item['candidate'] = True
            continue
        if item.get('dimension') in ('input_kind', 'output_kind', 'kind', 'scenario') and decision != 'accept':
            item['accepted'] = False
            item['candidate'] = True
            continue
        item['accepted'] = True
        item['candidate'] = False
        item['accepted_by'] = RULE_VERSION
    axes = guard_tag_fit(axes, annotation.get('facts') or [], text)
    if axes.get('classification_and_tag_fit') == 'fail':
        for item in annotation.get('facts') or []:
            if item.get('support') != 'legacy_unverified':
                item['accepted'] = False
                item['candidate'] = True
    for name in DIMENSIONS:
        values = []
        for item in annotation.get('facts') or []:
            if item.get('dimension') == name and item.get('accepted') and item.get('value_id'):
                role = item.get('role')
                values.append(item['value_id'] if role in (None, 'unknown') else {'id': item['value_id'], 'role': role})
        if values:
            annotation['dimensions'][name]['state'] = 'known'
            annotation['dimensions'][name]['reason'] = None
            annotation['dimensions'][name]['values'] = values
    grade = derive_grade({
        'independent': True, 'source_scope': 'summary_and_tasks', 'axes': axes,
        'na_evidence': {}, 'diversity': 'same_model_new_session'}, [])
    annotation['quality'] = {
        'grade': grade['grade'], 'model_review': 'completed', 'execution': 'not_run',
        'diversity': 'same_model_new_session', 'source_scope': 'summary_and_tasks',
        'rule_version': RULE_VERSION, 'axes': axes,
    }
    annotation['fingerprint'] = digest({
        'record_id': annotation['record_id'], 'rule_version': RULE_VERSION,
        'accepted': [item['fact_id'] for item in annotation['facts'] if item.get('accepted')],
        'source_hash': annotation.get('source_hash'),
    })
    return annotation


def repair_generated(skill, payload):
    """Fill unambiguous value ids from a saved response. Does not call a model or reuse the old grade."""
    annotation = map_record(skill, skill.get('tasks') or [], source_hash=source_digest(skill))
    absorb_facts(annotation, payload, skill)
    annotation['quality'] = {
        'grade': 'U', 'model_review': 'repair_pending_review', 'execution': 'not_run',
        'prior_review_reusable': False, 'rule_version': RULE_VERSION,
    }
    annotation['human_review_unchanged'] = True
    return annotation


MANIFEST_SCHEMA = 'atlas-enrichment-manifest/v0.1'
SOURCE_HASH_SEMANTICS = (
    'sha256 of canonical JSON {"summary": skills.summary, "tasks": [catalog_tasks rows]}. '
    'Canonical JSON sorts object keys, keeps array order, and uses separators (",", ":"). '
    'Each task row has id, position, goal, applicability, outputs, and tags, in position order. '
    'This is not the normalized SkillRecord file hash.'
)


def manifest_digest(ids, records):
    return digest({'ids': list(ids), 'records': records})


def parse_explicit_ids(text):
    ids = [piece.strip() for piece in str(text or '').split(',') if piece.strip()]
    if not 1 <= len(ids) <= 48:
        raise SystemExit('explicit ids must be 1 to 48')
    if len(ids) != len(set(ids)):
        raise SystemExit('explicit ids contain duplicates')
    return ids


def select_explicit_ids(database, ids):
    """Skills in the requested order. Missing, duplicate, or extra ids stop the command."""
    ids = list(ids)
    if not 1 <= len(ids) <= 48 or len(ids) != len(set(ids)):
        raise SystemExit('explicit ids must be 1 to 48 unique records')
    skills, _stats = load_catalog(database, ids=ids, with_tasks=True)
    by_id = {skill['id']: skill for skill in skills}
    if len(by_id) != len(ids) or any(record_id not in by_id for record_id in ids):
        raise SystemExit('explicit ids are not all current representatives')
    return [by_id[record_id] for record_id in ids]


def assert_manifest_current(skills, manifest):
    """Reject a stale, reordered, or incomplete manifest before any model call."""
    ids = manifest.get('ids')
    records = manifest.get('records')
    if not isinstance(ids, list) or not isinstance(records, list):
        raise SystemExit('manifest ids and records must be lists')
    if manifest.get('sha256') != manifest_digest(ids, records):
        raise SystemExit('manifest sha256 does not match its ids and records')
    if [record.get('id') if isinstance(record, dict) else None for record in records] != list(ids):
        raise SystemExit('manifest record order does not match ids')
    if not ids or len(ids) > 48 or len(ids) != len(set(ids)):
        raise SystemExit('manifest ids must be 1 to 48 unique records')
    by_id = {skill['id']: skill for skill in skills}
    if any(record_id not in by_id for record_id in ids):
        raise SystemExit('manifest ids are not all current representatives')
    for record_id, record in zip(ids, records):
        if record.get('source_hash') != source_digest(by_id[record_id]):
            raise SystemExit('manifest source_hash does not match the current catalogue')
    return [by_id[record_id] for record_id in ids]


def export_manifest(database, ids, out_path):
    skills = select_explicit_ids(database, ids)
    records = [{'id': skill['id'], 'source_hash': source_digest(skill)} for skill in skills]
    payload = {
        'schema': MANIFEST_SCHEMA,
        'id_order': 'input',
        'calls_model': False,
        'source_hash_semantics': SOURCE_HASH_SEMANTICS,
        'normalized_record_sha256': None,
        'ids': [skill['id'] for skill in skills],
        'records': records,
    }
    payload['sha256'] = manifest_digest(payload['ids'], records)
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)
    return payload


def load_manifest(path):
    raw = Path(path).read_bytes()
    payload = json.loads(raw)
    ids = payload.get('ids')
    records = payload.get('records')
    if not isinstance(ids, list) or not 1 <= len(ids) <= 48:
        raise SystemExit('manifest ids must be a list of 1 to 48 record ids')
    if len(ids) != len(set(ids)):
        raise SystemExit('manifest ids contain duplicates')
    if not isinstance(records, list) or [row.get('id') if isinstance(row, dict) else None for row in records] != ids:
        raise SystemExit('manifest record order does not match ids')
    if any(not isinstance(row.get('source_hash'), str) or not row.get('source_hash') for row in records):
        raise SystemExit('manifest records must carry source_hash')
    if payload.get('sha256') != manifest_digest(ids, records):
        raise SystemExit('manifest sha256 does not match its ids and records')
    return payload, hashlib.sha256(raw).hexdigest()


def source_digest(skill):
    return digest({'summary': skill.get('summary'), 'tasks': skill.get('tasks')})


def cursor_select(skills, completed, after, limit):
    """Take the next unfinished ids after the cursor. Errors are not retried and do not count as covered."""
    start = after or ''
    ordered = sorted(skills, key=lambda item: item['id'])
    selected = []
    skipped_errors = []
    for skill in ordered:
        if skill['id'] <= start:
            continue
        previous = completed.get(skill['id']) or {}
        same = previous.get('source_hash') == source_digest(skill)
        if same and previous.get('phase') == 'error':
            skipped_errors.append(skill['id'])
            continue
        if same and previous.get('phase') in ('generated', 'reviewed'):
            continue
        selected.append(skill)
        if len(selected) >= limit:
            break
    covered = start
    for skill in ordered:
        if skill['id'] <= start:
            continue
        previous = completed.get(skill['id']) or {}
        if previous.get('source_hash') == source_digest(skill) and previous.get('phase') in ('generated', 'reviewed'):
            covered = skill['id']
            continue
        break
    return selected, skipped_errors, covered


def covered_cursor(skills, completed, after):
    start = after or ''
    covered = start
    for skill in sorted(skills, key=lambda item: item['id']):
        if skill['id'] <= start:
            continue
        previous = completed.get(skill['id']) or {}
        if previous.get('source_hash') == source_digest(skill) and previous.get('phase') in ('generated', 'reviewed'):
            covered = skill['id']
            continue
        break
    return covered


def catalog_lines():
    lines = []
    catalog = value_catalog()
    for dimension in DIMENSIONS:
        values = catalog[dimension]
        if dimension == 'task_action':
            shown = [key + '(' + ACTION_TAGS[key][0] + ')' for key in values]
        elif dimension == 'tools':
            shown = [key + '(' + TOOL_TAGS[key][0] + ')' for key in values]
        elif values:
            shown = list(values)
        else:
            shown = ['无封闭词表，没有把握就不要写 fact']
        lines.append(dimension + ': ' + ', '.join(shown))
    return '\n'.join(lines)


def generation_prompt(skill):
    example = {
        '_example_is_fake': True,
        'facts': [{
            'dimension': 'tools', 'value_id': 'python', 'role': 'required',
            'quote': '假例子：使用 Python 完成一个与用户材料无关的动作。',
            'task_position': 1, 'proposed_value': None,
        }, {
            'dimension': 'tools', 'value_id': None, 'role': 'mentioned',
            'quote': '假例子：这里出现一个词表里没有的工具名。',
            'task_position': 1, 'proposed_value': '词表以外的新词',
        }],
    }
    return (
        '只根据下面的目录摘要和任务返回一个 JSON 对象。不要使用工具，不要把没出现的事实补进去，不要为了填满八个维度而编标签。'
        '这次供给只有 summary 和 tasks，不是原始全文。quote 必须是供给文字里的连续原文。'
        'dimension 只能是下面的键。value_id 只能是该维度列出的规范 id；词表里没有的词把 value_id 留空，并把新词放进 proposed_value，不要自己写成已接受。'
        'role 只能是 ' + ', '.join(TOOL_ROLES) + '。例子、禁止和替代不能写成 required。'
        'task_position 使用任务序号；引用只在摘要里就不要填 task_position。'
        '规范词表版本 ' + RULE_VERSION + '。\n'
        + catalog_lines()
        + '\n假例子，不是用户材料：\n'
        + json.dumps(example, ensure_ascii=False)
        + '\n目录摘要和任务：\n'
        + supplied_text(skill)
    )


def review_prompt(skill, annotation):
    visible = [{
        'dimension': item['dimension'], 'value_id': item.get('value_id'), 'role': item.get('role'),
        'value_status': item.get('value_status'), 'proposed_value': item.get('proposed_value'),
        'scope': item.get('scope'), 'quote': item.get('quote'),
        'program_evidence': item.get('program_evidence'),
    } for item in annotation.get('facts') or [] if item.get('support') != 'legacy_unverified']
    return (
        '独立审核这些候选。只看下面的摘要、任务和候选的 value_id、role、scope、quote。'
        '不要沿用生成会话，不要看生成者的推理。供给只有 summary 和 tasks，source_fidelity 不能写成 pass。'
        '无 quote、引用不在供给里、value_id 不在词表、引用属于另一个任务，或把否定句当成 required 的，decision 写 reject。'
        '证据不够就写 unknown，不要因为其它轴通过就接受全部候选。'
        '返回 JSON：axes 的键是 ' + ', '.join(AXES) + '，值只能是 unknown、pass、limited、fail、not_applicable。'
        'fact_decisions 的每一项含 dimension、value_id、quote、decision。decision 只能是 accept、reject、unknown。\n'
        + json.dumps({
            'source_scope': 'summary_and_tasks', 'source': supplied_text(skill), 'candidates': visible,
        }, ensure_ascii=False)
    )


def run_enrichment(client, out, skills, *, phase='both', limit=48, cost_policy='strict', workers=10,
                   max_requests=96, max_runtime=1500, after='', manifest=None, runtime_topics=None):
    """Generate, then review, through the same free-model client. Phases do not run 10+10."""
    from concurrent.futures import ThreadPoolExecutor
    import threading
    import time
    shared = global_writer_lock()
    store = Store(out)
    try:
        handle = store.lock()
    except SystemExit:
        shared.close()
        raise
    started = time.monotonic()
    workers = min(10, max(1, workers))
    state_path = Path(out) / 'batch-state.json'
    try:
        state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.is_file() else {
            'schema': SCHEMA, 'completed': {}, 'stopped': None, 'calls': 0}
        if state.get('stopped'):
            return {'stopped': state['stopped'], 'calls': 0, 'model_calls_this_process': 0, 'writes_catalogue': False}
        rejected = []
        skipped_errors = []
        if manifest:
            selected = assert_manifest_current(skills, manifest)
            covered = ''
        try:
            if hasattr(client, 'check_price'):
                client.check_price()
        except Exception as exc:
            return {'stopped': str(exc), 'calls': 0, 'model_calls_this_process': 0, 'writes_catalogue': False}
        if not manifest:
            selected, skipped_errors, covered = cursor_select(skills, state['completed'], after, limit)
        calls = {'n': 0}
        stop = threading.Event()
        stop_reason = {'text': None}
        gate = threading.Lock()
        peak = {'n': 0, 'current': 0}

        def one(skill, review):
            entered = False
            if stop.is_set() or time.monotonic() - started > max_runtime:
                return
            source_hash = source_digest(skill)
            with gate:
                previous = state['completed'].get(skill['id'])
                if previous and previous.get('source_hash') == source_hash and previous.get('phase') in ('error', 'reviewed'):
                    return
                if previous and previous.get('source_hash') == source_hash and previous.get('phase') == 'generated' and not review:
                    return
                if calls['n'] >= max_requests:
                    stop.set()
                    stop_reason['text'] = 'request budget reached'
                    return
                calls['n'] += 1
                peak['current'] += 1
                peak['n'] = max(peak['n'], peak['current'])
            entered = True
            try:
                if review and previous and previous.get('annotation'):
                    base = json.loads(json.dumps(previous['annotation']))
                else:
                    base = map_record(skill, skill.get('tasks') or [], source_hash=source_hash, runtime_topics=runtime_topics)
                for condition in parse_conditions(supplied_text(skill)):
                    if not condition['accepted']:
                        continue
                    closed = fact(
                        skill['id'], {'level': 'skill'}, condition['dimension'], condition['value_id'],
                        condition['role'], 'direct_explicit', condition['quote'], 'supplied_text',
                        source_hash, 'closed_phrase')
                    closed['program_evidence'] = True
                    closed['accepted'] = False
                    closed['candidate'] = True
                    closed['rule_version'] = RULE_VERSION
                    base['facts'].append(closed)
                attempt = Path(out) / 'attempts' / skill['id'] / ('review' if review else 'generate')
                attempt.mkdir(parents=True, exist_ok=True)
                title = ('ATLAS enrichment review ' if review else 'ATLAS enrichment generate ') + skill['id']
                if stop.is_set():
                    with gate:
                        calls['n'] -= 1
                        peak['current'] = max(0, peak['current'] - 1)
                    entered = False
                    return
                prompt = review_prompt(skill, base) if review else generation_prompt(skill)
                (attempt / 'prompt.txt').write_text(prompt, encoding='utf-8')
                reply = client.request(prompt, title, attempt)
                from classify_unfiled import accept_cost
                accounting = accept_cost(reply.get('usage') or {}, cost_policy)
                if reply.get('tool_events') or reply.get('timed_out') or reply.get('exit_code') not in (0, None):
                    stop.set()
                    stop_reason['text'] = 'tool_or_transport'
                    return
                payload = json.loads(reply.get('text') or '{}')
                if review:
                    generation = previous or {}
                    verdict = independent_review(
                        {'session_id': generation.get('session_id'), 'context_hash': generation.get('context_hash'),
                         'model': 'opencode-go/iceman-contributor-free'},
                        {'session_id': reply.get('session_id'), 'context_hash': digest(title),
                         'model': 'opencode-go/iceman-contributor-free'})
                    if not verdict['accepted']:
                        base['quality'] = {'grade': 'U', 'model_review': 'rejected_not_independent', 'execution': 'not_run'}
                    else:
                        settle_review(base, payload, skill)
                else:
                    absorb_facts(base, payload, skill)
                    base['quality'] = {'grade': 'U', 'model_review': 'draft_pending_review', 'execution': 'not_run',
                                       'rule_version': RULE_VERSION}
                entry = {
                    'phase': 'reviewed' if review else 'generated', 'source_hash': source_hash,
                    'session_id': reply.get('session_id'), 'context_hash': digest(title),
                    'cost_accounting': accounting, 'fee_proven': accounting == 'verified_zero',
                    'turns': (reply.get('usage') or {}).get('turns') or [],
                    'annotation': base,
                }
                with gate:
                    state['completed'][skill['id']] = entry
                    state['calls'] = calls['n']
                    store.append_holding_lock('model_runs', {
                        'record_id': skill['id'], 'phase': entry['phase'], 'session_id': entry['session_id'],
                        'cost_accounting': accounting, 'fee_proven': entry['fee_proven'],
                        'turns': entry['turns'],
                    })
            except Exception as exc:
                from classify_unfiled import CostHalt, CostUnknown, PriceStop
                if isinstance(exc, (PriceStop, CostHalt)):
                    stop.set()
                    stop_reason['text'] = str(exc)
                elif isinstance(exc, CostUnknown):
                    stop.set()
                    stop_reason['text'] = 'cost_unknown'
                else:
                    with gate:
                        state['completed'][skill['id']] = {
                            'phase': 'error', 'error': type(exc).__name__, 'source_hash': source_hash,
                            'fee_proven': False}
            finally:
                if entered:
                    with gate:
                        peak['current'] = max(0, peak['current'] - 1)
                        public = {key: value for key, value in state.items() if key != 'completed'}
                        public['completed'] = state['completed']
                        public['stopped'] = stop_reason['text']
                        state_path.write_text(json.dumps(public, ensure_ascii=False) + '\n', encoding='utf-8')

        def run_phase(review):
            pending = []
            for skill in selected:
                done = state['completed'].get(skill['id']) or {}
                if review and done.get('phase') != 'generated':
                    continue
                if not review and done.get('phase') in ('generated', 'reviewed'):
                    continue
                pending.append(skill)
            if not pending or stop.is_set():
                return
            with ThreadPoolExecutor(max_workers=workers) as pool:
                list(pool.map(lambda skill: one(skill, review), pending))

        if phase in ('generate', 'both'):
            run_phase(False)
        if phase in ('review', 'both') and not stop.is_set():
            run_phase(True)
        if not manifest:
            covered = covered_cursor(skills, state['completed'], after)
            skipped_errors = sorted(set(skipped_errors) | {
                key for key, item in state['completed'].items() if item.get('phase') == 'error'})
        manifest_out = {
            'schema': 'atlas-enrichment-run-manifest/v0.1',
            'calls_model': True, 'pilot_is_not_used': True,
            'selection': 'manifest' if manifest else 'cursor',
            'source_hashes_checked': any(
                isinstance(row, dict) and row.get('source_hash') for row in (manifest or {}).get('records') or []),
            'after': after or '',
            'covered_cursor': covered,
            'covered_cursor_stops_before_errors': True,
            'selected_ids': [skill['id'] for skill in selected],
            'skipped_errors': skipped_errors,
            'rejected': rejected,
            'stopped': stop_reason['text'],
            'sessions': [{
                'id': key, 'phase': item.get('phase'), 'session_id': item.get('session_id'),
                'cost_accounting': item.get('cost_accounting'), 'fee_proven': item.get('fee_proven'),
            } for key, item in state['completed'].items()],
        }
        (Path(out) / 'phase-manifest.json').write_text(json.dumps(manifest_out, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        annotations = [item['annotation'] for item in state['completed'].values() if item.get('annotation')]
        applied = store.apply_holding_lock(annotations, 'enrichment-batch', dry_run=not annotations)
        missing = sum(1 for item in state['completed'].values()
                      if item.get('cost_accounting') == 'missing_cost_allowed_by_user')
        verified = sum(1 for item in state['completed'].values() if item.get('cost_accounting') == 'verified_zero')
        return {
            'stopped': stop_reason['text'], 'calls': calls['n'], 'model_calls_this_process': calls['n'],
            'max_in_flight': peak['n'], 'workers': workers, 'verified_zero': verified,
            'missing_cost_allowed': missing,
            'fee_proven_all': verified > 0 and missing == 0 and not stop_reason['text'],
            'applied': applied, 'writes_catalogue': False, 'phase': phase,
        }
    finally:
        handle.close()
        shared.close()


def run(argv=None):
    parser = argparse.ArgumentParser(description='Enrichment sidecar and optional free-model batch. map/pilot/taxonomy do not call a model.')
    parser.add_argument('command', choices=('taxonomy', 'map', 'pilot', 'manifest', 'repair', 'rollback', 'run', 'aliases'))
    parser.add_argument('--database', type=Path)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--snapshot')
    parser.add_argument('--limit', type=int, default=None, help='run omits this to take 48; map omits this to read the whole catalogue. A number is a bounded slice, not a full-catalogue result')
    parser.add_argument('--phase', choices=('generate', 'review', 'both'), default='both')
    parser.add_argument('--cost-policy', choices=('strict', 'allow-missing-cost'), default='strict')
    parser.add_argument('--opencode', default='opencode')
    parser.add_argument('--workers', type=int, default=10)
    parser.add_argument('--max-requests', type=int, default=96)
    parser.add_argument('--max-runtime-seconds', type=int, default=1500)
    parser.add_argument('--after', default='', help='Continue after this record id. Empty starts at the first id and includes already classified records. Not the unfiled cursor')
    parser.add_argument('--runtime-allowlist', type=Path, help='Same c26 snapshot for taxonomy, map, and run. Omitted, unknown topics are kept')
    parser.add_argument('--attempt', type=Path, help='Saved model response JSON for repair. repair does not call a model')
    parser.add_argument('--manifest', type=Path, help='Manifest file written by the manifest command. run selects these ids in input order and checks source_hash before any model call')
    parser.add_argument('--ids', help='Comma-separated record ids in the order to keep, 1 to 48. Used by manifest and pilot. run does not accept this')
    args = parser.parse_args(argv)
    if args.command == 'taxonomy':
        report = taxonomy_reconciliation()
        if args.runtime_allowlist:
            raw = args.runtime_allowlist.read_bytes()
            report = merge_runtime_allowlist(report, json.loads(raw), hashlib.sha256(raw).hexdigest())
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps({key: report[key] for key in (
            'domain_count', 'allowed_pairs', 'unsorted_pairs', 'pairs_excluding_unsorted', 'unique_topic_strings')},
            ensure_ascii=False, indent=2))
        return 0
    if args.command == 'manifest' or (args.command == 'pilot' and args.ids):
        if not args.database:
            raise SystemExit('manifest export needs --database')
        payload = export_manifest(args.database, parse_explicit_ids(args.ids), args.out)
        print(json.dumps({
            'calls_model': False, 'id_order': payload['id_order'], 'ids': payload['ids'],
            'sha256': payload['sha256'], 'out': str(args.out),
            'source_hash_semantics': payload['source_hash_semantics'],
            'read_by_run': True,
        }, ensure_ascii=False, indent=2))
        return 0
    if args.command == 'pilot':
        skills = load_catalog(args.database, with_tasks=False)[0] if args.database else []
        plan = plan_pilot(skills, args.limit or 48)
        plan['calls_model'] = False
        plan['read_by_run'] = False
        plan['id_order'] = 'domain_sample'
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    if args.apply and args.dry_run:
        raise SystemExit('pass either --apply or --dry-run')
    runtime_topics = None
    if args.runtime_allowlist and args.command in ('map', 'run'):
        raw = args.runtime_allowlist.read_bytes()
        merged = merge_runtime_allowlist(taxonomy_reconciliation(), json.loads(raw), hashlib.sha256(raw).hexdigest())
        runtime_topics = {(item['domain_id'], item['topic']) for item in merged['only_in_runtime']}
        if args.command == 'run' or args.apply:
            args.out.mkdir(parents=True, exist_ok=True)
            (args.out / 'taxonomy-used.json').write_text(
                json.dumps(merged, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    if args.command == 'repair':
        ids = parse_explicit_ids(args.ids)
        if len(ids) != 1:
            raise SystemExit('repair takes exactly one id')
        if not args.attempt:
            raise SystemExit('repair needs --attempt')
        skill = select_explicit_ids(args.database, ids)[0]
        payload = json.loads(Path(args.attempt).read_text(encoding='utf-8'))
        annotation = repair_generated(skill, payload)
        Path(args.out).write_text(json.dumps(annotation, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps({
            'calls_model': False, 'rule_version': RULE_VERSION,
            'prior_review_reusable': False,
            'resolved': sum(1 for item in annotation['facts'] if item.get('value_status') == 'resolved_from_quote'),
            'still_without_value': sum(1 for item in annotation['facts'] if not item.get('value_id')),
            'grade': annotation['quality']['grade'],
        }, ensure_ascii=False, indent=2))
        return 0
    if args.command == 'run':
        from classify_unfiled import OpenCodeFreeClient
        if args.ids:
            raise SystemExit('run does not accept --ids; write a manifest first')
        if not args.manifest:
            raise SystemExit('run requires --manifest')
        if args.after:
            raise SystemExit('run --manifest does not use --after')
        if args.limit is not None:
            raise SystemExit('run --manifest does not use --limit')
        manifest, _file_hash = load_manifest(args.manifest)
        skills = select_explicit_ids(args.database, manifest['ids'])
        assert_manifest_current(skills, manifest)
        report = run_enrichment(
            OpenCodeFreeClient(args.opencode), args.out, skills, phase=args.phase,
            limit=48 if args.limit is None else max(1, args.limit),
            cost_policy=args.cost_policy, workers=args.workers, max_requests=args.max_requests,
            max_runtime=args.max_runtime_seconds, after=args.after, manifest=manifest,
            runtime_topics=runtime_topics)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2 if report.get('stopped') else 0
    if args.command == 'aliases':
        skills = load_catalog(args.database, with_tasks=False)[0]
        print(json.dumps(alias_report([
            {'relationship': 'representative', 'id': skill['id']} for skill in skills
        ]), ensure_ascii=False, indent=2))
        return 0
    if args.command == 'rollback':
        print(json.dumps(Store(args.out).rollback(args.snapshot, dry_run=not args.apply), ensure_ascii=False, indent=2))
        return 0
    report = map_catalogue(
        args.database, args.out, after=args.after, limit=args.limit, runtime_topics=runtime_topics,
        apply=args.apply, progress=lambda payload: print(json.dumps(payload, ensure_ascii=False), file=sys.stderr, flush=True))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(run())
