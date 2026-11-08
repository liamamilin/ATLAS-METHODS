import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import fcntl
import threading
import unittest

import test_batch_convert as fixtures
from batch_convert import build_parser, key_of, run_batch, write_json, write_jsonl
from run_corpus import baseline_records, plan_references, prepare_batch, read_queue, run, snapshot, wait_for_batch
from model_routing import ModelDeferred


class CorpusTests(unittest.TestCase):
    def rolling_fixture(self, root, count=7, workers=2):
        args, rows, items = fixtures.BatchTests().fixture(root, count)
        write_json(args.selection, {'items':items[:1]})
        run_batch(args, fixtures.FakeClient())
        queue = root/'queue.jsonl'
        write_jsonl(queue, items[1:])
        full = root/'full'
        full.mkdir()
        return argparse.Namespace(out=full, queue=queue, index=args.index, baseline=args.out,
                                  batch_size=2, workers=workers, retry_failed=False, max_batches=None)

    def setup_outputs(self, root, replies=None):
        args, rows, items = fixtures.BatchTests().fixture(root, 3)
        all_out = args.out
        run_batch(args, fixtures.FakeClient())
        baseline = root / 'baseline'
        key = key_of(items[0])
        (baseline / 'records').mkdir(parents=True)
        shutil.copyfile(all_out / 'records' / (key+'.json'), baseline / 'records' / (key+'.json'))
        shutil.copytree(all_out / 'attempts' / key, baseline / 'attempts' / key)
        state = json.loads((all_out/'state.json').read_text())
        write_json(baseline/'state.json', {key:state[key]})
        write_jsonl(baseline/'records.jsonl', [json.loads((baseline/'records'/(key+'.json')).read_text())])
        full = root / 'full'
        args.out = full / 'batches/0001'
        queue = root/'queue.jsonl'
        write_jsonl(queue, items[1:])
        write_json(args.selection, {'items':items[1:]})
        run_batch(args, fixtures.FakeClient(replies))
        return argparse.Namespace(out=full, queue=queue, index=args.index, baseline=baseline, snapshot='test'),rows,items

    def test_snapshot_keeps_sources_paths_and_reuses_baseline_without_counting_usage(self):
        with tempfile.TemporaryDirectory() as t:
            args,rows,items=self.setup_outputs(Path(t))
            r=snapshot(args)
            self.assertEqual((r['total_succeeded'],r['new_succeeded'],r['remaining']),(3,2,0))
            self.assertEqual(r['usage_this_run']['tokens']['input'],20)
            p=args.out/'checkpoints/test'
            entries=[json.loads(s) for s in (p/'index.jsonl').read_text().splitlines()]
            self.assertEqual(len({x['source']['collection_id'] for x in entries}),3)
            for x in entries:
                actual=p/x['record_path']
                self.assertTrue(actual.is_file())
                self.assertEqual(hashlib.sha256(actual.read_bytes()).hexdigest(),x['record_sha256'])

    def test_failed_record_does_not_hide_success_or_disappear_from_remaining(self):
        with tempfile.TemporaryDirectory() as t:
            args,rows,items=self.setup_outputs(Path(t),['bad JSON','bad JSON',fixtures.response()])
            r=snapshot(args)
            self.assertEqual((r['total_succeeded'],r['failed'],r['remaining']),(2,1,1))
            p=args.out/'checkpoints/test'
            left=json.loads((p/'remaining.jsonl').read_text())
            self.assertEqual(left['state'],'failed')
            self.assertTrue((p/left['detail_path']).is_file())

    def test_baseline_must_have_accessible_actual_records(self):
        with tempfile.TemporaryDirectory() as t:
            args,rows,items=self.setup_outputs(Path(t))
            next((args.baseline/'records').glob('*.json')).unlink()
            with self.assertRaises(FileNotFoundError):baseline_records(args.baseline)

    def test_snapshot_recovers_record_saved_before_state_publication(self):
        with tempfile.TemporaryDirectory() as t:
            args,rows,items=self.setup_outputs(Path(t))
            write_json(args.out/'batches/0001/state.json',{})
            result=snapshot(args)
            self.assertEqual(result['total_succeeded'],3)

    def test_changed_semantics_or_wrong_provenance_cannot_enter_snapshot(self):
        with tempfile.TemporaryDirectory() as t:
            args,rows,items=self.setup_outputs(Path(t))
            p=next((args.out/'batches/0001/records').glob('*.json'))
            d=json.loads(p.read_text());d['source']['repository']='wrong/repo';write_json(p,d)
            with self.assertRaisesRegex(ValueError,'actual collection index'):snapshot(args)
            self.assertFalse((args.out/'checkpoints/test').exists())

    def test_queue_and_batch_freeze_reject_duplicate_or_changed_identities(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);args,rows,items=fixtures.BatchTests().fixture(root,2)
            q=root/'queue.jsonl';write_jsonl(q,[items[0],items[0]])
            with self.assertRaises(ValueError):read_queue(q)
            batch=root/'batch';prepare_batch(items,args.index,batch)
            before=(batch/'selection.json').read_bytes()
            prepare_batch(items,args.index,batch)
            self.assertEqual(before,(batch/'selection.json').read_bytes())
            with self.assertRaises(ValueError):prepare_batch(items[::-1],args.index,batch)

    def test_direct_guides_use_manifest_storage_paths_and_preserve_omissions(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);args,rows,items=fixtures.BatchTests().fixture(root,1)
            r=rows[0];entry=args.index.parent/r['raw_path']
            entry.write_text('Read [core](guide.md). Optional [missing](missing.md). `script.py` [web](https://example.invalid/x.md)')
            refs,missing=plan_references(r,args.index)
            self.assertEqual(refs,[{'path':'guide.md'}])
            self.assertTrue(any(m['path'].endswith('missing.md') for m in missing))
            refs,missing=plan_references(r,args.index,budget=1)
            self.assertEqual(refs,[])
            self.assertTrue(any('budget' in m['reason'] for m in missing))

    def test_resumed_parent_waits_for_existing_batch_child_lock(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);ready=threading.Event()
            with (root/'.run.lock').open('a') as active:
                fcntl.flock(active,fcntl.LOCK_EX)
                def wait():
                    wait_for_batch(root);ready.set()
                thread=threading.Thread(target=wait);thread.start()
                self.assertFalse(ready.wait(0.05))
                fcntl.flock(active,fcntl.LOCK_UN)
                thread.join(timeout=2)
                self.assertTrue(ready.is_set())

    def test_rolling_fills_next_batch_before_slow_previous_task_and_caps_concurrency(self):
        class Client(fixtures.FakeClient):
            def __init__(self):
                super().__init__(); self.next_started=threading.Event(); self.lock=threading.Lock()
                self.active=0;self.peak=0;self.overlap=False
            def call(self, prompt, attempt):
                identity=json.loads((attempt/'metadata.json').read_text())['source']['collection_id']
                with self.lock:self.active+=1;self.peak=max(self.peak,self.active)
                try:
                    if identity=='fixture-1':
                        if not self.next_started.wait(3):raise RuntimeError('Batch barrier left a free worker idle')
                    if identity=='fixture-3':
                        self.overlap=True;self.next_started.set()
                    return super().call(prompt,attempt)
                finally:
                    with self.lock:self.active-=1
        with tempfile.TemporaryDirectory() as t:
            args=self.rolling_fixture(Path(t));client=Client();run(args,client)
            self.assertTrue(client.overlap);self.assertLessEqual(client.peak,2)
            p=json.loads((args.out/'progress.json').read_text())
            self.assertEqual((p['new_succeeded'],p['failed']),(6,0))
            self.assertEqual(p['scheduler_run']['max_in_flight'],2)
            previous={str(p):p.read_bytes() for p in args.out.glob('batches/*/records/*.json')}
            resumed=fixtures.FakeClient();run(args,resumed)
            self.assertEqual(resumed.calls,[])
            self.assertTrue(all(Path(p).read_bytes()==data for p,data in previous.items()))

    def test_rolling_failure_continues_and_only_explicit_retry_revisits_it(self):
        class Client(fixtures.FakeClient):
            def call(self,prompt,attempt):
                identity=json.loads((attempt/'metadata.json').read_text())['source']['collection_id']
                if identity=='fixture-1':
                    (attempt/'response.txt').write_text('invalid')
                    return {'text':'invalid','model':'fixture','finish':'stop','tool_events':[]}
                return super().call(prompt,attempt)
        with tempfile.TemporaryDirectory() as t:
            args=self.rolling_fixture(Path(t),count=5);run(args,Client())
            p=json.loads((args.out/'progress.json').read_text())
            self.assertEqual((p['new_succeeded'],p['failed']),(3,1))
            resumed=fixtures.FakeClient();run(args,resumed);self.assertEqual(resumed.calls,[])
            args.retry_failed=True;retry=fixtures.FakeClient();run(args,retry)
            self.assertEqual(len(retry.calls),1)
            self.assertEqual(json.loads((args.out/'progress.json').read_text())['new_succeeded'],4)

    def test_graceful_stop_drains_in_flight_and_resume_preserves_saved_results(self):
        stop=threading.Event();both=threading.Barrier(2)
        class Client(fixtures.FakeClient):
            def call(self,prompt,attempt):
                both.wait(timeout=3);stop.set()
                return super().call(prompt,attempt)
        with tempfile.TemporaryDirectory() as t:
            args=self.rolling_fixture(Path(t));client=Client();run(args,client,stop)
            p=json.loads((args.out/'progress.json').read_text())
            self.assertEqual(p['status'],'stopped');self.assertEqual(len(client.calls),2)
            saved={str(p):p.read_bytes() for p in args.out.glob('batches/*/records/*.json')}
            resumed=fixtures.FakeClient();run(args,resumed)
            self.assertEqual(len(resumed.calls),4)
            self.assertTrue(all(Path(p).read_bytes()==data for p,data in saved.items()))

    def test_deferred_item_remains_pending_then_resumes_without_losing_other_sources(self):
        class Client(fixtures.FakeClient):
            first=True
            def call(self,prompt,attempt):
                if self.first:
                    self.first=False
                    raise ModelDeferred('fixture free fallback unavailable','fixture')
                return super().call(prompt,attempt)
        with tempfile.TemporaryDirectory() as t:
            args=self.rolling_fixture(Path(t),count=3,workers=1);run(args,Client(),deferred_delay=.01)
            p=json.loads((args.out/'progress.json').read_text())
            self.assertEqual((p['new_succeeded'],p['failed']),(2,0))
            self.assertEqual(p['scheduler_run']['deferred'],1)

    def test_rolling_handoff_waits_for_old_batch_and_does_not_duplicate_its_calls(self):
        with tempfile.TemporaryDirectory() as t:
            args=self.rolling_fixture(Path(t),count=3,workers=1)
            items=read_queue(args.queue);directory=args.out/'batches/0001'
            selection=prepare_batch(items,args.index,directory)
            finished=threading.Event();client=fixtures.FakeClient();errors=[]
            def resume():
                try:run(args,client)
                except BaseException as exc:errors.append(exc)
                finally:finished.set()
            with (directory/'.run.lock').open('a') as old:
                fcntl.flock(old,fcntl.LOCK_EX)
                thread=threading.Thread(target=resume);thread.start()
                self.assertFalse(finished.wait(.05));self.assertEqual(client.calls,[])
                # The old surviving batch publishes its actual results while
                # the resumed queue is waiting for the same directory lock.
                batch_args=build_parser().parse_args(['--index',str(args.index),'--selection',str(selection),'--out',str(directory)])
                run_batch(batch_args,fixtures.FakeClient())
                fcntl.flock(old,fcntl.LOCK_UN)
                thread.join(timeout=3)
            self.assertTrue(finished.is_set());self.assertEqual(errors,[])
            self.assertEqual(client.calls,[])
            self.assertEqual(json.loads((args.out/'progress.json').read_text())['new_succeeded'],2)


if __name__=='__main__':unittest.main()
