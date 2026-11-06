#!/usr/bin/env python3
"""ATLAS-METHODS stage 1: collect raw SKILL.md files and repository text context.

Requires Python 3.10+ and Git. No third-party Python packages, LLM, API key,
installation of collected skills, execution of their scripts, or GitHub writes.
Default: collect the exact commits in sources.json. --refresh follows current HEAD.
Run --self-test for an offline test using a throwaway local Git repository.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from urllib.parse import quote

REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
FONTS = {".ttf", ".otf", ".woff", ".woff2", ".eot", ".ttc"}
REVIEW_HINTS = {"test", "tests", "fixture", "fixtures", "template", "templates",
                "examples", "deprecated", "in-progress", ".out-of-scope"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in rows), encoding="utf-8")
    tmp.replace(path)


def git(*args: str, directory: Path | None = None, timeout: int = 240) -> bytes:
    command = ["git", "-c", "core.hooksPath=/dev/null"]
    if directory is not None:
        command += ["--git-dir", str(directory)]
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    result = subprocess.run(command + list(args), capture_output=True, env=env, timeout=timeout)
    if result.returncode:
        message = result.stderr.decode("utf-8", "replace").strip()[-1500:]
        raise RuntimeError(f"Git failed: {message}")
    return result.stdout


def resolve_source(source: dict, cache: Path, refresh: bool) -> tuple[Path, str]:
    repo = source["repository"]
    if not REPO_RE.fullmatch(repo) or any(p in {".", ".."} for p in repo.split("/")):
        raise ValueError(f"Invalid owner/repository: {repo!r}")
    pinned = source.get("commit_sha")
    if pinned and not SHA_RE.fullmatch(pinned):
        raise ValueError(f"Invalid commit SHA for {repo}")
    directory = cache / (repo.replace("/", "__") + ".git")
    if not directory.exists():
        cache.mkdir(parents=True, exist_ok=True)
        # Bare clone: no checkout, no repository programs or package installs.
        git("clone", "--bare", "--depth", "1", f"https://github.com/{repo}.git", str(directory))
    if refresh or not pinned:
        git("fetch", "--depth", "1", "origin", "HEAD", directory=directory)
        commit = git("rev-parse", "FETCH_HEAD^{commit}", directory=directory).decode().strip()
    else:
        try:
            git("cat-file", "-e", pinned + "^{commit}", directory=directory)
        except RuntimeError:
            git("fetch", "--depth", "1", "origin", pinned, directory=directory)
        commit = git("rev-parse", pinned + "^{commit}", directory=directory).decode().strip()
        if commit != pinned:
            raise RuntimeError("Resolved commit does not match the requested pin")
    return directory, commit


def file_tree(directory: Path, commit: str) -> list[dict]:
    result = []
    for entry in git("ls-tree", "-r", "-l", "-z", "--full-tree", commit, directory=directory).split(b"\0"):
        if not entry:
            continue
        header, raw_path = entry.split(b"\t", 1)
        mode, kind, sha, size = header.decode("ascii").split()
        path = raw_path.decode("utf-8")
        relative = PurePosixPath(path)
        if relative.is_absolute() or ".." in relative.parts or "\\" in path:
            raise ValueError(f"Unsupported repository path: {path!r}")
        result.append({"path": path, "mode": mode, "kind": kind, "git_blob_sha": sha,
                       "bytes": int(size) if size != "-" else None})
    return sorted(result, key=lambda x: x["path"])


def blob_hash(data: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


def tree_blobs(directory: Path, entries: list[dict], max_bytes: int):
    """Read eligible objects through one Git process, keeping only one blob in memory."""
    command = ["git", "-c", "core.hooksPath=/dev/null", "--git-dir", str(directory), "cat-file", "--batch"]
    with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
        try:
            for entry in entries:
                data = None
                if (entry["kind"] == "blob" and entry["mode"] != "120000" and
                        PurePosixPath(entry["path"]).suffix.lower() not in FONTS and entry["bytes"] <= max_bytes):
                    sha = entry["git_blob_sha"]
                    process.stdin.write(sha.encode("ascii") + b"\n")
                    process.stdin.flush()
                    header = process.stdout.readline().split()
                    if header != [sha.encode("ascii"), b"blob", str(entry["bytes"]).encode("ascii")]:
                        raise RuntimeError(f"Unexpected Git blob response: {header!r}")
                    data = process.stdout.read(entry["bytes"])
                    if len(data) != entry["bytes"] or process.stdout.read(1) != b"\n":
                        raise RuntimeError(f"Truncated Git blob: {sha}")
                yield entry, data
            process.stdin.close()
            if process.wait(timeout=240):
                raise RuntimeError(process.stderr.read().decode("utf-8", "replace")[-1500:])
        finally:
            if process.poll() is None:
                process.kill()


def frontmatter(text: str) -> tuple[str | None, str | None]:
    # Preserve the full header; do not pretend this is a full YAML validator.
    lines = text.splitlines()
    if not lines or lines[0].lstrip("\ufeff") != "---":
        return None, None
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return None, None
    raw = "\n".join(lines[1:end])
    match = re.search(r"^name:\s*([^\n]+)$", raw, re.M)
    name = match.group(1).strip().strip("\"'") if match else None
    return raw, name


def license_paths(path: str, paths_by_parent: dict) -> list[str]:
    # Record candidate governing files, not a legal conclusion about their scope.
    parents = set(PurePosixPath(path).parent.parents) | {PurePosixPath(path).parent}
    return sorted(p for parent in parents for p in paths_by_parent.get(parent, []))


def export_source(repo: str, directory: Path, commit: str, output: Path,
                  max_bytes: int) -> tuple[list[dict], dict]:
    entries = file_tree(directory, commit)
    root = output / "raw" / repo / commit
    root.mkdir(parents=True, exist_ok=True)
    manifest = []
    # Save the whole repository's text context, not just each SKILL.md. This keeps
    # shared references/scripts at their original relative paths. Non-text assets
    # remain indexed with immutable upstream URLs, but are not exported.
    for entry, data in tree_blobs(directory, entries, max_bytes):
        row = dict(entry)
        path = entry["path"]
        row["source_url"] = f"https://github.com/{repo}/blob/{commit}/{quote(path)}"
        if entry["kind"] != "blob":
            row["status"] = "gitlink_not_exported"
        elif entry["mode"] == "120000":
            row["status"] = "symlink_not_followed"
        elif PurePosixPath(path).suffix.lower() in FONTS:
            row["status"] = "font_not_exported"
        elif entry["bytes"] > max_bytes:
            row["status"] = "over_size_limit"
        else:
            if blob_hash(data) != entry["git_blob_sha"] or len(data) != entry["bytes"]:
                raise RuntimeError(f"Content integrity failure: {repo}:{path}")
            row["sha256"] = hashlib.sha256(data).hexdigest()
            try:
                data.decode("utf-8")
                if b"\0" in data:
                    raise UnicodeError("NUL byte in text")
            except UnicodeError:
                row["status"] = "non_utf8_or_binary_not_exported"
            else:
                destination = root / path
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(data)
                row["status"] = "saved"
                row["raw_path"] = destination.relative_to(output).as_posix()
        manifest.append(row)
    manifest_path = output / "manifests" / repo / (commit + ".json")
    write_json(manifest_path, {"repository": repo, "commit_sha": commit, "files": manifest})
    candidates = [x for x in manifest if PurePosixPath(x["path"]).name.lower() == "skill.md"]
    packages = {PurePosixPath(x["path"]).parent: [] for x in candidates}
    licenses = {}
    for item in manifest:
        relative = PurePosixPath(item["path"])
        for parent in relative.parents:
            if parent in packages:
                packages[parent].append(item)
        if relative.name.upper().startswith(("LICENSE", "LICENCE", "COPYING", "NOTICE")):
            licenses.setdefault(relative.parent, []).append(item["path"])
    records = []
    for item in candidates:
        path = item["path"]
        parent = PurePosixPath(path).parent
        package = packages[parent]
        excluded = [x["path"] for x in package if x["status"] != "saved"]
        source_identity = f"{repo}:{path}"
        record = {
            "id": hashlib.sha256(source_identity.encode()).hexdigest()[:20],
            "repository": repo, "commit_sha": commit, "skill_path": path,
            "source_url": item["source_url"], "git_blob_sha": item["git_blob_sha"],
            "sha256": item.get("sha256"), "bytes": item["bytes"], "collected_at": now(),
            "state": "collected" if item["status"] == "saved" else item["status"],
            "raw_path": item.get("raw_path"), "execution_status": "not_executed",
            "quality_status": "not_evaluated", "license_candidates": license_paths(path, licenses),
            "repository_manifest": manifest_path.relative_to(output).as_posix(),
            "local_package_files": [x["path"] for x in package],
            "local_package_files_not_exported": excluded,
            "external_dependency_status": "not_fetched_or_verified",
            "path_review_hints": sorted(set(PurePosixPath(path).parts) & REVIEW_HINTS),
        }
        if item["status"] == "saved":
            text = (root / path).read_text(encoding="utf-8")
            header, name = frontmatter(text)
            record.update(frontmatter_raw=header, declared_name=name,
                          format_status="header_present_not_schema_validated" if header is not None else "no_parseable_frontmatter")
        records.append(record)
    report = {"repository": repo, "commit_sha": commit, "state": "text_snapshot_collected",
              "candidate_skill_files": len(candidates),
              "saved_skill_entries": sum(r["state"] == "collected" for r in records),
              "saved_text_files": sum(f["status"] == "saved" for f in manifest),
              "files_not_exported": sum(f["status"] != "saved" for f in manifest),
              "repository_manifest": manifest_path.relative_to(output).as_posix()}
    return records, report


def duplicate_groups(records: list[dict]) -> list[dict]:
    groups = {}
    for record in records:
        if record.get("state") == "collected":
            groups.setdefault(record["sha256"], []).append(record["id"])
    return [{"entry_sha256": sha, "record_ids": ids, "scope": "entry_text_only; packages may differ"}
            for sha, ids in sorted(groups.items()) if len(ids) > 1]


def collect(args) -> int:
    source_file = Path(args.sources)
    config = json.loads(source_file.read_text(encoding="utf-8"))
    sources = config["sources"]
    if args.only:
        missing = set(args.only) - {x["repository"] for x in sources}
        if missing:
            raise ValueError(f"Unknown sources: {sorted(missing)}")
        sources = [x for x in sources if x["repository"] in args.only]
    output = Path(args.out).resolve()
    output.mkdir(parents=True, exist_ok=True)
    records, reports, errors = [], [], []
    for i, source in enumerate(sources, 1):
        repo = source["repository"]
        print(f"[{i}/{len(sources)}] {repo}", flush=True)
        try:
            directory, commit = resolve_source(source, output / ".cache", args.refresh)
            items, report = export_source(repo, directory, commit, output, args.max_file_mb * 1024**2)
            records.extend(items)
            reports.append(report)
            print(f"  saved {report['saved_skill_entries']} entry files at {commit[:12]}", flush=True)
        except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
            errors.append({"repository": repo, "state": "failed", "error": str(exc)})
            print(f"  FAILED: {exc}", file=sys.stderr, flush=True)
    records.sort(key=lambda x: (x["repository"].lower(), x["skill_path"]))
    duplicate = duplicate_groups(records)
    summary = {"generated_at": now(), "requested_sources": len(sources),
               "completed_sources": len(reports), "failed_sources": len(errors),
               "candidate_skill_files": len(records),
               "saved_skill_entries": sum(x["state"] == "collected" for x in records),
               "unique_entry_texts": len({x["sha256"] for x in records if x["state"] == "collected"}),
               "duplicate_entry_groups": len(duplicate),
               "note": "Entry-text counts are not method counts. No quality evaluation, taxonomy, or skill execution performed.",
               "sources": reports, "errors": errors}
    write_jsonl(output / "skills.jsonl", records)
    write_json(output / "duplicates.json", duplicate)
    write_json(output / "collection-report.json", summary)
    write_json(output / "sources.lock.json", {"sources": [{"repository": r["repository"], "commit_sha": r["commit_sha"]} for r in reports]})
    print(json.dumps({k:v for k,v in summary.items() if k not in {"sources", "errors"}}, ensure_ascii=False, indent=2))
    return 1 if errors or summary["candidate_skill_files"] != summary["saved_skill_entries"] else 0


def self_test() -> None:
    with tempfile.TemporaryDirectory(prefix="atlas-test-") as temp:
        base = Path(temp); work = base / "upstream"; work.mkdir()
        git("init", str(work))
        text = b"---\nname: demo\ndescription: raw example\n---\n\nRead ../../references/check.md.\n"
        fixtures = {"skills/one/SKILL.md": text, "skills/two/SKILL.md": text,
                    "references/check.md": b"Shared context\n", "LICENSE": b"Fixture license\n",
                    "skills/one/picture.png": b"\x89PNG\0\xff", "skills/one/font.ttf": b"must not export",
                    "tests/fixtures/bad/SKILL.md": b"No frontmatter; still inventory it.\n",
                    "skills/one/empty.txt": b"", "skills/one/nested/context.txt": b"Nested text\n",
                    "skills/one/LICENSE": b"Local fixture license\n",
                    "skills/one/scripts/helper.py": b"raise RuntimeError('must never run')\n"}
        for name, data in fixtures.items():
            p=work/name; p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(data)
        git("-C", str(work), "add", ".")
        git("-C", str(work), "-c", "user.name=ATLAS Test", "-c", "user.email=atlas@example.invalid", "commit", "-m", "fixture")
        commit=git("-C", str(work), "rev-parse", "HEAD").decode().strip()
        output=base/"output"
        records, report=export_source("fixture/repository", work/".git", commit, output, 1024**2)
        assert report["saved_skill_entries"] == 3
        assert len(duplicate_groups(records)) == 1
        root=output/"raw"/"fixture/repository"/commit
        assert (root/"skills/one/SKILL.md").read_bytes() == text
        assert (root/"references/check.md").read_bytes() == b"Shared context\n"
        assert not (root/"skills/one/font.ttf").exists()
        assert not (root/"skills/one/picture.png").exists()
        assert (root/"skills/one/scripts/helper.py").exists()
        one = next(x for x in records if x["skill_path"] == "skills/one/SKILL.md")
        assert "skills/one/nested/context.txt" in one["local_package_files"]
        assert one["license_candidates"] == ["LICENSE", "skills/one/LICENSE"]
        assert (root/"skills/one/empty.txt").read_bytes() == b""
        bad=next(x for x in records if x["skill_path"].startswith("tests/"))
        assert bad["format_status"] == "no_parseable_frontmatter" and bad["path_review_hints"]
        repeated,_=export_source("fixture/repository", work/".git", commit, output, 1024**2)
        assert [x["sha256"] for x in records] == [x["sha256"] for x in repeated]
        write_jsonl(output/"skills.jsonl", records)
        assert len((output/"skills.jsonl").read_text().splitlines()) == 3
    print("PASS: exact content; shared references; duplicate grouping; binary/font inventory; no script execution; nonstandard candidates; repeatable export; JSONL output.")


def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", default=str(Path(__file__).with_name("sources.json")))
    parser.add_argument("--out", default="data")
    parser.add_argument("--only", action="append", help="Collect only a named owner/repository from sources.json; repeatable")
    parser.add_argument("--refresh", action="store_true", help="Resolve latest default-branch HEAD rather than using the recorded pins")
    parser.add_argument("--max-file-mb", type=int, default=2, help="Export UTF-8 text files up to this size; index other files without exporting")
    parser.add_argument("--self-test", action="store_true", help="Run offline fixture tests; no network or persistent changes")
    args=parser.parse_args()
    if not shutil.which("git"):
        parser.error("Git is required and was not found in PATH")
    if args.max_file_mb < 1:
        parser.error("--max-file-mb must be at least 1")
    if args.self_test:
        self_test(); return 0
    return collect(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
