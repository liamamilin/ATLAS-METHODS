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
from batch_convert import key_of, run_batch, write_json, write_jsonl
from run_corpus import baseline_records, plan_references, prepare_batch, read_queue, snapshot, wait_for_batch


class CorpusTests(unittest.TestCase):
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


if __name__=='__main__':unittest.main()
