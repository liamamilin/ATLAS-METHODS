"""Offline regression probes; synthetic cases are not real Skill conversions."""
from pathlib import Path
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import argparse
from jsonschema import Draft202012Validator
from check_record import check_record

HERE = Path(__file__).resolve().parent
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--original-schema', type=Path, help='Optional old schema for before/after probes')
parser.add_argument('--original-examples', type=Path, help='Optional previous examples for semantic-change comparison')
args = parser.parse_args()
OLD = args.original_schema
ORIGINAL_EXAMPLES = args.original_examples
new_schema = json.loads((HERE/'skill-record.v0.1.1.schema.json').read_text(encoding='utf-8'))
old_schema = json.loads(OLD.read_text(encoding='utf-8')) if OLD else None
for schema in (old_schema, new_schema):
    if schema is not None:
        Draft202012Validator.check_schema(schema)
old_validator = Draft202012Validator(old_schema) if old_schema else None
new_validator = Draft202012Validator(new_schema)
old_rows = ([json.loads(s) for s in ORIGINAL_EXAMPLES.read_text(encoding='utf-8').splitlines() if s.strip()]
            if ORIGINAL_EXAMPLES else [])
new_rows = [json.loads(s) for s in (HERE/'examples.v0.1.1.jsonl').read_text(encoding='utf-8').splitlines() if s.strip()]
assert len(new_rows) == 3
if old_rows:
    assert len(old_rows) == len(new_rows)

probes = []
if old_rows and old_validator:
    r = deepcopy(old_rows[0]); r['extraction']['files_read'][0]['coverage'] = 'partial'
    probes.append({'case':'partial_without_ranges', 'accepted_by_v0_1_0':old_validator.is_valid(r)})
    r['extraction']['files_read'][0]['ranges'] = [{'line_start':1, 'line_end':10}]
    probes.append({'case':'partial_with_ranges', 'accepted_by_v0_1_0':old_validator.is_valid(r)})
    r = deepcopy(old_rows[0]); r['content']['summary'] = None
    for key in ('tasks','rules','resources','notes'): r['content'][key] = []
    r['extraction']['gaps'] = []
    probes.append({'case':'empty_semantic_body', 'accepted_by_v0_1_0':old_validator.is_valid(r)})

records = []
for i, new in enumerate(new_rows):
    if old_rows:
        old = old_rows[i]
        old_copy = deepcopy(old); old_copy['schema_version'] = '0.1.1'
        assert old_copy == new, 'Unexpected semantic or source modification'
        if old_validator:
            assert old_validator.is_valid(old)
    result = check_record(new, new_schema)
    assert not result['errors'], result
    records.append({'name':new['content']['name'], 'structure':'passed', 'metadata_consistency':'passed',
                    'source_bytes_revalidated':False, 'source_semantics_revalidated':False,
                    'warnings':result['warnings']})

path = 'skills/fixture/SKILL.md'
evidence = [{'path':path, 'line_start':1, 'line_end':2}]
source_texts = {path:'\n'.join(f'fixture line {i}' for i in range(1,21))}
base = {
    'schema_version':'0.1.1',
    'source': {'collection_id':'synthetic-test-only', 'repository':'fixture/repo',
               'commit_sha':'0'*40, 'skill_path':path, 'git_blob_sha':'0'*40},
    'content': {'name':'fixture', 'title':'人工构造测试，不是来源采集', 'summary':None,
                'evidence':deepcopy(evidence), 'tasks':[],
                'rules':[{'text':'Preserve the source rule.', 'when':None, 'rationale':None, 'evidence':deepcopy(evidence)}],
                'resources':[], 'notes':[]},
    'extraction':{'language':'zh-CN', 'scope':'entry_only',
                  'files_read':[{'path':path,'coverage':'full'}], 'gaps':[],
                  'review_status':'unreviewed', 'execution_status':'not_executed'}
}
task = {'id':'t1', 'goal':'Fixture task', 'when':None, 'inputs':[], 'outputs':[],
        'steps':[], 'checks':[], 'evidence':deepcopy(evidence)}
cases = []
def case(name, record, error_fragment=None, warning_fragment=None):
    result = check_record(record, new_schema, source_texts)
    passed = (any(error_fragment in x for x in result['errors']) if error_fragment else not result['errors'])
    if warning_fragment:
        passed = passed and any(warning_fragment in x for x in result['warnings'])
    cases.append({'case':name, 'expected':'error: '+error_fragment if error_fragment else 'no consistency errors',
                  'passed':passed, 'errors':result['errors'], 'warnings':result['warnings']})
    assert passed, cases[-1]

