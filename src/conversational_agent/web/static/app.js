/* News Agent — browser app.

   A view over the same system the CLI drives. Two surfaces, as in the CLI:
   the reader pages (Today, Groups) never show a band, a score, or what a
   thread "revealed"; the Console is the builder's view of every model call.

   No framework and no build step. State lives in S; paint() rebuilds the page
   from it. Everything that comes from the model or the web is inserted as
   text, never as HTML, and links are only made from http(s) URLs. */
'use strict';
(() => {

const $view = document.getElementById('view');
const $rail = document.getElementById('rail');
const $toasts = document.getElementById('toasts');
const LS_USER = 'newsagent.user';

function freshToday() {
  return {
    phase: 'idle',        // idle | opening | active
    session: null,        // result of opening a session
    topic: null,          // {group_id, group_name, reason}
    briefState: 'none',   // none | loading | ready | empty | error
    briefError: '',
    exchange: null,       // the briefing + thread in front of the reader
    asking: false,
    pendingQuestion: '',
    closing: false,
    calls: [],            // model calls this visit made, for "behind the scenes"
    openThreads: [],
  };
}

const S = {
  booted: false,
  meta: null,
  users: [],
  userId: readStore(LS_USER),
  groups: [],
  route: { name: 'today', args: [] },
  polling: {},            // group id -> true while a news check runs
  checkingAll: false,
  today: freshToday(),
  page: {},               // data for the page on screen
  drafts: {},             // input text that must survive a repaint
  ui: {},                 // small toggles (forms open, filters)
};

function readStore(key) { try { return localStorage.getItem(key); } catch (e) { return null; } }
function writeStore(key, value) {
  try { value == null ? localStorage.removeItem(key) : localStorage.setItem(key, value); } catch (e) { /* private mode */ }
}

/* ---------- DOM helpers ---------- */

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  const a = attrs || {};
  for (const [k, v] of Object.entries(a)) {
    if (v == null || v === false || k === 'value') continue;
    if (k === 'class') el.className = v;
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
    else if (v === true) el.setAttribute(k, '');
    else el.setAttribute(k, v);
  }
  add(el, kids);
  if (a.value != null) el.value = a.value;
  return el;
}
function add(el, kids) {
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false || kid === '') continue;
    el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
const spinner = () => h('span', { class: 'spinner', 'aria-hidden': 'true' });

function safeUrl(url) {
  try {
    const u = new URL(String(url));
    return u.protocol === 'http:' || u.protocol === 'https:' ? u.href : null;
  } catch (e) { return null; }
}
function extLink(url, label) {
  const href = safeUrl(url);
  if (!href) return label || null;
  let text = label;
  if (!text) { try { text = new URL(href).hostname.replace(/^www\./, ''); } catch (e) { text = href; } }
  return h('a', { href, target: '_blank', rel: 'noopener noreferrer' }, text, ' ↗');
}
function paras(text) {
  return String(text || '').split(/\n\s*\n|\n/).map(s => s.trim()).filter(Boolean).map(s => h('p', null, s));
}

/* ---------- formatting ---------- */

function when(iso) {
  if (!iso) return 'never';
  const then = new Date(iso);
  if (isNaN(then)) return String(iso).slice(0, 16);
  const s = Math.round((Date.now() - then.getTime()) / 1000);
  if (s < 0) return dateLabel(iso);
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  if (s < 86400 * 7) return `${Math.floor(s / 86400)}d ago`;
  return dateLabel(iso);
}
function dateLabel(iso, withTime) {
  const d = new Date(iso);
  if (isNaN(d)) return String(iso || '');
  const opts = { day: 'numeric', month: 'short' };
  if (d.getFullYear() !== new Date().getFullYear()) opts.year = 'numeric';
  if (withTime) { opts.hour = '2-digit'; opts.minute = '2-digit'; }
  return d.toLocaleString(undefined, opts);
}
const stamp = iso => { const d = new Date(iso); return isNaN(d) ? String(iso || '-') : d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit' }); };
const money = x => (x == null ? '?' : '$' + (x < 0.01 ? x.toFixed(4) : x.toFixed(3)));
const secs = msv => (msv == null ? '-' : msv < 1000 ? `${msv} ms` : `${(msv / 1000).toFixed(1)} s`);
const num = x => (x == null ? '-' : Number(x).toLocaleString());
const plural = (n, one, many) => `${n} ${n === 1 ? one : (many || one + 's')}`;
function intervalLabel(mins) {
  if (mins % 1440 === 0) return mins === 1440 ? 'daily' : `every ${mins / 1440} days`;
  if (mins % 60 === 0) return mins === 60 ? 'hourly' : `every ${mins / 60} hours`;
  return `every ${mins} min`;
}
function isDue(g) {
  if (!g.last_polled_at) return true;
  return Date.now() - new Date(g.last_polled_at).getTime() >= g.poll_interval_minutes * 60000;
}

/* ---------- API ---------- */

async function call(method, path, body, params) {
  const headers = {};
  if (S.userId) headers['X-User-Id'] = S.userId;
  let url = path;
  if (params) {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) if (v != null && v !== '' && v !== false) q.set(k, v === true ? '1' : v);
    const qs = q.toString();
    if (qs) url += '?' + qs;
  }
  const init = { method, headers };
  if (method === 'POST') { headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(body || {}); }
  let res;
  try { res = await fetch(url, init); }
  catch (e) { throw new Error('Could not reach the server. Is it still running?'); }
  let data = null;
  try { data = await res.json(); } catch (e) { /* non-JSON error */ }
  if (!res.ok) throw new Error((data && data.error) || `Request failed (${res.status}).`);
  return data;
}
const get = (path, params) => call('GET', path, null, params);
const post = (path, body) => call('POST', path, body);

function toast(message, kind) {
  const el = h('div', { class: 'toast' + (kind === 'error' ? ' error' : ''), 'data-testid': 'toast' }, message);
  $toasts.append(el);
  setTimeout(() => el.remove(), kind ? 9000 : 4500);
}
const fail = e => toast(e && e.message ? e.message : String(e), 'error');
function noteCalls(res) { if (res && Array.isArray(res.calls)) S.today.calls.push(...res.calls); }

/* ---------- reading tracker ----------
   The browser is the first surface that can actually observe how a briefing
   was read: how long it was on screen in a visible tab, and how far down it
   the viewport got. Sent once per briefing, with the reader's first action
   (or as a beacon if they just leave). It is attention, never comprehension;
   the server stores it on the exchange and nowhere else. */

const Reading = {
  id: null, ms: 0, fraction: 0, done: true, timer: null, last: 0,
  start(exchange) {
    this.stop();
    this.id = exchange.id; this.ms = 0; this.fraction = 0;
    this.done = !!exchange.reading_recorded || !!exchange.closed;
    if (this.done) return;
    this.last = performance.now();
    this.timer = setInterval(() => this.tick(), 250);
  },
  tick() {
    const now = performance.now(); const dt = now - this.last; this.last = now;
    const el = document.querySelector('[data-reading-target]');
    if (!el || document.visibilityState !== 'visible' || dt > 2000) return;
    const r = el.getBoundingClientRect(); const vh = window.innerHeight;
    if (r.height <= 0 || r.top >= vh || r.bottom <= 0) return;
    this.ms += dt;
    this.fraction = Math.max(this.fraction, Math.min(1, (Math.min(r.bottom, vh) - r.top) / r.height));
  },
  snapshot() {
    if (this.done || !this.id) return null;
    this.tick();
    return { dwell_ms: Math.round(this.ms), scroll_fraction: Math.round(this.fraction * 1000) / 1000 };
  },
  finish() { this.done = true; this.stop(); },
  stop() { if (this.timer) clearInterval(this.timer); this.timer = null; },
};
window.addEventListener('pagehide', () => {
  const snap = Reading.snapshot();
  if (!snap || !S.userId || !navigator.sendBeacon) return;
  const url = `/api/exchanges/${encodeURIComponent(Reading.id)}/reading?user=${encodeURIComponent(S.userId)}`;
  navigator.sendBeacon(url, new Blob([JSON.stringify(snap)], { type: 'application/json' }));
});

/* ---------- data ---------- */

const me = () => S.users.find(u => u.id === S.userId) || null;
const groupById = id => S.groups.find(g => g.id === id) || null;

async function loadUsers() {
  S.users = await get('/api/users');
  if (!me()) {
    S.userId = S.users.length === 1 ? S.users[0].id : null;
    writeStore(LS_USER, S.userId);
  }
}
async function loadGroups() { S.groups = me() ? await get('/api/groups') : []; }
async function loadMeta() { S.meta = await get('/api/meta'); }
async function loadOpenThreads() { S.today.openThreads = me() ? await get('/api/open-exchanges') : []; }

async function switchUser(id) {
  Reading.stop();
  S.userId = id; writeStore(LS_USER, id);
  S.today = freshToday(); S.page = {}; S.groups = [];
  paint();
  try { await loadGroups(); await loadOpenThreads(); } catch (e) { fail(e); }
  route();
}

/* ---------- actions: reader ---------- */

async function createUser(name) {
  const user = await post('/api/users', { name });
  await loadUsers();
  await switchUser(user.id);
}

async function createGroup(fields) {
  const group = await post('/api/groups', fields);
  await loadGroups();
  return group;
}

async function checkGroup(id, quiet) {
  if (S.polling[id]) return null;
  S.polling[id] = true; paint();
  try {
    const res = await post(`/api/groups/${id}/poll`);
    noteCalls(res);
    const i = S.groups.findIndex(g => g.id === id);
    if (i >= 0 && res.group) S.groups[i] = res.group;
    const name = res.group ? res.group.name : 'that group';
    if (!quiet) {
      toast(res.material_found
        ? `${plural(res.material_found, 'thing')} worth knowing in ${name}.`
        : `Nothing new worth telling you in ${name}.`);
    }
    // The monitor's own note about the search (what it discarded and why).
    // Information, not a failure; the full text is in the console traces.
    if (res.note) toast(`${name}: ${res.note.length > 200 ? res.note.slice(0, 200) + '…' : res.note}`, 'note');
    return res;
  } catch (e) { fail(e); return null; }
  finally { delete S.polling[id]; if (S.route.name === 'group') refreshPage(); else paint(); }
}

async function checkDue() {
  if (S.checkingAll) return;
  S.checkingAll = true; paint();
  let found = 0;
  try {
    for (const g of S.groups.filter(isDue)) {
      const res = await checkGroup(g.id, true);
      if (res) found += res.material_found || 0;
    }
    toast(found ? `${plural(found, 'thing')} worth knowing. Open a session to hear them.` : 'Nothing new worth telling you.');
  } finally { S.checkingAll = false; paint(); }
}

async function openSession() {
  const t = S.today = Object.assign(freshToday(), { phase: 'opening', calls: S.today.calls });
  paint();
  try {
    const res = await post('/api/session');
    noteCalls(res);
    t.session = res; t.phase = 'active';
    loadGroups().then(paint, () => {});
    if (res.topic) await startBrief(res.topic);
    else paint();
  } catch (e) { t.phase = 'idle'; fail(e); paint(); }
}

async function startBrief(topic) {
  const t = S.today;
  t.phase = 'active'; t.topic = topic; t.briefState = 'loading'; t.exchange = null;
  paint();
  try {
    const res = await post(`/api/groups/${topic.group_id}/brief`);
    noteCalls(res);
    if (res.exchange) showExchange(res.exchange);
    else t.briefState = 'empty';
    loadGroups().then(paint, () => {});
  } catch (e) { t.briefState = 'error'; t.briefError = e.message; }
  paint();
}

function showExchange(exchange, topic) {
  const t = S.today;
  t.phase = 'active'; t.exchange = exchange; t.briefState = 'ready';
  if (topic !== undefined) t.topic = topic;
  if (!t.topic) t.topic = { group_id: exchange.group_id, group_name: exchange.group_name, reason: '' };
  Reading.start(exchange);
}

async function askQuestion() {
  const t = S.today; const x = t.exchange;
  const text = (S.drafts.ask || '').trim();
  if (!x || x.closed || t.asking || t.closing || !text) return;
  t.asking = true; t.pendingQuestion = text; S.drafts.ask = '';
  paint();
  scrollToEnd();
  try {
    const res = await post(`/api/exchanges/${x.id}/ask`, { text, reading: Reading.snapshot() });
    Reading.finish(); noteCalls(res);
    x.turns.push({ speaker: 'user', text }, { speaker: 'system', text: res.answer.text, source_url: res.answer.source_url });
  } catch (e) {
    fail(e);
    // The question may already be on record even though the answer failed;
    // show what the server actually holds rather than guessing.
    try { const fresh = await get(`/api/exchanges/${x.id}`); if (t.exchange === x) { t.exchange = fresh; } } catch (e2) { S.drafts.ask = text; }
  } finally { t.asking = false; t.pendingQuestion = ''; paint(); scrollToEnd(); }
}

async function closeThread() {
  const t = S.today; const x = t.exchange;
  if (!x || x.closed || t.closing || t.asking) return;
  t.closing = true; paint();
  try {
    const res = await post(`/api/exchanges/${x.id}/close`, { reading: Reading.snapshot() });
    Reading.finish(); noteCalls(res);
    x.closed = true;
    if (res.groups) S.groups = res.groups;
    t.openThreads = t.openThreads.filter(o => o.id !== x.id);
  } catch (e) { fail(e); }
  finally { t.closing = false; paint(); }
}

function backToToday() {
  Reading.stop();
  S.today = Object.assign(freshToday(), { calls: [] });
  paint();
  Promise.all([loadGroups(), loadOpenThreads()]).then(paint, fail);
}

function scrollToEnd() {
  requestAnimationFrame(() => {
    const el = document.querySelector('[data-thread-end]');
    if (el) el.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  });
}

/* ---------- rail ---------- */

function railNode() {
  const totalNew = S.groups.reduce((n, g) => n + (g.new || 0), 0);
  const r = S.route.name;
  const link = (href, label, current, extra) =>
    h('a', { href, 'aria-current': current ? 'page' : null, 'data-testid': `nav-${label.toLowerCase()}` }, h('span', null, label), extra);

  const nodes = [
    h('a', { class: 'brand', href: '#/' },
      h('span', { class: 'brand-mark' }, svgMark()),
      h('span', { class: 'brand-name' }, 'News Agent')),
    h('nav', { class: 'nav', 'aria-label': 'Main' },
      link('#/', 'Today', r === 'today', totalNew ? h('span', { class: 'count', title: 'Things not yet shown to you' }, totalNew) : null),
      link('#/groups', 'Groups', r === 'groups' || r === 'group'),
      h('div', { class: 'nav-label' }, 'Builder'),
      link('#/console', 'Console', r === 'console')),
  ];

  const foot = h('div', { class: 'rail-foot' });
  if (S.users.length) {
    if (S.ui.addingUser) {
      foot.append(h('form', { class: 'inline', onsubmit: async e => {
        e.preventDefault();
        const name = (S.drafts.newUser || '').trim(); if (!name) return;
        try { S.ui.addingUser = false; S.drafts.newUser = ''; await createUser(name); } catch (err) { S.ui.addingUser = true; fail(err); paint(); }
      } },
        h('input', { class: 'field', placeholder: 'Name', 'aria-label': 'New person name', 'data-keep': 'newUser', value: S.drafts.newUser || '', oninput: e => { S.drafts.newUser = e.target.value; } }),
        h('button', { class: 'btn small primary', type: 'submit' }, 'Add'),
        h('button', { class: 'btn small quiet', type: 'button', onclick: () => { S.ui.addingUser = false; paint(); } }, 'Cancel')));
    } else {
      foot.append(h('select', {
        'aria-label': 'Who is reading', 'data-testid': 'user-select', value: S.userId || '',
        onchange: e => {
          if (e.target.value === '+') { S.ui.addingUser = true; paint(); const f = document.querySelector('[data-keep="newUser"]'); if (f) f.focus(); }
          else switchUser(e.target.value);
        },
      },
        !me() ? h('option', { value: '' }, 'Choose a person…') : null,
        S.users.map(u => h('option', { value: u.id }, u.name)),
        h('option', { value: '+' }, 'Add a person…')));
    }
  }
  if (S.meta) {
    foot.append(h('div', { class: 'status-row', 'data-testid': 'live-status', title: S.meta.db_path },
      h('span', { class: 'dot ' + (S.meta.demo ? '' : S.meta.live_ready ? 'ok' : 'bad') }),
      S.meta.demo ? 'Recorded demo' : S.meta.live_ready ? 'Live calls ready' : 'No API credentials'));
    if (S.meta.local_model) {
      foot.append(h('div', { class: 'status-row', title: 'Calls routed to the local model: ' + S.meta.local_model.points.join(', ') },
        h('span', { class: 'dot ok' }), `Local model: ${S.meta.local_model.name}`));
    }
    const p = S.meta.poller;
    if (p) {
      foot.append(h('label', { class: 'switch', title: `Checks groups that are due every ${Math.round(p.tick_seconds / 60) || 1} min. Makes real model calls.` },
        h('span', null, 'Background checking'),
        h('input', { type: 'checkbox', checked: p.enabled, 'data-testid': 'poller-toggle', onchange: async e => {
          try { S.meta.poller = await post('/api/poller', { enabled: e.target.checked }); }
          catch (err) { fail(err); }
          paint();
        } }),
        h('span', { class: 'track' })));
      if (p.enabled) {
        foot.append(h('div', { class: 'status-row small muted' },
          h('span', { class: 'dot ' + (p.running ? 'busy' : 'ok') }),
          p.running ? 'Checking now…' : p.last_run_at ? `Last sweep ${when(p.last_run_at)}` : 'First sweep starting…'));
      }
    }
  }
  nodes.push(foot);
  return nodes;
}
function svgMark() {
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg'); svg.setAttribute('viewBox', '0 0 16 16');
  const path = document.createElementNS(ns, 'path');
  path.setAttribute('d', 'M2.5 4h11M2.5 8h11M2.5 12h6.5'); path.setAttribute('stroke', '#fff');
  path.setAttribute('stroke-width', '1.8'); path.setAttribute('stroke-linecap', 'round'); path.setAttribute('fill', 'none');
  svg.append(path); return svg;
}

/* ---------- today ---------- */

function onboardingUser() {
  return h('div', { class: 'column' },
    h('div', { class: 'card hero', 'data-testid': 'onboard-user' },
      h('p', { class: 'eyebrow' }, 'Welcome'),
      h('h1', { class: 'title' }, 'Stay conversant with the groups you care about.'),
      h('p', { class: 'subtitle' }, 'Pick the circles you want to keep up with. The agent watches the news for each one, tells you plainly what happened, and answers whatever you ask. It never quizzes you.'),
      h('form', { onsubmit: async e => {
        e.preventDefault();
        const name = (S.drafts.firstUser || '').trim(); if (!name) return;
        try { await createUser(name); S.drafts.firstUser = ''; } catch (err) { fail(err); }
      } },
        h('div', { class: 'form-row' },
          h('label', { class: 'lbl', for: 'first-user' }, 'What should we call you?'),
          h('input', { id: 'first-user', class: 'field', autocomplete: 'given-name', 'data-keep': 'firstUser', 'data-testid': 'first-user-name', value: S.drafts.firstUser || '', oninput: e => { S.drafts.firstUser = e.target.value; } })),
        h('div', { class: 'form-actions' }, h('button', { class: 'btn primary', type: 'submit', 'data-testid': 'first-user-submit' }, 'Continue')))));
}

function groupForm(opts) {
  const d = S.drafts;
  const suggestions = ['Premier League', 'Startup & VC', 'Natural wine', 'NBA', 'AI research'];
  const submit = async e => {
    e.preventDefault();
    const name = (d.groupName || '').trim(); if (!name || S.ui.savingGroup) return;
    S.ui.savingGroup = true; paint();
    try {
      const group = await createGroup({ name, description: d.groupDesc || '', poll_interval_minutes: d.groupInterval || null });
      d.groupName = ''; d.groupDesc = ''; d.groupInterval = '';
      S.ui.addingGroup = false;
      toast(`Now following ${group.name}.`);
      if (opts && opts.after) opts.after(group);
    } catch (err) { fail(err); }
    finally { S.ui.savingGroup = false; paint(); }
  };
  return h('form', { onsubmit: submit, 'data-testid': 'group-form' },
    h('div', { class: 'form-row' },
      h('label', { class: 'lbl', for: 'group-name' }, 'Group'),
      h('input', { id: 'group-name', class: 'field', placeholder: 'e.g. Premier League', maxlength: 80, 'data-keep': 'groupName', 'data-testid': 'group-name', value: d.groupName || '', oninput: e => { d.groupName = e.target.value; } })),
    opts && opts.suggest ? h('div', { class: 'chips', style: 'margin-top:10px' },
      suggestions.map(s => h('button', { class: 'chip', type: 'button', onclick: () => { d.groupName = s; paint(); } }, s))) : null,
    h('div', { class: 'form-row' },
      h('label', { class: 'lbl', for: 'group-desc' }, 'What it covers ', h('span', { class: 'muted', style: 'font-weight:400' }, '(optional, helps the search)')),
      h('input', { id: 'group-desc', class: 'field', placeholder: 'e.g. transfers, results, managers, the title race', maxlength: 400, 'data-keep': 'groupDesc', 'data-testid': 'group-desc', value: d.groupDesc || '', oninput: e => { d.groupDesc = e.target.value; } })),
    h('div', { class: 'form-row' },
      h('label', { class: 'lbl', for: 'group-interval' }, 'Check for news'),
      h('select', { id: 'group-interval', style: 'width:auto', value: d.groupInterval || '', onchange: e => { d.groupInterval = e.target.value; } },
        h('option', { value: '' }, 'Every 6 hours (default)'),
        h('option', { value: '60' }, 'Hourly'),
        h('option', { value: '180' }, 'Every 3 hours'),
        h('option', { value: '720' }, 'Every 12 hours'),
        h('option', { value: '1440' }, 'Daily'))),
    h('div', { class: 'form-actions' },
      h('button', { class: 'btn primary', type: 'submit', disabled: S.ui.savingGroup, 'data-testid': 'group-submit' }, 'Follow this group'),
      opts && opts.cancel ? h('button', { class: 'btn quiet', type: 'button', onclick: opts.cancel }, 'Cancel') : null));
}

function onboardingGroup() {
  return h('div', { class: 'column' },
    h('div', { class: 'card hero', 'data-testid': 'onboard-group' },
      h('p', { class: 'eyebrow' }, 'First group'),
      h('h1', { class: 'title' }, `What do you want to keep up with, ${firstName()}?`),
      h('p', { class: 'subtitle' }, 'A group is any circle whose conversations you want to follow. Nothing is assumed about what you already know; the first briefings explain terms as they go.'),
      groupForm({ suggest: true })));
}

const firstName = () => { const u = me(); return u ? u.name.split(' ')[0] : ''; };

function masthead() {
  const hour = new Date().getHours();
  const part = hour < 5 ? 'evening' : hour < 12 ? 'morning' : hour < 18 ? 'afternoon' : 'evening';
  return h('header', { class: 'masthead' },
    h('p', { class: 'eyebrow' }, new Date().toLocaleDateString(undefined, { weekday: 'long', day: 'numeric', month: 'long' })),
    h('h1', { class: 'title', 'data-testid': 'greeting' }, `Good ${part}, ${firstName()}.`));
}

function checkButton(g, label) {
  const busy = !!S.polling[g.id];
  return h('button', {
    class: 'btn small', type: 'button', disabled: busy || !S.meta || !S.meta.live_ready,
    'data-testid': `check-${g.id}`, title: 'Search the news for this group now. Takes up to a minute.',
    onclick: () => checkGroup(g.id),
  }, busy ? [spinner(), 'Looking…'] : (label || 'Check now'));
}

function groupStrip() {
  return h('div', { class: 'group-strip', 'data-testid': 'group-strip' }, S.groups.map(g =>
    h('div', { class: 'group-line' },
      h('a', { class: 'name', href: `#/groups/${g.id}` }, g.name),
      g.new ? h('span', { class: 'badge new', title: 'Happened, and not shown to you yet' }, `${g.new} new`) : null,
      h('span', { class: 'meta' }, g.last_polled_at ? `checked ${when(g.last_polled_at)}` : 'never checked'),
      h('span', { class: 'spacer' }),
      g.untold > 0 && !g.open_exchange_id
        ? h('button', { class: 'btn small', type: 'button', disabled: !S.meta || !S.meta.live_ready, 'data-testid': `tell-${g.id}`,
            title: 'A story here has not been told to you yet',
            onclick: () => startBrief({ group_id: g.id, group_name: g.name, reason: '' }) }, g.untold === 1 ? 'Tell me the story' : `Tell me a story (${g.untold})`)
        : null,
      checkButton(g))));
}

function idleBlock() {
  const t = S.today;
  const live = S.meta && S.meta.live_ready;
  const due = S.groups.filter(isDue);
  const waiting = S.groups.reduce((n, g) => n + (g.new || 0), 0);
  const anyBusy = S.checkingAll || Object.keys(S.polling).length > 0;
  const background = S.meta && S.meta.poller && S.meta.poller.enabled;
  const out = [];

  if (S.meta && S.meta.demo) {
    out.push(h('div', { class: 'card notice', 'data-testid': 'demo-notice' },
      h('strong', null, 'You are looking at a recorded session.'), ' ',
      'Everything here came from real runs: live web search, real model calls, real costs. The demo makes no new calls, so the buttons that would search or write are switched off. ',
      h('a', { href: '#/groups' }, 'Read what was told'), ' or open the ', h('a', { href: '#/console/traces' }, 'Console'), ' to see every call behind it.'));
  } else if (!live) {
    out.push(h('div', { class: 'card notice', 'data-testid': 'no-credentials' },
      h('strong', null, 'Live calls are switched off.'), ' ',
      'No Anthropic credentials were found when the server started, so it cannot search for news or write briefings. Your groups, goals, history and the whole console still work.'));
  }

  for (const x of t.openThreads) {
    out.push(h('div', { class: 'card notice', 'data-testid': 'open-thread' },
      h('p', { class: 'eyebrow' }, `${x.group_name} · left open ${when(x.raised_at)}`),
      h('p', { style: 'font:600 18px/1.3 var(--serif);margin-top:6px' }, x.topic || 'A briefing you have not finished'),
      h('div', { class: 'form-actions', style: 'margin-top:12px' },
        h('button', { class: 'btn', type: 'button', onclick: () => { showExchange(x, null); paint(); } }, 'Pick it back up'))));
  }

  const needCheck = live && due.length > 0 && !background && waiting === 0;
  let lede;
  if (needCheck) {
    lede = due.length === S.groups.length && due.every(g => !g.last_polled_at)
      ? 'Nothing has been checked yet. Look for news first, then open a session to hear it.'
      : `${plural(due.length, 'group')} ${due.length === 1 ? 'is' : 'are'} due a fresh look. Check for news, then open a session to hear it.`;
  } else if (waiting) {
    lede = `${plural(waiting, 'thing')} happened that you have not heard yet.`;
  } else {
    lede = 'Open a session to hear what is worth knowing since you were last here.';
  }
  const sessionBtn = h('button', { class: 'btn ' + (needCheck ? '' : 'primary'), type: 'button', disabled: !live, 'data-testid': 'open-session', onclick: openSession }, 'Catch me up');
  const checkBtn = h('button', { class: 'btn ' + (needCheck ? 'primary' : ''), type: 'button', disabled: !live || anyBusy, 'data-testid': 'check-due', onclick: checkDue },
    S.checkingAll ? [spinner(), 'Looking for news…'] : 'Check for news');
  out.push(h('div', { class: 'card hero', 'data-testid': 'today-idle' },
    h('p', { class: 'lede' }, lede),
    h('div', { class: 'form-actions' }, needCheck ? [checkBtn, sessionBtn] : [sessionBtn, due.length ? checkBtn : null]),
    anyBusy ? h('p', { class: 'small muted', style: 'margin-top:12px' }, 'Searching and judging what matters. This usually takes under a minute per group.') : null));

  out.push(h('h2', { class: 'section-title' }, 'Your groups'), groupStrip());
  return out;
}

function sessionBlock(s) {
  const out = [h('p', { class: 'eyebrow' }, 'This session'), h('p', { class: 'framing', 'data-testid': 'framing' }, s.framing || '')];
  if (s.events.length) {
    out.push(h('h2', { class: 'section-title' }, `While you were away (${s.events.length})`));
    out.push(h('div', { 'data-testid': 'session-events' }, s.events.map(e =>
      h('article', { class: 'event' },
        h('p', { class: 'eyebrow' }, [e.group_name, e.occurred_at ? dateLabel(e.occurred_at) : null].filter(Boolean).join(' · ')),
        h('h3', null, e.headline),
        e.detail ? h('p', { class: 'detail' }, e.detail) : null,
        e.why_it_matters ? h('p', { class: 'matters' }, h('strong', null, 'Why it matters: '), e.why_it_matters) : null,
        (e.source_name || safeUrl(e.source_url)) ? h('p', { class: 'source' }, 'Source: ', extLink(e.source_url, e.source_name) || e.source_name) : null))));
  }
  if (s.held_back) out.push(h('p', { class: 'small muted', style: 'margin-top:14px' }, `${plural(s.held_back, 'lower-value item')} held back for a later session.`));
  if (s.why) {
    out.push(h('details', { class: 'plain' }, h('summary', null, 'Why this session looks like this'), h('div', { class: 'body' }, s.why)));
  }
  return out;
}

function briefingBlock() {
  const t = S.today; const x = t.exchange; const topic = t.topic || {};
  if (t.briefState === 'loading') {
    return h('section', { class: 'briefing', 'data-testid': 'briefing-loading' },
      h('p', { class: 'eyebrow' }, topic.group_name || ''),
      h('div', { class: 'waiting' }, spinner(), 'Writing your briefing…'));
  }
  if (t.briefState === 'error') {
    return h('section', { class: 'briefing' },
      h('p', { class: 'eyebrow' }, topic.group_name || ''),
      h('p', { class: 'muted', style: 'margin-top:10px' }, t.briefError || 'The briefing could not be written.'),
      h('div', { class: 'form-actions' }, h('button', { class: 'btn', type: 'button', onclick: () => startBrief(topic) }, 'Try again')));
  }
  if (t.briefState === 'empty') {
    return h('section', { class: 'briefing', 'data-testid': 'briefing-empty' },
      h('p', { class: 'eyebrow' }, topic.group_name || ''),
      h('p', { class: 'muted', style: 'margin-top:10px' }, 'Nothing new to tell you here. Every story so far has already been told once.'));
  }
  if (!x) return null;

  const out = h('section', { class: 'briefing', 'data-testid': 'briefing' },
    h('p', { class: 'eyebrow' }, x.group_name),
    x.topic ? h('h2', { class: 'topic', 'data-testid': 'briefing-topic' }, x.topic) : null,
    topic.reason ? h('p', { class: 'why-now' }, 'Why now: ', topic.reason) : null,
    h('div', { class: 'briefing-body', 'data-reading-target': '', 'data-testid': 'briefing-body' }, paras(x.briefing)),
    x.source && (x.source.source_name || safeUrl(x.source.source_url))
      ? h('p', { class: 'source' }, 'Source: ', extLink(x.source.source_url, x.source.source_name) || x.source.source_name,
          x.source.occurred_at ? ` · ${dateLabel(x.source.occurred_at)}` : '')
      : null);

  const thread = h('div', { class: 'thread', 'data-testid': 'thread' });
  for (const turn of x.turns) thread.append(turnNode(turn));
  if (t.asking) {
    thread.append(h('div', { class: 'turn user' }, t.pendingQuestion));
    thread.append(h('div', { class: 'turn pending', 'data-testid': 'answer-pending' }, spinner(), 'Finding you a reliable answer…'));
  }
  if (thread.childNodes.length) out.append(thread);

  if (!x.closed) {
    const busy = t.asking || t.closing;
    out.append(h('form', { class: 'ask', 'data-testid': 'ask-form', onsubmit: e => { e.preventDefault(); askQuestion(); } },
      h('div', { class: 'ask-row' },
        h('textarea', {
          class: 'field', rows: 1, maxlength: 2000, placeholder: 'Ask anything about this…', 'aria-label': 'Ask a question',
          'data-keep': 'ask', 'data-testid': 'ask-input', value: S.drafts.ask || '',
          oninput: e => { S.drafts.ask = e.target.value; e.target.style.height = 'auto'; e.target.style.height = Math.min(e.target.scrollHeight + 2, 200) + 'px'; },
          onkeydown: e => { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); askQuestion(); } },
        }),
        h('button', { class: 'btn primary', type: 'submit', disabled: busy, 'data-testid': 'ask-submit' }, 'Ask')),
      h('div', { class: 'ask-hint' },
        h('span', null, 'Ask as much or as little as you like. Nothing here is a test.'),
        h('button', { class: 'btn quiet small', type: 'button', disabled: busy, 'data-testid': 'close-thread', onclick: closeThread },
          t.closing ? [spinner(), 'Wrapping up…'] : 'That’s all for now'))));
  } else {
    const more = S.groups.filter(g => g.untold > 0);
    out.append(h('div', { class: 'closed-note', 'data-testid': 'thread-closed' },
      h('p', null, more.length ? 'That’s this one done. There is more when you want it.' : 'That’s this one done, and you are up to date.'),
      h('div', { class: 'form-actions' },
        more.map(g => h('button', { class: 'btn', type: 'button', 'data-testid': `next-${g.id}`, onclick: () => startBrief({ group_id: g.id, group_name: g.name, reason: '' }) }, `Next in ${g.name}`)),
        h('button', { class: 'btn quiet', type: 'button', 'data-testid': 'back-today', onclick: backToToday }, 'Back to today'))));
  }
  out.append(h('span', { 'data-thread-end': '' }));
  return out;
}

