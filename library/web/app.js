'use strict';

const byId = (id) => document.getElementById(id);
const number = new Intl.NumberFormat('zh-CN');
const fields = ['q', 'category', 'topic', 'repo', 'sort', 'grouped'];
const publicSnapshot = document.documentElement.dataset.catalog;
let filters = {};
let meta;
let categories;
let pending;
let detailRequest;
let detailTab = 'method';

function element(tag, content, className) {
  const node = document.createElement(tag);
  if (content !== undefined && content !== null) node.textContent = content;
  if (className) node.className = className;
  return node;
}

function button(label, action) {
  const node = element('button', label);
  node.type = 'button';
  node.addEventListener('click', action);
  return node;
}

function note(message) {
  return element('p', message, 'notice');
}

async function load(path, signal) {
  if (publicSnapshot) return window.atlasPublicCatalog.load(publicSnapshot, path, signal);
  const response = await fetch(path, { signal });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || '本机资料库暂不可用');
  return body;
}

function stateFromURL() {
  const params = new URLSearchParams(location.search);
  return Object.fromEntries([...fields, 'page', 'skill', 'source'].map((key) => [key, params.get(key) || '']));
}

function setURL(values, replace = false) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) {
    if (value && !(key === 'page' && String(value) === '1')) params.set(key, value);
  }
  const target = location.pathname + (params.size ? '?' + params.toString() : '');
  if (location.pathname + location.search !== target) {
    if (replace) history.replaceState(null, '', target);
    else history.pushState(null, '', target);
  }
}

function setOptions(select, options) {
  while (select.options.length > 1) select.remove(1);
  for (const [id, label] of options) select.append(new Option(label, id));
}

function initLayout() {
  const metrics = [['目录条目', meta.representatives], ['已转换来源', meta.normalized],
    ['已合并副本', meta.same_package + (meta.same_format || 0)]];
  for (const [label, value] of metrics) {
    const item = element('div', null, 'metric');
    item.append(element('dt', label), element('dd', number.format(value)));
    byId('metrics').append(item);
  }
  const t = new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(meta.as_of));
  byId('updated').textContent = '目录更新 ' + t;
  categories = new Map(meta.categories.map((c) => [c.id, c]));
  const nav = byId('category-nav');
  function categoryButton(id, label, count) {
    const node = button('', () => navigate({ category: id, topic: '', page: 1 }));
    node.className = 'nav-button';
    node.dataset.category = id;
    node.append(element('span', label), element('span', number.format(count), 'nav-count'));
    nav.append(node);
  }
  categoryButton('', '全部 Skill', meta.representatives);
  let section;
  for (const cat of meta.categories) {
    if (cat.section !== section) nav.append(element('p', cat.section, 'nav-heading'));
    section = cat.section;
    categoryButton(cat.id, cat.label, cat.count);
  }
  setOptions(byId('category-select'), meta.categories.map((cat) => [cat.id, cat.label]));
  setOptions(byId('repo'), meta.repos.map((repo) => [repo, repo]));
}

function updateControls() {
  byId('query').value = filters.q;
  byId('category-select').value = filters.category;
  byId('repo').value = filters.repo;
  byId('sort').value = filters.sort;
  byId('grouped').checked = filters.grouped === '1';
  const topics = [...new Set(meta.topics.filter((t) => !filters.category || t.category === filters.category).map((t) => t.topic))];
  setOptions(byId('topic'), topics.map((topic) => [topic, topic]));
  byId('topic').value = filters.topic;
  byId('topic').parentElement.hidden = topics.length === 0;
  byId('clear').hidden = !fields.some((key) => !!filters[key]);
  for (const node of document.querySelectorAll('.nav-button')) {
    node.setAttribute('aria-pressed', String(node.dataset.category === filters.category));
  }
  byId('result-title').textContent = categories.get(filters.category)?.label || '全部 Skill';
}

function navigate(update) {
  setURL({ ...filters, ...update, skill: '', source: '' });
  renderPage();
}

