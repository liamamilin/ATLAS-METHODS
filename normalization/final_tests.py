"""Final offline regression tests. Synthetic cases are not real Skill conversions."""
from __future__ import annotations
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import subprocess
import sys
import tempfile
from jsonschema import Draft202012Validator
from check_record import CHECKER_VERSION, check_record, strict_json_loads

HERE = Path(__file__).resolve().parent
SCHEMA_PATH = HERE / 'skill-record.v0.1.1.schema.json'
schema = json.loads(SCHEMA_PATH.read_text(encoding='utf-8'))
Draft202012Validator.check_schema(schema)
path = 'skills/demo/SKILL.md'
ref = 'skills/demo/references/guide.md'
texts = {path: '\n'.join(f'entry {i}' for i in range(1, 21)), ref: 'guide 1\nguide 2\nguide 3'}
evidence = [{'path': path, 'line_start': 1, 'line_end': 2}]
base = {
    'schema_version': '0.1.1',
    'source': {'collection_id': 'synthetic-only', 'repository': 'fixture/repo',
               'commit_sha': '0'*40, 'skill_path': path, 'git_blob_sha': '0'*40},
    'content': {'name': 'demo', 'title': '人工测试', 'summary': None,
                'evidence': deepcopy(evidence), 'tasks': [],
                'rules': [{'text': 'Preserve concrete instructions.', 'when': None,
                           'rationale': None, 'evidence': deepcopy(evidence)}],
                'resources': [], 'notes': []},
    'extraction': {'language': 'zh-CN', 'scope': 'entry_only',
                   'files_read': [{'path': path, 'coverage': 'full'}], 'gaps': [],
                   'review_status': 'unreviewed', 'execution_status': 'not_executed'}
}
cases = []
def ok(name, condition, observed):
    cases.append({'case': name, 'passed': bool(condition), 'observed': observed})
    if not condition:
        raise AssertionError(cases[-1])

def warns(result, fragment):
    return any(fragment in item for item in result['warnings'])

def errs(result, fragment):
    return any(fragment in item for item in result['errors'])

for name, supplied in [('missing_text_argument', None), ('empty_text_mapping', {}),
                        ('unrelated_text_mapping', {'other.md': 'other'})]:
    r = check_record(deepcopy(base), schema, supplied)
    ok(name, not r['errors'] and warns(r, 'source_bytes_not_checked') and warns(r, path), r)

multi = deepcopy(base)
multi['extraction']['scope'] = 'entry_and_references'
multi['extraction']['files_read'].append({'path': ref, 'coverage': 'full'})
multi['content']['rules'].append({'text': 'Reference rule', 'when': None, 'rationale': None,
                                 'evidence': [{'path': ref, 'line_start': 1, 'line_end': 2}]})
r = check_record(multi, schema, {path: texts[path]})
ok('missing_one_reference_text_is_explicit', not r['errors'] and warns(r, ref), r)
r = check_record(multi, schema, texts)
ok('complete_supplied_texts_do_not_warn_missing', not r['errors'] and not warns(r, 'source_bytes_not_checked') and 'provenance' in r['checks_scope'] and 'not checked' in r['checks_scope'], r)
r = check_record(base, schema, {path: None})
ok('invalid_text_value_is_error_not_crash', errs(r, 'invalid_source_text:'), r)
r = check_record(base, schema, [])
ok('invalid_text_mapping_is_error_not_crash', errs(r, 'invalid_source_texts:'), r)
ranged = deepcopy(base)
ranged['extraction']['files_read'][0].update(coverage='partial', ranges=[{'line_start':1,'line_end':1},{'line_start':3,'line_end':4}])
r = check_record(ranged, schema, texts)
ok('nonadjacent_read_ranges_cannot_cover_gap', errs(r, 'evidence_outside_read_ranges'), r)
section = deepcopy(base)
section['content']['evidence'] = [{'path':path, 'section':'Overview'}]
r = check_record(section, schema, texts)
ok('section_locator_stays_explicitly_unresolved', not r['errors'] and warns(r, 'section_locator_unresolved'), r)
r = check_record(base, schema, {path: 'only one line'})
ok('bounds_are_checked_when_text_is_supplied', errs(r, 'evidence_out_of_bounds'), r)
for name, payload in [('duplicate_root_key', '{"a":1,"a":2}'),
                      ('duplicate_nested_key', '{"tasks":{"steps":[],"steps":["real"]}}'),
                      ('non_json_constant', '{"value":NaN}')]:
    try:
        strict_json_loads(payload)
    except ValueError as exc:
        ok(name, True, str(exc))
    else:
        ok(name, False, 'unexpectedly accepted')