function turnNode(turn) {
  if (turn.speaker === 'user') return h('div', { class: 'turn user' }, turn.text);
  const src = safeUrl(turn.source_url);
  return h('div', { class: 'turn system' }, paras(turn.text), src ? h('p', { class: 'source' }, 'Source: ', extLink(src)) : null);
}

function scenesBlock() {
  const calls = S.today.calls;
  if (!calls.length) return null;
  const known = calls.filter(c => c.cost != null);
  const cost = known.reduce((n, c) => n + c.cost, 0);
  return h('details', { class: 'scenes', 'data-testid': 'scenes' },
    h('summary', null, `Behind the scenes: ${plural(calls.length, 'model call')}`, known.length ? ` · ${money(cost)}` : ''),
    h('ol', { class: 'calls' }, calls.map(c =>
      h('li', null, h('a', { href: `#/console/traces/${c.id}` },
        h('span', { class: 'pt' }, c.point),
        h('span', { class: 'grow' }, c.model),
        c.error ? h('span', { class: 'err' }, 'failed') : null,
        h('span', { class: 'muted' }, secs(c.latency_ms)),
        h('span', { class: 'muted' }, money(c.cost)))))));
}

function todayNode() {
  if (!S.users.length || !me()) return S.users.length ? choosePerson() : onboardingUser();
  if (!S.groups.length) return onboardingGroup();
  const t = S.today;
  const col = h('div', { class: 'column', 'data-testid': 'today' }, masthead());
  if (t.phase === 'idle') add(col, [idleBlock()]);
  else if (t.phase === 'opening') col.append(h('div', { class: 'waiting', 'data-testid': 'session-opening' }, spinner(), 'Deciding what is worth your time…'));
  else {
    if (t.session) add(col, [sessionBlock(t.session)]);
    const b = briefingBlock();
    if (b) col.append(b);
    const threadLive = t.exchange && !t.exchange.closed;
    if (!threadLive && t.briefState !== 'loading' && !(t.exchange && t.exchange.closed)) {
      // The session may surface a story without raising it as a topic. The
      // reader can still ask to be told: pull-only, and never told twice.
      const more = t.briefState === 'none' ? S.groups.filter(g => g.untold > 0) : [];
      const live = S.meta && S.meta.live_ready;
      col.append(h('div', { class: 'form-actions', style: 'margin-top:28px' },
        more.map(g => h('button', { class: 'btn primary', type: 'button', disabled: !live, 'data-testid': `tell-${g.id}`,
          onclick: () => startBrief({ group_id: g.id, group_name: g.name, reason: '' }) }, S.groups.length > 1 ? `Tell me more: ${g.name}` : 'Tell me more')),
        h('button', { class: 'btn quiet', type: 'button', 'data-testid': 'back-today', onclick: backToToday }, 'Back to today')));
    }
  }
  const scenes = scenesBlock();
  if (scenes) col.append(scenes);
  return col;
}

