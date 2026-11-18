"""Plan duplicate skips for pending sources, then optionally resume conversion."""
import argparse
from collections import Counter, defaultdict, deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading

from batch_convert import (
    build_parser, digest, key_of, make_client, now, prepare_input, selected_rows,
    source_of, valid_duplicate, write_json, write_jsonl,
)
from check_record import strict_json_loads
from dedup_plan import load_plan
from duplicate_skills import SkillDeduplicator, package_key
from model_routing import ModelDeferred
from run_corpus import HERE, baseline_records, plan_references, read_queue


def decide(item, candidates, deduplicator, args, client):
    row = deduplicator.rows[(item['collection_id'], item['commit_sha'])]
    texts, reads, inputs, blocks = prepare_input(row, item, args.index)
    for candidate in candidates:
        result = deduplicator.compare(blocks, item.get('reference_selection'), candidate,
                                      row, args, item, client)
        if result and result.get('refused'):
            return {'status': 'failed', 'failure_stage': 'content_filter',
                    'reason': 'Provider refused the source comparison', 'comparison': result['comparison']}
        if result:
            return {'status': 'duplicate', 'representative': candidate,
                    'fingerprint': package_key(blocks, item.get('reference_selection')), **result}
    return {'status': 'unique'}


def run(args, client=None, stop_event=None, retry_delay=300):
    stop_event = stop_event or threading.Event()
    stage = args.out / args.name
    if (stage / 'report.json').is_file() and (stage / 'plan.jsonl').is_file():
        load_plan(stage / 'plan.jsonl', digest(args.queue.read_bytes()))
        report = strict_json_loads((stage / 'report.json').read_text())
        write_json(stage / 'progress.json', {
            'updated_at': now(), 'status': 'complete', 'workers': args.workers,
            'sources': report['pending_at_start'], 'remaining': 0,
            'decisions': {'unique': report['unique_to_convert'], 'duplicate': report['duplicates'],
                          'failed': report['failed']},
        })
        return True
    queue = read_queue(args.queue)
    baseline = baseline_records(args.baseline)
    wanted = queue + [{'collection_id': r['source']['collection_id'], 'commit_sha': r['source']['commit_sha']}
                      for _, r in baseline]
    rows = selected_rows(args.index, wanted)
    saved_paths = [path for path, _ in baseline] + sorted((args.out / 'batches').glob('*/records/*.json'))
    saved_keys = {path.stem for path in saved_paths}
    schema = strict_json_loads((HERE / 'skill-record.v0.1.1.schema.json').read_text())
    prior_states, selections = {}, {}
    for path in (args.out / 'batches').glob('*/state.json'):
        for key, state in strict_json_loads(path.read_text()).items():
            prior_states[key] = (state, path.parent)
    for path in (args.out / 'batches').glob('*/selection.json'):
        selections.update({key_of(i): i for i in strict_json_loads(path.read_text())['items']})
    pending = []
    for item in queue:
        key = key_of(item)
        state, directory = prior_states.get(key, ({}, args.out))
        if key in saved_keys or state.get('state') == 'failed' or valid_duplicate(state, directory, schema):
            continue
        pending.append(selections.get(key, item))
    stage.mkdir(parents=True, exist_ok=True)
    stamp = {'queue_sha256': digest(args.queue.read_bytes()),
             'input_keys': [key_of(i) for i in pending]}
    if (stage / 'input.json').exists() and strict_json_loads((stage / 'input.json').read_text()) != stamp:
        raise ValueError('Pending source set changed; use a new deduplication name')
    write_json(stage / 'input.json', stamp)
    write_json(stage / 'runtime.json', {'pid': os.getpid(), 'started_at': now(), 'workers': args.workers,
                                       'command': sys.argv})
    state_path = stage / 'state.json'
    states = strict_json_loads(state_path.read_text()) if state_path.exists() else {}

    def publish(status):
        counts = Counter(s['status'] for s in states.values())
        write_json(stage / 'progress.json', {'updated_at': now(), 'status': status, 'workers': args.workers,
                                            'sources': len(pending), 'decisions': dict(counts),
                                            'remaining': len(pending) - sum(counts[s] for s in ('unique', 'duplicate', 'failed'))})

    publish('preparing')
    dedup = SkillDeduplicator(args.index, rows,
                             ((p, strict_json_loads(p.read_text())) for p in saved_paths))
    prepared = {}
    groups = {}
    exact_links = {}
    for original in pending:
        if stop_event.is_set():
            publish('stopped')
            return False
        item = dict(original)
        key = key_of(item)
        row = rows[(item['collection_id'], item['commit_sha'])]
        try:
            if 'reference_selection' not in item:
                item['references'], missing = plan_references(row, args.index)
                item['reference_selection'] = {'not_supplied': missing}
            texts, reads, inputs, blocks = prepare_input(row, item, args.index)
            fingerprint = digest(json.dumps([
                package_key(blocks, item.get('reference_selection')),
                dedup.unread_context(row, blocks),
            ], sort_keys=True).encode())
            for candidate in sorted(dedup.hashes.get(row['sha256'], ())):
                if dedup.entries[candidate][1]['kind'] != 'normalized_record':
                    continue
                result = dedup.compare(blocks, item.get('reference_selection'), candidate,
                                       row, argparse.Namespace(out=stage), item, None, exact_only=True)
                if result:
                    exact_links[key] = {'status': 'duplicate', 'representative': candidate,
                                        'match': result['match'], 'reason': result['reason'],
                                        'fingerprint': fingerprint}
                    break
            if key not in exact_links and fingerprint in groups:
                exact_links[key] = {'status': 'duplicate', 'representative': groups[fingerprint],
                                    'match': 'identical_supplied_text',
                                    'reason': 'Same supplied package and unread file inventory',
                                    'fingerprint': fingerprint}
            if key not in exact_links:
                groups[fingerprint] = key
                dedup.add_source(stage / 'source-packages' / (key + '.json'), row, item,
                                 texts[row['skill_path']], reads, inputs)
            prepared[key] = {**item, 'source': source_of(row), 'files': inputs}
        except (OSError, ValueError, KeyError) as exc:
            prepared[key] = {**item, 'source': source_of(row), 'files': []}
            states[key] = {'status': 'unique', 'warning': 'Source preparation failed: ' + str(exc)}
    states.update(exact_links)
    write_json(stage / 'selection.json', {'items': list(prepared.values())})
    write_json(state_path, states)
    publish('finding_candidates')
    representatives = {key for key in prepared if key not in exact_links and key in dedup.entries}
    parent = {key: key for key in representatives}

    def find(key):
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    known = defaultdict(list)
    order = {key_of(item): n for n, item in enumerate(pending)}
    for key in representatives:
        row = dedup.entries[key][2]
        text = (args.index.parent / row['raw_path']).read_text(encoding='utf-8')
        for candidate in dedup.candidates(row, text):
            if candidate in representatives:
                a, b = find(key), find(candidate)
                parent[max(a, b)] = min(a, b)
            elif dedup.entries[candidate][1]['kind'] == 'normalized_record':
                known[key].append(candidate)
    components = defaultdict(list)
    for key in representatives:
        components[find(key)].append(key)
    candidate_map = {}
    for members in components.values():
        converted = Counter(c for key in members for c in known[key])
        if converted:
            candidates = [key for key, _ in converted.most_common(2)]
        else:
            canonical = min(members, key=lambda key: order[key])
            candidates = [canonical]
            states[canonical] = {'status': 'unique'}
        for key in members:
            if key not in candidates:
                candidate_map[key] = candidates
    write_json(stage / 'candidates.json', candidate_map)
    write_json(state_path, states)
    batch_args = build_parser().parse_args([
        '--selection', str(stage / 'selection.json'), '--out', str(stage), '--index', str(args.index),
        '--workers', str(args.workers), '--go-fallback', args.go_fallback, '--go-backup', args.go_backup,
    ])
    client = client or make_client(batch_args)
    todo = deque(key for key in candidate_map if states.get(key, {}).get('status') not in ('unique', 'duplicate', 'failed'))
    while todo and not stop_event.is_set():
        deferred = []
        paused = False
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {}
            while todo or futures:
                while todo and len(futures) < args.workers and not paused and not stop_event.is_set():
                    key = todo.popleft()
                    future = pool.submit(decide, prepared[key], candidate_map[key], dedup, batch_args, client)
                    futures[future] = key
                if not futures:
                    break
                done, _ = wait(futures, timeout=1, return_when=FIRST_COMPLETED)
                for future in done:
                    key = futures.pop(future)
                    try:
                        states[key] = future.result()
                    except ModelDeferred as exc:
                        states[key] = {'status': 'deferred', 'reason': str(exc)}
                        deferred.append(key)
                        paused = True
                    except Exception as exc:
                        states[key] = {'status': 'unique', 'warning': type(exc).__name__ + ': ' + str(exc)[:500]}
                    write_json(state_path, states)
                    publish('comparing')
        todo.extendleft(reversed(deferred))
        if todo and not stop_event.is_set():
            publish('waiting_for_model')
            stop_event.wait(retry_delay)
    if stop_event.is_set():
        publish('stopped')
        return False

    sources = {key: profile[1]['source'] for key, profile in dedup.entries.items()}
    sources.update({key: item['source'] for key, item in prepared.items()})
    ready, entries = [], []
    for key, item in prepared.items():
        current = key
        proofs = []
        while states.get(current, {}).get('status') == 'duplicate':
            decision = states[current]
            target = decision['representative']
            proof = {**decision, 'source': sources[current], 'representative': sources[target]}
            if 'comparison' in proof:
                proof['comparison'] = os.path.relpath(stage / proof['comparison'], stage / 'proofs')
            proof_path = stage / 'proofs' / (current + '.json')
            write_json(proof_path, proof)
            proofs.append({'path': os.path.relpath(proof_path, stage / 'decisions'), 'sha256': digest(proof_path.read_bytes())})
            current = target
        status = states.get(key, {}).get('status', 'unique')
        entry = {'source': item['source'], 'representative': sources[current],
                 'status': status, 'proofs': proofs, 'files': item['files']}
        if status == 'failed':
            entry['reason'] = states[key].get('reason')
            if states[key].get('comparison'):
                entry['failure_comparison'] = os.path.relpath(stage / states[key]['comparison'], stage / 'decisions')
        if states.get(key, {}).get('warning'):
            entry['warning'] = states[key]['warning']
        write_json(stage / 'decisions' / (key + '.json'), entry)
        entries.append({'source': item['source'], 'status': status,
                        'decision_path': 'decisions/' + key + '.json',
                        'decision_sha256': digest((stage / 'decisions' / (key + '.json')).read_bytes())})
        if current == key and status == 'unique':
            ready.append({k: v for k, v in item.items() if k not in ('source', 'files')})
    write_jsonl(stage / 'plan.jsonl', entries)
    write_jsonl(stage / 'unique.queue.jsonl', ready)
    usage = defaultdict(Counter)
    checks = Counter()
    for path in (stage / 'similarity_checks').glob('*/*/metadata.json'):
        metadata = strict_json_loads(path.read_text())
        checks[metadata.get('state', 'unfinished')] += 1
        model = metadata.get('model', 'unknown')
        details = metadata.get('usage', {})
        usage[model]['sessions'] += bool(metadata.get('session_id'))
        usage[model]['sessions_without_usage'] += bool(metadata.get('session_id')) and not details.get('available', False)
        for turn in details.get('turns', []):
            for kind in ('input', 'output', 'reasoning'):
                usage[model][kind] += turn['tokens'].get(kind) or 0
    write_json(stage / 'report.json', {'as_of': now(), 'pending_at_start': len(pending),
                                     'unique_to_convert': len(ready),
                                     'duplicates': sum(e['status'] == 'duplicate' for e in entries),
                                     'failed': sum(e['status'] == 'failed' for e in entries),
                                     'comparison_attempts': dict(checks),
                                     'usage_by_model': {model: dict(values) for model, values in usage.items()},
                                     'billing_cost': None,
                                     'queue_sha256': stamp['queue_sha256']})
    publish('complete')
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=HERE.parent / 'data/normalized/full-001')
    parser.add_argument('--index', type=Path, default=HERE.parent / 'data/cumulative/skills.jsonl.gz')
    parser.add_argument('--queue', type=Path, default=HERE / 'batches/full-001.queue.jsonl')
    parser.add_argument('--baseline', type=Path, default=HERE.parent / 'data/normalized/batch-001')
    parser.add_argument('--name', default='dedup-001')
    parser.add_argument('--workers', type=int, default=10)
    parser.add_argument('--go-fallback', default='opencode-go/beam-free')
    parser.add_argument('--go-backup', default='opencode-go/iceman-contributor-free')
    parser.add_argument('--then-convert', action='store_true')
    args = parser.parse_args()
    if args.workers < 1 or args.name in ('', '.', '..') or Path(args.name).name != args.name:
        parser.error('Use a positive worker count and a single directory name')
    args.out, args.index, args.queue, args.baseline = (p.resolve() for p in (
        args.out, args.index, args.queue, args.baseline))
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    with (args.out / '.run.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        completed = run(args, stop_event=stop)
    if completed and args.then_convert and not stop.is_set():
        command = [sys.executable, '-u', str(HERE / 'run_corpus.py'), '--out', str(args.out),
                   '--index', str(args.index), '--queue', str(args.queue), '--baseline', str(args.baseline),
                   '--workers', str(args.workers), '--go-fallback', args.go_fallback, '--go-backup', args.go_backup,
                   '--dedup-plan', str(args.out / args.name / 'plan.jsonl')]
        with (args.out / 'worker.log').open('ab', buffering=0) as log:
            worker = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                      start_new_session=True)
        print(json.dumps({'conversion_pid': worker.pid}), flush=True)


if __name__ == '__main__':
    main()
