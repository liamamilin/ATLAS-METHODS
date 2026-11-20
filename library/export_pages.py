#!/usr/bin/env python3
"""Export a GitHub Pages site containing only catalogue and source metadata."""
import argparse
from collections import Counter, defaultdict
from contextlib import closing
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil

from serve import ID, WEB, database, meta

REPO = re.compile(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+')
REVISION = re.compile(r'[0-9a-f]{40}')
META_FIELDS = (
    'as_of', 'normalized', 'representatives', 'same_package', 'same_format',
    'sources', 'categories', 'repos', 'topics',
)


def source_row(row, repo_ids):
    identifier, repo, path, revision, relationship = (
        row[k] for k in ('id', 'repo', 'path', 'revision', 'relationship')
    )
    parts = PurePosixPath(path).parts
    if (not ID.fullmatch(identifier) or not REPO.fullmatch(repo) or not REVISION.fullmatch(revision)
            or not parts or path.startswith('/') or '..' in parts or parts[-1].casefold() != 'skill.md'):
        raise ValueError(f'Cannot publish an invalid GitHub source: {identifier}')
    return [identifier, repo_ids[repo], path, revision, relationship]


def snapshot(con):
    existing = meta(con)
    public = {field: existing[field] for field in META_FIELDS}
    repo_ids = {name: i for i, name in enumerate(public['repos'])}
    groups = defaultdict(list)
    for row in con.execute("""
        SELECT skill, id, repo, path, revision, relationship FROM sources
        ORDER BY skill, (id = skill) DESC, repo, path, revision
    """):
        groups[row['skill']].append(source_row(row, repo_ids))
    entries = []
    counts = Counter()
    for row in con.execute('SELECT id, category, topic, label_from FROM skills ORDER BY generated DESC, id'):
        sources = groups[row['id']]
        if not sources or sources[0][0] != row['id']:
            raise ValueError('Missing representative source')
        name = PurePosixPath(sources[0][2]).parent.name or public['repos'][sources[0][1]].split('/')[1]
        entries.append([name, row['category'], row['topic'], row['label_from'], sources])
        counts[row['category']] += 1
    if (len(entries) != public['representatives']
            or sum(len(entry[-1]) for entry in entries) != public['sources']
            or any(entry['count'] != counts[entry['id']] for entry in public['categories'])):
        raise ValueError('Published index counts do not match the catalogue')
    return {'meta': public, 'entries': entries}


def export(out):
    out.mkdir(parents=True, exist_ok=True)
    if any(out.iterdir()):
        raise ValueError('Export destination must be empty')
    with closing(database()) as con:
        con.execute('BEGIN')
        data = snapshot(con)
    payload = json.dumps(data, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    version = hashlib.sha256(payload).hexdigest()[:12]
    (out / 'catalog.json').write_bytes(payload)
    html = (WEB / 'index.html').read_text(encoding='utf-8')
    html = html.replace('<html lang="zh-CN">', f'<html lang="zh-CN" data-catalog="./catalog.json?v={version}">')
    html = html.replace('<script src="./app.js" defer></script>',
                        f'<script src="./pages_catalog.js?v={version}" defer></script>\n'
                        f'  <script src="./app.js?v={version}" defer></script>')
    html = html.replace('./style.css"', f'./style.css?v={version}"')
    html = html.replace('正在读取本机目录', '正在读取公开目录')
    html = html.replace('按用途找做法，查看步骤与限制，回到固定版本的来源。',
                        '按领域、专题和仓库浏览 Skill，打开 GitHub 固定版本。')
    html = html.replace('搜索名称、用途或仓库…', '搜索名称、领域或仓库…')
    (out / 'index.html').write_text(html, encoding='utf-8')
    for name in ('app.js', 'pages_catalog.js', 'style.css'):
        shutil.copyfile(WEB / name, out / name)
    (out / '.nojekyll').touch()
    return {'version': version, 'bytes': len(payload), **{k: data['meta'][k] for k in ('as_of', 'representatives', 'sources')}}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path, help='An empty directory for the static site')
    args = parser.parse_args()
    print(json.dumps(export(args.output), ensure_ascii=False, indent=2))
