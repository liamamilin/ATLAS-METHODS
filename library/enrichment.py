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


def absorb_facts(annotation, payload, text):
    for raw in payload.get('facts') or []:
        if not isinstance(raw, dict) or raw.get('dimension') not in DIMENSIONS:
            continue
        quote = raw.get('quote') if isinstance(raw.get('quote'), str) else ''
        quote_ok = bool(quote) and quote in text
        value = raw.get('value_id')
        role = raw.get('role') if raw.get('role') in TOOL_ROLES else 'unknown'
        if raw.get('dimension') == 'tools' and quote_ok and value in TOOL_TAGS:
            detected = tool_role_from_text(quote, value)
            if detected in ('prohibited', 'example', 'alternative'):
                role = detected
        known_value = value in ACTION_TAGS or value in TOOL_TAGS or value in INPUT_KINDS or value in OUTPUT_KINDS or value in KIND_VALUES
        mentions_value = True
        if value in TOOL_TAGS and quote_ok:
            mentions_value = tool_role_from_text(quote, value) is not None or TOOL_TAGS[value][0].casefold() in quote.casefold()
        if value in ACTION_TAGS and quote_ok:
            mentions_value = ACTION_TAGS[value][0] in quote or any(
                alias in quote.casefold() for alias in ACTION_TAGS[value][1].casefold().split('|'))
        accepted = quote_ok and known_value and mentions_value
        item = fact(
            annotation['record_id'],
            {'level': 'task', 'task_position': raw.get('task_position')} if raw.get('task_position') is not None else {'level': 'skill'},
            raw['dimension'], value, role,
            'direct_explicit' if accepted else 'bounded_inference',
            quote if quote_ok else None, 'model_quote', annotation.get('source_hash') or '', 'model_generation')
        item['accepted'] = accepted
        item['candidate'] = not accepted
        annotation['facts'].append(item)
        if accepted:
            facet = annotation['dimensions'][raw['dimension']]
            facet['state'] = 'known'
            facet['reason'] = None
            facet['values'].append(value if role == 'unknown' else {'id': value, 'role': role})
    annotation['fingerprint'] = digest({
        'record_id': annotation['record_id'], 'facts': [item['fact_id'] for item in annotation['facts']],
        'source_hash': annotation.get('source_hash'),
    })
    return annotation


def manifest_digest(ids, records):
    return digest({'ids': list(ids), 'records': records})


def load_manifest(path):
    raw = Path(path).read_bytes()
    payload = json.loads(raw)
    ids = payload.get('ids')
    if not isinstance(ids, list) or not ids or len(ids) > 48:
        raise SystemExit('manifest ids must be a list of 1 to 48 record ids')
    records = payload.get('records') or []
    expected = payload.get('sha256')
    actual = manifest_digest(ids, records)
    if expected != actual:
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


def generation_prompt(skill):
    return (
        '只根据下面给出的目录文字返回一个 JSON 对象，不要使用工具，不要补充未出现的事实。'
        'facts 的 quote 必须是给出文字里的连续原文。dimension 只能是 '
        + ', '.join(DIMENSIONS) + '。role 只能是 ' + ', '.join(TOOL_ROLES) + '。'
        '例子、禁止和替代不能写成 required。\n'
        + supplied_text(skill)
    )


