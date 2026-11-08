"""Run the frozen corpus queue as small resumable batches using batch_convert.

New record bodies stay local; snapshots publish provenance/paths and progress.
No upstream commands run and no source is refreshed or semantically deduplicated.
"""
import argparse
from collections import Counter
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import subprocess
import sys
import time
from urllib.parse import unquote

from batch_convert import HERE, key_of, load_selection, now, selected_rows, source_of, write_json, write_jsonl
from check_record import check_record, strict_json_loads

BASELINE = HERE.parent / 'data/normalized/batch-001'


def baseline_records(baseline):
    records = [json.loads(s) for s in (baseline / 'records.jsonl').read_text().splitlines() if s.strip()]
    schema = json.loads((HERE / 'skill-record.v0.1.1.schema.json').read_text())
    seen, result = set(), []
    for record in records:
        identity = (record['source']['collection_id'], record['source']['commit_sha'])
        if identity in seen:
            raise ValueError('Baseline repeats a source version')
        seen.add(identity)
        path = baseline / 'records' / (key_of({'collection_id': identity[0], 'commit_sha': identity[1]}) + '.json')
        if json.loads(path.read_text()) != record or check_record(record, schema)['errors']:
            raise ValueError('Baseline result missing, changed or invalid')
        result.append((path, record))
    return result


def read_queue(path):
    items = [json.loads(s) for s in path.read_text().splitlines() if s.strip()]
    ids = [(x['collection_id'], x['commit_sha']) for x in items]
    if len(ids) != len(set(ids)):
        raise ValueError('Queue repeats a source version')
    return items


def plan_references(row, index, budget=40000):
    """Direct local text guides only. Freeze choices; do not recursively crawl."""
    entry = (index.parent / row['raw_path']).read_bytes().decode('utf-8')
    manifest = json.loads((index.parent / row['repository_manifest']).read_text())
    files = {f['path']: f for f in manifest['files']}
    base = PurePosixPath(row['skill_path']).parent.as_posix()
    candidates = re.findall(r'\[[^\]]*\]\(([^)\s]+)', entry)
    candidates += re.findall(r'`([^`\n]+\.(?:md|txt))`', entry, flags=re.I)
    # A compiled guide is often the core expansion of a short Skill entry.
    for name in ['AGENTS.md', 'README.md']:
        if name in entry:
            candidates.insert(0, name)
    seen, chosen, omitted = set(), [], []
    root = (index.parent / row['collection_root']).resolve()
    for link in candidates:
        link = unquote(link.split('#', 1)[0].split('?', 1)[0]).strip('<>')
        if not link or '://' in link or link.startswith(('/', 'mailto:')):
            continue
        if not link.lower().endswith(('.md', '.txt')):
            continue
        possibilities = [posixpath.normpath(posixpath.join(base, link)), posixpath.normpath(link)]
        path = next((p for p in possibilities if p in files), possibilities[0])
        if path == row['skill_path'] or path in seen:
            continue
        seen.add(path)
        if PurePosixPath(path).name.lower().startswith(('license', 'notice', 'changelog', 'contributing')):
            continue
        f = files.get(path)
        if not f or f.get('status') != 'saved':
            omitted.append({'path': path, 'reason': f.get('status') if f else 'path_not_in_manifest'})
            continue
        size = (root / f['raw_path']).stat().st_size
        if len(chosen) >= 4 or size > budget:
            omitted.append({'path': path, 'reason': 'direct_guide_budget; entry remains full'})
            continue
        chosen.append({'path': path})
        budget -= size
    return chosen, omitted


