"""Run this bundle's tests afresh. Optional pinned source snapshots strengthen the check."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import re
import subprocess
import sys
from check_record import CHECKER_VERSION, check_record, strict_json_loads

HERE=Path(__file__).resolve().parent

def load(p):return strict_json_loads(p.read_text(encoding='utf-8'))
def digest(p):return hashlib.sha256(p.read_bytes()).hexdigest()

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root',type=Path,help='Optional snapshots at <root>/<owner>/<repo>/<commit>/<repo-relative-file>. No downloads.')
    args=parser.parse_args()
    report={'generated_at':datetime.now(timezone.utc).isoformat(),'status':'running','schema_version':'0.1.1','checker_version':CHECKER_VERSION,
            'environment':{'python':platform.python_version(),'platform':platform.system(),'jsonschema':importlib.metadata.version('jsonschema')},'runs':[]}
    try:
        if not __debug__:raise RuntimeError('Run tests without -O/PYTHONOPTIMIZE; assertion tests must remain enabled.')
        for script,artifact in [('recheck_tests.py','test-report.json'),('final_tests.py','final-review-report.json'),('convergence_tests.py','convergence-tests-report.json'),('test_response_contract.py',None)]:
            process=subprocess.run([sys.executable,str(HERE/script)],cwd=HERE,capture_output=True,text=True,encoding='utf-8',timeout=90)
            report['runs'].append({'script':script,'exit_code':process.returncode,'stdout':process.stdout.strip(),'stderr':process.stderr.strip()})
            if process.returncode:raise RuntimeError(f'{script} failed; see runs')
        old=load(HERE/'test-report.json');prev=load(HERE/'final-review-report.json');new=load(HERE/'convergence-tests-report.json')
        contract_run=next(r for r in report['runs'] if r['script']=='test_response_contract.py')
        contract_count=re.search(r'Ran (\d+) tests? in ',contract_run['stderr'])
        if not contract_count:raise RuntimeError('Response contract test count missing from unittest report')
        response_tests=int(contract_count.group(1))
        report['regressions']={'baseline':old['synthetic_test_count'],'prior_final':prev['new_synthetic_test_count'],'convergence':new['test_count'],'response_contract':response_tests,'total':old['synthetic_test_count']+prev['new_synthetic_test_count']+new['test_count']+response_tests,'all_passed':True,'interval_set_oracle_trials':1000}
        report['legacy_example_records']=3
        report['hand_authored_pilot_records']=4
        report['pilot_unique_source_versions']=3
        report['example_scope']='3 legacy entry examples; 2 expanded pilots; 1 tool-backed pilot; 1 new entry-only API pilot. One legacy record shares identity with an expanded pilot; not seven independent Skills.'
        report['source_snapshot_verification']={'status':'not_rechecked_this_run','reason':'Supply --source-root to repeat byte/hash and supplied-text range checks; source-audit.json is a prior observation, not fresh proof.'}
        if args.source_root:
            manifest=load(HERE/'source-audit.json');groups={};verified=0
            for f in manifest['files']:
                p=args.source_root/f['repository']/f['commit_sha']/f['path']
                b=p.read_bytes()
                blob=hashlib.sha1(b'blob '+str(len(b)).encode()+b'\0'+b).hexdigest()
                if len(b)!=f['bytes'] or hashlib.sha256(b).hexdigest()!=f['sha256'] or blob!=f['git_blob_sha']:
                    raise ValueError(f'Pinned source mismatch: {f["repository"]}:{f["path"]}')
                groups.setdefault((f['repository'],f['commit_sha']),{})[f['path']]=b.decode('utf-8');verified+=1
            schema=load(HERE/'skill-record.v0.1.1.schema.json');checks=[]
            for line in (HERE/'pilot-records.jsonl').read_text(encoding='utf-8').splitlines():
                r=strict_json_loads(line);s=r['source'];texts=groups[(s['repository'],s['commit_sha'])]
                entry=next(f for f in manifest['files'] if (f['repository'],f['commit_sha'],f['path'])==(s['repository'],s['commit_sha'],s['skill_path']))
                if s['git_blob_sha']!=entry['git_blob_sha'] or s['entry_sha256']!=entry['sha256']:raise ValueError('Pilot provenance mismatch with fetched manifest')
                result=check_record(r,schema,texts)
                if result['errors'] or result['warnings']:raise ValueError(result)
                checks.append({'repository':s['repository'],'skill_path':s['skill_path'],'result':'passed'})
            report['source_snapshot_verification']={'status':'passed','files':verified,'pilot_records':checks,'scope':'Bytes match returned Git blob identifiers; original-file line bounds and declared read coverage checked. No independent semantic grader or attestation of model delivery.'}
        report['schema_sha256']=digest(HERE/'skill-record.v0.1.1.schema.json')
        report['schema_unchanged_from_prior_delivery']=report['schema_sha256']=='6967575208f4898393154af8f7ef3ba05f7b07928cf4f80e9768e1a042f9494a'
        report['status']='passed'
        report['limitations']=['All supplied samples are assistant-authored illustrations, not a measured automated conversion pipeline.','Legacy internal-comms and the expanded pilot share a source identity; do not import both as distinct skills.','No full-corpus processing, independent semantic acceptance, upstream execution, or GitHub write was performed.','Runtime tested only in the environment above; other Python versions/platforms were not executed.']
    except (OSError,ValueError,RuntimeError,subprocess.TimeoutExpired) as exc:
        report['status']='failed';report['error']=str(exc)
    report['artifacts_sha256']={p.name:digest(p) for p in HERE.iterdir() if p.is_file() and p.name not in {'AUDIT-REPORT.json','bundle-manifest.json'} and p.suffix in {'.py','.json','.jsonl','.md','.txt'}}
    (HERE/'AUDIT-REPORT.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:report[k] for k in ['status','schema_version','checker_version','regressions','source_snapshot_verification'] if k in report},ensure_ascii=False,indent=2))
    return 0 if report['status']=='passed' else 1

if __name__=='__main__':raise SystemExit(main())
