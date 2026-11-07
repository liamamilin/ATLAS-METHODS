"""Offline regression tests for this review. No network or upstream execution."""
from __future__ import annotations
from copy import deepcopy
from pathlib import Path
from datetime import datetime, timezone
import importlib.util
import json
import random
import subprocess
import sys
import tempfile
from check_record import CHECKER_VERSION, check_record, interval_covered, source_line_count, strict_json_loads, walk

HERE=Path(__file__).resolve().parent
schema=strict_json_loads((HERE/'skill-record.v0.1.1.schema.json').read_text(encoding='utf-8'))
base=strict_json_loads((HERE/'examples.v0.1.1.jsonl').read_text(encoding='utf-8').splitlines()[0])
cases=[]
def check(name, condition, observation=None):
    cases.append({'case':name,'passed':bool(condition),'observation':observation})
    if not condition: raise AssertionError(cases[-1])
def errors(r,fragment,texts=None):
    result=check_record(r,schema,texts)
    return any(fragment in e for e in result['errors']),result

r=deepcopy(base);r['content']['title']=' \t\n'
ok,res=errors(r,'blank_string');check('blank_title_not_present_value',ok,res)
r=deepcopy(base);r['content']['tasks'][0]['goal']='  '
ok,res=errors(r,'blank_string');check('blank_goal_not_present_value',ok,res)
r=deepcopy(base);r['content']['notes']=[{'topic':'command','text':'  echo "two  spaces"\n', 'evidence':r['content']['evidence']}]
before=deepcopy(r);res=check_record(r,schema)
check('meaningful_command_whitespace_preserved',not res['errors'] and r==before,res)
for bad in ['../outside.md','/tmp/SKILL.md','skills/../SKILL.md','skills//SKILL.md',r'C:\temp\SKILL.md','https://example.org/SKILL.md']:
    r=deepcopy(base);r['source']['skill_path']=bad;r['extraction']['files_read']=[{'path':bad,'coverage':'full'}]
    for _,n in walk(r['content']):
        if isinstance(n,dict) and 'evidence' in n:
            for e in n['evidence']:e['path']=bad
    ok,res=errors(r,'invalid_repository_path');check('invalid_evidence_path_'+bad,ok,res)
r=deepcopy(base);p='资料/任务 guide/SKILL.md';r['source']['skill_path']=p;r['extraction']['files_read']=[{'path':p,'coverage':'full'}]
for _,n in walk(r['content']):
    if isinstance(n,dict) and 'evidence' in n:
        for e in n['evidence']:e['path']=p
check('unicode_and_space_paths_allowed',not check_record(r,schema)['errors'])
r=deepcopy(base);r['content']['resources'][0]['locator']='../../outside-reference.md'
check('resource_locator_is_data_not_read_path',not check_record(r,schema)['errors'])
for text,want in [('',0),('a',1),('a\n',1),('a\nb',2),('a\r\nb\r\n',2),('a\u2028b',1),('a\v\fb',1)]:
    check('LF_line_count_'+repr(text),source_line_count(text)==want,{'expected':want,'actual':source_line_count(text)})
r=deepcopy(base);p=r['source']['skill_path'];r['content'].update(tasks=[],rules=[],resources=[],notes=[],evidence=[{'path':p,'line_start':2,'line_end':2}]);r['extraction']['gaps']=[]
ok,res=errors(r,'evidence_out_of_bounds',{p:'a\u2028b'});check('unicode_separator_cannot_invent_source_line',ok,res)
r=deepcopy(base);r['content']['evidence'][0]['line_start']=1.0;r['content']['evidence'][0]['line_end']=2.0
check('integral_JSON_numbers_remain_valid',not check_record(r,schema)['errors'])
for payload in ['{"value":"\\ud800"}','{"\\udfff":"value"}']:
    try:strict_json_loads(payload)
    except ValueError as e:check('unpaired_surrogate_'+payload,'invalid_unicode' in str(e),str(e))
    else:check('unpaired_surrogate_'+payload,False)
