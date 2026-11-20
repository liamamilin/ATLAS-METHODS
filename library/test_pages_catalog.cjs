const { test } = require('node:test');
const assert = require('node:assert/strict');
const { fromSnapshot } = require('./web/pages_catalog.js');

test('finds the displayed title even when the GitHub path differs', () => {
  const { route } = fromSnapshot({
    meta: { repos: ['owner/repo'], categories: [{ id: 'legal', label: '法律实务与合规' }] },
    entries: [['KI-Sicherheitskomponente', 'legal', '', 'title', [
      ['source-1', 0, 'de/tmt/skills/sicherheitskomponente-mit-ki/SKILL.md', 'fixed-revision', 'representative'],
    ]]],
  });
  for (const q of ['KI–Sicherheitskomponente', 'KI-Sicherheitskomponente', 'ki sicherheitskomponente',
    'ＫＩ＿Sicherheitskomponente', 'sicherheitskomponente-mit-ki']) {
    assert.equal(route('/api/skills?q=' + encodeURIComponent(q)).items[0].id, 'source-1', q);
  }
  assert.equal(route('/api/skills?q=' + encodeURIComponent('不存在的条目')).total, 0);
});

test('a published overview appears in detail and can be searched', () => {
  const overview = { summary: '保留连续视图', when: '现有应用', steps: ['先检查路由'], limits: [] };
  const { route } = fromSnapshot({
    meta: { repos: ['owner/repo'], categories: [{ id: 'code', label: '软件开发' }] },
    entries: [
      ['First', 'code', '', 'title', [['source-1', 0, 'a/SKILL.md', 'revision', 'representative']]],
      ['Second', 'code', '', 'title', [['source-2', 0, 'b/SKILL.md', 'revision', 'representative']]],
    ],
    overviews: { 'source-1': overview },
  });
  assert.equal(route('/api/skill/source-1').overview, overview);
  assert.equal(route('/api/skill/source-2').overview, null);
  assert.equal(route('/api/skills?q=' + encodeURIComponent('连续视图')).items[0].id, 'source-1');
});
