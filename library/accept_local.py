#!/usr/bin/env python3
"""Read-only diagnosis for the local catalogue and the already running page.

Does not rebuild, import, export, or bind a new public port. It opens the
SQLite file read-only and, when asked, talks only to an existing loopback server.
"""
import argparse
import json
from pathlib import Path
import sqlite3
from urllib.error import URLError
from urllib.request import urlopen

from classification_overrides import DATABASE, OVERRIDES, connect_readonly, load_overrides

REVIEW_TEXT = ('未人工审阅', '已审阅', '待修订')


def catalogue_status(database=DATABASE, overrides=OVERRIDES):
    database, overrides = Path(database), Path(overrides)
    if not database.is_file():
        return {'ok': False, 'error': f'目录文件不存在: {database}'}
    try:
        connection = connect_readonly(database)
    except (OSError, sqlite3.Error, ValueError) as exc:
        return {'ok': False, 'error': f'目录无法只读打开: {exc}'}
    try:
        quick = connection.execute('PRAGMA quick_check').fetchone()[0]
        meta = {}
        for row in connection.execute('SELECT key, value FROM meta'):
            try:
                meta[row['key']] = json.loads(row['value'])
            except (TypeError, json.JSONDecodeError):
                meta[row['key']] = row['value']
        reviews = dict(connection.execute('SELECT review, COUNT(*) FROM skills GROUP BY review'))
        labels = dict(connection.execute('SELECT label_from, COUNT(*) FROM skills GROUP BY label_from'))
        representatives = connection.execute('SELECT COUNT(*) FROM skills').fetchone()[0]
        sources = connection.execute('SELECT COUNT(*) FROM sources').fetchone()[0]
        tasks = connection.execute('SELECT COUNT(*) FROM catalog_tasks').fetchone()[0]
    except sqlite3.Error as exc:
        connection.close()
        return {'ok': False, 'error': f'目录结构无法读取: {exc}'}
    connection.close()
    stored = meta.get('classification_overrides') or {}
    suspended = stored.get('suspended') or []
    override_report = {'present': overrides.is_file(), 'entries': 0, 'error': None}
    if overrides.is_file():
        try:
            override_report['entries'] = len(load_overrides(overrides).get('entries') or {})
        except ValueError as exc:
            override_report['error'] = str(exc)
    status = {
        'ok': quick == 'ok' and override_report['error'] is None,
        'path': str(database),
        'quick_check': quick,
        'as_of': meta.get('as_of'),
        'representatives': representatives,
        'sources': sources,
        'tasks': tasks,
        'reviews': reviews,
        'labels': labels,
        'overrides_applied': stored.get('applied', 0),
        'overrides_suspended': len(suspended),
        'suspended_sample': suspended[:20],
        'override_file': override_report,
    }
    if quick != 'ok':
        status['error'] = '目录完整性检查未通过'
    return status


def server_status(base, catalogue):
    url = base.rstrip('/') + '/api/meta'
    try:
        with urlopen(url, timeout=5) as response:
            payload = json.load(response)
    except (OSError, URLError, json.JSONDecodeError) as exc:
        return {'running': False, 'matches_file': False, 'error': str(exc)}
    compared = {
        key: {'file': catalogue.get(key), 'server': payload.get(key)}
        for key in ('as_of', 'representatives', 'sources')
    }
    matches = all(item['file'] == item['server'] for item in compared.values())
    return {'running': True, 'matches_file': matches, 'compared': compared, 'url': url}