def review_prompt(skill, annotation):
    visible = [{
        'dimension': item['dimension'], 'value_id': item['value_id'], 'role': item['role'],
        'quote': item.get('quote'), 'accepted': item.get('accepted'),
    } for item in annotation.get('facts') or []]
    return (
        '独立审核下面的候选标签。只根据给出的目录文字和候选 quote，不要沿用生成会话，不要看生成者的推理。'
        '返回 JSON，axes 的键必须是 ' + ', '.join(AXES) + '，值只能是 unknown、pass、limited、fail、not_applicable。'
        '没有固定源文就不要把 source_fidelity 写成 pass。\n'
        + json.dumps({'source': supplied_text(skill), 'candidates': visible}, ensure_ascii=False)
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
        if manifest:
            actual = manifest_digest(manifest.get('ids') or [], manifest.get('records') or [])
            if manifest.get('sha256') != actual:
                raise SystemExit('manifest sha256 does not match its ids and records')
        try:
            if hasattr(client, 'check_price'):
                client.check_price()
        except Exception as exc:
            return {'stopped': str(exc), 'calls': 0, 'model_calls_this_process': 0, 'writes_catalogue': False}
        rejected = []
        skipped_errors = []
        if manifest:
            by_id = {skill['id']: skill for skill in skills}
            declared = {row.get('id'): row.get('source_hash') for row in manifest.get('records') or [] if isinstance(row, dict)}
            selected = []
            for record_id in manifest['ids']:
                skill = by_id.get(record_id)
                if skill is None:
                    rejected.append({'id': record_id, 'reason': 'not_in_catalog'})
                    continue
                current_hash = source_digest(skill)
                expected_hash = declared.get(record_id)
                if expected_hash and expected_hash != current_hash:
                    rejected.append({'id': record_id, 'reason': 'stale_source_hash'})
                    continue
                selected.append(skill)
            covered = after or ''
        else:
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
                    base['facts'].append(fact(
                        skill['id'], {'level': 'skill'}, condition['dimension'], condition['value_id'],
                        condition['role'], 'direct_explicit', condition['quote'], 'supplied_text',
                        source_hash, 'closed_phrase'))
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
                        axes = payload.get('axes') if isinstance(payload.get('axes'), dict) else {}
                        text = supplied_text(skill)
                        if any(item.get('accepted') and (not item.get('quote') or item['quote'] not in text)
                               for item in base.get('facts') or []):
                            axes['source_fidelity'] = 'fail'
                        for raw in payload.get('facts') or []:
                            quote = raw.get('quote') if isinstance(raw, dict) else None
                            if quote and quote not in text:
                                axes['source_fidelity'] = 'fail'
                        grade = derive_grade({
                            'independent': True, 'source_scope': 'fixed_source', 'axes': axes,
                            'na_evidence': {}, 'diversity': 'same_model_new_session'}, [])
                        base['quality'] = {'grade': grade['grade'], 'model_review': 'completed',
                                           'execution': 'not_run', 'diversity': 'same_model_new_session'}
                else:
                    absorb_facts(base, payload, supplied_text(skill))
                    base['quality'] = {'grade': 'U', 'model_review': 'draft_searchable', 'execution': 'not_run'}
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
    parser.add_argument('command', choices=('taxonomy', 'map', 'pilot', 'rollback', 'run', 'aliases'))
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
    parser.add_argument('--manifest', type=Path, help='JSON with ids, records, and sha256. run selects only these ids, checks the hash, and does not scan other records')
    parser.add_argument('--ids', help='Comma-separated record ids, 1 to 48. Selects only those ids. Does not check source hashes; use --manifest for the hashed list')
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
    if args.command == 'pilot':
        skills = load_catalog(args.database, with_tasks=False)[0] if args.database else []
        plan = plan_pilot(skills, args.limit or 48)
        plan['calls_model'] = False
        plan['read_by_run'] = False
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
    if args.command == 'run':
        from classify_unfiled import OpenCodeFreeClient
        if args.manifest and args.ids:
            raise SystemExit('pass only one of --manifest and --ids')
        manifest = None
        if args.manifest:
            manifest, _file_hash = load_manifest(args.manifest)
            skills = load_catalog(args.database, ids=manifest['ids'])[0]
        elif args.ids:
            ids = [piece.strip() for piece in args.ids.split(',') if piece.strip()]
            if not 1 <= len(ids) <= 48:
                raise SystemExit('--ids accepts 1 to 48 record ids')
            manifest = {'ids': ids, 'records': [], 'sha256': manifest_digest(ids, [])}
            skills = load_catalog(args.database, ids=ids)[0]
        else:
            skills = load_catalog(args.database)[0]
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
