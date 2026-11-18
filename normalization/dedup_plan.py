"""Read source deduplication decisions without changing SkillRecord."""
import os
from pathlib import Path

from batch_convert import digest, key_of, now, source_of, write_json
from check_record import check_record, strict_json_loads


def read_decision(path, checksum, source=None, representative=None):
    data = path.read_bytes()
    if digest(data) != checksum:
        raise ValueError('Deduplication decision changed: ' + str(path))
    decision = strict_json_loads(data.decode())
    if source is not None and decision['source'] != source:
        raise ValueError('Deduplication source mismatch')
    if representative is not None and decision['representative'] != representative:
        raise ValueError('Deduplication representative mismatch')
    current = decision['source']
    for item in decision['proofs']:
        proof_path = path.parent / item['path']
        raw = proof_path.read_bytes()
        if digest(raw) != item['sha256']:
            raise ValueError('Deduplication proof changed')
        proof = strict_json_loads(raw.decode())
        if proof['source'] != current:
            raise ValueError('Broken deduplication proof chain')
        if proof['match'] == 'model_equivalent':
            comparison = proof_path.parent / proof['comparison']
            metadata = strict_json_loads(comparison.read_text())
            response = strict_json_loads((comparison.parent / 'response.txt').read_text())
            artifact = Path(metadata['candidate_record'])
            if (metadata.get('state') != 'succeeded' or response != metadata.get('decision')
                    or response.get('equivalent') is not True
                    or metadata.get('source') != current or metadata.get('candidate_source') != proof['representative']
                    or digest(artifact.read_bytes()) != metadata['candidate_record_sha256']):
                raise ValueError('Invalid equivalence comparison')
        elif (proof['match'] not in ('identical_supplied_text', 'same_markdown_structure')
              or len(proof.get('fingerprint', '')) != 64):
            raise ValueError('Unknown deduplication proof')
        current = proof['representative']
    if decision['status'] == 'duplicate' and (not decision['proofs'] or current != decision['representative']):
        raise ValueError('Deduplication representative has no proof chain')
    return decision


def load_plan(path, queue_sha256):
    report = strict_json_loads((path.parent / 'report.json').read_text())
    if report['queue_sha256'] != queue_sha256:
        raise ValueError('Deduplication plan belongs to a different frozen queue')
    result = {}
    for line in path.read_text().splitlines():
        item = strict_json_loads(line)
        source = item['source']
        key = key_of({'collection_id': source['collection_id'], 'commit_sha': source['commit_sha']})
        if key in result:
            raise ValueError('Deduplication plan repeats a source')
        decision_path = path.parent / item['decision_path']
        read_decision(decision_path, item['decision_sha256'], source)
        result[key] = (decision_path, item['decision_sha256'])
    return result


def planned_state(args, item, row, plan_entry, record_paths, schema):
    path, checksum = plan_entry
    source = source_of(row)
    decision = read_decision(path, checksum, source)
    if decision['status'] == 'unique':
        return None
    base = {'collection_id': item['collection_id'], 'commit_sha': item['commit_sha'],
            'key': key_of(item), 'updated_at': now(), 'source': source, 'inputs': decision['files']}
    if decision['status'] == 'failed':
        return {**base, 'state': 'failed', 'failure_stage': 'content_filter', 'error': decision.get('reason'),
                'plan_decision': os.path.relpath(path, args.out), 'plan_sha256': checksum}
    representative = decision['representative']
    target_key = key_of({'collection_id': representative['collection_id'], 'commit_sha': representative['commit_sha']})
    target = record_paths.get(target_key)
    if target is None:
        raise ValueError('Representative is outside the frozen corpus')
    if target.is_file():
        raw = target.read_bytes()
        record = strict_json_loads(raw.decode())
        if record['source'] != representative or check_record(record, schema)['errors']:
            raise ValueError('Invalid converted representative')
        write_json(args.out / 'inputs' / (key_of(item) + '.json'), {'source': source, 'files': decision['files']})
        return {**base, 'state': 'skipped_duplicate', 'duplicate_of': {
            'match': 'planned_equivalent', 'source': representative,
            'record_path': os.path.relpath(target, args.out), 'record_sha256': digest(raw),
            'plan_decision': os.path.relpath(path, args.out), 'plan_sha256': checksum,
            'reason': 'Deduplicated before conversion; representative result is accessible',
        }}
    return {**base, 'state': 'pending_duplicate', 'representative': representative,
            'plan_decision': os.path.relpath(path, args.out), 'plan_sha256': checksum,
            'reason': 'Representative conversion has not succeeded'}
