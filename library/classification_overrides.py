"""Persistent browse-label overrides for the local catalogue.

The catalogue database is replaced on every rebuild. Reviewed directory
labels live in a JSON override file keyed by the representative source id
(the same 32-hex id stored on skills.id and sources.id). Rebuild applies an
override only when that source is still the representative and the pinned
identity and reviewed directory text still match. It does not change
SkillRecords, review status, tasks, provenance, or duplicate links.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import quote

from taxonomy import LABELS, LEGAL_TOPICS, TOPIC_PATTERNS, UNSORTED_TOPIC

ROOT = Path(__file__).resolve().parents[1]
DATABASE = ROOT / 'data/library/catalog.sqlite3'
OVERRIDES = ROOT / 'data/library/classification-overrides.json'
SCHEMA = 'atlas-classification-overrides/v1'
PROPOSAL_SCHEMA = 'atlas-material-classification-proposals/v1'
PRESERVE_LABELS = frozenset({'sample', 'rule', 'source'})
CATALOGUE_LABEL = 'catalogue'
ID = re.compile(r'[a-f0-9]{32}')
NOTE = '覆盖层只改本机目录的领域和专题。review 保持原值，不表示源文已审或方法有效。保留待定项不写入。'


def representative_key(collection_id, commit_sha):
    """Match normalization.batch_convert.key_of without importing the converter."""
    return hashlib.sha256((collection_id + '\0' + commit_sha).encode()).hexdigest()[:32]


def static_topics(category):
    topics = set(TOPIC_PATTERNS.get(category, {}))
    if category in LABELS and category != 'unfiled':
        topics.add(UNSORTED_TOPIC)
    if category == 'legal':
        topics.update(LEGAL_TOPICS.values())
    if category == 'integration':
        topics.add('通用 MCP 工具连接')
    return topics


def connect_readonly(path):
    if not Path(path).is_file():
        raise ValueError('本机目录不存在，先运行 python3 library/build.py')
    con = sqlite3.connect(f'file:{quote(str(path))}?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    return con


def catalogue_topics(path):
    """Topics already materialised for a category, including path-derived legal labels."""
    found = {}
    if not Path(path).is_file():
        return found
    try:
        con = connect_readonly(path)
    except (sqlite3.Error, ValueError):
        return found
    try:
        rows = con.execute('SELECT DISTINCT category, topic FROM skills')
        for row in rows:
            found.setdefault(row['category'], set()).add(row['topic'])
    except sqlite3.Error:
        return {}
    finally:
        con.close()
    return found


def topic_allowed(category, topic, known):
    if category not in LABELS or category == 'unfiled' or not isinstance(topic, str) or not topic:
        return False
    if topic in static_topics(category):
        return True
    return topic in known.get(category, ())


def content_view(title, summary, tasks):
    rows = []
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        goal = task.get('goal') or ''
        if not goal:
            continue
        applicability = task.get('applicability')
        if applicability is None:
            applicability = task.get('when') or ''
        rows.append({'goal': goal, 'applicability': applicability})
    return {'title': title or '', 'summary': summary or '', 'tasks': rows}


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'duplicate_json_key: {key}')
        result[key] = value
    return result


def strict_load(text):
    def reject(value):
        raise ValueError(f'non_json_constant: {value}')

    return json.loads(text, object_pairs_hook=_strict_object, parse_constant=reject)


def empty_store():
    return {'schema': SCHEMA, 'entries': {}}


def load_overrides(path):
    path = Path(path)
    if not path.exists():
        return empty_store()
    try:
        data = strict_load(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f'分类覆盖层无法读取: {path}') from exc
    if not isinstance(data, dict) or data.get('schema') != SCHEMA or not isinstance(data.get('entries'), dict):
        raise ValueError(f'分类覆盖层格式不符: {path}')
    for key, entry in data['entries'].items():
        if not ID.fullmatch(key) or not isinstance(entry, dict):
            raise ValueError(f'分类覆盖层格式不符: {path}')
    return data


def save_overrides(path, store):
    path = Path(path)
    payload = json.dumps(store, ensure_ascii=False, indent=2, sort_keys=True) + '\n'
    if path.is_file() and path.read_text(encoding='utf-8') == payload:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(payload, encoding='utf-8')
    temporary.replace(path)
    return True


def entry_edited(entry):
    target = {'category': entry.get('category'), 'topic': entry.get('topic'), 'label_from': entry.get('label_from')}
    return bool(entry.get('edited')) or target != entry.get('imported_target')


def _classification(row):
    return {'category': row['category'], 'topic': row['topic'], 'label_from': row['label_from'], 'review': row['review']}


def _identity_from_source(row):
    return {
        'collection_id': row['collection_id'], 'commit_sha': row['revision'], 'repository': row['repo'],
        'skill_path': row['path'], 'git_blob_sha': row['blob'], 'entry_sha256': row['sha256'],
    }


def _same_identity(pinned, current):
    keys = ('collection_id', 'commit_sha', 'repository', 'skill_path', 'git_blob_sha', 'entry_sha256')
    return all(pinned.get(key) == current.get(key) for key in keys)


def _tasks(con, skill_id):
    return [dict(row) for row in con.execute(
        'SELECT goal, applicability FROM catalog_tasks WHERE skill = ? ORDER BY position, id', (skill_id,)
    )]


def _sources(con, skill_id):
    return [dict(row) for row in con.execute(
        'SELECT id, skill, collection_id, repo, path, revision, blob, sha256, relationship FROM sources WHERE skill = ? OR id = ?',
        (skill_id, skill_id),
    )]


def load_catalogue_entry(con, skill_id):
    skill = con.execute('SELECT * FROM skills WHERE id = ?', (skill_id,)).fetchone()
    sources = _sources(con, skill_id)
    if skill is None:
        pointed = next((row for row in sources if row['id'] == skill_id), None)
        return None, sources, pointed
    representative = next((row for row in sources if row['id'] == skill_id and row['skill'] == skill_id), None)
    tasks = _tasks(con, skill_id)
    return skill, sources, representative


def _proposal_sources(record):
    sources = record.get('sources')
    if not isinstance(sources, list):
        return None, 'sources 不是列表'
    representatives = [item for item in sources if isinstance(item, dict) and item.get('relationship') == 'representative']
    if len(representatives) != 1:
        return None, '需要且只能有一条 relationship 为 representative 的来源'
    item = representatives[0]
    if not isinstance(item.get('repo'), str) or not isinstance(item.get('path'), str):
        return None, '代表来源缺少 repo 或 path'
    return item, None


def _target_label(current_label):
    if current_label in PRESERVE_LABELS:
        return current_label, True
    return CATALOGUE_LABEL, False


def _conflict(record, reason, detail):
    return {
        'id': record.get('id'), 'ordinal': record.get('ordinal'), 'title': record.get('title') or '',
        'reason': reason, 'detail': detail,
    }


def _source_notice(proposal_sources, catalogue_sources):
    def view(item, repo_key, path_key):
        return item.get(repo_key), item.get(path_key), item.get('relationship')

    proposal = sorted(view(item, 'repo', 'path') for item in proposal_sources if isinstance(item, dict))
    current = sorted(view(item, 'repo', 'path') for item in catalogue_sources)
    if proposal == current:
        return None
    return '来源副本集合与提案快照不同；代表身份和目录内容仍一致，未改副本关系'


def plan_import(proposal, con, store, known_topics):
    """Decide each proposal row. Conflicts are skipped and never overwrite."""
    records = proposal['records']
    changes, conflicts, retained, pending = [], [], [], []
    entries = store.get('entries') or {}
    for record in records:
        decision = record.get('decision')
        if decision == 'retain_unfiled':
            retained.append({
                'id': record.get('id'), 'ordinal': record.get('ordinal'), 'title': record.get('title') or '',
                'retention_reason': record.get('retention_reason'),
            })
            continue
        if decision != 'propose_existing_category':
            conflicts.append(_conflict(record, 'malformed_record', 'decision 必须是 propose_existing_category 或 retain_unfiled'))
            continue
        original = record.get('original_classification')
        if not isinstance(original, dict) or any(
            not isinstance(original.get(key), str)
            for key in ('category', 'topic', 'label_from', 'review')
        ):
            conflicts.append(_conflict(record, 'malformed_record', '缺少 original_classification'))
            continue
        if not isinstance(record.get('title'), str) or not isinstance(record.get('summary'), str) or not isinstance(record.get('tasks'), list):
            conflicts.append(_conflict(record, 'malformed_record', '缺少标题、摘要或任务'))
            continue
        flags = record.get('quality_flags') or []
        if not isinstance(flags, list) or not all(isinstance(flag, str) for flag in flags):
            conflicts.append(_conflict(record, 'malformed_record', 'quality_flags 必须是字符串列表'))
            continue
        representative, source_error = _proposal_sources(record)
        if source_error:
            conflicts.append(_conflict(record, 'malformed_record', source_error))
            continue
        category, topic = record.get('proposed_category'), record.get('proposed_topic')
        if category not in LABELS or category == 'unfiled':
            conflicts.append(_conflict(record, 'invalid_category', f'不在既有领域中: {category}'))
            continue
        if not topic_allowed(category, topic, known_topics):
            conflicts.append(_conflict(record, 'invalid_topic', f'{category} 下没有既有专题: {topic}'))
            continue
        skill, catalogue_sources, representative_row = load_catalogue_entry(con, record['id'])
        if skill is None:
            pointed = con.execute(
                'SELECT skill, relationship FROM sources WHERE id = ?', (record['id'],)
            ).fetchone()
            if pointed is not None:
                conflicts.append(_conflict(record, 'not_current_representative', '该身份仍在来源表中，但已不是当前代表'))
            else:
                same_path = con.execute(
                    '''SELECT id FROM sources WHERE relationship = ? AND repo = ? AND path = ?''',
                    ('representative', representative['repo'], representative['path']),
                ).fetchall()
                if same_path:
                    conflicts.append(_conflict(
                        record, 'version_mismatch',
                        f"同路径当前代表是 {same_path[0]['id']}，提案身份 {record['id']} 已不是该版本",
                    ))
                else:
                    conflicts.append(_conflict(record, 'not_in_catalogue', '目录中没有这个代表身份'))
            continue
        if representative_row is None or representative_row['relationship'] != 'representative':
            conflicts.append(_conflict(record, 'not_current_representative', '该身份已不是当前代表'))
            continue
        try:
            pinned = _identity_from_source(representative_row)
            computed = representative_key(pinned['collection_id'], pinned['commit_sha'])
        except (KeyError, TypeError):
            conflicts.append(_conflict(record, 'identity_mismatch', '目录代表来源缺身份字段'))
            continue
        if computed != record['id'] or representative_row['id'] != record['id']:
            conflicts.append(_conflict(record, 'identity_mismatch', '目录代表键与 collection_id/commit 不一致'))
            continue
        if pinned['repository'] != representative['repo'] or pinned['skill_path'] != representative['path']:
            conflicts.append(_conflict(record, 'identity_mismatch', '代表仓库或路径与提案不一致'))
            continue
        if record.get('repo') not in (None, pinned['repository']):
            conflicts.append(_conflict(record, 'identity_mismatch', '条目 repo 与代表来源不一致'))
            continue
        current_content = content_view(skill['title'], skill['summary'], _tasks(con, record['id']))
        proposal_content = content_view(record['title'], record['summary'], record['tasks'])
        if current_content != proposal_content:
            conflicts.append(_conflict(record, 'content_drift', '标题、摘要或任务/适用条件与目录当前值不一致'))
            continue
        live = _classification(skill)
        expected = {key: original[key] for key in ('category', 'topic', 'label_from', 'review')}
        if live['label_from'] in PRESERVE_LABELS or expected['label_from'] in PRESERVE_LABELS:
            if live['label_from'] != expected['label_from']:
                conflicts.append(_conflict(
                    record, 'expected_original_mismatch',
                    '当前 source/rule/sample 标签与提案预期原值不同，未覆盖',
                ))
                continue
            label_from, preserve = _target_label(live['label_from'])
        else:
            label_from, preserve = CATALOGUE_LABEL, False
        target = {'category': category, 'topic': topic, 'label_from': label_from}
        existing = entries.get(record['id'])
        if existing is not None:
            if not _same_identity(existing.get('identity') or {}, pinned):
                conflicts.append(_conflict(record, 'identity_mismatch', '已有覆盖层的代表身份与当前目录不一致'))
                continue
            if (existing.get('base') or {}).get('review') != live['review']:
                conflicts.append(_conflict(record, 'expected_original_mismatch', '目录 review 与导入时钉住的原值不同，未改写覆盖'))
                continue
            if content_view(existing.get('content', {}).get('title'), existing.get('content', {}).get('summary'),
                            existing.get('content', {}).get('tasks')) != current_content:
                conflicts.append(_conflict(record, 'content_drift', '已有覆盖层钉住的目录内容与当前目录不一致'))
                continue
            existing_target = {'category': existing.get('category'), 'topic': existing.get('topic'), 'label_from': existing.get('label_from')}
            if entry_edited(existing) and existing_target != target:
                conflicts.append(_conflict(record, 'local_edit', '该身份已有人工改过的覆盖，未覆盖'))
                continue
            if existing_target != target:
                conflicts.append(_conflict(record, 'override_conflict', '该身份已有不同的分类覆盖，未覆盖'))
                continue
            item = {
                'id': record['id'], 'ordinal': record.get('ordinal'), 'title': skill['title'],
                'from': live, 'to': {**target, 'review': live['review']}, 'status': 'unchanged',
                'catalogue_matches': live['category'] == target['category'] and live['topic'] == target['topic'] and live['label_from'] == target['label_from'],
            }
            notice = _source_notice(record.get('sources') or [], catalogue_sources)
            if notice:
                item['source_set_notice'] = notice
            if not item['catalogue_matches']:
                pending.append({'id': record['id'], 'ordinal': record.get('ordinal'), 'title': skill['title']})
            changes.append(item)
            continue
        if live != expected:
            conflicts.append(_conflict(
                record, 'expected_original_mismatch',
                f"目录当前为 {live['category']}/{live['topic']}/{live['label_from']}/{live['review']}，"
                f"提案预期原值为 {expected['category']}/{expected['topic']}/{expected['label_from']}/{expected['review']}",
            ))
            continue
        entry = {
            'id': record['id'], 'category': category, 'topic': topic, 'label_from': label_from,
            'preserve_label': preserve, 'imported_target': dict(target), 'base': expected, 'identity': pinned,
            'content': proposal_content, 'batch_id': proposal['batch_id'],
            'proposal_sha256': proposal['batch_id'], 'proposal_created_at': proposal.get('created_at'),
            'proposal_catalog_as_of': proposal.get('catalog_as_of'), 'ordinal': record.get('ordinal'),
            'decision': decision, 'quality_flags': flags,
            'source_record_sha256': record.get('source_record_sha256'), 'reason': record.get('reason'),
            'edited': False,
        }
        item = {
            'id': record['id'], 'ordinal': record.get('ordinal'), 'title': skill['title'],
            'from': live, 'to': {**target, 'review': live['review']}, 'status': 'apply',
            'catalogue_matches': False, 'entry': entry,
        }
        notice = _source_notice(record.get('sources') or [], catalogue_sources)
        if notice:
            item['source_set_notice'] = notice
        changes.append(item)
    return {'changes': changes, 'conflicts': conflicts, 'retained': retained, 'pending_rebuild': pending}


def apply_plan(store, plan):
    entries = dict(store.get('entries') or {})
    for change in plan['changes']:
        if change['status'] == 'apply':
            entries[change['id']] = change['entry']
    return {'schema': SCHEMA, 'entries': entries}


def matching_batch(store, batch):
    identifiers = {entry.get('batch_id') for entry in store.get('entries', {}).values() if entry.get('batch_id')}
    if batch in identifiers:
        return batch
    matches = sorted(item for item in identifiers if isinstance(item, str) and item.startswith(batch))
    if len(batch) >= 8 and len(matches) == 1:
        return matches[0]
    if not matches:
        raise ValueError('找不到这个导入批次')
    raise ValueError('批次前缀对应多批，请写完整 batch_id')


def plan_rollback(store, batch):
    batch_id = matching_batch(store, batch)
    remove, preserve, untouched = [], [], []
    for key, entry in store.get('entries', {}).items():
        if entry.get('batch_id') != batch_id:
            untouched.append(key)
            continue
        if entry_edited(entry):
            preserve.append({'id': key, 'category': entry.get('category'), 'topic': entry.get('topic'), 'label_from': entry.get('label_from')})
        else:
            remove.append(key)
    return {'batch_id': batch_id, 'remove': sorted(remove), 'preserve': preserve, 'untouched': sorted(untouched)}


def apply_rollback(store, rollback):
    removing = set(rollback['remove'])
    entries = {key: value for key, value in store.get('entries', {}).items() if key not in removing}
    return {'schema': store.get('schema', SCHEMA), 'entries': entries}


def assign_browse_label(store, source, title, summary, tasks, category, label_from, topic, known_topics):
    """Return category, label, topic, effect, reason. effect is applied or automatic."""
    automatic = (category, label_from, topic, 'automatic', None)
    try:
        key = representative_key(source['collection_id'], source['commit_sha'])
    except (KeyError, TypeError):
        return automatic
    entry = (store.get('entries') or {}).get(key)
    if entry is None:
        return automatic
    pinned = entry.get('identity') or {}
    current = {
        'collection_id': source.get('collection_id'), 'commit_sha': source.get('commit_sha'),
        'repository': source.get('repository'), 'skill_path': source.get('skill_path'),
        'git_blob_sha': source.get('git_blob_sha'), 'entry_sha256': source.get('entry_sha256'),
    }
    if not _same_identity(pinned, current):
        return category, label_from, topic, 'automatic', 'identity_mismatch'
    stored = entry.get('content') or {}
    if content_view(title, summary, tasks) != content_view(stored.get('title'), stored.get('summary'), stored.get('tasks')):
        return category, label_from, topic, 'automatic', 'content_drift'
    if not topic_allowed(entry.get('category'), entry.get('topic'), known_topics):
        return category, label_from, topic, 'automatic', 'invalid_topic'
    if entry.get('preserve_label'):
        if label_from != entry.get('label_from'):
            return category, label_from, topic, 'automatic', 'label_changed'
        return entry['category'], label_from, entry['topic'], 'applied', None
    if entry.get('label_from') not in {CATALOGUE_LABEL, *PRESERVE_LABELS, 'title', 'unfiled'}:
        return category, label_from, topic, 'automatic', 'invalid_override'
    return entry['category'], entry['label_from'], entry['topic'], 'applied', None


def load_proposal(path):
    path = Path(path)
    try:
        raw = path.read_bytes()
        data = strict_load(raw.decode('utf-8'))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f'提案文件无法读取: {exc}') from exc
    if not isinstance(data, dict):
        raise ValueError('提案文件必须是对象')
    metadata = data.get('metadata')
    records = data.get('records')
    if not isinstance(metadata, dict) or metadata.get('schema') != PROPOSAL_SCHEMA:
        raise ValueError(f'提案 schema 必须是 {PROPOSAL_SCHEMA}')
    if not isinstance(records, list):
        raise ValueError('提案 records 必须是列表')
    seen = set()
    for index, record in enumerate(records, 1):
        if not isinstance(record, dict) or not isinstance(record.get('id'), str) or not ID.fullmatch(record['id']):
            raise ValueError(f'第 {index} 条缺少 32 位代表身份')
        if record['id'] in seen:
            raise ValueError(f'重复的代表身份: {record["id"]}')
        seen.add(record['id'])
    return {
        'records': records, 'batch_id': hashlib.sha256(raw).hexdigest(),
        'created_at': metadata.get('created_at_shanghai'),
        'catalog_as_of': metadata.get('catalog_as_of_shanghai'),
        'metadata': metadata,
    }


def parse_shanghai_stamp(value):
    if not isinstance(value, str) or not value.endswith(' UTC+8'):
        return None
    try:
        parsed = datetime.strptime(value[:-len(' UTC+8')], '%Y-%m-%d %H:%M:%S')
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone(timedelta(hours=8)))


def snapshot_notice(proposal_stamp, catalogue_stamp):
    proposal_time = parse_shanghai_stamp(proposal_stamp)
    catalogue_time = None
    if isinstance(catalogue_stamp, str):
        try:
            catalogue_time = datetime.fromisoformat(catalogue_stamp)
        except ValueError:
            catalogue_time = None
        if catalogue_time is not None and catalogue_time.tzinfo is None:
            catalogue_time = catalogue_time.replace(tzinfo=timezone.utc)
    if proposal_time is None or catalogue_time is None:
        return '无法对齐提案快照时间与目录 as_of；已按每条代表身份、原分类和目录内容核对，不一致则跳过。'
    if proposal_time != catalogue_time:
        return '目录 as_of 与提案快照时间不同；已按每条代表身份、原分类和目录内容核对，不一致则跳过，不整批强行覆盖。'
    return None


def catalogue_stamp(con):
    try:
        row = con.execute("SELECT value FROM meta WHERE key = 'as_of'").fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    try:
        return json.loads(row['value'])
    except (TypeError, json.JSONDecodeError):
        return row['value']


def report_counts(plan):
    return {
        'apply': sum(item['status'] == 'apply' for item in plan['changes']),
        'unchanged': sum(item['status'] == 'unchanged' for item in plan['changes']),
        'pending_rebuild': len(plan['pending_rebuild']),
        'conflicts': len(plan['conflicts']),
        'retained': len(plan['retained']),
    }


def public_changes(plan):
    return [{key: value for key, value in change.items() if key != 'entry'} for change in plan['changes']]


def unused_overrides(store, applied_ids, suspended_ids, member_ids):
    rows = []
    for key in sorted((store.get('entries') or {})):
        if key in applied_ids or key in suspended_ids:
            continue
        reason = 'not_representative' if key in member_ids else 'not_in_catalogue'
        rows.append({'id': key, 'reason': reason})
    return rows