function choosePerson() {
  return h('div', { class: 'column' }, h('div', { class: 'card hero' },
    h('p', { class: 'eyebrow' }, 'Welcome back'),
    h('h1', { class: 'title' }, 'Who is reading?'),
    h('div', { class: 'form-actions' }, S.users.map(u => h('button', { class: 'btn', type: 'button', onclick: () => switchUser(u.id) }, u.name)))));
}

/* ---------- groups ---------- */

function groupFacts(g) {
  return h('div', { class: 'facts' },
    h('span', null, g.last_polled_at ? `Checked ${when(g.last_polled_at)}` : 'Never checked'),
    h('span', null, `Checks ${intervalLabel(g.poll_interval_minutes)}`),
    h('span', null, g.last_engaged ? `You last asked something ${when(g.last_engaged)}` : 'You have not asked anything here yet'));
}

function groupsNode() {
  if (!me()) return todayNode();
  const col = h('div', { class: 'column', 'data-testid': 'groups' },
    h('header', { class: 'masthead' },
      h('p', { class: 'eyebrow' }, 'Groups'),
      h('h1', { class: 'title' }, 'The circles you keep up with'),
      h('p', { class: 'subtitle' }, 'Each group is watched on its own schedule. “New” counts what has happened that you have not been shown yet.')));
  for (const g of S.groups) {
    col.append(h('div', { class: 'card group-card', 'data-testid': 'group-card' },
      h('div', { class: 'head' },
        h('h3', null, h('a', { href: `#/groups/${g.id}` }, g.name)),
        g.new ? h('span', { class: 'badge new' }, `${g.new} new`) : null,
        g.goal ? h('span', { class: 'badge' }, `Goal · ${dateLabel(g.goal.deadline)}`) : null),
      g.description ? h('p', { class: 'desc' }, g.description) : null,
      groupFacts(g),
      h('div', { class: 'actions' },
        checkButton(g),
        h('a', { class: 'btn small quiet', href: `#/groups/${g.id}` }, 'Open'))));
  }
  if (S.ui.addingGroup || !S.groups.length) {
    col.append(h('div', { class: 'card', style: 'margin-top:20px' },
      h('h3', { style: 'font:600 18px/1.3 var(--serif)' }, 'Follow another group'),
      groupForm({ cancel: S.groups.length ? () => { S.ui.addingGroup = false; paint(); } : null, suggest: !S.groups.length })));
  } else {
    col.append(h('div', { class: 'form-actions' },
      h('button', { class: 'btn', type: 'button', 'data-testid': 'add-group', onclick: () => { S.ui.addingGroup = true; paint(); const f = document.getElementById('group-name'); if (f) f.focus(); } }, 'Follow another group')));
  }
  return col;
}

