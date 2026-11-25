#!/usr/bin/env python3
"""Versioned skill enrichment sidecar. It does not alter classification v2 records."""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
from pathlib import Path
import sqlite3

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


def map_record(skill, tasks, gaps=None, source_hash=''):
    """Copy the existing category and only mark legacy task tags as unverified."""
    record_id = skill['id']
    dimensions = {name: {'state': 'unknown', 'reason': 'not_yet_annotated', 'values': []} for name in DIMENSIONS}
    facts = []
    primary = {
        'domain_id': skill.get('category') if skill.get('category') in LABELS else 'unfiled',
        'topic': skill.get('topic'),
        'label_from': skill.get('label_from'),
        'topic_resolved': bool(skill.get('topic')) and skill.get('topic') != UNSORTED_TOPIC and skill.get('category') != 'unfiled',
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
    if inputs == [] or inputs is None:
        dimensions['input_kind'] = {'state': 'unknown', 'reason': 'not_in_source', 'values': []}
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
    for item in facts:
        grouped.setdefault(scope_key(item), []).append(item)
    scopes = grouped.values() if same_task else [facts]
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
        finally:
            handle.close()

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
            folder = self.out / 'ledger'
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / (kind + '.jsonl')
            line = json.dumps(entry, ensure_ascii=False)
            existing = path.read_text(encoding='utf-8') if path.is_file() else ''
            if line + '\n' in existing or line in existing.splitlines():
                return {'appended': False, 'idempotent': True}
            with path.open('a', encoding='utf-8') as handle_out:
                handle_out.write(line + '\n')
            return {'appended': True}
        finally:
            handle.close()


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


def load_catalog(database, limit=None):
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    rows = connection.execute('SELECT id, category, topic, label_from, review FROM skills ORDER BY id')
    skills = []
    for row in rows:
        tasks = [dict(item) for item in connection.execute(
            'SELECT id, position, goal, applicability, outputs, tags FROM catalog_tasks WHERE skill = ? ORDER BY position',
            (row['id'],))]
        for task in tasks:
            try:
                task['tags'] = json.loads(task['tags'])
            except (TypeError, json.JSONDecodeError):
                task['tags'] = {}
        skills.append({**dict(row), 'tasks': tasks})
        if limit and len(skills) >= limit:
            break
    connection.close()
    return skills


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


def run(argv=None):
    parser = argparse.ArgumentParser(description='Build enrichment sidecars. Does not call a model or write the catalogue.')
    parser.add_argument('command', choices=('taxonomy', 'map', 'pilot', 'rollback'))
    parser.add_argument('--database', type=Path)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--snapshot')
    parser.add_argument('--limit', type=int, default=48)
    args = parser.parse_args(argv)
    if args.command == 'taxonomy':
        report = taxonomy_reconciliation()
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps({key: report[key] for key in (
            'domain_count', 'allowed_pairs', 'unsorted_pairs', 'pairs_excluding_unsorted', 'unique_topic_strings')},
            ensure_ascii=False, indent=2))
        return 0
    if args.command == 'pilot':
        skills = load_catalog(args.database) if args.database else []
        print(json.dumps(plan_pilot(skills, args.limit), ensure_ascii=False, indent=2))
        return 0
    if args.command == 'rollback':
        print(json.dumps(Store(args.out).rollback(args.snapshot, dry_run=not args.apply), ensure_ascii=False, indent=2))
        return 0
    skills = load_catalog(args.database)
    annotations = [map_record(skill, skill['tasks'], source_hash=skill['id']) for skill in skills]
    result = Store(args.out).apply(annotations, 'deterministic-map', dry_run=not args.apply)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(run())
