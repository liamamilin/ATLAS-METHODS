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
