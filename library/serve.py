#!/usr/bin/env python3
"""Read the saved Atlas library over a loopback-only HTTP interface."""
import argparse
from contextlib import closing
import errno
import hashlib
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import parse_qs, quote, urlsplit

from search import search_text
from task_search import ACTION_TAGS, ANY_TAG_QUERY, TASK_HITS, TOOL_TAGS, available, match_query
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


def catalogue_scope(params):
    conditions, arguments = [], []
    repo = value(params, 'repo')
    if repo:
        conditions.append('EXISTS (SELECT 1 FROM sources r WHERE r.skill = s.id AND r.repo = ?)')
        arguments.append(repo)
    if value(params, 'grouped') == '1':
        conditions.append('s.source_count > 1')
    return conditions, arguments


def linked_facets(con, query, exact, terms, scope, scope_args, *, category, topic, action, tool):
    """Count each choice with the other filters still in effect."""
    expression, expand, _ = match_query(query)
    tagged = bool(action or tool)
    use_exact = bool(exact) and (not expand or not tagged)
    literal = f'({exact})' if use_exact else '1'
    literal_args = terms if use_exact else ()
    scope_where = 'WHERE ' + ' AND '.join(scope) if scope else ''
    if tagged:
        topics = ('SELECT category, topic, COUNT(DISTINCT id) AS count FROM matching '
                  'WHERE action_ok AND tool_ok GROUP BY category, topic')
    else:
        scope_match = 'literal OR id IN (SELECT id FROM matching)' if expand else 'literal'
        topics = f'SELECT category, topic, COUNT(*) AS count FROM scoped WHERE {scope_match} GROUP BY category, topic'
    rows = con.execute(f'''
        WITH scoped AS MATERIALIZED (
            SELECT s.id, s.category, s.topic, {literal} AS literal FROM skills s {scope_where}
        ), matching AS MATERIALIZED (
            SELECT s.id, s.category, s.topic, t.tags,
                   (? = '' OR EXISTS (SELECT 1 FROM json_each(t.tags, '$.action') WHERE value = ?)) AS action_ok,
                   (? = '' OR EXISTS (SELECT 1 FROM json_each(t.tags, '$.tool') WHERE value = ?)) AS tool_ok
            FROM task_terms
            JOIN catalog_tasks t ON t.id = task_terms.rowid
            JOIN scoped s ON s.id = t.skill
            WHERE task_terms MATCH ? {'AND s.literal' if not expand else ''}
        ), tag_scope AS MATERIALIZED (
            SELECT * FROM matching WHERE (? = '' OR category = ?) AND (? = '' OR topic = ?)
        )
        SELECT 'topic' AS kind, category, topic AS choice, count FROM ({topics})
        UNION ALL
        SELECT 'action', '', j.value, COUNT(DISTINCT s.id)
        FROM tag_scope s, json_each(s.tags, '$.action') j WHERE s.tool_ok GROUP BY j.value
        UNION ALL
        SELECT 'tool', '', j.value, COUNT(DISTINCT s.id)
        FROM tag_scope s, json_each(s.tags, '$.tool') j WHERE s.action_ok GROUP BY j.value
    ''', (*literal_args, *scope_args, action, action, tool, tool, expression or ANY_TAG_QUERY,
          category, category, topic, topic))
    result = {'topics': [], 'action': {}, 'tool': {}}
    for kind, parent, choice, count in rows:
        if kind == 'topic':
            result['topics'].append({'category': parent, 'topic': choice, 'count': count})
        else:
            result[kind][choice] = count
    return result


