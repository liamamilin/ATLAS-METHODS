"""Restore explicit evidence paths on saved responses without calling a model.

The attempt request and response stay byte-for-byte. A record is written only
after the existing response schema and check_record both pass. The only allowed
content change is an exact evidence path named by the operator for that record.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator

from batch_convert import digest, now, write_json
from check_record import check_record, source_line_count, strict_json_loads
from export_response_schema import response_schema

HERE = Path(__file__).resolve().parent
JOURNAL_NAME = 'evidence-paths'


def file_sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def journal_directory(batch, key):
    return batch / 'recoveries' / JOURNAL_NAME / key


def _decode_prefix(text):
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate_json_key: ' + key)
            result[key] = value
        return result

    return json.JSONDecoder(object_pairs_hook=unique_object).raw_decode(text)[0]


def supplied_request(text):
    language = None
    for line in text.splitlines():
        if line.startswith('本次输出语言：'):
            language = line.split('：', 1)[1].strip()
    marker = '\n{"source_identity"'
    if marker not in text:
        raise ValueError('request has no supplied source payload')
    payload = _decode_prefix(text[text.index(marker) + 1:])
    if not language:
        raise ValueError('request has no output language')
    return language, payload


def text_from_numbered(block):
    count = block['source_line_count']
    if not isinstance(count, int) or count < 1:
        raise ValueError('source_line_count is not a positive integer')
    lines = [''] * count
    seen = set()
    for raw in block['numbered_source'].split('\n'):
        if raw == '':
            continue
        number, sep, body = raw.partition('|')
        if sep != '|' or not number.isdigit():
            raise ValueError('numbered source line has no line number')
        number = int(number)
        if number in seen or not 1 <= number <= count:
            raise ValueError('numbered source line is duplicated or outside the file')
        seen.add(number)
        lines[number - 1] = body
    if seen != set(range(1, count + 1)):
        raise ValueError('numbered source does not cover the supplied file')
    body = '\n'.join(lines)
    candidates = [body] if body.endswith('\n') else [body, body + '\n']
    return candidates


def evidence_entries(content):
    found = []

    def walk(node):
        if isinstance(node, dict):
            evidence = node.get('evidence')
            if isinstance(evidence, list):
                for item in evidence:
                    if isinstance(item, dict):
                        found.append(item)
            for key, value in node.items():
                if key != 'evidence':
                    walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(content)
    return found


def same_except_evidence_path(before, after, source, target):
    if type(before) is not type(after):
        return False
    if isinstance(before, dict):
        if list(before) != list(after):
            return False
        for key in before:
            if key == 'path' and before[key] == source and after[key] == target:
                continue
            if not same_except_evidence_path(before[key], after[key], source, target):
                return False
        return True
    if isinstance(before, list):
        return (len(before) == len(after) and
                all(same_except_evidence_path(left, right, source, target)
                    for left, right in zip(before, after)))
    return before == after


def load_plan(path):
    plan = strict_json_loads(Path(path).read_text(encoding='utf-8'))
    if plan.get('attempt') != '004' or not isinstance(plan.get('records'), list) or not plan['records']:
        raise ValueError('plan must name attempt 004 and at least one record')
    seen = set()
    for item in plan['records']:
        identity = (item.get('batch'), item.get('key'))
        replacements = item.get('replacements')
        if (not isinstance(item.get('batch'), str) or not item['batch']
                or not isinstance(item.get('key'), str) or not item['key']
                or identity in seen or not isinstance(replacements, list) or len(replacements) != 1):
            raise ValueError('each plan record needs one unique batch, key, and replacement')
        replacement = replacements[0]
        source, target = replacement.get('from'), replacement.get('to')
        if (not isinstance(source, str) or not isinstance(target, str)
                or not source or not target or source == target or source.strip() != source
                or target.strip() != target):
            raise ValueError('replacement paths must be exact, distinct, non-empty strings')
        seen.add(identity)
    return plan


def _fail(key, reason):
    return {'key': key, 'status': 'refused', 'reason': reason}


def _load_case(root, item):
    batch = root / 'batches' / item['batch']
    key = item['key']
    state_path = batch / 'state.json'
    states = strict_json_loads(state_path.read_text(encoding='utf-8'))
    if not isinstance(states, dict) or 'collection_id' in states:
        raise ValueError('state.json must be the batch map: ' + item['batch'])
    state = states.get(key)
    if not isinstance(state, dict):
        return states, _fail(key, 'record is absent from batch state')
    if state.get('state') == 'skipped_duplicate' or state.get('failure_stage') == 'content_filter':
        return states, _fail(key, 'skipped or content-filtered records are not path-recovery candidates')
    attempt = batch / state.get('attempt', '')
    if state.get('attempt') != f'attempts/{key}/004' or attempt.name != '004':
        return states, _fail(key, 'recovery is limited to the retained attempt 004')
    return states, {
        'key': key, 'batch': batch, 'state': state, 'attempt': attempt,
        'replacement': item['replacements'][0],
    }


def _source_text(case):
    key = case['key']
    inputs = strict_json_loads((case['batch'] / 'inputs' / (key + '.json')).read_text(encoding='utf-8'))
    files = inputs.get('files')
    if not isinstance(files, list) or len(files) != 1 or files[0].get('coverage') != 'full':
        raise ValueError(key + ' was not supplied as exactly one full file')
    supplied = files[0]
    if supplied.get('path') != case['replacement']['to']:
        raise ValueError(key + ' replacement target is not the supplied file path')
    language, current = supplied_request((case['attempt'] / 'request.txt').read_text(encoding='utf-8'))
    previous_request = case['attempt'].parent / '003' / 'request.txt'
    if not previous_request.is_file():
        raise ValueError(key + ' is missing attempt 003 request')
    _, previous = supplied_request(previous_request.read_text(encoding='utf-8'))
    if previous.get('sources') != current.get('sources') or previous.get('source_identity') != current.get('source_identity'):
        raise ValueError(key + ' attempt 003 and 004 did not supply the same source')
    blocks = current.get('sources')
    if (not isinstance(blocks, list) or len(blocks) != 1 or blocks[0].get('path') != supplied['path']
            or blocks[0].get('coverage') != 'full'):
        raise ValueError(key + ' request does not contain the one supplied full file')
    if current.get('source_identity') != inputs.get('source') or case['state'].get('source') != inputs.get('source'):
        raise ValueError(key + ' source identity disagrees across state, inputs, and request')
    matched = None
    for candidate in text_from_numbered(blocks[0]):
        encoded = candidate.encode('utf-8')
        if digest(encoded) == supplied.get('sha256') and len(encoded) == supplied.get('bytes'):
            matched = candidate
            break
    if matched is None or source_line_count(matched) != supplied.get('source_lines'):
        raise ValueError(key + ' supplied text does not match the saved input hash')
    if blocks[0].get('delivered_ranges') != [{'line_start': 1, 'line_end': supplied['source_lines']}]:
        raise ValueError(key + ' full file was not delivered as one complete range')
    return language, inputs, matched


def _corrected_response(case, language, inputs, source_text, schema, contract):
    key = case['key']
    source, target = case['replacement']['from'], case['replacement']['to']
    original_bytes = (case['attempt'] / 'response.txt').read_bytes()
    response = strict_json_loads(original_bytes.decode('utf-8'))
    if set(response) != {'content', 'gaps'}:
        raise ValueError(key + ' response is not a content/gaps object')
    corrected = copy.deepcopy(response)
    replaced = already = 0
    for entry in evidence_entries(corrected['content']):
        path = entry.get('path')
        if path == source:
            entry['path'] = target
            replaced += 1
        elif path == target:
            already += 1
        else:
            raise ValueError(key + ' evidence path is neither the approved path nor the supplied path')
    if replaced < 1:
        raise ValueError(key + ' approved wrong path does not occur in attempt 004')
    if not same_except_evidence_path(response, corrected, source, target):
        raise ValueError(key + ' correction would change more than the approved evidence path')
    schema_errors = list(Draft202012Validator(contract).iter_errors(corrected))
    if schema_errors:
        raise ValueError(key + ' corrected response fails the response schema')
    record = {
        'schema_version': '0.1.1', 'source': inputs['source'], 'content': corrected['content'],
        'extraction': {
            'language': language, 'scope': 'entry_only',
            'files_read': [{'path': target, 'coverage': 'full'}], 'gaps': corrected['gaps'],
            'review_status': 'unreviewed', 'execution_status': 'not_executed',
        },
    }
    checked = check_record(record, schema, source_texts={target: source_text})
    if checked['errors'] or any(warning.startswith('empty_content:') for warning in checked['warnings']):
        raise ValueError(key + ' corrected record fails the existing checker')
    return {
        'original_bytes': original_bytes, 'response': response, 'corrected': corrected,
        'record': record, 'checked': checked, 'replaced': replaced, 'already': already,
        'inputs': inputs,
    }


def _metadata_producer(case):
    metadata_path = case['attempt'] / 'metadata.json'
    if not metadata_path.is_file():
        raise ValueError(case['key'] + ' attempt 004 has no metadata')
    metadata = strict_json_loads(metadata_path.read_text(encoding='utf-8'))
    model = metadata.get('model')
    if not isinstance(model, str) or not model or metadata.get('source') != case['state'].get('source'):
        raise ValueError(case['key'] + ' attempt metadata does not match the saved source')
    return 'opencode/' + model, file_sha256(metadata_path)


def evaluate(root, plan):
    """Validate every approved record. This writes nothing."""
    root = Path(root).resolve()
    schema = strict_json_loads((HERE / 'skill-record.v0.1.1.schema.json').read_text(encoding='utf-8'))
    contract = response_schema(schema)
    results = []
    for item in plan['records']:
        key = item['key']
        try:
            _states, case = _load_case(root, item)
            if case.get('status') == 'refused':
                results.append(case)
                continue
            language, inputs, source_text = _source_text(case)
            prepared = _corrected_response(case, language, inputs, source_text, schema, contract)
            record_path = case['batch'] / 'records' / (key + '.json')
            journal = journal_directory(case['batch'], key) / 'journal.json'
            status = 'ready'
            if record_path.exists() or journal.exists():
                status = 'conflict'
                if journal.is_file() and record_path.is_file():
                    saved = strict_json_loads(journal.read_text(encoding='utf-8'))
                    record = strict_json_loads(record_path.read_text(encoding='utf-8'))
                    if (saved.get('original_response_sha256') == digest(prepared['original_bytes'])
                            and record.get('content') == prepared['record']['content']
                            and record.get('extraction', {}).get('gaps') == prepared['record']['extraction']['gaps']
                            and record.get('source') == prepared['record']['source']
                            and file_sha256(record_path) == saved.get('record_sha256')):
                        status = 'already_applied'
            results.append({
                'key': key, 'batch': item['batch'], 'status': status,
                'source': inputs['source'], 'replacement': case['replacement'],
                'evidence_paths_replaced': prepared['replaced'],
                'evidence_paths_already_correct': prepared['already'],
                'original_response_sha256': digest(prepared['original_bytes']),
                'source_text_sha256': digest(source_text.encode('utf-8')),
                'check_errors': prepared['checked']['errors'],
                'warnings': prepared['checked']['warnings'],
            })
        except (OSError, ValueError, KeyError, TypeError, UnicodeError) as exc:
            results.append(_fail(key, str(exc)))
    return results


def apply_plan(root, plan):
    """Write recovered records only when every approved record validates."""
    root = Path(root).resolve()
    preview = evaluate(root, plan)
    blocked = [item for item in preview if item['status'] not in ('ready', 'already_applied')]
    if blocked:
        raise ValueError('evidence path recovery refused; no records written')
    producers = {}
    for item in plan['records']:
        if next(result for result in preview if result['key'] == item['key'])['status'] == 'already_applied':
            continue
        _states, case = _load_case(root, item)
        if case.get('status') == 'refused':
            raise ValueError('evidence path recovery refused; no records written')
        producers[item['key']] = _metadata_producer(case)
    schema = strict_json_loads((HERE / 'skill-record.v0.1.1.schema.json').read_text(encoding='utf-8'))
    contract = response_schema(schema)
    written = []
    for item in plan['records']:
        preview_item = next(result for result in preview if result['key'] == item['key'])
        if preview_item['status'] == 'already_applied':
            written.append({**preview_item, 'wrote': False})
            continue
        states, case = _load_case(root, item)
        if case.get('status') == 'refused':
            raise ValueError('evidence path recovery refused; no records written')
        before_attempts = {path: path.read_bytes() for path in case['attempt'].parent.rglob('*') if path.is_file()}
        language, inputs, source_text = _source_text(case)
        prepared = _corrected_response(case, language, inputs, source_text, schema, contract)
        if digest(prepared['original_bytes']) != preview_item['original_response_sha256']:
            raise ValueError(item['key'] + ' response changed between preview and write')
        producer, metadata_sha = producers[item['key']]
        prompt_version = 'target-first-' + digest((HERE / 'CONVERSION-PROMPT.md').read_bytes())[:12]
        record = prepared['record']
        record['extraction']['producer'] = producer
        record['extraction']['prompt_version'] = prompt_version
        record['extraction']['generated_at'] = now()
        checked = check_record(record, schema, source_texts={case['replacement']['to']: source_text})
        if checked['errors'] or any(warning.startswith('empty_content:') for warning in checked['warnings']):
            raise ValueError(item['key'] + ' corrected record fails the existing checker')
        record_path = case['batch'] / 'records' / (item['key'] + '.json')
        journal_path = journal_directory(case['batch'], item['key']) / 'journal.json'
        response_path = journal_path.parent / 'response.json'
        if record_path.exists() or journal_path.exists() or response_path.exists():
            raise ValueError(item['key'] + ' recovery files appeared during the write')
        write_json(response_path, prepared['corrected'])
        write_json(record_path, record)
        journal = {
            'kind': 'evidence_path_recovery', 'key': item['key'], 'batch': item['batch'],
            'attempt': '004', 'source': inputs['source'], 'replacement': case['replacement'],
            'evidence_paths_replaced': prepared['replaced'],
            'evidence_paths_already_correct': prepared['already'],
            'original_response_sha256': digest(prepared['original_bytes']),
            'corrected_response_sha256': file_sha256(response_path),
            'record_sha256': file_sha256(record_path),
            'source_text_sha256': digest(source_text.encode('utf-8')),
            'metadata_sha256': metadata_sha, 'check_errors': checked['errors'],
            'applied_at': record['extraction']['generated_at'],
        }
        write_json(journal_path, journal)
        if file_sha256(case['attempt'] / 'response.txt') != journal['original_response_sha256']:
            raise ValueError(item['key'] + ' attempt response changed while recovery was written')
        current = states[item['key']]
        states[item['key']] = {
            **{field: value for field, value in current.items() if field not in ('failure_stage', 'error')},
            'state': 'succeeded', 'record': f'records/{item["key"]}.json', 'attempt': current['attempt'],
            'previous_failure_stage': current.get('failure_stage'), 'previous_error': current.get('error'),
            'evidence_path_recovery': f'recoveries/{JOURNAL_NAME}/{item["key"]}/journal.json',
            'updated_at': journal['applied_at'],
        }
        write_json(case['batch'] / 'state.json', states)
        after_attempts = {path: path.read_bytes() for path in case['attempt'].parent.rglob('*') if path.is_file()}
        if after_attempts != before_attempts:
            raise ValueError(item['key'] + ' attempt files changed')
        written.append({**preview_item, 'status': 'applied', 'wrote': True,
                        'record_sha256': journal['record_sha256'],
                        'corrected_response_sha256': journal['corrected_response_sha256']})
    return written


def evidence_journal_status(attempt):
    """Return absent, or raise when a recovery journal does not prove the attempt."""
    journal_path = journal_directory(attempt.parents[2], attempt.parent.name) / 'journal.json'
    if not journal_path.is_file():
        return 'absent'
    journal = strict_json_loads(journal_path.read_text(encoding='utf-8'))
    response_path = journal_path.parent / 'response.json'
    original = (attempt / 'response.txt').read_bytes()
    if (journal.get('attempt') != attempt.name or journal.get('key') != attempt.parent.name
            or journal.get('original_response_sha256') != digest(original)
            or not response_path.is_file()
            or file_sha256(response_path) != journal.get('corrected_response_sha256')):
        raise ValueError('evidence path journal does not match the retained attempt')
    original_response = strict_json_loads(original.decode('utf-8'))
    corrected = strict_json_loads(response_path.read_text(encoding='utf-8'))
    replacement = journal.get('replacement') or {}
    if not same_except_evidence_path(original_response, corrected, replacement.get('from'), replacement.get('to')):
        raise ValueError('evidence path journal changes more than the approved path')
    return 'valid'


def load_attempt_response(attempt):
    """Original response, or the journaled path correction when one was proved."""
    original = strict_json_loads((attempt / 'response.txt').read_text(encoding='utf-8'))
    if evidence_journal_status(attempt) == 'absent':
        return original
    corrected = strict_json_loads((journal_directory(attempt.parents[2], attempt.parent.name) / 'response.json').read_text(encoding='utf-8'))
    return corrected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('preview', 'apply'))
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--plan', type=Path, required=True)
    args = parser.parse_args()
    plan = load_plan(args.plan)
    if args.command == 'preview':
        results = evaluate(args.root, plan)
        print(json.dumps({'command': 'preview', 'results': results}, ensure_ascii=False, indent=2))
        return 0 if all(item['status'] in ('ready', 'already_applied') for item in results) else 1
    results = apply_plan(args.root, plan)
    print(json.dumps({'command': 'apply', 'results': results}, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