function goalSection(g) {
  const active = g.goal;
  const past = (g.goals || []).filter(x => x.status !== 'active');
  const out = [h('h2', { class: 'section-title' }, 'Goal')];
  if (active) {
    const closeGoal = expired => async () => {
      try { await post(`/api/goals/${active.id}/close`, { expired }); toast(expired ? 'Goal let go.' : 'Goal marked done.'); await loadGroups(); refreshPage(); }
      catch (e) { fail(e); }
    };
    out.push(h('div', { class: 'card', 'data-testid': 'active-goal' },
      h('p', { style: 'font:600 18px/1.35 var(--serif)' }, active.description),
      h('p', { class: 'small muted', style: 'margin-top:4px' }, `By ${dateLabel(active.deadline)}`),
      h('div', { class: 'form-actions', style: 'margin-top:12px' },
        h('button', { class: 'btn small', type: 'button', 'data-testid': 'goal-done', onclick: closeGoal(false) }, 'Mark done'),
        h('button', { class: 'btn small quiet', type: 'button', onclick: closeGoal(true) }, 'Let it lapse')),
      h('p', { class: 'small muted', style: 'margin-top:10px' }, 'Closing a goal changes nothing else. The group keeps being watched.')));
  } else {
    const d = S.drafts;
    const today = new Date(); const min = new Date(today.getTime() + 86400000).toISOString().slice(0, 10);
    out.push(h('form', { class: 'card', 'data-testid': 'goal-form', onsubmit: async e => {
      e.preventDefault();
      try {
        await post(`/api/groups/${g.id}/goals`, { description: d.goalDesc || '', deadline: d.goalDate || '' });
        d.goalDesc = ''; d.goalDate = ''; toast('Goal added.'); await loadGroups(); refreshPage();
      } catch (err) { fail(err); }
    } },
      h('p', { class: 'muted small' }, 'Preparing for something? A goal makes this group a priority until the date passes.'),
      h('div', { class: 'form-row' },
        h('label', { class: 'lbl', for: 'goal-desc' }, 'What you are preparing for'),
        h('input', { id: 'goal-desc', class: 'field', placeholder: 'e.g. dinner with the investors on Thursday', maxlength: 400, 'data-keep': 'goalDesc', 'data-testid': 'goal-desc', value: d.goalDesc || '', oninput: e => { d.goalDesc = e.target.value; } })),
      h('div', { class: 'form-row' },
        h('label', { class: 'lbl', for: 'goal-date' }, 'By when'),
        h('input', { id: 'goal-date', class: 'field', type: 'date', min, style: 'width:auto', 'data-keep': 'goalDate', 'data-testid': 'goal-date', value: d.goalDate || '', oninput: e => { d.goalDate = e.target.value; } })),
      h('div', { class: 'form-actions' }, h('button', { class: 'btn', type: 'submit', 'data-testid': 'goal-submit' }, 'Add goal'))));
  }
  if (past.length) {
    out.push(h('details', { class: 'plain' },
      h('summary', null, `${plural(past.length, 'earlier goal')}`),
      h('div', { class: 'body' }, past.map(p => h('p', null, `${p.description} · ${p.status} · ${dateLabel(p.deadline)}`)))));
  }
  return out;
}