def prepare_batch(items, index, directory):
    selection = directory / 'selection.json'
    if selection.exists():
        saved = load_selection(selection)
        if [(x['collection_id'], x['commit_sha']) for x in saved] != [(x['collection_id'], x['commit_sha']) for x in items]:
            raise ValueError('Existing batch selection differs from frozen queue')
        return selection
    rows = selected_rows(index, items)
    planned = []
    for item in items:
        row = rows.get((item['collection_id'], item['commit_sha']))
        result = dict(item, references=[])
        if row:
            try:
                result['references'], omitted = plan_references(row, index)
                result['reference_selection'] = {'policy': 'direct local markdown/text guides, at most four / 40000 bytes; no recursion',
                                                 'not_supplied': omitted}
            except (OSError, UnicodeError, ValueError) as exc:
                result['reference_selection'] = {'error': str(exc)}
        planned.append(result)
    write_json(selection, {'items': planned})
    return selection


def wait_for_batch(directory):
    """A crashed/restarted queue parent must not duplicate its surviving child."""
    lock_path = directory / '.run.lock'
    if lock_path.exists():
        with lock_path.open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)


def snapshot(args):
    """One immutable, content-free delivery checkpoint; running files are atomic."""
    items = read_queue(args.queue)
    allowed = {(x['collection_id'], x['commit_sha']) for x in items}
    checkpoint = args.out / 'checkpoints' / args.snapshot
    baseline = baseline_records(args.baseline)
    baseline_ids = {(r['source']['collection_id'], r['source']['commit_sha']) for _,r in baseline}
    if baseline_ids & allowed:
        raise ValueError('Frozen queue repeats a completed baseline source version')
    identities, index_rows = set(), []
    paths = [p for p, _ in baseline] + sorted((args.out / 'batches').glob('*/records/*.json'))
    schema = json.loads((HERE / 'skill-record.v0.1.1.schema.json').read_text())
    state_cache = {}
    for path in paths:
        data = path.read_bytes()
        record = json.loads(data)
        if check_record(record, schema)['errors']:
            raise ValueError('Invalid saved record: ' + str(path))
        source = record['source']
        identity = (source['collection_id'], source['commit_sha'])
        if identity in identities:
            raise ValueError('Duplicate normalized source version')
        if path.is_relative_to(args.out) and 'batches' in path.relative_to(args.out).parts:
            if identity not in allowed:
                raise ValueError('Record outside frozen queue')
        directory = path.parent.parent
        if directory not in state_cache:
            state_path = directory / 'state.json'
            state_cache[directory] = json.loads(state_path.read_text()) if state_path.exists() else {}
        state = state_cache[directory].get(key_of({'collection_id': identity[0], 'commit_sha': identity[1]}), {})
        attempt = directory / state['attempt'] if 'attempt' in state else None
        if attempt is not None and json.loads((attempt / 'metadata.json').read_text()).get('state') != 'succeeded':
            attempt = None
        if attempt is None:
            # Record creation precedes state.json publication. A live snapshot
            # or a crash between those writes can recover from the accepted
            # attempt, exactly as the batch runner recovers the saved record.
            candidates = directory / 'attempts' / key_of({'collection_id': identity[0], 'commit_sha': identity[1]})
            for candidate in sorted(candidates.glob('*/metadata.json'), key=lambda p: int(p.parent.name), reverse=True):
                meta = json.loads(candidate.read_text())
                if meta.get('state') == 'succeeded':
                    attempt = candidate.parent
                    break
        if attempt is None:
            raise ValueError('Saved record has no retained model attempt: ' + str(path))
        response = strict_json_loads((attempt / 'response.txt').read_text())
        if record['content'] != response['content'] or record['extraction']['gaps'] != response['gaps']:
            raise ValueError('Saved content differs from retained model response: ' + str(path))
        identities.add(identity)
        index_rows.append({'source': source, 'record_path': os.path.relpath(path, checkpoint),
                           'record_sha256': hashlib.sha256(data).hexdigest(),
                           'producer': record['extraction'].get('producer'),
                           'review_status': record['extraction']['review_status'],
                           'execution_status': record['extraction']['execution_status']})
    wanted = [{'collection_id': r['source']['collection_id'], 'commit_sha': r['source']['commit_sha']} for r in index_rows]
    collected = selected_rows(args.index, wanted)
    for row in index_rows:
        identity = (row['source']['collection_id'], row['source']['commit_sha'])
        if identity not in collected or row['source'] != source_of(collected[identity]):
            raise ValueError('Snapshot source differs from actual collection index')
    failed = {}
    totals = Counter()
    by_model = {}
    sessions, model_sessions, unknown, unfinished = 0, 0, 0, 0
    for path in sorted((args.out / 'batches').glob('*/state.json')):
        for state in json.loads(path.read_text()).values():
            if 'collection_id' not in state:
                continue
            identity = (state['collection_id'], state['commit_sha'])
            if state['state'] == 'failed' and identity not in identities:
                failed[identity] = {'failure_stage': state.get('failure_stage'), 'detail_path': os.path.relpath(path, checkpoint)}
    for path in sorted((args.out / 'batches').glob('*/attempts/*/*/metadata.json')):
        d = json.loads(path.read_text())
        unfinished += not bool(d.get('finished_at'))
        if not d.get('session_id'):
            continue
        sessions += 1
        usage = d.get('usage', {})
        model_sessions += bool(usage.get('turns'))
        unknown += not usage.get('available', False)
        model = d.get('model', 'unknown')
        model_totals = by_model.setdefault(model, Counter())
        model_totals['sessions'] += 1
        for turn in usage.get('turns', []):
            t = turn['tokens']
            for k in ['input', 'output', 'reasoning']:
                totals[k] += t.get(k, 0) or 0
                model_totals[k] += t.get(k, 0) or 0
            for k in ['read', 'write']:
                totals['cache_' + k] += t.get('cache', {}).get(k, 0) or 0
                model_totals['cache_' + k] += t.get('cache', {}).get(k, 0) or 0
    remaining = [dict(i, state='failed' if (i['collection_id'], i['commit_sha']) in failed else 'pending',
                      **failed.get((i['collection_id'], i['commit_sha']), {}))
                 for i in items if (i['collection_id'], i['commit_sha']) not in identities]
    checkpoint.mkdir(parents=True, exist_ok=False)
    write_jsonl(checkpoint / 'index.jsonl', index_rows)
    write_jsonl(checkpoint / 'remaining.jsonl', remaining)
    report = {'as_of': now(), 'corpus_saved_entries': len(items) + len(baseline), 'reused': len(baseline),
              'new_succeeded': len(identities) - len(baseline), 'total_succeeded': len(identities),
              'remaining': len(remaining), 'failed': len(failed),
              'pending_including_active_or_deferred': len(remaining) - len(failed),
              'usage_this_run': {'sessions': sessions, 'model_sessions': model_sessions,
                                 'sessions_without_usage': unknown, 'unfinished_attempt_records': unfinished,
                                 'tokens': dict(totals), 'by_model': {k: dict(v) for k,v in by_model.items()},
                                 'billing_cost': None},
              'record_storage': 'Local persistent files; record_path and detail_path are relative to this checkpoint directory. Public index contains provenance, hashes and paths only.',
              'queue_sha256': hashlib.sha256(args.queue.read_bytes()).hexdigest()}
    write_json(checkpoint / 'report.json', report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def run(args):
    queue = read_queue(args.queue)
    baseline = baseline_records(args.baseline)
    baseline_ids = {(r['source']['collection_id'], r['source']['commit_sha']) for _,r in baseline}
    if baseline_ids & {(i['collection_id'],i['commit_sha']) for i in queue}:
        raise ValueError('Frozen queue includes an already completed baseline version')
    progress_path = args.out / 'progress.json'
    progress = json.loads(progress_path.read_text()) if progress_path.exists() else {'batches': {}}
    write_json(args.out / 'runtime.json', {'pid': os.getpid(), 'started_at': now(), 'command': sys.argv})
    for n, start in enumerate(range(0, len(queue), args.batch_size), 1):
        directory = args.out / 'batches' / f'{n:04}'
        selection = prepare_batch(queue[start:start+args.batch_size], args.index, directory)
        report_path = directory / 'report.json'
        while True:
            progress.update(status='waiting_for_batch_lock', active_batch=n, updated_at=now())
            write_json(progress_path, progress)
            wait_for_batch(directory)
            old = json.loads(report_path.read_text()) if report_path.exists() else None
            # Completed batches (including recorded failures) are checkpoints.
            # Individual records remain available for explicit later retry.
            state_path = directory / 'state.json'
            saved = json.loads(state_path.read_text()) if state_path.exists() else {}
            available = sum(s.get('state') == 'succeeded' and (directory / s['record']).is_file()
                            for s in saved.values())
            if old and old['pending'] == 0 and available == old['succeeded'] and (not args.retry_failed or old['failed'] == 0):
                report = old
                break
            progress.update(status='running', active_batch=n, updated_at=now())
            write_json(progress_path, progress)
            command = [sys.executable, '-u', str(HERE / 'batch_convert.py'), '--selection', str(selection),
                       '--out', str(directory), '--index', str(args.index), '--workers', str(args.workers)]
            if args.retry_failed:
                command.append('--retry-failed')
            result = subprocess.run(command, stdin=subprocess.DEVNULL)
            if not report_path.exists():
                raise RuntimeError(f'Batch {n} did not write a report; exit {result.returncode}')
            report = json.loads(report_path.read_text())
            if old and report.get('generated_at') == old.get('generated_at'):
                raise RuntimeError(f'Batch {n} exited without a fresh report; exit {result.returncode}')
            if report['pending'] == 0:
                break
            progress.update(status='waiting_for_model', active_batch=n, updated_at=now())
            write_json(progress_path, progress)
            # Quota-deferred entries remain pending. Successful and failed items
            # are skipped by the existing converter when the same batch resumes.
            for _ in range(30):
                time.sleep(10)
        progress['batches'][str(n)] = {k: report[k] for k in ('selected', 'succeeded', 'failed', 'pending')}
        progress.update(status='running', completed_batches=n, updated_at=now(),
                        new_succeeded=sum(b['succeeded'] for b in progress['batches'].values()),
                        failed=sum(b['failed'] for b in progress['batches'].values()))
        progress['remaining'] = len(queue) - progress['new_succeeded']
        write_json(progress_path, progress)
        if args.max_batches and n >= args.max_batches:
            return
    progress.update(status='first_pass_finished', updated_at=now())
    write_json(progress_path, progress)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--queue', type=Path, default=HERE / 'batches/full-001.queue.jsonl')
    p.add_argument('--out', type=Path, default=HERE.parent / 'data/normalized/full-001')
    p.add_argument('--index', type=Path, default=HERE.parent / 'data/cumulative/skills.jsonl.gz')
    p.add_argument('--baseline', type=Path, default=BASELINE)
    p.add_argument('--batch-size', type=int, default=25)
    p.add_argument('--workers', type=int, default=4)
    p.add_argument('--max-batches', type=int)
    p.add_argument('--retry-failed', action='store_true', help='One finite additional pass over recorded failures')
    p.add_argument('--snapshot', help='Write an immutable provenance-only checkpoint, without model calls')
    args = p.parse_args()
    args.out = args.out.resolve();args.index = args.index.resolve();args.queue = args.queue.resolve()
    args.baseline = args.baseline.resolve()
    args.out.mkdir(parents=True, exist_ok=True)
    if args.snapshot:
        snapshot(args)
        return
    if args.batch_size < 1 or args.workers < 1 or (args.max_batches is not None and args.max_batches < 1):
        p.error('batch-size and workers must be positive')
    with (args.out / '.run.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        run(args)


if __name__ == '__main__':
    main()
