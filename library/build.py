#!/usr/bin/env python3
"""Build a replaceable local catalogue from saved SkillRecords."""
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path, PurePosixPath
import posixpath
import sqlite3
import sys
import tempfile

from search import search_text
from taxonomy import BY_LABEL, classify, topic_for
from markdown_it import MarkdownIt

ROOT = Path(__file__).resolve().parents[1]
NORMALIZED = ROOT / 'data/normalized'
RUN = NORMALIZED / 'full-001'
INDEX = ROOT / 'data/cumulative/skills.jsonl.gz'
DATABASE = ROOT / 'data/library/catalog.sqlite3'
sys.path.insert(0, str(ROOT / 'normalization'))
from batch_convert import key_of, now, repository_manifest, selected_rows, source_of, valid_duplicate
from duplicate_skills import formatting_probe, markdown_structure


@dataclass
class Saved:
    path: Path
    source: dict
    title: str
    summary: str
    goals: str
    search: str
    category: str
    label_from: str
    review: str
    generated: str
    reads: list
    covered_steps: int


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def identity(source):
    return source['collection_id'], source['commit_sha']


def gather_records(as_of):
    cutoff = datetime.fromisoformat(as_of)
    labels = {
        identity(sample['source']): BY_LABEL[sample['primary_category']]
        for sample in read_json(ROOT / 'docs/content-distribution-20261118.sample.json')['sample']
    }
    paths = sorted((NORMALIZED / 'batch-001/records').glob('*.json'))
    paths += sorted((RUN / 'batches').glob('*/records/*.json'))
    records, ignored, audit = {}, Counter(), Counter()
    for path in paths:
        try:
            record = read_json(path)
            source, content, extraction = (record[k] for k in ('source', 'content', 'extraction'))
            if record['schema_version'] != '0.1.1' or path.stem != key_of(source):
                ignored['invalid_record_identity'] += 1
                continue
            generated = extraction['generated_at']
            if datetime.fromisoformat(generated) > cutoff:
                continue
            tasks = content.get('tasks') or []
            label = labels.get(identity(source))
            category, label_from = classify(source, content, label)
            if label is not None:
                guessed, _ = classify(source, content)
                audit['reference_sample'] += 1
                audit['classified_without_sample'] += guessed != 'unfiled'
                audit['matched_without_sample'] += guessed == label
            title = content.get('title') or content.get('name') or PurePosixPath(source['skill_path']).parent.name
            goals = '\n'.join(t.get('goal') or '' for t in tasks)
            saved = Saved(path, source, title, content.get('summary') or '', goals,
                          content.get('name') or '', category, label_from, extraction['review_status'],
                          generated, extraction['files_read'], sum(len(t.get('steps') or []) for t in tasks))
            if path.stem in records:
                ignored['repeated_record_identity'] += 1
                continue
            records[path.stem] = saved
        except (OSError, UnicodeError, ValueError, KeyError, TypeError, AttributeError):
            ignored['unreadable_record'] += 1
    return records, ignored, audit


def find_aliases():
    for path in sorted((RUN / 'batches').glob('*/state.json')):
        for value in read_json(path).values():
            if value.get('state') == 'skipped_duplicate':
                yield value, path.parent


def package_signature(saved, row, entry_structure=None):
    """Compare fixed file inventories plus delivered path and range coverage."""
    manifest, files = repository_manifest(INDEX.parent / row['repository_manifest'])
    if (manifest['repository'], manifest['commit_sha']) != (row['repository'], row['commit_sha']):
        raise ValueError('Manifest identity mismatch')
    entry = row['skill_path']
    base = posixpath.dirname(entry)
    package = row.get('local_package_files') or []
    if entry not in package or files[entry]['git_blob_sha'] != row['git_blob_sha']:
        raise ValueError('Missing entry in package manifest')

    def file_key(name):
        item = files[name]
        blob = item.get('git_blob_sha')
        if not blob:
            raise ValueError('Missing file identity')
        marker = entry_structure if name == entry and entry_structure else blob
        return [posixpath.relpath(name, base), item['status'], marker]

    payload = [
        entry_structure or row['sha256'],
        sorted(file_key(name) for name in package),
        sorted([file_key(read['path']), read['coverage'], read.get('ranges')] for read in saved.reads),
        sorted(posixpath.relpath(name, base) for name in row.get('local_package_files_not_exported', [])),
        row.get('external_dependency_status'),
    ]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def group_records(records, rows, ignored):
    bodies = Counter(r.source['entry_sha256'] for r in records.values())
    groups = defaultdict(list)
    for r in sorted(records.values(), key=lambda r: rows[identity(r.source)]['repository_manifest']):
        source = r.source
        key = key_of(source)
        if bodies[source['entry_sha256']] > 1:
            try:
                key = package_signature(r, rows[identity(source)])
            except (OSError, UnicodeError, ValueError, KeyError, TypeError):
                ignored['package_not_verified'] += 1
        groups[key].append(r)
    return groups, bodies


