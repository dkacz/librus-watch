"""Strona panelu admina: HTML, CSS i JS jako stałe (bez zewnętrznych zasobów i bez CDN).

Dane wstawiane są wyłącznie przez `textContent` i atrybuty SVG, nigdy przez `innerHTML`.
"""

INDEX_HTML = """<!doctype html>
<html lang="pl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>librus-watch · panel</title>
<link rel="stylesheet" href="/app.css">
</head>
<body>
<header><h1>librus-watch</h1><span id="updated" class="muted"></span></header>
<main>
  <section id="tiles" class="tiles" aria-label="Zdrowie usługi"></section>
  <section><h2>Dzieci</h2><table id="children"></table></section>
  <section><h2>Pozycje wykryte na dobę</h2><div id="c-findings" class="chart"></div></section>
  <section><h2>Oceny rodziców na dobę</h2><div id="c-ratings" class="chart"></div></section>
  <section><h2>Model: zapytania na dobę</h2><div id="c-llm" class="chart"></div></section>
  <section><h2>Sito: nowe teksty wg decyzji</h2><div id="c-sieve" class="chart"></div></section>
  <section><h2>Błędy modułów Librusa</h2><div id="c-errors" class="chart"></div></section>
  <section><h2>Moduły: ostatnie pobranie</h2><table id="modules"></table></section>
  <p class="muted">Panel pokazuje same liczby z migawki usługi; treści wiadomości i nazwisk w nim nie ma.</p>
</main>
<script src="/app.js"></script>
</body>
</html>
"""

APP_CSS = """
:root{--bg:#f6f7f9;--fg:#1a1d23;--muted:#6b7280;--card:#fff;--line:#e3e6eb;
--ok:#1a7f4b;--warn:#b7791f;--down:#c0392b;
--s1:#2f6fed;--s2:#e08a1e;--s3:#1a9e8f;--s4:#9b59b6;--s5:#c0392b;--s6:#6b7280;--s7:#8d6e63}
@media(prefers-color-scheme:dark){:root{--bg:#14161a;--fg:#e6e8ec;--muted:#9aa3b2;--card:#1d2026;
--line:#2c3038;--ok:#4cc38a;--warn:#e6b450;--down:#ff7a6b;
--s1:#6ea0ff;--s2:#f0a64a;--s3:#4cc3b5;--s4:#c08bd6;--s5:#ff7a6b;--s6:#9aa3b2;--s7:#b59a8f}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif}
header{display:flex;gap:16px;align-items:baseline;padding:16px;border-bottom:1px solid var(--line)}
h1{font-size:18px;margin:0}h2{font-size:14px;margin:0 0 8px;color:var(--muted);font-weight:600}
main{max-width:980px;margin:0 auto;padding:16px}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;margin-bottom:14px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;
background:none;border:0;padding:0}
.tile{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px}
.tile b{display:block;font-size:22px}.tile span{color:var(--muted);font-size:13px}
.ok{color:var(--ok)}.warn{color:var(--warn)}.down{color:var(--down)}.muted{color:var(--muted);font-size:13px}
table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:6px 8px;
border-bottom:1px solid var(--line);font-size:14px}th{color:var(--muted);font-weight:600}
.chart svg{width:100%;height:auto;display:block}
.legend{display:flex;flex-wrap:wrap;gap:4px 12px;margin-top:6px;font-size:13px;color:var(--muted)}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px}
.empty{color:var(--muted);font-size:13px}
@media(max-width:560px){main{padding:12px}header{padding:12px}}
"""