def probe_page(base, shots=None):
    from chrome_probe import ChromePage, chrome_path
    if not chrome_path():
        return {'checked': False, 'ok': False, 'error': '未找到 Chrome，无法做页面验收'}
    from tempfile import TemporaryDirectory
    profile = TemporaryDirectory(ignore_cleanup_errors=True)
    page = ChromePage.start(profile.name)
    observations = {'checked': True, 'ok': False}
    try:
        page.open(base.rstrip('/') + '/')
        state = page.wait(lambda item: item['busy'] == 'false')
        observations['updated'] = state['updated']
        observations['count'] = state['count']
        observations['metric'] = state['metric']
        if state['error'] or '目录读取失败' in state['updated'] or not state['cards']:
            observations['error'] = state['error'] or '页面没有列出条目'
            return observations
        page.shot(str(Path(shots) / 'accept-list.png') if shots else None)
        clicked = page.evaluate('''(() => {
            const node = [...document.querySelectorAll('.nav-button[data-category]')]
                .find((item) => item.dataset.category && !item.dataset.topic && !item.disabled);
            if (!node) return '';
            node.click();
            return node.dataset.category;
        })()''')
        if not clicked:
            observations['error'] = '页面没有可点的领域'
            return observations
        state = page.wait(lambda item: item['busy'] == 'false' and item['cards'])
        page.click_text('#results .card h3', state['cards'][0])
        state = page.wait(lambda item: item['dialogOpen'] and any(text in item['dialogText'] for text in REVIEW_TEXT))
        observations['review_visible'] = any(text in state['dialogText'] for text in REVIEW_TEXT)
        page.shot(str(Path(shots) / 'accept-detail.png') if shots else None)
        page.click_text('#detail .tabs button', '来源')
        state = page.wait(lambda item: '固定提交' in item['dialogText'] or item['links'])
        observations['source_visible'] = '固定提交' in state['dialogText'] or bool(state['links'])
        page.shot(str(Path(shots) / 'accept-sources.png') if shots else None)
        observations['ok'] = observations['review_visible'] and observations['source_visible']
        if not observations['ok']:
            observations['error'] = '详情没有同时显示审阅状态和固定来源'
        return observations
    finally:
        page.close()
        profile.cleanup()


def diagnose(database=DATABASE, overrides=OVERRIDES, base='http://127.0.0.1:8765', require_server=False, page=False, shots=None):
    catalogue = catalogue_status(database, overrides)
    report = {'catalogue': catalogue, 'writes': False}
    if not catalogue.get('ok', False) and 'path' not in catalogue:
        report['ok'] = False
        return report
    server = server_status(base, catalogue) if catalogue.get('ok') else {'running': False, 'matches_file': False}
    report['server'] = server
    ok = bool(catalogue.get('ok'))
    if require_server and not server.get('running'):
        ok = False
        report['error'] = '本机服务没有在预期地址响应'
    if server.get('running') and not server.get('matches_file'):
        ok = False
        report['error'] = '正在响应的服务与目录文件的时间或数量不一致'
    if page:
        if not server.get('running'):
            report['page'] = {'checked': False, 'ok': False, 'error': '服务未响应，未打开页面'}
            ok = False
        else:
            report['page'] = probe_page(base, shots)
            ok = ok and report['page'].get('ok', False)
            shown = ''.join(ch for ch in str(report['page'].get('metric', '')) if ch.isdigit())
            if report['page'].get('ok') and shown != str(catalogue.get('representatives')):
                ok = False
                report['page']['ok'] = False
                report['page']['error'] = '页面目录条目数与目录文件不一致'
            if not report['page'].get('ok'):
                report['error'] = report['page'].get('error') or report.get('error')
    report['ok'] = ok
    return report


def run(argv=None):
    parser = argparse.ArgumentParser(description='只读核对本机目录文件，以及已经打开的本机页面。不重建、不导入、不换端口。')
    parser.add_argument('--database', type=Path, default=DATABASE)
    parser.add_argument('--overrides', type=Path, default=OVERRIDES)
    parser.add_argument('--base', default='http://127.0.0.1:8765')
    parser.add_argument('--require-server', action='store_true')
    parser.add_argument('--page', action='store_true', help='用本机 Chrome 打开已有页面并查看一条详情')
    parser.add_argument('--shots', type=Path, help='页面截图目录。省略则不写文件')
    args = parser.parse_args(argv)
    report = diagnose(
        args.database, args.overrides, args.base, args.require_server, args.page,
        args.shots,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report.get('catalogue', {}).get('error') and 'path' not in report.get('catalogue', {}):
        return 2
    return 0 if report.get('ok') else 1


if __name__ == '__main__':
    raise SystemExit(run())