function groupNode() {
  if (!me()) return todayNode();
  const g = S.page.group;
  if (S.page.error) return h('div', { class: 'column' }, h('p', { class: 'muted' }, S.page.error), h('p', { style: 'margin-top:12px' }, h('a', { href: '#/groups' }, '← All groups')));
  if (!g) return h('div', { class: 'column' }, h('div', { class: 'waiting' }, spinner(), 'Loading…'));
  const live = S.meta && S.meta.live_ready;
  const col = h('div', { class: 'column', 'data-testid': 'group-detail' },
    h('header', { class: 'masthead' },
      h('p', { class: 'eyebrow' }, h('a', { href: '#/groups', style: 'text-decoration:none' }, '← Groups')),
      h('h1', { class: 'title' }, g.name),
      g.description ? h('p', { class: 'subtitle' }, g.description) : null),
    h('div', { class: 'inline' },
      g.new ? h('span', { class: 'badge new' }, `${g.new} new`) : null,
      checkButton(g),
      g.open_exchange_id
        ? h('button', { class: 'btn small primary', type: 'button', onclick: async () => {
            try { const x = await get(`/api/exchanges/${g.open_exchange_id}`); S.today = Object.assign(freshToday(), { calls: S.today.calls }); showExchange(x, null); location.hash = '#/'; }
            catch (e) { fail(e); } } }, 'Pick up the open thread')
        : g.untold > 0
          ? h('button', { class: 'btn small primary', type: 'button', disabled: !live, 'data-testid': 'brief-next', onclick: () => {
              S.today = Object.assign(freshToday(), { calls: S.today.calls });
              location.hash = '#/';
              startBrief({ group_id: g.id, group_name: g.name, reason: '' });
            } }, g.untold === 1 ? 'Tell me the story' : `Tell me the next story (${g.untold})`)
          : null,
      h('a', { class: 'btn small quiet', href: `#/console/ledger/${g.id}` }, 'Builder view')),
    groupFacts(g),
    goalSection(g),
    h('h2', { class: 'section-title' }, 'What you have been told'));

  if (!g.history.length) {
    col.append(h('p', { class: 'muted' }, g.last_polled_at
      ? 'Nothing yet. When something worth knowing turns up, open a session and it will be told here.'
      : 'Nothing yet. This group has not been checked for news.'));
  }
  for (const x of g.history) {
    const item = h('article', { class: 'history-item', 'data-testid': 'history-item' },
      h('p', { class: 'when' }, dateLabel(x.raised_at, true), x.closed ? '' : ' · still open'),
      x.topic ? h('h3', null, x.topic) : null,
      h('div', { class: 'text' }, paras(x.briefing)),
      x.source && safeUrl(x.source.source_url) ? h('p', { class: 'source small muted', style: 'margin-top:8px' }, 'Source: ', extLink(x.source.source_url, x.source.source_name)) : null);
    if (x.turns.length) {
      const asked = x.turns.filter(t => t.speaker === 'user').length;
      item.append(h('details', { class: 'plain' },
        h('summary', null, `You asked ${plural(asked, 'question')}`),
        h('div', { class: 'thread' }, x.turns.map(turnNode))));
    }
    col.append(item);
  }
  return col;
}