function card(item) {
  const params = new URLSearchParams(location.search);
  params.set('skill', item.id);
  params.delete('source');
  const link = element('a', null, 'card');
  link.href = '?' + params.toString();
  const label = categories.get(item.category)?.label;
  link.append(element('span', item.topic ? label + ' · ' + item.topic : label, 'pill'));
  link.append(element('h3', item.title));
  link.append(element('p', item.summary || item.path || '打开详情查看原记录。'));
  const footer = element('div', null, 'card-foot');
  footer.append(element('span', item.repo), element('span', number.format(item.source_count) + ' 个来源'));
  link.append(footer);
  link.addEventListener('click', (event) => {
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    detailTab = 'method';
    setURL({ ...filters, skill: item.id });
    filters = stateFromURL();
    syncDetail();
  });
  return link;
}

async function renderPage() {
  filters = stateFromURL();
  updateControls();
  syncDetail();
  pending?.abort();
  pending = new AbortController();
  const signal = pending.signal;
  const results = byId('results');
  const query = new URLSearchParams();
  for (const key of [...fields, 'page']) if (filters[key]) query.set(key, filters[key]);
  results.setAttribute('aria-busy', 'true');
  try {
    const page = await load('/api/skills?' + query.toString(), signal);
    if (signal.aborted) return;
    results.replaceChildren(...page.items.map(card));
    if (!page.items.length) results.append(note('没有匹配的 Skill。试试更短的关键词，或清除筛选。'));
    byId('result-count').textContent = number.format(page.total) + ' 条';
    const last = Math.max(1, Math.ceil(page.total / page.page_size));
    byId('pagination').hidden = last === 1;
    byId('previous').disabled = page.page <= 1;
    byId('next').disabled = page.page >= last;
    byId('previous').onclick = () => { navigate({ page: page.page - 1 }); window.scrollTo(0, 0); };
    byId('next').onclick = () => { navigate({ page: page.page + 1 }); window.scrollTo(0, 0); };
    byId('page-info').textContent = page.page + ' / ' + last;
  } catch (error) {
    if (!signal.aborted) {
      results.replaceChildren(note(error.message));
      byId('pagination').hidden = true;
      byId('result-count').textContent = '';
    }
  } finally {
    if (!signal.aborted) results.setAttribute('aria-busy', 'false');
  }
}

function evidenceList(points) {
  if (!points?.length) return null;
  const list = element('ul', null, 'evidence');
  for (const e of points) {
    const location = e.line_start ? ' · 行 ' + e.line_start + (e.line_end !== e.line_start ? '–' + e.line_end : '') : '';
    list.append(element('li', e.path + location + (e.section ? ' · ' + e.section : '')));
  }
  const details = element('details');
  details.append(element('summary', '证据位置'), list);
  return details;
}

function stringList(body, heading, rows) {
  if (!rows?.length) return;
  const list = element('ul');
  for (const text of rows) list.append(element('li', text));
  body.append(element('h4', heading), list);
}

function objectList(body, name, entries, ordered = false) {
  if (!entries?.length) return;
  const list = element(ordered ? 'ol' : 'ul');
  for (const item of entries) {
    const li = element('li', item.action || item.text || item.detail || item.name || item.title || item.locator || '');
    if (item.when) li.append(element('p', '适用条件 · ' + item.when, 'evidence'));
    if (item.rationale) li.append(element('p', item.rationale, 'evidence'));
    if (item.locator && item.name) li.append(element('p', item.locator, 'evidence'));
    if (item.usage) li.append(element('p', item.usage, 'evidence'));
    const evidence = evidenceList(item.evidence);
    if (evidence) li.append(evidence);
    list.append(li);
  }
  body.append(element('h4', name), list);
}

const labelSource = { sample: '已有样本分类', rule: '规则初分', source: '按来源专题归类', title: '按标题关键词初分', unfiled: '待归类' };
const relationship = {
  representative: '代表记录', same_package: '相同材料包',
  planned_equivalent: '去重计划关联', identical_supplied_text: '相同供给材料',
  same_markdown_structure: '相同 Markdown 结构', model_equivalent: '同义判定关联',
};
const reviewStatus = { unreviewed: '未人工审阅', reviewed: '已审阅', needs_revision: '待修订' };