valid = '{"a":[1,2],"b":{"a":"中文"}}'
ok('same_key_in_different_objects_is_valid', strict_json_loads(valid) == {'a':[1,2],'b':{'a':'中文'}}, valid)
with tempfile.TemporaryDirectory(prefix='atlas-final-') as directory:
    target = Path(directory) / 'records.jsonl'
    def cli(payload):
        target.write_text(payload, encoding='utf-8')
        p = subprocess.run([sys.executable, str(HERE/'check_record.py'), '--schema', str(SCHEMA_PATH), str(target)],
                           capture_output=True, text=True, encoding='utf-8', timeout=20)
        return p, [json.loads(line) for line in p.stdout.splitlines() if line.strip()]
    for name, payload in [('empty_jsonl_is_not_success', ''), ('blank_jsonl_is_not_success', ' \n\n')]:
        p, result = cli(payload)
        ok(name, p.returncode == 1 and result and errs(result[0], 'empty_input'), result)
    p, result = cli('{"a":1,"a":2}\n'+json.dumps(base, ensure_ascii=False)+'\n')
    ok('bad_record_does_not_block_next_record', p.returncode == 1 and len(result) == 2 and errs(result[0], 'duplicate_json_key') and not result[1]['errors'], result)
    p, result = cli(json.dumps(base, ensure_ascii=False)+'\n')
    ok('valid_record_with_missing_source_text_still_checkable', p.returncode == 0 and len(result)==1 and warns(result[0], 'source_bytes_not_checked'), result)
    p, result = cli('{bad json}\n'+json.dumps(base, ensure_ascii=False)+'\n')
    ok('malformed_json_does_not_block_next_record', p.returncode == 1 and len(result)==2 and result[0]['errors'] and not result[1]['errors'], result)

samples=[]
for line in (HERE/'examples.v0.1.1.jsonl').read_text(encoding='utf-8').splitlines():
    record = strict_json_loads(line)
    result = check_record(record, schema)
    assert not result['errors'], result
    samples.append({'name':record['content']['name'], 'structure_consistency':'passed',
                    'source_text_supplied':False, 'warnings':result['warnings']})
prior = json.loads((HERE/'test-report.json').read_text(encoding='utf-8'))
report = {
    'generated_at': datetime.now(timezone.utc).isoformat(),
    'schema_version':'0.1.1', 'checker_patch':CHECKER_VERSION,
    'schema_sha256': hashlib.sha256(SCHEMA_PATH.read_bytes()).hexdigest(),
    'checker_sha256': hashlib.sha256((HERE/'check_record.py').read_bytes()).hexdigest(),
    'existing_sample_count':len(samples), 'existing_sample_results':samples,
    'original_synthetic_test_count':prior['synthetic_test_count'],
    'original_synthetic_pass_count':prior['synthetic_pass_count'],
    'new_synthetic_test_count':len(cases), 'new_synthetic_pass_count':sum(x['passed'] for x in cases),
    'total_synthetic_tests':prior['synthetic_test_count']+len(cases),
    'new_synthetic_cases':cases,
    'limitations':[
        'The existing samples are entry-only and unreviewed; no new real-source conversion was performed.',
        'Raw GitHub source bytes were not refetched or independently hash-verified in this test run.',
        'Synthetic tests check structure, metadata consistency and CLI behavior, not semantic fidelity.',
        'Reading a record declaration is not independent proof of actual model input coverage.',
        'No GitHub writes; no Skill installed or executed.'
    ]
}
(HERE/'final-review-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps({k:report[k] for k in ('schema_version','checker_patch','existing_sample_count','original_synthetic_pass_count','new_synthetic_pass_count','total_synthetic_tests')},ensure_ascii=False))