/* ---------- console ---------- */

const TABS = [['ledger', 'Ledger'], ['traces', 'Traces'], ['stats', 'Stats'], ['models', 'Models'], ['prompts', 'Prompts'], ['runs', 'Eval runs']];

function table(headers, rows, opts) {
  const o = opts || {};
  if (!rows.length) return h('div', { class: 'table-wrap' }, h('div', { class: 'empty' }, o.empty || 'Nothing here yet.'));
  return h('div', { class: 'table-wrap' }, h('table', { 'data-testid': o.testid || null },
    h('thead', null, h('tr', null, headers.map(c => h('th', { class: c.num ? 'num' : null }, c.label)))),
    h('tbody', null, rows.map(row => {
      const tr = h('tr', { class: [row.href ? 'link' : '', row.total ? 'total' : ''].join(' ').trim() || null }, headers.map((c, i) => {
        const cell = row.cells[i];
        return h('td', { class: [c.num ? 'num' : '', c.nowrap ? 'nowrap' : '', c.clip ? 'clip' : ''].join(' ').trim() || null, title: c.clip && typeof cell === 'string' ? cell : null }, cell == null ? '-' : cell);
      }));
      if (row.href) tr.addEventListener('click', e => { if (!e.target.closest('a')) location.hash = row.href; });
      return tr;
    }))));
}
const col = (label, flags) => Object.assign({ label }, flags || {});
const stateTag = s => h('span', { class: `state ${s}` }, s);
const terms = list => (list && list.length ? list.join(', ') : '');

function consoleNode() {
  const tab = S.route.args[0] || 'ledger';
  const wrap = h('div', { class: 'wide', 'data-testid': 'console' },
    h('header', null,
      h('p', { class: 'eyebrow' }, 'Builder view'),
      h('h1', { class: 'title' }, 'Console'),
      h('p', { class: 'subtitle' }, 'What the system believes and what every model call did. None of this is ever shown on the reader pages.')),
    h('nav', { class: 'tabs', 'aria-label': 'Console sections' }, TABS.map(([id, label]) =>
      h('a', { href: `#/console/${id}`, 'aria-current': id === tab ? 'page' : null, 'data-testid': `tab-${id}` }, label))));
  if (S.page.error) { wrap.append(h('div', { class: 'banner' }, S.page.error)); return wrap; }
  const body = { ledger: ledgerTab, traces: tracesTab, stats: statsTab, models: modelsTab, prompts: promptsTab, runs: runsTab }[tab];
  add(wrap, [body ? body() : h('p', { class: 'muted' }, 'No such section.')]);
  return wrap;
}

const loadingRow = () => h('div', { class: 'waiting' }, spinner(), 'Loading…');

function ledgerTab() {
  if (!me()) return h('div', { class: 'banner' }, 'Choose a person to see their ledger.');
  if (!S.groups.length) return h('div', { class: 'banner' }, 'No groups yet. The ledger fills in as briefings are read and questions are asked.');
  const d = S.page.ledger;
  const gid = S.route.args[1] || S.groups[0].id;
  const out = [h('div', { class: 'toolbar' },
    h('select', { 'aria-label': 'Group', 'data-testid': 'ledger-group', value: gid, onchange: e => { location.hash = `#/console/ledger/${e.target.value}`; } },
      S.groups.map(g => h('option', { value: g.id }, g.name))))];
  if (!d) { out.push(loadingRow()); return out; }

  const rp = d.reading_pattern;
  out.push(h('div', { class: 'kpis' },
    h('div', { class: 'kpi', 'data-testid': 'kpi-band' }, h('div', { class: 'k' }, 'Derived band'), h('div', { class: 'v' }, d.proficiency), h('div', { class: 'n' }, 'Computed on read. A prior for pitch, never shown to the reader.')),
    h('div', { class: 'kpi' }, h('div', { class: 'k' }, 'Terms not re-glossed'), h('div', { class: 'v' }, `${d.known} / ${d.total}`), h('div', { class: 'n' }, `States: ${d.known_states.join(', ')}`)),
    h('div', { class: 'kpi' }, h('div', { class: 'k' }, 'Unshown events'), h('div', { class: 'v' }, d.new), h('div', { class: 'n' }, 'Material, detected, not yet surfaced.')),
    h('div', { class: 'kpi' }, h('div', { class: 'k' }, 'Skips resolved'), h('div', { class: 'v' }, `${rp.resolved}`), h('div', { class: 'n' }, `${rp.informed_skips} informed · ${rp.lazy_skips} lazy · ${rp.unresolved_skips} unresolved · p(informed) ${rp.p_informed}`))));

  const filter = S.ui.ledgerState || '';
  out.push(h('h2', { class: 'section-title' }, 'Concept ledger'));
  out.push(h('div', { class: 'chips', style: 'margin-bottom:12px' },
    h('button', { class: 'chip', type: 'button', 'aria-pressed': String(!filter), onclick: () => { S.ui.ledgerState = ''; paint(); } }, `All ${d.total}`),
    d.state_counts.map(s => h('button', { class: 'chip', type: 'button', title: s.gloss, 'aria-pressed': String(filter === s.state), onclick: () => { S.ui.ledgerState = s.state; paint(); } }, `${s.state} ${s.count}`))));
  const rows = d.ledger.filter(c => !filter || c.state === filter);
  out.push(table(
    [col('Term'), col('State'), col('Subdomain'), col('Seen', { num: true }), col('Used', { num: true }), col('Missed', { num: true }), col('Read', { num: true }), col('In band'), col('Last seen', { nowrap: true }), col('Evidence')],
    rows.map(c => ({ cells: [c.term, stateTag(c.state), c.subdomain || '', c.exposure_count, c.correct_uses, c.misunderstandings, c.read_explanations, c.counts_toward_band ? 'yes' : '', when(c.last_seen_at), c.evidence || ''] })),
    { testid: 'ledger-table', empty: filter ? `No terms in state “${filter}”.` : 'Empty. A new group starts with an empty ledger by design: nothing is assumed, and terms are explained inline.' }));
  out.push(h('p', { class: 'small muted', style: 'margin-top:8px' }, 'Seen = times the term was used in front of them. Exposure is counted but never promotes a term: measured against ground truth, silence was anti-predictive.'));

  if (d.subdomains.length) {
    out.push(h('h2', { class: 'section-title' }, 'Familiarity by subdomain'));
    out.push(table([col('Subdomain'), col('Band'), col('Known', { num: true }), col('Attested', { num: true })],
      d.subdomains.map(s => ({ cells: [s.subdomain, s.band, s.known, s.attested] }))));
  }

  out.push(h('h2', { class: 'section-title' }, 'Briefings and what each thread revealed'));
  out.push(table(
    [col('When', { nowrap: true }), col('Topic'), col('Asked', { num: true }), col('Read'), col('Attention', { num: true }), col('Skip'), col('Understood'), col('Asked about'), col('Missed'), col('Glossed')],
    d.exchanges.map(x => ({ cells: [
      dateLabel(x.raised_at, true) + (x.closed_at ? '' : ' (open)'), x.topic || '', x.user_turns,
      x.read_quality ? `${x.read_quality}${x.reading_source && x.reading_source !== 'observed' ? ` (${x.reading_source})` : ''}` : '',
      x.attention == null ? '' : x.attention, x.skip_kind || '', terms(x.understood), terms(x.asked_about), terms(x.not_understood), terms(x.explained_terms)] })),
    { empty: 'No briefings yet.' }));

  out.push(h('h2', { class: 'section-title' }, 'Events the monitor recorded'));
  out.push(table(
    [col('When', { nowrap: true }), col('Headline'), col('Material'), col('Score', { num: true }), col('Shown'), col('Told'), col('Reasoning', { clip: true })],
    d.events.map(e => ({ cells: [
      dateLabel(e.occurred_at), safeUrl(e.source_url) ? extLink(e.source_url, e.headline) : e.headline,
      e.is_material == null ? 'unjudged' : e.is_material ? 'yes' : 'no',
      e.materiality_score == null ? '' : Math.round(e.materiality_score), e.surfaced ? 'yes' : '', e.told ? 'yes' : '', e.materiality_reason || ''] })),
    { empty: 'No events recorded. Check this group for news first.' }));
  return out;
}

