/* clMetaHtml 的風險標籤：不碰瀏覽器，只餵 fixture 看輸出的 HTML。 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const strings = {
  'updates.cl_none': 'changelog —', 'updates.cl_pending': 'pending', 'updates.risk_age': 'age {n}',
  'updates.risk_age_today': 'today', 'updates.risk_age_none': 'age —', 'updates.risk_forum_none': '論壇 —',
  'updates.risk_forum_n': '論壇 {n} 篇（最新 {d}）', 'updates.risk_forum_zero': '論壇 0 篇', 'updates.risk_forum_announcement': '論壇 0 篇 · 公告', 'updates.risk_forum_announcement_hint': 'ann {title} {n}',
};
const t = (k, p = {}) => (strings[k] || k).replace(/\{(\w+)\}/g, (_, n) => p[n]);
const esc = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const state = {meta: {}};
const ctx = vm.createContext({state, t, esc, Date, Math, console});
const src = fs.readFileSync('static/js/updates.js', 'utf8');
const a = src.indexOf('function clMetaHtml'), b = src.indexOf('/* 背景抓 changelog');
assert.ok(a > 0 && b > a, 'clMetaHtml block located');
vm.runInContext(src.slice(a, b), ctx);
const iso = d => new Date(Date.now() - d * 86400000).toISOString().replace(/\.\d+Z$/, 'Z');
const render = meta => { state.meta = {p: meta}; return ctx.clMetaHtml({name: 'p'}); };
const cl = {ok: true, urgency: null, summary: '', entries: 0, cves: 0};
const forum = (total) => ({term: '7.0.0-1024', url: 'https://f.example/search?q=7.0.0-1024', ok: true, total,
  topics: total ? [{id: 1, title: 'Freeze <b>', created: '2026-10-30T09:00:00Z', posts: 2, category_id: 721}] : []});

let h = render({...cl, risk: {published: iso(2), forum: forum(3)}});
assert.match(h, /tag sec/); assert.match(h, /age 2/);
assert.match(h, /class="tag reboot" href="https:\/\/f\.example\/search\?q=7\.0\.0-1024"/);
assert.match(h, /論壇 3 篇（最新 10-30）/); assert.match(h, /Freeze &lt;b&gt;/);

h = render({...cl, risk: {published: iso(20), forum: forum(0)}});
assert.doesNotMatch(h, /tag sec/); assert.match(h, /age 20/); assert.match(h, /tag muted" href/); assert.match(h, /論壇 0 篇/);

h = render({...cl, risk: {published: iso(3), forum: {...forum(0), announcement: {id: 388030, title: '620 release feedback & discussion', posts: 2, created: iso(1), url: 'https://f.example/t/388030'}}}});
assert.match(h, /tag muted" href="https:\/\/f\.example\/t\/388030"/); assert.match(h, /論壇 0 篇 · 公告/); assert.doesNotMatch(h, /tag reboot/);
h = render({...cl, risk: {published: iso(0), forum: null}});
assert.match(h, /today/); assert.doesNotMatch(h, /論壇/);

h = render({...cl, risk: {published: null, published_note: 'HTTP 401', forum: {term: 'x', url: 'u', ok: false, note: 'timeout', total: 0, topics: []}}});
assert.match(h, /title="HTTP 401">age —/); assert.match(h, /title="timeout">論壇 —/);

h = render({ok: false, note: 'n', risk: {published: iso(9), forum: forum(1)}});
assert.match(h, /changelog —/); assert.match(h, /age 9/); assert.match(h, /tag reboot/);

h = render({ok: false, note: 'n'});
assert.match(h, /changelog —/); assert.doesNotMatch(h, /age|論壇/);
assert.equal(render(null), `<div class="sub1 clmeta muted">pending</div>`);
console.log('OK updates ui');