check('valid_unicode_surrogate_pair_is_allowed',strict_json_loads('"\\ud83d\\ude00"')=='😀')

rng=random.Random(20261107)
for _ in range(1000):
    ranges=[]
    for _ in range(rng.randrange(10)):
        a,b=sorted([rng.randrange(1,31),rng.randrange(1,31)])
        ranges.append({'line_start':a,'line_end':b})
    a,b=sorted([rng.randrange(1,31),rng.randrange(1,31)])
    expected=all(any(q['line_start']<=i<=q['line_end'] for q in ranges) for i in range(a,b+1))
    assert interval_covered(a,b,ranges)==expected,(a,b,ranges)
check('interval_coverage_matches_independent_set_oracle',True,{'trials':1000,'seed':20261107})

with tempfile.TemporaryDirectory(prefix='atlas-converge-') as temp:
    records=Path(temp)/'records.jsonl'
    valid=json.dumps(base,ensure_ascii=False).encode()+b'\n'
    def cli(payload):
        records.write_bytes(payload)
        result=subprocess.run([sys.executable,str(HERE/'check_record.py'),str(records)],capture_output=True,timeout=25)
        return result,[json.loads(l) for l in result.stdout.splitlines() if l.strip()]
    for name,bad in [('invalid_utf8',b'\xff\n'),('excessive_nesting',b'['*1500+b'0'+b']'*1500+b'\n'),('unpaired_surrogate',b'{"x":"\\ud800"}\n')]:
        p,rows=cli(bad+valid)
        check('bad_line_isolation_'+name,p.returncode==1 and len(rows)==2 and rows[0]['errors'] and not rows[1]['errors'],{'exit_code':p.returncode,'records':len(rows)})
    for name,bad in [('null',b'null\n'),('list',b'[]\n'),('boolean',b'true\n'),('string',b'"hello"\n')]:
        p,rows=cli(bad+valid)
        check('schema_shape_isolation_'+name,p.returncode==1 and len(rows)==2 and rows[0]['errors'] and not rows[1]['errors'])
    # Validate the CLI invocation when launched from a directory outside this bundle.
    records.write_bytes(valid)
    p=subprocess.run([sys.executable,str(HERE/'check_record.py'),str(records)],cwd=temp,capture_output=True,timeout=25)
    check('cli_default_schema_is_script_relative',p.returncode==0,p.stderr.decode('utf-8'))

# These are explicitly hand-authored source-grounded audit examples, not a trained model benchmark.
pilots=[strict_json_loads(l) for l in (HERE/'pilot-records.jsonl').read_text(encoding='utf-8').splitlines() if l.strip()]
for r in pilots:
    result=check_record(r,schema)
    check('pilot_structure_'+r['source']['repository'],not result['errors'],result)
check('pilot_has_multifile_read_coverage',all(len(r['extraction']['files_read'])>1 and r['extraction']['scope']=='entry_and_references' for r in pilots))
check('original_3P_contradiction_is_not_silently_fixed',any(g['reason']=='conflicting' for g in pilots[0]['extraction']['gaps']))
check('newsletter_recommendation_not_hardened',any('建议约 20–25' in q['text'] for q in pilots[0]['content']['rules']))
check('reviewer_and_coordinator_rules_keep_actor_scope',all(q['when'] for q in pilots[1]['content']['rules']))
check('audit_samples_not_self_certified',all(r['extraction']['review_status']=='unreviewed' and r['extraction']['execution_status']=='not_executed' for r in pilots))
report={'generated_at':datetime.now(timezone.utc).isoformat(),'checker_version':CHECKER_VERSION,'test_count':len(cases),'passed_count':sum(x['passed'] for x in cases),'cases':cases,'limits':['Synthetic tests and example invariants are not independent semantic evaluation.','This offline run does not retrieve sources or attest actual model delivery.']}
(HERE/'convergence-tests-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps({'new_regressions':len(cases),'passed':report['passed_count'],'interval_oracle_trials':1000}))