function tracesTab() {
  const arg = S.route.args[1] || '';
  if (arg && !arg.startsWith('run:')) return traceDetail();
  const d = S.page.traces;
  const f = S.ui.traceFilter || (S.ui.traceFilter = { point: '', all: false });
  const runId = arg.startsWith('run:') ? arg.slice(4) : '';
  const out = [h('div', { class: 'toolbar' },
    h('select', { 'aria-label': 'Judgment point', 'data-testid': 'trace-point', value: f.point, onchange: e => { f.point = e.target.value; refreshPage(); } },
      h('option', { value: '' }, 'All calls'), ((d && d.points) || []).map(p => h('option', { value: p }, p))),
    runId ? h('span', { class: 'badge' }, `Run ${runId}`, ' ', h('a', { href: '#/console/traces' }, 'clear'))
      : h('select', { 'aria-label': 'Scope', value: f.all ? '1' : '', onchange: e => { f.all = e.target.value === '1'; refreshPage(); } },
          h('option', { value: '' }, me() ? `${me().name} + system calls` : 'System calls only'),
          h('option', { value: '1' }, 'All users (includes eval personas)')),
    h('span', { class: 'grow' }),
    h('button', { class: 'btn small', type: 'button', onclick: refreshPage }, 'Refresh'))];
  if (!d) { out.push(loadingRow()); return out; }
  out.push(h('p', { class: 'small muted', style: 'margin-bottom:10px' }, `${plural(d.rows.length, 'call')} · scope: ${d.scope}`));
  out.push(table(
    [col('When', { nowrap: true }), col('Call'), col('Model', { nowrap: true }), col('Prompt'), col('Time', { num: true }), col('In', { num: true }), col('Out', { num: true }), col('Cost', { num: true }), col('Verdict / reasoning', { clip: true })],
    d.rows.map(r => ({ href: `#/console/traces/${r.id}`, cells: [
      stamp(r.created_at), h('span', { class: 'pt-chip' }, r.point), r.model, r.prompt_version, secs(r.latency_ms), num(r.input_tokens), num(r.output_tokens), money(r.cost),
      r.error ? h('span', { class: 'err' }, 'ERROR ' + r.error) : (r.reasoning || r.verdict_preview || '')] })),
    { testid: 'traces-table', empty: 'No calls recorded in this scope yet. Every judgment call writes one row here.' }));
  return out;
}

function traceDetail() {
  const r = S.page.trace;
  if (!r) return loadingRow();
  const pretty = v => (v == null ? '(none)' : typeof v === 'string' ? v : JSON.stringify(v, null, 2));
  return [
    h('p', { style: 'margin-bottom:16px' }, h('a', { href: '#/console/traces' }, '← All calls')),
    h('div', { class: 'card', 'data-testid': 'trace-detail' },
      h('div', { class: 'inline', style: 'margin-bottom:14px' }, h('span', { class: 'pt-chip' }, r.point), h('span', { class: 'mono muted' }, r.id), r.error ? h('span', { class: 'err' }, 'failed') : null),
      h('dl', { class: 'kv' },
        h('dt', null, 'When'), h('dd', null, stamp(r.created_at)),
        h('dt', null, 'Model'), h('dd', null, `${r.model} · effort ${r.effort || '-'}`),
        h('dt', null, 'Prompt version'), h('dd', null, h('a', { href: `#/console/prompts/${r.point}` }, r.prompt_version)),
        h('dt', null, 'Time and tokens'), h('dd', null, `${secs(r.latency_ms)} · ${num(r.input_tokens || 0)} in / ${num(r.output_tokens || 0)} out`),
        h('dt', null, 'Cost'), h('dd', null, r.cost == null ? 'price unknown for this model' : '$' + r.cost.toFixed(6)),
        h('dt', null, 'User / group'), h('dd', { class: 'mono' }, `${r.user_id || '-'} / ${r.group_id || '-'}`),
        r.run_id ? [h('dt', null, 'Eval run'), h('dd', { class: 'mono' }, h('a', { href: `#/console/traces/run:${r.run_id}` }, r.run_id))] : null)),
    r.error ? [h('h2', { class: 'section-title' }, 'Error'), h('pre', { class: 'code err' }, r.error)] : null,
    h('h2', { class: 'section-title' }, 'Reasoning'), h('div', { class: 'card', style: 'font:16px/1.6 var(--serif)' }, paras(r.reasoning || '(none)')),
    h('div', { class: 'split' },
      h('div', null, h('h2', { class: 'section-title' }, 'Verdict'), h('pre', { class: 'code', 'data-testid': 'trace-verdict' }, pretty(r.verdict))),
      h('div', null, h('h2', { class: 'section-title' }, 'Input context'), h('pre', { class: 'code' }, pretty(r.input)))),
  ];
}

function statsTab() {
  const d = S.page.stats;
  const f = S.ui.traceFilter || (S.ui.traceFilter = { point: '', all: false });
  const out = [h('div', { class: 'toolbar' },
    h('select', { 'aria-label': 'Scope', value: f.all ? '1' : '', onchange: e => { f.all = e.target.value === '1'; refreshPage(); } },
      h('option', { value: '' }, me() ? `${me().name} + system calls` : 'System calls only'),
      h('option', { value: '1' }, 'All users (includes eval personas)')))];
  if (!d) { out.push(loadingRow()); return out; }
  const t = d.total;
  if (t) {
    out.push(h('div', { class: 'kpis' },
      h('div', { class: 'kpi' }, h('div', { class: 'k' }, 'Calls'), h('div', { class: 'v' }, num(t.calls)), h('div', { class: 'n' }, `${t.errors} failed`)),
      h('div', { class: 'kpi' }, h('div', { class: 'k' }, 'Cost'), h('div', { class: 'v' }, '$' + t.cost.toFixed(2) + (t.cost_known ? '' : ' +?')), h('div', { class: 'n' }, 'From logged token counts')),
      h('div', { class: 'kpi' }, h('div', { class: 'k' }, 'Mean latency'), h('div', { class: 'v' }, secs(t.mean_ms)), h('div', { class: 'n' }, `p95 ${secs(t.p95_ms)}`)),
      h('div', { class: 'kpi' }, h('div', { class: 'k' }, 'Tokens'), h('div', { class: 'v' }, num(t.input_tokens + t.output_tokens)), h('div', { class: 'n' }, `${num(t.input_tokens)} in · ${num(t.output_tokens)} out`))));
  }
  const line = r => ({ total: r.point === 'TOTAL', cells: [
    r.point === 'TOTAL' ? 'Total' : h('span', { class: 'pt-chip' }, r.point), num(r.calls), r.errors, r.calls ? (100 * r.errors / r.calls).toFixed(1) + '%' : '0%',
    secs(r.mean_ms), secs(r.p95_ms), num(r.input_tokens), num(r.output_tokens), '$' + r.cost.toFixed(4) + (r.cost_known ? '' : ' +?'), r.models.join(', ')] });
  out.push(h('p', { class: 'small muted', style: 'margin:14px 0 10px' }, `Scope: ${d.scope}`));
  out.push(table(
    [col('Call'), col('Calls', { num: true }), col('Errors', { num: true }), col('Err %', { num: true }), col('Mean', { num: true }), col('p95', { num: true }), col('In tokens', { num: true }), col('Out tokens', { num: true }), col('Cost', { num: true }), col('Models')],
    d.rows.map(line).concat(t ? [line(t)] : []), { testid: 'stats-table', empty: 'No calls recorded in this scope yet.' }));
  out.push(h('p', { class: 'small muted', style: 'margin-top:10px' },
    'Pricing per 1M tokens (in / out): ', d.pricing.map(p => `${p.model} $${p.input_per_million} / $${p.output_per_million}`).join(' · '),
    '. “+?” marks calls on a model with no price entry; their tokens are counted and their cost is not guessed.'));
  return out;
}

