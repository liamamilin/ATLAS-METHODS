#!/usr/bin/env python3
"""Merge existing collection indexes, retaining their local material and source paths."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

from collect_skills import blob_hash, duplicate_groups, now, write_json, write_jsonl


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def merge(collections, output, status_files=()):
    output = Path(output).resolve()
    records, snapshots, source_reports, statuses = {}, {}, {}, {}
    manifests, manifest_files = {}, {}
    for collection in collections:
        root = Path(collection).resolve()
        index = root / "skills.jsonl"
        if not index.exists():
            continue  # Failed/no-Skill scans have no saved entrance index.
        report = read_json(root / "collection-report.json")
        for error in report.get("errors", []):
            statuses[error["repository"].lower()] = {**error, "state": "failed"}
        for source in report["sources"]:
            key = source["repository"].lower(), source["commit_sha"]
            source_state = ("no_skill_files" if source.get("candidate_skill_files") == 0 else
                            "partial_text_snapshot" if source.get("saved_skill_entries", 0) < source.get("candidate_skill_files", 0) else
                            "text_snapshot_collected")
            statuses.setdefault(key[0], {"repository": source["repository"], "commit_sha": source["commit_sha"], "state": source_state})
            if source_state == "no_skill_files":
                continue
            snapshots.setdefault(key, {"repository": source["repository"], "commit_sha": source["commit_sha"],
                                       "collection_root": os.path.relpath(root, output)})
            source_reports.setdefault(key, {**source, "repository_manifest": os.path.relpath(root / source["repository_manifest"], output)})
        for line in index.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            key = record["repository"].lower(), record["commit_sha"], record["skill_path"]
            if key in records:
                if records[key].get("sha256") != record.get("sha256") or records[key]["state"] != record["state"]:
                    raise ValueError(f"Conflicting source entry: {key}")
                continue
            manifest_path = (root / record["repository_manifest"]).resolve()
            if manifest_path not in manifests:
                manifests[manifest_path] = read_json(manifest_path)
                manifest_files[manifest_path] = {f["path"]: f for f in manifests[manifest_path]["files"]}
            manifest = manifests[manifest_path]
            if (manifest["repository"], manifest["commit_sha"]) != (record["repository"], record["commit_sha"]):
                raise ValueError(f"Wrong manifest for {key}")
            item = manifest_files[manifest_path][record["skill_path"]]
            if record["state"] == "collected":
                raw = (root / record["raw_path"]).resolve()
                data = raw.read_bytes()  # Index-only entries cannot count as saved material.
                if (hashlib.sha256(data).hexdigest() != record["sha256"] or
                        blob_hash(data) != record["git_blob_sha"] or
                        item["status"] != "saved" or item["sha256"] != record["sha256"] or
                        (root / item["raw_path"]).resolve() != raw):
                    raise ValueError(f"Material/hash mismatch: {key}")
                record["raw_path"] = os.path.relpath(raw, output)
            record["repository_manifest"] = os.path.relpath(manifest_path, output)
            record["collection_root"] = os.path.relpath(root, output)
            records[key] = record
    for status_file in status_files:
        for status in read_json(Path(status_file)):
            statuses[status["repository"].lower()] = status
    successful_repos = {repo for repo, _ in snapshots}
    for repo, status in statuses.items():
        if status["state"] in {"text_snapshot_collected", "partial_text_snapshot"} and repo not in successful_repos:
            raise ValueError(f"Successful source has no accessible collection index: {repo}")
    rows = sorted(records.values(), key=lambda r: (r["repository"].lower(), r["skill_path"], r["commit_sha"]))
    duplicates = duplicate_groups(rows)
    for group in duplicates:
        group["members"] = [{"repository": r["repository"], "commit_sha": r["commit_sha"], "skill_path": r["skill_path"]}
                            for r in rows if r.get("state") == "collected" and r.get("sha256") == group["entry_sha256"]]
    saved = [r for r in rows if r["state"] == "collected"]
    failures = [s for s in statuses.values() if s["state"] == "failed"]
    partial = [s for s in statuses.values() if s["state"] == "partial_text_snapshot"]
    no_skills = [s for repo, s in statuses.items() if s["state"] == "no_skill_files" and repo not in successful_repos]
    pending = [s for repo, s in statuses.items() if s["state"] not in {"failed", "no_skill_files", "text_snapshot_collected", "partial_text_snapshot"} and repo not in successful_repos]
    incomplete_repos = {s["repository"].lower() for s in failures + partial}
    for (repo, _), source_report in source_reports.items():
        if repo in incomplete_repos:
            source_report["state"] = statuses[repo]["state"]
    summary = {"generated_at": now(), "requested_sources": len(successful_repos | statuses.keys()),
               "completed_sources": len(successful_repos - incomplete_repos), "indexed_snapshots": len(snapshots),
               "partial_sources": len(partial), "partial_results": partial,
               "failed_sources": len(failures), "no_skill_sources": len(no_skills), "pending_sources": len(pending),
               "candidate_skill_files": len(rows), "saved_skill_entries": len(saved),
               "distinct_skill_paths": len({(r["repository"].lower(), r["skill_path"]) for r in saved}),
               "unique_entry_texts": len({r["sha256"] for r in saved}), "duplicate_entry_groups": len(duplicates),
               "sources": list(source_reports.values()), "errors": failures, "no_skill_results": no_skills,
               "pending_results": pending,
               "note": "Indexes merged by repository/commit/path. Raw paths and manifests are relative to this output directory. Versions are snapshots, not additional independent Skill paths."}
    write_jsonl(output / "skills.jsonl", rows)
    write_json(output / "duplicates.json", duplicates)
    write_json(output / "sources.lock.json", {"sources": list(snapshots.values())})
    write_json(output / "collection-report.json", summary)
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("collections", nargs="+", type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--status", action="append", default=[], type=Path, help="Per-source attempt results to retain failures and no-Skill scans")
    args = parser.parse_args()
    result = merge(args.collections, args.out, args.status)
    print(json.dumps({k: v for k, v in result.items() if k not in {"sources", "errors", "no_skill_results", "pending_results", "partial_results"}}, ensure_ascii=False, indent=2))
