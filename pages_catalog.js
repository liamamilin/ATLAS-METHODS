'use strict';

(function expose(root) {
  const PAGE_SIZE = 24;
  const fold = (text) => text.normalize('NFKC').toLowerCase().replace(/[-_/\u2010-\u2015\u2212\s]+/g, ' ').trim();
  const compare = new Intl.Collator('zh-CN', { numeric: true }).compare;
  const encodePath = (path) => path.split('/').map(encodeURIComponent).join('/');

  function fromSnapshot({ meta, entries, overviews = {} }) {
    const categoryNames = new Map(meta.categories.map(({ id, label }) => [id, label]));
    const bySource = new Map();
    const records = entries.map(([title, category, topic, labelFrom, rows], order) => {
      const sources = rows.map(([id, repoIndex, path, revision, relationship]) => ({
        id, repo: meta.repos[repoIndex], path, revision, relationship, license: [],
        url: 'https://github.com/' + encodePath(meta.repos[repoIndex]) + '/blob/' +
          encodeURIComponent(revision) + '/' + encodePath(path),
      }));
      const first = sources[0];
      const summary = overviews[first.id]?.summary || '';
      const record = {
        id: first.id, title, category, topic, summary, label_from: labelFrom,
        repo: first.repo, path: first.path, source_count: sources.length, sources, order,
        searchTitle: fold(title),
        search: fold([title, summary, categoryNames.get(category) || '', topic,
          ...sources.flatMap(({ repo, path }) => [repo, path])].join(' ')),
      };
      for (const source of sources) bySource.set(source.id, record);
      return record;
    });

    function route(path) {
      const [routeName, queryString] = path.split('?');
      const query = new URLSearchParams(queryString);
      if (routeName === '/api/meta') return meta;
      if (routeName.startsWith('/api/skill/')) {
        const id = decodeURIComponent(routeName.slice('/api/skill/'.length));
        const record = bySource.get(id);
        if (!record) throw new Error('找不到此来源');
        return {
          id: record.id, display_source: id, title: record.title, category: record.category,
          topic: record.topic, label_from: record.label_from, sources: record.sources,
          overview: overviews[id] || overviews[record.id] || null,
        };
      }
      if (routeName !== '/api/skills') throw new Error('此内容仅在本机资料库可读');
      const q = fold((query.get('q') || '').trim().slice(0, 160));
      const terms = q.split(/\s+/).filter(Boolean).slice(0, 8);
      const repo = query.get('repo');
      const group = query.get('grouped') === '1';
      const category = query.get('category');
      const topic = query.get('topic');
      const sort = query.get('sort');
      const items = records.filter((r) =>
        (!category || r.category === category) && (!topic || r.topic === topic) &&
        (!repo || r.sources.some((s) => s.repo === repo)) && (!group || r.source_count > 1) &&
        terms.every((term) => r.search.includes(term)));
      const byTitle = (a, b) => compare(a.title, b.title) || a.order - b.order;
      if (sort === 'title') items.sort(byTitle);
      else if (sort === 'sources') items.sort((a, b) => b.source_count - a.source_count || byTitle(a, b));
      else if (sort !== 'new' && q) {
        items.sort((a, b) => Number(b.searchTitle.includes(q)) - Number(a.searchTitle.includes(q)) || byTitle(a, b));
      }
      const page = Math.max(1, Math.min(Number.parseInt(query.get('page'), 10) || 1, Math.ceil(items.length / PAGE_SIZE)));
      const start = (page - 1) * PAGE_SIZE;
      return {
        total: items.length, page, page_size: PAGE_SIZE,
        items: items.slice(start, start + PAGE_SIZE).map(({ id, title, summary, path, category, topic, repo, source_count }) =>
          ({ id, title, summary, path, category, topic, repo, source_count })),
      };
    }
    return { route };
  }

  let loading;
  async function load(url, path, signal) {
    if (!loading) {
      loading = fetch(url).then((response) => {
        if (!response.ok) throw new Error('公开目录暂不可用');
        return response.json();
      }).then(fromSnapshot);
    }
    const catalog = await loading;
    if (signal?.aborted) throw new DOMException('Request cancelled', 'AbortError');
    return catalog.route(path);
  }

  if (typeof module !== 'undefined') module.exports = { fromSnapshot };
  else root.atlasPublicCatalog = { load };
})(globalThis);
