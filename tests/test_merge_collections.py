import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from collect_skills import blob_hash, duplicate_groups, export_source, git, write_json, write_jsonl
from merge_collections import merge
from verify_collection import verify


class MergeTests(unittest.TestCase):
    def fixture(self, base, name, repo="test/one", commit="1" * 40):
        root = base / name
        path = "hidden/.plugin/nested/SKILL.md"
        raw = f"raw/{repo}/{commit}/{path}"
        data = b"---\nname: preserved\n---\nUnchanged bytes.\r\n"
        destination = root / raw
        destination.parent.mkdir(parents=True)
        destination.write_bytes(data)
        manifest = f"manifests/{repo}/{commit}.json"
        item = {"path": path, "status": "saved", "raw_path": raw, "sha256": hashlib.sha256(data).hexdigest()}
        record = {"id": hashlib.sha256(f"{repo}:{path}".encode()).hexdigest()[:20], "repository": repo,
                  "commit_sha": commit, "skill_path": path, "state": "collected", "raw_path": raw,
                  "sha256": item["sha256"], "git_blob_sha": blob_hash(data), "repository_manifest": manifest}
        write_json(root / manifest, {"repository": repo, "commit_sha": commit, "files": [item]})
        write_jsonl(root / "skills.jsonl", [record])
        write_json(root / "collection-report.json", {"sources": [{"repository": repo, "commit_sha": commit, "repository_manifest": manifest}]})
        return root, destination

    def test_repeat_merge_retains_sources_bytes_and_relative_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            old, raw = self.fixture(base, "old")
            new, _ = self.fixture(base, "new", "test/two")
            before = raw.read_bytes()
            out = base / "merged"
            first = merge([old, new, old], out)
            index = (out / "skills.jsonl").read_bytes()
            second = merge([old, new, old], out)
            self.assertEqual(index, (out / "skills.jsonl").read_bytes())
            self.assertEqual(first["saved_skill_entries"], 2)
            self.assertEqual(second["unique_entry_texts"], 1)
            self.assertEqual(second["duplicate_entry_groups"], 1)
            self.assertEqual(raw.read_bytes(), before)
            for line in index.splitlines():
                row = json.loads(line)
                self.assertEqual((out / row["raw_path"]).read_bytes(), before)
                self.assertTrue((out / row["repository_manifest"]).is_file())

    def test_versions_are_snapshots_and_failed_sources_do_not_remove_success(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            old, _ = self.fixture(base, "old")
            newer, _ = self.fixture(base, "newer", commit="2" * 40)
            status = base / "attempts.json"
            write_json(status, [{"repository": "test/failed", "state": "failed", "error": "unavailable pin"},
                                {"repository": "test/empty", "state": "no_skill_files"}])
            report = merge([old, newer], base / "merged", [status])
            self.assertEqual(report["completed_sources"], 1)
            self.assertEqual(report["indexed_snapshots"], 2)
            self.assertEqual(report["distinct_skill_paths"], 1)
            self.assertEqual(report["failed_sources"], 1)
            self.assertEqual(report["no_skill_sources"], 1)

    def test_missing_or_changed_raw_material_cannot_count_as_collected(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            old, raw = self.fixture(base, "old")
            raw.write_bytes(b"changed")
            with self.assertRaises(ValueError):
                merge([old], base / "merged")
            raw.unlink()
            with self.assertRaises(FileNotFoundError):
                merge([old], base / "merged")

    def test_existing_git_verifier_follows_merged_paths_and_unsaved_symlinks(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            work = base / "upstream"
            work.mkdir()
            (work / "SKILL.md").write_bytes(b"---\nname: demo\n---\nOriginal.\n")
            (work / "LICENSE").write_bytes(b"Fixture license\n")
            (work / ".alias").mkdir()
            (work / ".alias" / "SKILL.md").symlink_to("../SKILL.md")
            git("init", str(work))
            git("-C", str(work), "add", ".")
            git("-C", str(work), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "fixture")
            commit = git("-C", str(work), "rev-parse", "HEAD").decode().strip()
            root = base / "batch" / "repositories" / "fixture" / "one"
            cache = root / ".cache" / "fixture__one.git"
            cache.parent.mkdir(parents=True)
            git("clone", "--bare", str(work), str(cache))
            rows, source = export_source("fixture/one", cache, commit, root, 1024**2)
            write_jsonl(root / "skills.jsonl", rows)
            write_json(root / "collection-report.json", {"sources": [source]})
            out = base / "merged"
            status = base / "attempts.json"
            write_json(status, [{"repository": "fixture/one", "state": "partial_text_snapshot"}])
            summary = merge([root], out, [status])
            verification = verify(out / "sources.lock.json", out)
            self.assertEqual(verification["result"], "passed")
            self.assertEqual(verification["verified_skill_entries"], 1)
            self.assertEqual(verification["verified_candidate_entries"], 2)
            self.assertEqual(summary["partial_sources"], 1)
            self.assertEqual(summary["completed_sources"], 0)
            inferred = merge([root], base / "inferred")
            self.assertEqual(inferred["partial_sources"], 1)


if __name__ == "__main__":
    unittest.main()