function copyText(record, source) {
  const c = record.content;
  const lines = [c.title || c.name || record.source.skill_path, c.summary, ''];
  for (const task of c.tasks || []) {
    lines.push(task.goal);
    if (task.when) lines.push('适用条件 · ' + task.when);
    (task.steps || []).forEach((step, i) => lines.push((i + 1) + '. ' + step.action + (step.when ? '（' + step.when + '）' : '')));
    for (const item of task.outputs || []) lines.push('产出 · ' + item);
    for (const item of task.checks || []) lines.push('检查 · ' + item.text);
    lines.push('');
  }
  for (const item of c.rules || []) lines.push('限制 · ' + item.text);
  for (const item of record.extraction.gaps || []) lines.push('缺口 · ' + item.detail);
  if (source) lines.push('', '固定来源 · ' + source.url);
  lines.push('审阅状态 · ' + (reviewStatus[record.extraction.review_status] || record.extraction.review_status));
  return lines.filter((line) => line !== null && line !== undefined).join('\n');
}

function renderMethod(container, record) {
  const c = record.content;
  if (c.tasks?.length) container.append(element('h3', '做法'));
  for (const task of c.tasks || []) {
    const box = element('details');
    box.open = c.tasks.length === 1;
    box.append(element('summary', task.goal || '任务'));
    if (task.when) box.append(element('p', '适用条件 · ' + task.when));
    stringList(box, '输入', task.inputs);
    objectList(box, '步骤', task.steps, true);
    stringList(box, '产出', task.outputs);
    objectList(box, '检查', task.checks);
    const evidence = evidenceList(task.evidence);
    if (evidence) box.append(evidence);
    container.append(box);
  }
  objectList(container, '规则与限制', c.rules);
  objectList(container, '参考资源', c.resources);
  objectList(container, '补充说明', c.notes);
  objectList(container, '已知缺口', record.extraction.gaps);
  if (!container.childNodes.length) container.append(note('本条记录仅提供顶部摘要。'));
}

function renderSources(container, data) {
  for (const source of data.sources) {
    const box = element('details');
    box.open = source.id === data.display_source;
    box.append(element('summary', source.repo + ' · ' + (relationship[source.relationship] || '关联来源')));
    const path = element('div');
    path.append(element('code', source.path));
    box.append(path);
    box.append(element('p', '固定提交 ' + source.revision, 'evidence'));
    if (source.license.length) box.append(element('p', '许可线索 · ' + source.license.join('、'), 'evidence'));
    const actions = element('div', null, 'actions');
    const link = element('a', 'GitHub 固定版本');
    link.href = source.url;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    actions.append(link);
    if (source.has_record && source.id !== data.display_source) {
      actions.append(button('查看该来源的转换', () => {
        setURL({ ...filters, skill: data.id, source: source.id });
        filters = stateFromURL();
        detailTab = 'method';
        syncDetail();
      }));
    }
    if (source.has_raw) {
      const read = button('查看本机原文', async () => {
        const visible = box.querySelector('.raw-block');
        if (visible) { visible.remove(); read.textContent = '查看本机原文'; return; }
        read.disabled = true;
        try {
          const original = await load('/api/source/' + source.id);
          box.append(element('pre', original.text, 'raw-block'));
          read.textContent = '收起原文';
        } catch (error) {
          box.append(element('p', error.message, 'evidence'));
        } finally {
          read.disabled = false;
        }
      });
      actions.append(read);
    }
    box.append(actions);
    container.append(box);
  }
}

