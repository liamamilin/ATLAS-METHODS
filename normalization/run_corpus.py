"""Run the frozen corpus queue as small resumable batches using batch_convert.

New record bodies stay local; snapshots publish provenance/paths and progress.
No upstream commands run and no source is refreshed or semantically deduplicated.
"""
import argparse
from collections import Counter, deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import signal
import sys
import threading
import time
from urllib.parse import unquote

from batch_convert import (HERE, accept_future, build_parser, convert_one, finish_run, key_of,
                           load_selection, make_client, now, prepare_run, selected_rows,
                           source_of, summarize, write_json, write_jsonl)
from check_record import check_record, strict_json_loads
from luna_client import LUNA
from model_routing import limit_signal

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


def recover_quota_failures(args, router=None):
    """Requeue only provider-quota rejections proved by retained error events.

    Keep original attempts unchanged and archive complete old states locally
    before publishing corrected states/reports. Must hold the queue lock.
    """
    config = json.loads((HERE/'model-routing.json').read_text())
    plan = []
    for path in sorted((args.out/'batches').glob('*/state.json')):
        for key, state in json.loads(path.read_text()).items():
            if state.get('state') != 'failed' or (path.parent/'records'/(key+'.json')).exists():
                continue
            attempt = path.parent/state.get('attempt', '')
            if not (attempt/'events.jsonl').is_file() or not (attempt/'metadata.json').is_file():
                continue
            metadata = json.loads((attempt/'metadata.json').read_text())
            if metadata.get('model') != config['primary'] or metadata.get('source') != state.get('source'):
                continue
            events = []
            for line in (attempt/'events.jsonl').read_text().splitlines():
                try:
                    events.append(strict_json_loads(line))
                except ValueError:
                    continue
            signal = limit_signal(events)
            if signal and signal['kind'] == 'quota':
                plan.append((path, key, state, signal))
    if not plan:
        print(json.dumps({'recovered_quota_failures':0}))
        return {'recovered_quota_failures':0}
    journal = args.out/'recoveries'/('quota-'+str(time.time_ns()))
    journal.mkdir(parents=True)
    with gzip.open(journal/'previous-states.jsonl.gz', 'wt', encoding='utf-8') as archive:
        for path, key, state, signal in plan:
            archive.write(json.dumps({'batch':path.parent.name, 'key':key, 'state':state}, ensure_ascii=False)+'\n')
    write_jsonl(journal/'requeued.jsonl', [
        {'collection_id':s['collection_id'], 'commit_sha':s['commit_sha'],
         'state_path':os.path.relpath(p, journal), 'state_key':key, 'previous_attempt':s['attempt'],
         'previous_state_sha256':hashlib.sha256(json.dumps(s, sort_keys=True).encode()).hexdigest(),
         'limit':limit, 'action':'deferred'} for p,key,s,limit in plan])
    grouped = {}
    for path,key,state,signal in plan:
        grouped.setdefault(path,[]).append((key,state))
    for path, changes in grouped.items():
        with (path.parent/'.run.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
            states = json.loads(path.read_text())
            for key, previous in changes:
                if states.get(key) != previous or (path.parent/'records'/(key+'.json')).exists():
                    raise ValueError('State changed during quota recovery; retained archive: '+str(journal))
                states[key] = {**previous, 'state':'deferred', 'failure_stage':'model_availability',
                               'error':'Provider quota rejection; pending model availability',
                               'updated_at':now(), 'recovered_from':os.path.relpath(journal/'requeued.jsonl', path.parent)}
            write_json(path, states)
            old = json.loads((path.parent/'report.json').read_text())
            batch_args = build_parser().parse_args(['--selection',str(path.parent/'selection.json'),
                                                   '--out',str(path.parent),'--index',str(args.index)])
            batch_args.routing_policy = old.get('routing_policy')
            summarize(batch_args, load_selection(batch_args.selection), states, old['invocation'])
    # This is a real observed provider limit, not a simulated production flag.
    router = router or make_client(batch_args)
    observed = max((x[3] for x in plan), key=lambda x:x.get('retry_at',0))
    router.observe(config['primary'], None, {'limit':observed})
    batches = {}
    for p in (args.out/'batches').glob('*/report.json'):
        report = json.loads(p.read_text());batches[str(int(p.parent.name))] = {
            k:report[k] for k in ('selected','succeeded','failed','pending')}
    progress = json.loads((args.out/'progress.json').read_text()) if (args.out/'progress.json').exists() else {}
    progress.update(status='ready_to_resume', updated_at=now(), batches=batches,
                    failed=sum(v['failed'] for v in batches.values()),
                    new_succeeded=sum(v['succeeded'] for v in batches.values()))
    write_json(args.out/'progress.json', progress)
    result = {'recovered_at':now(), 'recovered_quota_failures':len(plan), 'batches':len(grouped),
              'remaining_content_failures':progress['failed'], 'preserved_full_run_successes':progress['new_succeeded'],
              'observed_limit':observed, 'archive':'previous-states.jsonl.gz', 'manifest':'requeued.jsonl'}
    write_json(journal/'report.json', result)
    print(json.dumps({**result,'journal':str(journal)},ensure_ascii=False,indent=2))
    return result


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


def run(args, client=None, stop_event=None, deferred_delay=300):
    """One bounded pool spans the existing batch directories; no batch barrier.

    The main thread alone publishes each batch's state/report. Directory locks
    still protect old surviving batch subprocesses and independent CLI runs.
    SIGTERM drains in-flight conversions before releasing locks.
    """
    queue = read_queue(args.queue)
    baseline = baseline_records(args.baseline)
    baseline_ids = {(r['source']['collection_id'], r['source']['commit_sha']) for _,r in baseline}
    if baseline_ids & {(i['collection_id'],i['commit_sha']) for i in queue}:
        raise ValueError('Frozen queue includes an already completed baseline version')
    progress_path = args.out / 'progress.json'
    progress = {'batches': {}}
    stop_event = stop_event or threading.Event()
    write_json(args.out / 'runtime.json', {'pid': os.getpid(), 'started_at': now(), 'command': sys.argv,
                                          'scheduler': 'rolling', 'workers': args.workers})
    contexts, ready, futures = {}, deque(), {}
    next_batch, exhausted, retry_at = 1, False, None
    stats = {'scheduled': 0, 'finished': 0, 'succeeded': 0, 'failed': 0, 'deferred': 0,
             'occupied_worker_seconds': 0.0, 'max_in_flight': 0}
    started = None

    def publish(status='running'):
        elapsed = time.monotonic() - started if started is not None else 0
        occupied = stats['occupied_worker_seconds'] + sum(time.monotonic()-v[3] for v in futures.values())
        progress.update(status=status, scheduler='rolling', workers=args.workers, updated_at=now(),
                        active_batch=min(contexts) if contexts else None, active_batches=sorted(contexts),
                        completed_batches=sum(b['pending'] == 0 for b in progress['batches'].values()),
                        new_succeeded=sum(b['succeeded'] for b in progress['batches'].values()),
                        failed=sum(b['failed'] for b in progress['batches'].values()),
                        scheduler_run={**stats, 'elapsed_seconds': elapsed, 'in_flight': len(futures),
                                       'worker_occupation_ratio': occupied/(elapsed*args.workers) if elapsed else 0})
        progress['remaining'] = len(queue) - progress['new_succeeded']
        write_json(progress_path, progress)

    def save_report(n, report):
        progress['batches'][str(n)] = {k: report[k] for k in ('selected', 'succeeded', 'failed', 'pending')}

    def close_if_finished(n):
        context = contexts[n]
        if context['active'] == 0 and context['unscheduled'] == 0 and not context['deferred']:
            save_report(n, finish_run(context))
            context['lock'].close()
            del contexts[n]

    def open_batch(n):
        nonlocal client
        start = (n-1)*args.batch_size
        directory = args.out / 'batches' / f'{n:04}'
        selection = prepare_batch(queue[start:start+args.batch_size], args.index, directory)
        report_path = directory / 'report.json'
        lock = (directory / '.run.lock').open('a')
        try:
            publish('waiting_for_batch_lock')
            fcntl.flock(lock, fcntl.LOCK_EX)
            old = json.loads(report_path.read_text()) if report_path.exists() else None
            state_path = directory / 'state.json'
            saved = json.loads(state_path.read_text()) if state_path.exists() else {}
            available = sum(s.get('state') == 'succeeded' and (directory / s['record']).is_file()
                            for s in saved.values())
            if old and old['pending'] == 0 and available == old['succeeded'] and (not args.retry_failed or old['failed'] == 0):
                save_report(n, old)
                lock.close()
                return
            command = ['--selection', str(selection), '--out', str(directory), '--index', str(args.index),
                       '--workers', str(args.workers)]
            if args.retry_failed:
                command.append('--retry-failed')
            if getattr(args, 'luna_fallback', False):
                command.append('--luna-fallback')
            batch_args = build_parser().parse_args(command)
            context = prepare_run(batch_args)
            if client is None:
                client = make_client(batch_args)
            if getattr(client, 'uses_fallback', False):
                config = json.loads(batch_args.routing_config.read_text())
                batch_args.routing_policy = {**config, 'state_path': str(batch_args.routing_state)}
                if batch_args.luna_fallback:
                    batch_args.routing_policy['fallback_override'] = LUNA
            context.update(lock=lock, active=0, unscheduled=len(context['todo']), deferred=[])
            contexts[n] = context
            ready.extend((n, item, row) for item, row in context['todo'])
            save_report(n, summarize(batch_args, context['selection'], context['states'], context['invocation']))
            close_if_finished(n)
        except BaseException:
            lock.close()
            raise

    try:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            while True:
                if retry_at is not None and time.monotonic() >= retry_at:
                    for n, context in contexts.items():
                        ready.extend((n, item, row) for item, row in context['deferred'])
                        context['unscheduled'] += len(context['deferred'])
                        context['deferred'] = []
                    retry_at = None
                while len(futures) < args.workers and not stop_event.is_set() and retry_at is None:
                    if not ready:
                        if exhausted:
                            break
                        if (next_batch-1)*args.batch_size >= len(queue) or (args.max_batches and next_batch > args.max_batches):
                            exhausted = True
                            break
                        open_batch(next_batch)
                        next_batch += 1
                        continue
                    n, item, row = ready.popleft()
                    context = contexts[n]
                    if started is None:
                        started = time.monotonic()
                        stats['first_submitted_at'] = now()
                    old = context['states'].get(key_of(item), {})
                    submitted = time.monotonic()
                    future = pool.submit(convert_one, item, row, context['args'], client,
                                         context['schema'], context['contract'],
                                         old.get('review_feedback') or (old.get('error', '') if old.get('state') == 'failed' else ''))
                    futures[future] = (n, item, row, submitted)
                    context['active'] += 1
                    context['unscheduled'] -= 1
                    stats['scheduled'] += 1
                    stats['max_in_flight'] = max(stats['max_in_flight'], len(futures))
                if not futures:
                    if stop_event.is_set():
                        publish('stopped')
                        break
                    if retry_at is not None:
                        publish('waiting_for_model')
                        stop_event.wait(min(10, max(0, retry_at-time.monotonic())))
                        continue
                    publish('first_pass_finished' if not args.max_batches else 'batch_limit_finished')
                    break
                publish('draining' if stop_event.is_set() else 'running')
                done, _ = wait(futures, timeout=1, return_when=FIRST_COMPLETED)
                for future in done:
                    n, item, row, submitted = futures.pop(future)
                    context = contexts[n]
                    context['active'] -= 1
                    state, report = accept_future(context, item, future)
                    stats['occupied_worker_seconds'] += time.monotonic()-submitted
                    stats['finished'] += 1
                    stats[state['state']] += 1
                    save_report(n, report)
                    if state['state'] == 'deferred':
                        context['deferred'].append((item, row))
                        retry_at = time.monotonic() + deferred_delay
                    close_if_finished(n)
                publish('draining' if stop_event.is_set() else 'running')
    finally:
        for context in contexts.values():
            context['lock'].close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--queue', type=Path, default=HERE / 'batches/full-001.queue.jsonl')
    p.add_argument('--out', type=Path, default=HERE.parent / 'data/normalized/full-001')
    p.add_argument('--index', type=Path, default=HERE.parent / 'data/cumulative/skills.jsonl.gz')
    p.add_argument('--baseline', type=Path, default=BASELINE)
    p.add_argument('--batch-size', type=int, default=25)
    p.add_argument('--workers', type=int, default=4)
    p.add_argument('--luna-fallback', action='store_true', help='Explicitly use local Atlas Luna during primary cooldown')
    p.add_argument('--max-batches', type=int)
    p.add_argument('--retry-failed', action='store_true', help='One finite additional pass over recorded failures')
    p.add_argument('--snapshot', help='Write an immutable provenance-only checkpoint, without model calls')
    p.add_argument('--recover-quota-failures', action='store_true', help='Requeue retained primary quota rejections; preserve all original attempts')
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
        if args.recover_quota_failures:
            recover_quota_failures(args)
            return
        stop = threading.Event()
        old_handlers = {s: signal.signal(s, lambda *_: stop.set()) for s in (signal.SIGTERM, signal.SIGINT)}
        try:
            run(args, stop_event=stop)
        finally:
            for s, handler in old_handlers.items():
                signal.signal(s, handler)


if __name__ == '__main__':
    main()