def list_skills(con, params):
    query = search_text(value(params, 'q'))
    terms = query.split()[:8]
    category = value(params, 'category', 24)
    topic = value(params, 'topic')
    action = value(params, 'action', 32)
    tool = value(params, 'tool', 32)
    category = category if category in LABELS else ''
    action = action if action in ACTION_TAGS else ''
    tool = tool if tool in TOOL_TAGS else ''
    scope, scope_args = catalogue_scope(params)
    has_facets = bool(query or category or topic or action or tool or scope) and available(con)
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
    if has_facets and (query or action or tool):
        expression, expand, focus = match_query(query, action, tool)
        if expression:
            prelude, hit_args = TASK_HITS, [focus, focus, expression]
            filtered = bool(action or tool)
            join = 'JOIN' if filtered else 'LEFT JOIN'
            source += f' {join} best b ON b.skill = s.id AND b.choice = 1'
            task_join = ' LEFT JOIN catalog_tasks t ON t.id = b.id'
            task_columns = ', t.position, t.goal, t.applicability, t.outputs, t.tags'
            if expand:
                conditions = [] if filtered else [f'(b.id IS NOT NULL OR ({exact}))']
                if filtered:
                    arguments = []
    if category:
        conditions.append('s.category = ?')
        arguments.append(category)
    if topic:
        conditions.append('s.topic = ?')
        arguments.append(topic)
    conditions.extend(scope)
    arguments.extend(scope_args)
    enrichment_filter = None
    matched = enrichment_ids(params)
    if matched == 'missing':
        enrichment_filter = 'index_missing'
        conditions.append('0')
    elif isinstance(matched, set):
        enrichment_filter = 'applied'
        if not matched:
            conditions.append('0')
        else:
            conditions.append('s.id IN (' + ','.join('?' * len(matched)) + ')')
            arguments.extend(sorted(matched))
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
    result = {'items': items, 'total': count, 'page': page, 'page_size': PAGE_SIZE}
    if enrichment_filter:
        result['enrichment_filter'] = enrichment_filter
    if has_facets:
        result['facets'] = linked_facets(
            con, query, exact, terms, scope, scope_args, category=category, topic=topic, action=action, tool=tool,
        )
    return result


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


def enrichment_ids(params):
    dimension = value(params, 'edim', 40)
    state = value(params, 'estate', 32)
    chosen = value(params, 'evalue', 80)
    dimension2 = value(params, 'edim2', 40)
    chosen2 = value(params, 'evalue2', 80)
    same = value(params, 'esame', 2)
    role = value(params, 'erole', 16)
    if role not in ('required', 'legacy'):
        role = 'any'
    if not any((dimension, state, chosen, dimension2, chosen2, same, role if role != 'any' else '')):
        return None
    try:
        from enrichment import load_current_annotations, search_annotations
    except ImportError:
        return 'missing'
    annotations = load_current_annotations(enrichment_dir())
    if annotations is None:
        return 'missing'
    filters = []
    if dimension:
        filters.append((dimension, [chosen] if chosen else [], state or None))
    if dimension2:
        filters.append((dimension2, [chosen2] if chosen2 else [], None))
    if not filters:
        return None
    return set(search_annotations(annotations, filters, same != '0', role))


def enrichment_dir():
    override = os.environ.get('ATLAS_ENRICHMENT_DIR')
    return Path(override) if override else ROOT / 'data/library/enrichment'


def enrichment_detail(skill_id):
    folder = enrichment_dir()
    if not (folder / 'current.json').is_file():
        return None
    try:
        from enrichment import detail_for
        return detail_for(folder, skill_id)
    except (OSError, ValueError, json.JSONDecodeError):
        return None


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
    payload = {'id': skill['id'], 'category': skill['category'], 'topic': skill['topic'],
               'label_from': skill['label_from'], 'record': record,
               'display_source': display_source, 'sources': sources}
    enrichment = enrichment_detail(skill['id'])
    if enrichment is not None:
        payload['enrichment'] = enrichment
    return payload


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


def bind(port):
    try:
        return ThreadingHTTPServer(('127.0.0.1', port), Handler)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            raise SystemExit(
                f'{port} 已有进程在监听。打开 http://127.0.0.1:{port} ，'
                '或运行 python3 library/accept_local.py 核对它是否仍是当前目录。不要再启动第二个服务。'
            ) from exc
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    if not DATABASE.is_file():
        parser.error('Run python3 library/build.py to create the local catalogue first')
    with bind(args.port) as server:
        print(f'Atlas Skill Library: http://127.0.0.1:{server.server_port}', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