function renderDetail(data) {
  const root = byId('detail-content');
  const record = data.record;
  const category = categories.get(data.category)?.label || '待归类';
  if (!record) {
    const title = element('h2', data.title);
    title.id = 'detail-title';
    const sources = element('section', null, 'source-list');
    sources.append(element('h3', '固定版本来源 · ' + data.sources.length));
    renderSources(sources, data);
    root.replaceChildren(element('span', data.topic ? category + ' · ' + data.topic : category, 'pill'),
      title, element('p', labelSource[data.label_from] || '', 'detail-banner'), sources);
    return;
  }
  const title = element('h2', record.content.title || record.content.name || record.source.skill_path);
  title.id = 'detail-title';
  const top = element('span', data.topic ? category + ' · ' + data.topic : category, 'pill');
  const status = [reviewStatus[record.extraction.review_status] || record.extraction.review_status, labelSource[data.label_from]].filter(Boolean).join(' · ');
  root.replaceChildren(top, title, element('p', status, 'detail-banner'));
  if (record.content.summary) root.append(element('p', record.content.summary, 'summary'));
  const tabs = element('div', null, 'tabs');
  const body = element('section', null, 'detail-body source-list');
  function renderTab() {
    body.replaceChildren();
    for (const node of tabs.children) node.setAttribute('aria-pressed', String(node.dataset.tab === detailTab));
    if (detailTab === 'method') renderMethod(body, record);
    else renderSources(body, data);
  }
  for (const [id, label] of [['method', '做法与限制'], ['sources', '来源 · ' + data.sources.length]]) {
    const tab = button(label, () => { detailTab = id; renderTab(); });
    tab.dataset.tab = id;
    tabs.append(tab);
  }
  const toolbar = element('div', null, 'detail-tools');
  const copyStatus = element('span', '', 'evidence');
  copyStatus.setAttribute('role', 'status');
  const copy = button('复制做法', async () => {
    try {
      await navigator.clipboard.writeText(copyText(record, data.sources.find((s) => s.id === data.display_source)));
      copyStatus.textContent = '已复制';
    } catch {
      copyStatus.textContent = '剪贴板不可用，可直接选中文本复制';
    }
  });
  toolbar.append(copy, copyStatus);
  root.append(toolbar, tabs, body);
  renderTab();
}

async function syncDetail() {
  detailRequest?.abort();
  const dialog = byId('detail');
  if (!filters.skill) {
    if (dialog.open) dialog.close();
    return;
  }
  detailRequest = new AbortController();
  const signal = detailRequest.signal;
  const heading = element('h2', '正在读取');
  heading.id = 'detail-title';
  byId('detail-content').replaceChildren(heading);
  if (!dialog.open) dialog.showModal();
  dialog.scrollTop = 0;
  try {
    const suffix = filters.source ? '?source=' + encodeURIComponent(filters.source) : '';
    const data = await load('/api/skill/' + encodeURIComponent(filters.skill) + suffix, signal);
    if (!signal.aborted) renderDetail(data);
  } catch (error) {
    if (!signal.aborted) {
      heading.textContent = '无法打开 Skill';
      byId('detail-content').append(note(error.message));
    }
  }
}

async function start() {
  try {
    meta = await load('/api/meta');
    initLayout();
    byId('search-form').addEventListener('submit', (event) => {
      event.preventDefault();
      navigate({ q: byId('query').value.trim(), page: 1 });
    });
    byId('category-select').addEventListener('change', () => navigate({ category: byId('category-select').value, topic: '', page: 1 }));
    for (const key of ['sort', 'repo', 'topic', 'grouped']) {
      byId(key).addEventListener('change', () => navigate({ [key]: key === 'grouped' ? (byId(key).checked ? '1' : '') : byId(key).value, page: 1 }));
    }
    byId('clear').addEventListener('click', () => navigate(Object.fromEntries([...fields, 'page'].map((key) => [key, '']))));
    byId('close-detail').addEventListener('click', () => byId('detail').close());
    byId('detail').addEventListener('close', () => {
      if (!stateFromURL().skill) return;
      setURL({ ...stateFromURL(), skill: '', source: '' }, true);
      filters = stateFromURL();
    });
    window.addEventListener('popstate', renderPage);
    await renderPage();
  } catch (error) {
    byId('updated').textContent = '目录读取失败';
    byId('results').replaceChildren(note(error.message));
    byId('results').setAttribute('aria-busy', 'false');
  }
}

start();