def representative(records):
    return min(records, key=lambda r: (
        r.review != 'reviewed', r.label_from != 'sample', -r.covered_steps, key_of(r.source),
    ))


def group_format_variants(groups, rows, ignored):
    parser = MarkdownIt('commonmark').enable('table')
    buckets = defaultdict(list)
    for key, group in groups.items():
        sample = representative(group)
        row = rows[identity(sample.source)]
        try:
            raw = (INDEX.parent / row['raw_path']).resolve()
            if not raw.is_relative_to(ROOT):
                raise ValueError('Invalid original path')
            data = raw.read_bytes()
            if hashlib.sha256(data).hexdigest() != row['sha256']:
                raise ValueError('Entry checksum mismatch')
            text = data.decode('utf-8')
        except (OSError, ValueError, KeyError, TypeError):
            ignored['format_not_verified'] += 1
            continue
        buckets[formatting_probe(text)].append((key, sample, row))

    verified = {}
    for bucket in buckets.values():
        if len({row['sha256'] for _, _, row in bucket}) < 2:
            continue
        for key, sample, row in bucket:
            if any(f['coverage'] != 'full' for f in sample.reads):
                continue
            try:
                data = (INDEX.parent / row['raw_path']).read_bytes()
                if hashlib.sha256(data).hexdigest() != row['sha256']:
                    continue
                structure = markdown_structure(data.decode('utf-8'), parser)
                if structure[0] != 'markdown':
                    continue
                signature = package_signature(sample, row, json.dumps(structure, sort_keys=True, ensure_ascii=False))
            except (OSError, ValueError, KeyError, TypeError):
                ignored['format_not_verified'] += 1
                continue
            previous = verified.setdefault(signature, key)
            if previous != key:
                groups[previous].extend(groups.pop(key))


SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE skills (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, summary TEXT NOT NULL,
    category TEXT NOT NULL, label_from TEXT NOT NULL, topic TEXT NOT NULL,
    review TEXT NOT NULL, generated TEXT NOT NULL, repo TEXT NOT NULL,
    record_path TEXT NOT NULL, source_count INTEGER NOT NULL,
    search_title TEXT NOT NULL, search_text TEXT NOT NULL
);
CREATE INDEX skills_category ON skills(category);
CREATE INDEX skills_generated ON skills(generated);
CREATE INDEX skills_topic ON skills(topic);
CREATE TABLE sources (
    id TEXT PRIMARY KEY, skill TEXT NOT NULL REFERENCES skills(id), collection_id TEXT NOT NULL,
    repo TEXT NOT NULL, path TEXT NOT NULL, revision TEXT NOT NULL, blob TEXT NOT NULL,
    sha256 TEXT NOT NULL, license TEXT NOT NULL, raw_path TEXT,
    record_path TEXT, relationship TEXT NOT NULL
);
CREATE INDEX sources_skill ON sources(skill);
CREATE INDEX sources_repo ON sources(repo, skill);
"""


def build():
    as_of = now()
    records, ignored, audit = gather_records(as_of)
    aliases = list(find_aliases())
    rows = selected_rows(INDEX, [r.source for r in records.values()] + [a['source'] for a, _ in aliases])
    invalid = [key for key, saved in records.items()
               if identity(saved.source) not in rows or source_of(rows[identity(saved.source)]) != saved.source]
    for key in invalid:
        del records[key]
    ignored['index_mismatch'] += len(invalid)
    if not records:
        raise ValueError('No saved SkillRecords match the fixed collection index')
    groups, bodies = group_records(records, rows, ignored)
    package_groups = len(groups)
    group_format_variants(groups, rows, ignored)
    schema = read_json(ROOT / 'normalization/skill-record.v0.1.1.schema.json')
    source_to_rep = {}
    group_rows = {}
    for grouped in groups.values():
        r = representative(grouped)
        key = key_of(r.source)
        group_rows[key] = (r, grouped, [])
        source_to_rep.update((key_of(member.source), key) for member in grouped)
    seen = set(source_to_rep)
    for state, directory in aliases:
        source = state['source']
        key = key_of(source)
        row = rows.get(identity(source))
        target = state.get('duplicate_of') or {}
        target_source = target.get('source')
        if key in seen or not row or source_of(row) != source or not target_source:
            continue
        rep = source_to_rep.get(key_of(target_source))
        if rep is None or not valid_duplicate(state, directory, schema):
            ignored['alias_not_verified'] += 1
            continue
        group_rows[rep][2].append((row, target.get('match', 'prior_alias')))
        seen.add(key)

    DATABASE.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=DATABASE.parent, prefix='catalog-', suffix='.tmp', delete=False) as temp:
        pending = Path(temp.name)
    con = sqlite3.connect(pending)
    try:
        con.executescript(SCHEMA)
        category_counts = Counter()
        label_counts = Counter()
        distinct_repositories = set()
        with con:
            for key, (r, group, matches) in group_rows.items():
                topic = topic_for(r.source)
                category_counts[r.category] += 1
                label_counts[r.label_from] += 1
                member_rows = []
                for member in group:
                    match = 'same_package' if member.source['entry_sha256'] == r.source['entry_sha256'] else 'same_markdown_structure'
                    member_rows.append((rows[identity(member.source)], member, 'representative' if member is r else match))
                member_rows.extend((row, None, match) for row, match in matches)
                searches = [r.title, r.search, r.summary, r.goals, topic]
                searches += [row['repository'] + ' ' + row['skill_path'] for row, _, _ in member_rows]
                con.execute('INSERT INTO skills VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)', (
                    key, r.title, r.summary, r.category, r.label_from, topic, r.review, r.generated,
                    r.source['repository'], r.path.relative_to(ROOT).as_posix(), len(member_rows),
                    search_text(r.title + ' ' + r.search), search_text(' '.join(searches)),
                ))
                for row, saved, match in member_rows:
                    source = source_of(row)
                    distinct_repositories.add(source['repository'])
                    con.execute('INSERT INTO sources VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', (
                        key_of(source), key, source['collection_id'], source['repository'], source['skill_path'],
                        source['commit_sha'], source['git_blob_sha'], source['entry_sha256'],
                        json.dumps(row.get('license_candidates') or [], ensure_ascii=False), row.get('raw_path'),
                        saved.path.relative_to(ROOT).as_posix() if saved else None, match,
                    ))
            report = {
                'as_of': as_of, 'normalized': len(records), 'representatives': len(groups),
                'same_package': len(records) - package_groups, 'same_format': package_groups - len(groups),
                'entry_hash_groups': len(bodies),
                'prior_aliases': len(seen) - len(records), 'sources': len(seen),
                'repositories': len(distinct_repositories), 'categories': dict(category_counts),
                'labels': dict(label_counts), 'excluded': dict(ignored), 'category_check': dict(audit),
            }
            con.executemany('INSERT INTO meta VALUES (?,?)', ((k, json.dumps(v)) for k, v in report.items()))
        assert con.execute('PRAGMA quick_check').fetchone() == ('ok',)
        assert con.execute('SELECT COUNT(*) FROM sources').fetchone()[0] == report['sources']
        con.close()
        pending.replace(DATABASE)
    finally:
        con.close()
        pending.unlink(missing_ok=True)
    return report


if __name__ == '__main__':
    print(json.dumps(build(), ensure_ascii=False, indent=2))