APP_JS = r"""
const NS = 'http://www.w3.org/2000/svg';
const COLORS = ['--s1','--s2','--s3','--s4','--s5','--s6','--s7'];
const el = (tag, text, cls) => {
  const e = document.createElement(tag);
  if (text !== undefined) e.textContent = text;
  if (cls) e.className = cls;
  return e;
};
const svg = (tag, attrs) => {
  const e = document.createElementNS(NS, tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  return e;
};
const label = {ok: 'OK', warn: 'uwaga', down: 'awaria'};
const fmt = (iso) => iso ? iso.replace('T', ' ').slice(0, 16) : 'brak';

function tile(parent, value, caption, cls) {
  const t = el('div', undefined, 'tile');
  t.append(el('b', value, cls), el('span', caption));
  parent.append(t);
}

function sum(rows, pick) {
  const total = {};
  for (const r of rows) {
    const o = pick(r) || {};
    for (const k in o) total[k] = (total[k] || 0) + o[k];
  }
  return total;
}

function stacked(id, rows, pick) {
  const box = document.getElementById(id);
  box.replaceChildren();
  const keys = Object.keys(sum(rows, pick)).sort();
  if (!rows.length || !keys.length) { box.append(el('p', 'brak danych', 'empty')); return; }
  const W = 900, H = 160, pad = 18, bw = Math.max(4, (W - 2 * pad) / rows.length - 3);
  const totals = rows.map(r => keys.reduce((a, k) => a + ((pick(r) || {})[k] || 0), 0));
  const max = Math.max(1, ...totals);
  const s = svg('svg', {viewBox: `0 0 ${W} ${H + 16}`, role: 'img'});
  rows.forEach((r, i) => {
    let y = H;
    const x = pad + i * ((W - 2 * pad) / rows.length);
    keys.forEach((k, j) => {
      const v = (pick(r) || {})[k] || 0;
      if (!v) return;
      const h = (v / max) * (H - 14);
      y -= h;
      const rect = svg('rect', {x, y, width: bw, height: h, rx: 2,
        fill: `var(${COLORS[j % COLORS.length]})`});
      const t = svg('title', {}); t.textContent = `${r.day} · ${k}: ${v}`;
      rect.append(t); s.append(rect);
    });
  });
  const first = svg('text', {x: pad, y: H + 13, fill: 'var(--muted)', 'font-size': 11});
  first.textContent = rows[0].day;
  const last = svg('text', {x: W - pad, y: H + 13, fill: 'var(--muted)', 'font-size': 11,
    'text-anchor': 'end'});
  last.textContent = rows[rows.length - 1].day + ` · max ${max}`;
  s.append(first, last);
  box.append(s);
  const lg = el('div', undefined, 'legend');
  keys.forEach((k, j) => {
    const item = el('span', ' ' + k);
    const sw = el('i'); sw.style.background = `var(${COLORS[j % COLORS.length]})`;
    item.prepend(sw); lg.append(item);
  });
  box.append(lg);
}

function table(id, head, rows) {
  const t = document.getElementById(id);
  t.replaceChildren();
  const tr = el('tr'); head.forEach(h => tr.append(el('th', h))); t.append(tr);
  rows.forEach(r => {
    const row = el('tr');
    r.forEach(c => {
      const td = el('td');
      if (c && typeof c === 'object') { td.textContent = c.text; td.className = c.cls; }
      else td.textContent = c;
      row.append(td);
    });
    t.append(row);
  });
}

function render(now, health, daily) {
  document.getElementById('updated').textContent =
    now ? 'migawka: ' + fmt(now.generated_at) : 'brak migawki';
  const tiles = document.getElementById('tiles'); tiles.replaceChildren();
  tile(tiles, label[health.service] || '?', 'usługa · ' + health.reason, health.service);
  tile(tiles, now ? fmt(now.last_cycle) : 'brak', 'ostatni cykl'
    + (health.cycle_age_minutes != null ? ` (${health.cycle_age_minutes} min przed migawką)` : ''));
  const today = daily.length ? daily[daily.length - 1] : null;
  tile(tiles, now ? String(now.llm_today.requests) : '0',
    'zapytań do modelu dziś · tokenów ' + (now ? now.llm_today.tokens : 0));
  tile(tiles, now ? String(now.audit_open) : '0', 'otwartych audytów');
  if (today) {
    const found = Object.values(today.findings || {}).reduce((a, b) => a + b, 0);
    tile(tiles, String(found), 'pozycji dziś · wycofanych ' + today.findings_retracted);
    const r = today.ratings || {};
    tile(tiles, `${r.confirmed || 0} / ${r.wrong || 0}`, 'oceny dziś: trafne / błędne');
    tile(tiles, String(today.cycles.n), `cykli dziś · śr. ${today.cycles.avg_seconds} s`);
  }
  const kids = (now ? now.children : []).map(c => {
    const h = health.children[c.id] || {state: 'warn', reason: '?'};
    return [c.id, {text: label[h.state] + ' · ' + h.reason, cls: h.state},
      fmt(c.last_success), String(c.logins_today), c.last_error || '–'];
  });
  table('children', ['dziecko', 'stan', 'ostatni sukces', 'logowań dziś', 'ostatni błąd'], kids);
  stacked('c-findings', daily, r => r.findings);
  stacked('c-ratings', daily, r => r.ratings);
  stacked('c-llm', daily, r => ({zapytania: (r.llm || {}).requests || 0}));
  stacked('c-sieve', daily, r => r.sieve);
  stacked('c-errors', daily, r => r.module_errors);
  const mods = [];
  if (now) for (const child in now.modules)
    for (const m in now.modules[child]) mods.push([child, m, fmt(now.modules[child][m])]);
  table('modules', ['dziecko', 'moduł', 'ostatnie pobranie'], mods);
}

async function load() {
  try {
    const [a, b] = await Promise.all([fetch('/api/now'), fetch('/api/daily?days=30')]);
    const n = await a.json(), d = await b.json();
    render(n.now, n.health, d.daily);
  } catch (e) {
    document.getElementById('updated').textContent = 'błąd odczytu';
  }
}
load();
setInterval(load, 60000);
"""
