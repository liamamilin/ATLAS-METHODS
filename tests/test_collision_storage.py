import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from collect_skills import export_source, git, write_json, write_jsonl
from merge_collections import merge
from verify_collection import verify


class CollisionStorageTests(unittest.TestCase):
    def test_distinct_git_paths_survive_case_and_unicode_collisions_and_reruns(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            work = base / "upstream"
            work.mkdir()
            git("init", str(work))
            git("-C", str(work), "config", "core.ignorecase", "false")
            git("-C", str(work), "config", "core.precomposeUnicode", "false")
            fixtures = {"skills/demo/SKILL.md": b"---\nname: upper\n---\nUpper bytes.\n",
                        "skills/demo/skill.md": b"---\nname: lower\n---\nLower bytes.\n",
                        "caf\u00e9/SKILL.md": b"Composed original.\n",
                        "cafe\u0301/SKILL.md": b"Decomposed original.\n",
                        "references/context.md": b"Shared context.\n"}
            # Populate the Git index directly: the host cannot create both names.
            for path, data in fixtures.items():
                sha = subprocess.run(["git", "-C", str(work), "hash-object", "-w", "--stdin"],
                                     input=data, capture_output=True, check=True).stdout.decode().strip()
                git("-C", str(work), "update-index", "--add", "--cacheinfo", f"100644,{sha},{path}")
            git("-C", str(work), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "case fixture")
            commit = git("-C", str(work), "rev-parse", "HEAD").decode().strip()
            out = base / "batch"
            cache = out / ".cache" / "fixture__case.git"
            cache.parent.mkdir(parents=True)
            git("clone", "--bare", str(work), str(cache))
            legacy = out / "raw" / "fixture/case" / commit / "skills/demo/skill.md"
            legacy.parent.mkdir(parents=True)
            legacy.write_bytes(fixtures["skills/demo/skill.md"])
            first, report = export_source("fixture/case", cache, commit, out, 1024**2)
            self.assertEqual(report["saved_skill_entries"], 4)
            self.assertEqual(len({r["raw_path"] for r in first}), 4)
            for r in first:
                self.assertEqual((out / r["raw_path"]).read_bytes(), fixtures[r["skill_path"]])
            self.assertFalse(legacy.exists())
            self.assertEqual(next(r for r in first if r["skill_path"].endswith("demo/SKILL.md"))["declared_name"], "upper")
            second, _ = export_source("fixture/case", cache, commit, out, 1024**2)
            self.assertEqual([(r["raw_path"], r["sha256"]) for r in first], [(r["raw_path"], r["sha256"]) for r in second])
            self.assertEqual((out / "raw/fixture/case" / commit / "references/context.md").read_bytes(), fixtures["references/context.md"])
            write_jsonl(out / "skills.jsonl", second)
            write_json(out / "collection-report.json", {"sources": [report]})
            merged = base / "merged"
            merge([out], merged)
            result = verify(merged / "sources.lock.json", merged)
            self.assertEqual(result["verified_skill_entries"], 4)


if __name__ == "__main__":
    unittest.main()
