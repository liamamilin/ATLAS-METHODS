#!/usr/bin/env python3
"""Read the saved Atlas library over a loopback-only HTTP interface."""
import argparse
from contextlib import closing
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import parse_qs, quote, urlsplit

from search import search_text
from task_search import ACTION_TAGS, TASK_HITS, TOOL_TAGS, available, match_query
from taxonomy import CATEGORIES, LABELS, UNSORTED_TOPIC

ROOT = Path(__file__).resolve().parents[1]
DATABASE = ROOT / 'data/library/catalog.sqlite3'
WEB = Path(__file__).resolve().parent / 'web'
INDEX_DIR = ROOT / 'data/cumulative'
NORMALIZED = ROOT / 'data/normalized'
ID = re.compile(r'[a-f0-9]{32}')
PAGE_SIZE = 24


def database():
    con = sqlite3.connect(f'file:{quote(str(DATABASE))}?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    return con


def value(params, key, limit=160):
    return params.get(key, [''])[0].strip()[:limit]


def list_skills(con, params):
    query = search_text(value(params, 'q'))
    terms = query.split()[:8]
    categories = value(params, 'category', 24)
    repo = value(params, 'repo')
    topic = value(params, 'topic')
    action = value(params, 'action', 32)
    tool = value(params, 'tool', 32)
    try:
        page = min(100000, max(1, int(value(params, 'page', 10) or 1)))
    except ValueError:
        page = 1
    exact = ' AND '.join('instr(s.search_text, ?) > 0' for _ in terms)
    conditions = [exact] if exact else []
    arguments = list(terms)
    prelude, hit_args = '', []
    source = 'FROM skills s'
    task_columns, task_join = '', ''
    if (query or action or tool) and available(con):
        expression, expand, focus = match_query(query, action, tool)
        if expression:
            prelude, hit_args = TASK_HITS, [focus, focus, expression]
            filtered = action in ACTION_TAGS or tool in TOOL_TAGS
            join = 'JOIN' if filtered else 'LEFT JOIN'
            source += f' {join} best b ON b.skill = s.id AND b.choice = 1'
            task_join = ' LEFT JOIN catalog_tasks t ON t.id = b.id'
            task_columns = ', t.position, t.goal, t.applicability, t.outputs, t.tags'
            if expand:
                conditions = [] if filtered else [f'(b.id IS NOT NULL OR ({exact}))']
                if filtered:
                    arguments = []
    if categories in LABELS:
        conditions.append('s.category = ?')
        arguments.append(categories)
    if repo:
        conditions.append('EXISTS (SELECT 1 FROM sources r WHERE r.skill = s.id AND r.repo = ?)')
        arguments.append(repo)
    if topic:
        conditions.append('s.topic = ?')
        arguments.append(topic)
    if value(params, 'grouped') == '1':
        conditions.append('s.source_count > 1')
    where = 'WHERE ' + ' AND '.join(conditions) if conditions else ''
    sort = value(params, 'sort', 20)
    orders = {'new': 's.generated DESC', 'title': 's.search_title', 'sources': 's.source_count DESC, s.search_title'}
    order, order_args = orders.get(sort, 's.generated DESC'), []
    if sort not in orders and prelude:
        order = """CASE WHEN instr(s.search_title, NULLIF(?, '')) > 0 THEN 0
                        WHEN b.id IS NOT NULL THEN 1 ELSE 2 END, b.preferred DESC, b.score, s.search_title"""
        order_args.append(query)
    elif terms and sort not in orders:
        order = 'CASE WHEN instr(s.search_title, ?) > 0 THEN 0 ELSE 1 END, s.search_title'
        order_args.append(query)
    count = con.execute(f'{prelude} SELECT COUNT(*) {source} {where}', (*hit_args, *arguments)).fetchone()[0]
    page = min(page, max(1, (count + PAGE_SIZE - 1) // PAGE_SIZE))
    rows = con.execute(f'''
        {prelude}
        SELECT s.id, s.title, s.summary, s.category, s.topic, s.repo, s.source_count, s.review{task_columns}
        {source}{task_join} {where} ORDER BY {order}, s.id LIMIT ? OFFSET ?
    ''', (*hit_args, *arguments, *order_args, PAGE_SIZE, (page - 1) * PAGE_SIZE)).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        if prelude:
            position, goal, when, outputs, tags = (
                item.pop(key) for key in ('position', 'goal', 'applicability', 'outputs', 'tags')
            )
            if position is not None:
                item['match'] = {'position': position, 'goal': goal, 'when': when,
                                 'outputs': json.loads(outputs), 'tags': json.loads(tags)}
        items.append(item)
    return {'items': items, 'total': count, 'page': page, 'page_size': PAGE_SIZE}


def meta(con):
    data = {row['key']: json.loads(row['value']) for row in con.execute('SELECT * FROM meta')}
    data['task_search'] = bool(data.get('task_tags')) and available(con)
    data['categories'] = [
        {'id': key, 'label': name, 'section': section, 'count': data['categories'].get(key, 0)}
        for key, name, section, _ in CATEGORIES
    ]
    data['repos'] = [row['repo'] for row in con.execute('SELECT DISTINCT repo FROM sources ORDER BY repo COLLATE NOCASE')]
    data['topics'] = [dict(row) for row in con.execute(
        """SELECT category, topic, COUNT(*) AS count FROM skills
           WHERE topic <> '' GROUP BY category, topic
           ORDER BY category, topic = ?, count DESC, topic""", (UNSORTED_TOPIC,)
    )]
    return data


def local_file(path, base, allowed):
    result = (base / path).resolve()
    if not result.is_relative_to(allowed.resolve()):
        raise ValueError('Out of scope path')
    return result


def github_url(row):
    return 'https://github.com/' + quote(row['repo'], safe='/') + '/blob/' + quote(row['revision'], safe='') + '/' + quote(row['path'], safe='/')


def get_skill(con, skill_id, requested_source):
    target = con.execute('SELECT skill FROM sources WHERE id = ?', (skill_id,)).fetchone()
    if not target:
        return None
    skill = con.execute('SELECT * FROM skills WHERE id = ?', (target['skill'],)).fetchone()
    sources = []
    display_record = skill['record_path']
    display_source = skill['id']
    selected = requested_source or skill_id
    for row in con.execute('SELECT * FROM sources WHERE skill = ? ORDER BY (id = ?) DESC, repo, path, revision',
                           (skill['id'], skill['id'])):
        if row['id'] == selected and row['record_path']:
            display_record, display_source = row['record_path'], row['id']
        sources.append({
            **{k: row[k] for k in ('id', 'repo', 'path', 'revision', 'blob', 'sha256', 'relationship')},
            'url': github_url(row), 'license': json.loads(row['license']),
            'has_record': row['record_path'] is not None,
            'has_raw': bool(row['raw_path']),
        })
    record = json.loads(local_file(display_record, ROOT, NORMALIZED).read_text(encoding='utf-8'))
    return {'id': skill['id'], 'category': skill['category'], 'topic': skill['topic'],
            'label_from': skill['label_from'], 'record': record,
            'display_source': display_source, 'sources': sources}


def get_raw(con, source_id):
    row = con.execute('SELECT raw_path, sha256 FROM sources WHERE id = ?', (source_id,)).fetchone()
    if not row or not row['raw_path']:
        return None
    data = local_file(row['raw_path'], INDEX_DIR, ROOT).read_bytes()
    if hashlib.sha256(data).hexdigest() != row['sha256']:
        raise ValueError('Entry hash mismatch')
    return {'text': data.decode('utf-8')}


class Handler(BaseHTTPRequestHandler):
    assets = {'/': ('index.html', 'text/html'), '/style.css': ('style.css', 'text/css'),
              '/app.js': ('app.js', 'text/javascript')}

    def send_data(self, status, content_type, data):
        self.send_response(status)
        self.send_header('Content-Type', content_type + '; charset=utf-8')
        self.send_header('Content-Length', len(data))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy',
                         "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(data)

    def json(self, data, status=200):
        self.send_data(status, 'application/json', json.dumps(data, ensure_ascii=False).encode())

    def do_GET(self):
        port = self.server.server_port
        if self.headers.get('Host') not in {f'localhost:{port}', f'127.0.0.1:{port}'}:
            self.json({'error': '请从本机地址打开资料库'}, 403)
            return
        url = urlsplit(self.path)
        if url.path in self.assets:
            name, kind = self.assets[url.path]
            self.send_data(200, kind, (WEB / name).read_bytes())
            return
        if url.path == '/favicon.ico':
            self.send_data(204, 'text/plain', b'')
            return
        try:
            with closing(database()) as con:
                if url.path == '/api/meta':
                    result = meta(con)
                elif url.path == '/api/skills':
                    result = list_skills(con, parse_qs(url.query))
                else:
                    route, _, identifier = url.path.rpartition('/')
                    if not ID.fullmatch(identifier):
                        result = None
                    elif route == '/api/skill':
                        result = get_skill(con, identifier, value(parse_qs(url.query), 'source', 32))
                    elif route == '/api/source':
                        result = get_raw(con, identifier)
                    else:
                        result = None
            if result is None:
                self.json({'error': '找不到此条目或本机原文'}, 404)
            else:
                self.json(result)
        except FileNotFoundError:
            self.json({'error': '本机原文或转换文件缺失'}, 404)
        except sqlite3.OperationalError:
            self.json({'error': '目录尚未生成或暂不可读'}, 503)
        except (ValueError, UnicodeError):
            self.json({'error': '无法核对本机文件'}, 409)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    if not DATABASE.is_file():
        parser.error('Run python3 library/build.py to create the local catalogue first')
    with ThreadingHTTPServer(('127.0.0.1', args.port), Handler) as server:
        print(f'Atlas Skill Library: http://127.0.0.1:{server.server_port}', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
