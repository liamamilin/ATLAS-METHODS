#!/usr/bin/env python3
"""Audit a full pinned collection against local Git objects; never run upstream code."""
from __future__ import annotations

import argparse
from bisect import bisect_left
from collections import Counter, defaultdict
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import subprocess
from urllib.parse import quote


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def git(cache, *args, input=None):
    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "--git-dir", str(cache), *args],
        input=input, capture_output=True, check=True, timeout=240,
    ).stdout


def verify(sources, output):
    configured = read_json(sources)["sources"]
    pins = {s["repository"]: s["commit_sha"] for s in configured}
    assert len(pins) == len(configured), "Duplicate configured repository"
    lock = read_json(output / "sources.lock.json")["sources"]
    assert len(lock) == len(pins)
    assert {s["repository"]: s["commit_sha"] for s in lock} == pins
    roots = {s["repository"]: Path(s.get("collection_root", ".")) for s in lock}
    rows = [json.loads(line) for line in (output / "skills.jsonl").read_text().splitlines()]
    assert len({r["id"] for r in rows}) == len(rows), "Duplicate source record"
    assert len({(r["repository"], r["skill_path"]) for r in rows}) == len(rows)
    assert {r["repository"] for r in rows} <= pins.keys()
    report = read_json(output / "collection-report.json")
    reports = {r["repository"]: r for r in report["sources"]}
    assert len(report["sources"]) == len(pins) and set(reports) == set(pins)
    assert report["completed_sources"] + report.get("partial_sources", 0) == len(pins)
    assert report["failed_sources"] == len(report["errors"])
    assert report["requested_sources"] == (report["completed_sources"] + report.get("partial_sources", 0) + report["failed_sources"] +
                                            report.get("no_skill_sources", 0) + report.get("pending_sources", 0))
    totals = Counter()
    results = []
    manifests = []
    grouped = defaultdict(list)
    for repo, commit in pins.items():
        collection_root = output / roots[repo]
        cache = collection_root / ".cache" / (repo.replace("/", "__") + ".git")
        assert git(cache, "rev-parse", commit + "^{commit}").decode().strip() == commit
        manifest_path = roots[repo] / "manifests" / repo / (commit + ".json")
        manifest = read_json(output / manifest_path)
        manifests.append(manifest)
        assert manifest["repository"] == repo and manifest["commit_sha"] == commit
        files = {f["path"]: f for f in manifest["files"]}
        sorted_paths = sorted(files)
        assert len(files) == len(manifest["files"])
        tree = {}
        for entry in git(cache, "ls-tree", "-r", "-l", "-z", "--full-tree", commit).split(b"\0"):
            if entry:
                header, path = entry.split(b"\t", 1)
                mode, kind, sha, size = header.decode().split()
                tree[path.decode()] = (mode, kind, sha, None if size == "-" else int(size))
        assert set(tree) == set(files), f"Incomplete manifest: {repo}"
        saved = [f for f in files.values() if f["status"] == "saved"]
        shas = list(dict.fromkeys(f["git_blob_sha"] for f in saved))
        stream = io.BytesIO(git(cache, "cat-file", "--batch", input="".join(s + "\n" for s in shas).encode()))
        blobs = {}
        for sha in shas:
            actual, kind, size = stream.readline().decode().split()
            assert actual == sha and kind == "blob"
            blobs[sha] = stream.read(int(size))
            assert len(blobs[sha]) == int(size) and stream.read(1) == b"\n"
        assert stream.read() == b""
        counts = Counter()
        for path, f in files.items():
            assert tree[path] == (f["mode"], f["kind"], f["git_blob_sha"], f["bytes"])
            assert f["source_url"] == f"https://github.com/{repo}/blob/{commit}/{quote(path)}"
            counts[f["status"]] += 1
            if f["status"] == "saved":
                expected_path = (Path("raw") / repo / commit / path).as_posix()
                assert f["raw_path"] == expected_path
                data = (collection_root / expected_path).read_bytes()
                assert data == blobs[f["git_blob_sha"]], f"Changed bytes: {repo}:{path}"
                assert hashlib.sha256(data).hexdigest() == f["sha256"]
                assert len(data) == f["bytes"]
                totals["bytes_compared"] += len(data)
            else:
                assert not (collection_root / "raw" / repo / commit / path).exists(), f"Unexpected export: {path}"
        candidates = {p for p in tree if PurePosixPath(p).name.lower() == "skill.md"}
        entries = [r for r in rows if r["repository"] == repo]
        assert {r["skill_path"] for r in entries} == candidates
        for r in entries:
            f = files[r["skill_path"]]
            assert r["commit_sha"] == commit
            assert r["state"] == ("collected" if f["status"] == "saved" else f["status"])
            assert r["id"] == hashlib.sha256(f"{repo}:{r['skill_path']}".encode()).hexdigest()[:20]
            for key in ("git_blob_sha", "bytes", "source_url"):
                assert r[key] == f[key]
            assert r.get("sha256") == f.get("sha256")
            if f["status"] == "saved":
                assert r["raw_path"] == (roots[repo] / f["raw_path"]).as_posix()
                grouped[r["sha256"]].append(r["id"])
            else:
                assert r["raw_path"] is None
            assert r["repository_manifest"] == manifest_path.as_posix()
            assert r["execution_status"] == "not_executed"
            assert r["quality_status"] == "not_evaluated"
            parent = PurePosixPath(r["skill_path"]).parent
            if parent == PurePosixPath("."):
                package = set(sorted_paths)
            else:
                prefix = parent.as_posix()
                package = set(sorted_paths[bisect_left(sorted_paths, prefix + "/"):bisect_left(sorted_paths, prefix + "0")])
            assert set(r["local_package_files"]) == package
            assert set(r["local_package_files_not_exported"]) == {p for p in package if files[p]["status"] != "saved"}
            assert all(p in files for p in r["license_candidates"])
            totals["entries_with_license_candidates" if r["license_candidates"] else "entries_without_license_candidates"] += 1
        source_report = reports[repo]
        assert source_report["commit_sha"] == commit
        assert source_report["candidate_skill_files"] == len(entries)
        assert source_report["saved_skill_entries"] == sum(r["state"] == "collected" for r in entries)
        assert source_report["saved_text_files"] == counts["saved"]
        assert source_report["files_not_exported"] == len(files) - counts["saved"]
        totals.update(counts)
        results.append({"repository": repo, "commit_sha": commit, "candidate_skill_entries": len(entries),
                        "skill_entries": sum(r["state"] == "collected" for r in entries),
                        "manifest_files": len(files), "statuses": dict(counts)})
    expected_groups = {sha: set(ids) for sha, ids in grouped.items() if len(ids) > 1}
    duplicates = read_json(output / "duplicates.json")
    assert len(duplicates) == len(expected_groups)
    assert {g["entry_sha256"]: set(g["record_ids"]) for g in duplicates} == expected_groups
    assert report["candidate_skill_files"] == len(rows)
    saved_entries = sum(r["state"] == "collected" for r in rows)
    assert report["saved_skill_entries"] == saved_entries
    assert report["unique_entry_texts"] == len(grouped)
    assert report["duplicate_entry_groups"] == len(expected_groups)
    stable = {"skills": [{k: v for k, v in r.items() if k != "collected_at"} for r in rows],
              "sources": lock, "duplicates": duplicates, "manifests": manifests,
              "report": {k: v for k, v in report.items() if k != "generated_at"}}
    snapshot_sha256 = hashlib.sha256(json.dumps(stable, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return {"result": "passed", "verification": "Every saved file byte-compared with pinned Git blob; complete tree/manifest, provenance, counts and duplicate groups checked.",
            "snapshot_sha256_excluding_run_timestamps": snapshot_sha256,
            "sources": results, "totals": dict(totals), "verified_candidate_entries": len(rows), "verified_skill_entries": saved_entries,
            "unique_entry_texts": len(grouped), "duplicate_entry_groups": len(expected_groups),
            "entries_in_duplicate_groups": sum(map(len, expected_groups.values())),
            "duplicate_entries_beyond_first": sum(len(ids) - 1 for ids in expected_groups.values())}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", type=Path, default=Path("sources.json"))
    parser.add_argument("--out", type=Path, default=Path("data"))
    parser.add_argument("--baseline-report", type=Path, help="Require the same snapshot as a previous verification, excluding run timestamps")
    args = parser.parse_args()
    result = verify(args.sources, args.out)
    if args.baseline_report:
        baseline = read_json(args.baseline_report)
        assert result["snapshot_sha256_excluding_run_timestamps"] == baseline["snapshot_sha256_excluding_run_timestamps"], "Repeat collection changed the snapshot"
        result["repeatability"] = "passed; indexes and manifests unchanged except run timestamps; no extra source records"
    print(json.dumps(result, ensure_ascii=False, indent=2))
