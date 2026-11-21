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
DEFAULT_QUERY = '视图过渡'
CATALOGUE_MARK = '目录归类，未审源文'
UNFILED_MARK = '待归类'


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


def _shot(page, shots, name):
    if shots:
        page.shot(str(Path(shots) / name))


def _fail(observations, error):
    observations['ok'] = False
    observations['error'] = error
    return observations


def _source_visible(state):
    return '固定提交' in state.get('dialogText', '') or bool(state.get('links'))


def inspect_page(page, shots=None, query=DEFAULT_QUERY):
    """Read-only browser checks. `page` is a ChromePage or a test double."""
    observations = {'checked': True, 'ok': False}
    try:
        state = page.wait(lambda item: item['busy'] == 'false')
        observations['updated'] = state['updated']
        observations['count'] = state['count']
        observations['metric'] = state['metric']
        if state.get('error') or '目录读取失败' in state.get('updated', '') or not state.get('cards'):
            return _fail(observations, state.get('error') or '页面没有列出条目')
        full_count, full_cards = state['count'], list(state['cards'])
        _shot(page, shots, 'accept-list.png')

        page.evaluate(
            '(() => { const query = document.getElementById("query");'
            f' query.value = {json.dumps(query)};'
            ' document.getElementById("search-form").requestSubmit(); })()'
        )
        state = page.wait(lambda item: item['busy'] == 'false')
        if not state.get('cards'):
            return _fail(observations, f'搜索「{query}」没有结果')
        if state['count'] == full_count and list(state['cards']) == full_cards:
            return _fail(observations, f'搜索「{query}」没有改变列表')
        observations['search'] = {'query': query, 'count': state['count'], 'cards': state['cards'][:8]}
        _shot(page, shots, 'accept-search.png')

        page.evaluate('document.getElementById("clear").click()')
        state = page.wait(lambda item: item['busy'] == 'false' and item['count'] == full_count)
        if not state.get('cards'):
            return _fail(observations, '清除搜索后没有恢复列表')

        chosen = page.evaluate('''(() => {
            const buttons = [...document.querySelectorAll('.nav-button[data-category]')];
            const domain = (item) => item.dataset.category && item.dataset.category !== 'unfiled'
                && !item.dataset.topic && !item.disabled;
            const named = buttons.find((item) => domain(item) && item.textContent.includes('软件开发与架构'));
            const node = named || buttons.find(domain);
            if (!node) return null;
            node.click();
            return {category: node.dataset.category, label: node.querySelector('span')?.textContent || node.dataset.category};
        })()''')
        if not chosen or not chosen.get('category'):
            return _fail(observations, '页面没有可点的已归类领域')
        state = page.wait(lambda item: item['busy'] == 'false' and item.get('cards'))
        page.click_text('#results .card h3', state['cards'][0])
        state = page.wait(lambda item: item['dialogOpen'] and '未人工审阅' in item['dialogText'])
        catalogue_or_domain = CATALOGUE_MARK in state['dialogText'] or chosen['label'] in state['dialogText']
        if not catalogue_or_domain:
            return _fail(observations, '已归类详情没有显示目录归类或领域名')
        _shot(page, shots, 'accept-detail.png')
        page.click_text('#detail .tabs button', '来源')
        state = page.wait(lambda item: item['dialogOpen'] and _source_visible(item))
        if not _source_visible(state):
            return _fail(observations, '已归类详情没有固定来源')
        observations['classified'] = {
            'category': chosen['category'], 'label': chosen['label'],
            'review_visible': True, 'catalogue_or_domain_visible': True, 'source_visible': True,
        }
        _shot(page, shots, 'accept-sources.png')

        page.evaluate('document.getElementById("close-detail").click()')
        page.wait(lambda item: not item['dialogOpen'])
        page.evaluate('document.getElementById("clear").click()')
        page.wait(lambda item: item['busy'] == 'false' and item['count'] == full_count)

        opened = page.evaluate('''(() => {
            const node = [...document.querySelectorAll('.nav-button[data-category]')]
                .find((item) => item.dataset.category === 'unfiled' && !item.dataset.topic && !item.disabled);
            if (!node) return false;
            node.click();
            return true;
        })()''')
        if not opened:
            return _fail(observations, '页面没有可点的待归类')
        state = page.wait(lambda item: item['busy'] == 'false' and item.get('cards'))
        page.click_text('#results .card h3', state['cards'][0])
        state = page.wait(lambda item: item['dialogOpen'] and '未人工审阅' in item['dialogText'])
        if UNFILED_MARK not in state['dialogText']:
            return _fail(observations, '待归类详情没有显示待归类')
        if CATALOGUE_MARK in state['dialogText']:
            return _fail(observations, '待归类详情被显示成目录归类覆盖')
        _shot(page, shots, 'accept-unfiled.png')
        page.click_text('#detail .tabs button', '来源')
        state = page.wait(lambda item: item['dialogOpen'] and _source_visible(item))
        if not _source_visible(state):
            return _fail(observations, '待归类详情没有固定来源')
        observations['unfiled'] = {
            'review_visible': True, 'unfiled_visible': True, 'source_visible': True,
            'classified_override': False,
        }
        _shot(page, shots, 'accept-unfiled-sources.png')
        observations['ok'] = True
        return observations
    except (AssertionError, RuntimeError) as exc:
        return _fail(observations, str(exc) or '页面验收未完成')


def probe_page(base, shots=None, query=DEFAULT_QUERY):
    from chrome_probe import ChromePage, chrome_path
    if not chrome_path():
        return {'checked': False, 'ok': False, 'error': '未找到 Chrome，无法做页面验收'}
    from tempfile import TemporaryDirectory
    profile = TemporaryDirectory(ignore_cleanup_errors=True)
    page = ChromePage.start(profile.name)
    try:
        page.open(base.rstrip('/') + '/')
        return inspect_page(page, shots, query)
    finally:
        page.close()
        profile.cleanup()


def diagnose(database=DATABASE, overrides=OVERRIDES, base='http://127.0.0.1:8765', require_server=False, page=False, shots=None, query=DEFAULT_QUERY):
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
            report['page'] = probe_page(base, shots, query)
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
    parser.add_argument('--page', action='store_true', help='用本机 Chrome 只读打开已有页面，检查搜索、已归类和待归类详情')
    parser.add_argument('--query', default=DEFAULT_QUERY, help='页面搜索用的关键词，默认「视图过渡」')
    parser.add_argument('--shots', type=Path, help='页面截图目录。省略则不写文件')
    args = parser.parse_args(argv)
    report = diagnose(
        args.database, args.overrides, args.base, args.require_server, args.page,
        args.shots, args.query,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report.get('catalogue', {}).get('error') and 'path' not in report.get('catalogue', {}):
        return 2
    return 0 if report.get('ok') else 1


if __name__ == '__main__':
    raise SystemExit(run())