function modelsTab() {
  const d = S.page.models;
  if (!d) return loadingRow();
  return [
    table([col('Call'), col('Kind'), col('Model'), col('Effort'), col('Prompt'), col('Override')],
      d.rows.map(r => ({ cells: [h('span', { class: 'pt-chip' }, r.point), r.kind, r.local ? [r.model, ' ', h('span', { class: 'badge' }, 'local')] : r.model, r.effort,
        h('a', { href: `#/console/prompts/${r.point}` }, r.prompt_version), r.overrides.length ? r.overrides.join(' + ') : ''] })), { testid: 'models-table' }),
    h('p', { class: 'small muted', style: 'margin-top:10px' },
      `${d.judgment_points} scored judgment points + ${d.routed_calls - d.judgment_points} generative calls = ${d.routed_calls} routed calls. `,
      'Re-route without a code change by exporting MODEL_<POINT> or EFFORT_<POINT> before starting the server.'),
  ];
}

function promptsTab() {
  if (S.route.args[1]) {
    const p = S.page.prompt;
    if (!p) return loadingRow();
    return [
      h('p', { style: 'margin-bottom:16px' }, h('a', { href: '#/console/prompts' }, '← All prompts')),
      h('div', { class: 'inline', style: 'margin-bottom:12px' }, h('span', { class: 'pt-chip' }, p.point), h('span', { class: 'badge' }, `version ${p.version}`), h('span', { class: 'mono muted' }, p.file)),
      h('pre', { class: 'code', style: 'max-height:none', 'data-testid': 'prompt-text' }, p.text),
    ];
  }
  const d = S.page.prompts;
  if (!d) return loadingRow();
  return table([col('Call'), col('Version'), col('Size', { num: true }), col('Modified', { nowrap: true }), col('File')],
    d.map(r => ({ href: `#/console/prompts/${r.point}`, cells: [h('span', { class: 'pt-chip' }, r.point), r.version, r.chars ? num(r.chars) + ' chars' : '', r.modified ? dateLabel(r.modified, true) : '', r.file] })), { testid: 'prompts-table' });
}

function runsTab() {
  const d = S.page.runs;
  if (!d) return loadingRow();
  return table([col('Run', { nowrap: true }), col('Suite'), col('Started', { nowrap: true }), col('Finished', { nowrap: true }), col('Result'), col('Metrics', { clip: true })],
    d.map(r => ({ href: `#/console/traces/run:${r.id}`, cells: [
      h('span', { class: 'mono' }, r.id), r.suite, dateLabel(r.started_at, true), r.finished_at ? dateLabel(r.finished_at, true) : 'running',
      r.passed == null ? '' : h('span', { class: 'badge ' + (r.passed ? 'ok' : 'bad') }, r.passed ? 'pass' : 'fail'),
      Object.entries(r.metrics).map(([k, v]) => `${k}=${v}`).join(', ')] })),
    { testid: 'runs-table', empty: 'No eval runs recorded in this store. The persona harness writes one row per run.' });
}

/* ---------- router and paint ---------- */

function parseRoute() {
  const parts = (location.hash || '#/').replace(/^#\/?/, '').split('/').filter(Boolean).map(p => { try { return decodeURIComponent(p); } catch (e) { return p; } });
  if (!parts.length) return { name: 'today', args: [] };
  if (parts[0] === 'groups') return parts[1] ? { name: 'group', args: [parts[1]] } : { name: 'groups', args: [] };
  if (parts[0] === 'console') return { name: 'console', args: parts.slice(1) };
  return { name: 'today', args: [] };
}

let loadToken = 0;
async function loadPage() {
  const token = ++loadToken;
  const r = S.route; const page = {};
  try {
    if (r.name === 'today') {
      if (me()) { await loadGroups(); if (S.today.phase === 'idle') await loadOpenThreads(); }
    } else if (r.name === 'groups') {
      await loadGroups();
    } else if (r.name === 'group') {
      if (me()) page.group = await get(`/api/groups/${r.args[0]}`);
    } else if (r.name === 'console') {
      const tab = r.args[0] || 'ledger'; const arg = r.args[1] || '';
      const f = S.ui.traceFilter || { point: '', all: false };
      if (tab === 'ledger') {
        if (me()) { if (!S.groups.length) await loadGroups(); const gid = arg || (S.groups[0] && S.groups[0].id); if (gid) page.ledger = await get(`/api/console/groups/${gid}`); }
      } else if (tab === 'traces') {
        if (arg && !arg.startsWith('run:')) page.trace = await get(`/api/console/traces/${arg}`, { all_users: true });
        else page.traces = await get('/api/console/traces', { point: f.point, all_users: f.all, run_id: arg.startsWith('run:') ? arg.slice(4) : '', limit: 200 });
      } else if (tab === 'stats') page.stats = await get('/api/console/stats', { all_users: f.all });
      else if (tab === 'models') page.models = await get('/api/console/models');
      else if (tab === 'prompts') { if (arg) page.prompt = await get(`/api/console/prompts/${arg}`); else page.prompts = await get('/api/console/prompts'); }
      else if (tab === 'runs') page.runs = await get('/api/console/runs');
    }
  } catch (e) { page.error = e.message; }
  if (token !== loadToken) return;
  S.page = page;
  paint();
}
function refreshPage() { return loadPage(); }

function route() {
  const next = parseRoute();
  const changed = next.name !== S.route.name || next.args.join('/') !== S.route.args.join('/');
  S.route = next;
  if (changed) { S.page = {}; window.scrollTo(0, 0); }
  paint();
  loadPage();
}

function paint() {
  const active = document.activeElement;
  const keep = active && active.dataset ? active.dataset.keep : null;
  let sel = null;
  if (keep) { try { sel = [active.selectionStart, active.selectionEnd]; } catch (e) { sel = null; } }

  $rail.replaceChildren(...railNode());
  let node;
  if (!S.booted) node = h('div', { class: 'column' }, loadingRow());
  else if (S.route.name === 'groups') node = groupsNode();
  else if (S.route.name === 'group') node = groupNode();
  else if (S.route.name === 'console') node = consoleNode();
  else node = todayNode();
  $view.replaceChildren(node);
  const titles = { today: 'Today', groups: 'Groups', group: (S.page.group && S.page.group.name) || 'Group', console: 'Console' };
  document.title = `${titles[S.route.name] || 'Today'} · News Agent`;

  if (keep) {
    const el = document.querySelector(`[data-keep="${keep}"]`);
    if (el) { el.focus({ preventScroll: true }); if (sel && sel[0] != null) { try { el.setSelectionRange(sel[0], sel[1]); } catch (e) { /* date inputs */ } } }
  }
}

async function boot() {
  S.route = parseRoute();
  paint();
  try {
    await Promise.all([loadMeta(), loadUsers()]);
    await loadGroups();
  } catch (e) { fail(e); }
  S.booted = true;
  paint();
  loadPage();
  // Keep the status rail honest while the background poller works.
  setInterval(async () => {
    if (document.visibilityState !== 'visible') return;
    try {
      const before = S.meta && S.meta.poller ? S.meta.poller.last_run_at : null;
      await loadMeta();
      const p = S.meta.poller;
      if (p && p.enabled && p.last_run_at !== before) { await loadGroups(); }
      $rail.replaceChildren(...railNode());
    } catch (e) { /* server restarting */ }
  }, 20000);
}

window.addEventListener('hashchange', route);
boot();

})();