case('pure_rule_no_procedure_is_valid',deepcopy(base))
r=deepcopy(base); r['content']['tasks']=[deepcopy(task)]
case('missing_checks_and_rationale_are_allowed',r)
r=deepcopy(base); r['content']['rules']=[]
case('empty_body_kept_with_warning_not_automatic_success',r,warning_fragment='empty_content')
r=deepcopy(base); r['content']['tasks']=[deepcopy(task),deepcopy(task)]
case('duplicate_task_id',r,'duplicate_task_id')
r=deepcopy(base); r['extraction']['files_read'][0]['coverage']='partial'
case('partial_read_requires_ranges',r,'schema')
r=deepcopy(base); r['extraction']['files_read'][0].update(coverage='partial',ranges=[{'line_start':1,'line_end':3}])
case('partial_read_covers_evidence',r)
r=deepcopy(r); r['content']['rules'][0]['evidence'][0].update(line_start=8,line_end=10)
case('evidence_outside_actual_read',r,'evidence_outside_read_ranges')
r=deepcopy(base); r['extraction']['files_read'][0].update(coverage='partial',ranges=[{'line_start':5,'line_end':2}])
case('reversed_read_range',r,'read_range_reversed')
r=deepcopy(base); r['extraction']['files_read'][0].update(coverage='partial',ranges=[{'line_start':1,'line_end':30}])
case('read_range_exceeds_file',r,'read_range_out_of_bounds')
r=deepcopy(base); r['content']['evidence'][0].update(line_start=5,line_end=1)
case('reversed_evidence_range',r,'evidence_range_reversed')
r=deepcopy(base); r['content']['evidence'][0].update(line_start=1,line_end=40)
case('evidence_range_exceeds_file',r,'evidence_out_of_bounds')
r=deepcopy(base); r['content']['evidence'][0]['path']='unread.md'
case('unread_evidence_file',r,'evidence_unread')
r=deepcopy(base); r['extraction']['scope']='entry_and_references'
case('scope_contradicts_read_files',r,'scope_mismatch')
r=deepcopy(base); r['extraction']['files_read']=[{'path':'other.md','coverage':'full'}]
case('entry_absent_from_reads',r,'entry_not_read')
r=deepcopy(base); r['extraction']['gaps']=[{'target':'/content/tasks/99','reason':'other','detail':'fixture'}]
case('dangling_gap_target',r,'gap_target_missing')
r=deepcopy(base); r['extraction']['files_read'].append(deepcopy(r['extraction']['files_read'][0]))
case('duplicate_file_read_rows',r,'duplicate_read_file')
r=deepcopy(base); r['content']['resources']=[{'name':'unread helper', 'kind':'script', 'locator':'scripts/helper.py',
                                            'usage':'Mentioned by entry; not inspected.', 'evidence':deepcopy(evidence)}]
case('resource_mentioned_is_not_required_to_be_read',r)
r=deepcopy(base); r['content']['tasks']=[deepcopy(task),{**deepcopy(task),'id':'t2','goal':'Another fixture task'}]
r['content']['rules'][0]['when']='Only while performing t1'
case('multi_task_condition_is_representable_without_new_id_graph',r)
r=deepcopy(base); r['extraction']['files_read'][0].update(coverage='partial',ranges=[{'line_start':1,'line_end':1},{'line_start':2,'line_end':4}])
case('adjacent_ranges_cover_a_span',r)
r=deepcopy(base); r['extraction']['files_read'][0]['ranges']=[{'line_start':1,'line_end':20}]
case('full_does_not_take_partial_ranges',r,'schema')

report = {'generated_at':datetime.now(timezone.utc).isoformat(),
          'original_schema_sha256':hashlib.sha256(OLD.read_bytes()).hexdigest() if OLD else None,
          'patched_schema_sha256':hashlib.sha256((HERE/'skill-record.v0.1.1.schema.json').read_bytes()).hexdigest(),
          'original_schema_probes':probes, 'existing_sample_results':records,
          'synthetic_test_count':len(cases), 'synthetic_pass_count':sum(c['passed'] for c in cases), 'synthetic_tests':cases,
          'limitations':['The three real-source sample records were supplied artifacts, not newly fetched source files.',
                         'Source identity, raw-file hashes and semantic fidelity were not independently revalidated in this run.',
                         'Synthetic fixtures test structure and consistency only, not generalization across real skills.',
                         'No Skill installed/executed; no GitHub writes.']}
(HERE/'test-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps({'samples':len(records),'synthetic_tests':len(cases),'passed':sum(c['passed'] for c in cases)},ensure_ascii=False))
