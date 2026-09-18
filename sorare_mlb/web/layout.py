"""Společný layout: design tokeny (OFF+BRAND), navigace a JS pomocníci.

Styl: pergamenové plátno, jediné písmo, obří verzálkové nadpisy se
zhuštěným řádkováním, vlasové linky místo stínů a jediný barevný moment —
duhová koule na dashboardu. Stavy se nerozlišují barvou, ale znakem
a řezem písma, aby paleta zůstala černobílá.
"""
from __future__ import annotations

from html import escape

NAV = [
    ("dashboard", "/", "Přehled"),
    ("rewards", "/vyhry", "Výhry"),
    ("ledger", "/bilance", "Bilance"),
    ("mlb", "/mlb", "MLB"),
    ("mlb-lineups", "/mlb/sestavy", "Sestavy MLB"),
    ("mlb-calibration", "/mlb/kalibrace", "Kalibrace"),
    ("mlb-history", "/mlb/historie", "Historie MLB"),
    ("football", "/fotbal", "Fotbal"),
    ("football-lineups", "/fotbal/sestavy", "Sestavy fotbal"),
    ("football-history", "/fotbal/historie", "Historie fotbal"),
    ("settings", "/nastaveni", "Nastavení"),
]

CSS = r"""
:root {
  --parchment: #e5e4e0; --ink: #1d1d1d; --paper: #ffffff;
  --ash: #bfbebe; --stone: #cdcdc9; --ink-soft: rgba(29,29,29,.62);
  --sphere: linear-gradient(255deg, rgb(250,203,14), rgb(240,107,168) 30%,
            rgb(120,186,230) 65%, rgb(255,255,255));
  --font: "Inter Tight", "Inter", ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
  --gap: 19px; --pad: 30px; --section: 76px;
}
* { box-sizing: border-box; }
html { background: var(--parchment); }
body {
  margin: 0; background: var(--parchment); color: var(--ink);
  font: 400 15px/1.4 var(--font); letter-spacing: .01em;
  -webkit-font-smoothing: antialiased; font-variant-numeric: tabular-nums;
}
a { color: inherit; }
.page { max-width: 1400px; margin: 0 auto; padding: 0 var(--pad) 119px; }

/* ---------- navigace ---------- */
.top {
  display: flex; justify-content: space-between; align-items: baseline;
  gap: var(--gap); padding: 22px 0; border-bottom: 1px solid var(--ash);
  flex-wrap: wrap;
}
.mark { font-weight: 700; font-size: 15px; letter-spacing: .02em; text-decoration: none; }
.mark span { font-weight: 400; }
nav { display: flex; flex-wrap: wrap; gap: 4px 22px; }
nav a {
  font-size: 11px; font-weight: 700; letter-spacing: .05em; text-transform: uppercase;
  text-decoration: none; padding: 5px 0; border-bottom: 1px solid transparent;
}
nav a:hover, nav a[aria-current="page"] { border-bottom-color: var(--ink); }

/* ---------- typografie ---------- */
.display {
  font-size: clamp(56px, 10vw, 103px); line-height: .88; letter-spacing: .013em;
  text-transform: uppercase; font-weight: 400; margin: 0;
}
.head { position: relative; padding: 76px 0 46px; }
.head .lede { max-width: 46ch; font-size: 18px; margin: 30px 0 0; }
.label {
  font-size: 11px; letter-spacing: .05em; text-transform: uppercase;
  margin: 0 0 var(--gap); font-weight: 400;
}
h2.title { font-size: 34px; line-height: 1; font-weight: 400; margin: 0 0 var(--gap); letter-spacing: .013em; }
.big {
  font-size: clamp(26px, 3.1vw, 46px); line-height: 1.05; letter-spacing: .013em;
  overflow-wrap: anywhere; min-width: 0;
}
.muted { color: var(--ink-soft); }
.small { font-size: 11px; letter-spacing: .05em; text-transform: uppercase; }
p { max-width: 70ch; }

/* ---------- sekce a plochy ---------- */
section { margin-top: var(--section); padding-top: var(--gap); border-top: 1px solid var(--ash); }
.split { display: grid; grid-template-columns: minmax(180px, 1fr) 3fr; gap: var(--gap); }
.grid { display: grid; gap: 1px; background: var(--ash); border: 1px solid var(--ash); }
.g2 { grid-template-columns: repeat(2, 1fr); }
.g3 { grid-template-columns: repeat(3, 1fr); }
.g4 { grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); }
.g5 { grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); }
.gfit { grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); }
.mid { font-size: clamp(22px, 2.4vw, 34px); line-height: 1.05; letter-spacing: .013em; overflow-wrap: anywhere; }
.cell { background: var(--paper); padding: var(--pad); min-width: 0; overflow: hidden; }
.cell.stone { background: var(--stone); }
.cell .label { margin-bottom: 30px; }
.paper-grid {
  background-color: var(--paper);
  background-image:
    linear-gradient(var(--parchment) 1px, transparent 1px),
    linear-gradient(90deg, var(--parchment) 1px, transparent 1px);
  background-size: 24px 24px;
}
@media (max-width: 900px) {
  .g3, .g4, .g5 { grid-template-columns: repeat(2, 1fr); }
  .split { grid-template-columns: 1fr; }
}
@media (max-width: 560px) {
  :root { --pad: 19px; --section: 46px; }
  .g2, .g3, .g4, .g5 { grid-template-columns: 1fr; }
}

/* ---------- koule a kruhy (jen dashboard) ---------- */
.hero { min-height: 620px; overflow: hidden; isolation: isolate; }
.sphere {
  position: absolute; right: 4%; top: 40px; width: min(46vw, 520px); aspect-ratio: 1;
  border-radius: 50%; background: var(--sphere); z-index: -1;
}
.rings { position: absolute; right: calc(4% - 90px); top: -50px; width: min(46vw, 520px); aspect-ratio: 1; z-index: -2; }
.rings i {
  position: absolute; inset: 0; border: 1px solid var(--ash); border-radius: 50%;
}
.rings i:nth-child(2) { inset: -70px; }
.rings i:nth-child(3) { inset: -140px; }
.hero .display { position: relative; }
@media (max-width: 700px) { .sphere, .rings { width: 70vw; opacity: .9; } .hero { min-height: 420px; } }

/* ---------- ovládací prvky ---------- */
.btn {
  display: inline-flex; align-items: center; gap: 8px; font: inherit;
  font-size: 11px; letter-spacing: .05em; text-transform: uppercase;
  color: var(--ink); background: transparent; border: 1px solid var(--ink);
  border-radius: 10px; padding: 8px 19px; cursor: pointer; text-decoration: none;
}
.btn:hover:not(:disabled) { background: var(--ink); color: var(--parchment); }
.btn:disabled { opacity: .4; cursor: not-allowed; }
.btn[aria-pressed="true"] { background: var(--ink); color: var(--parchment); }
.link { text-decoration: none; border-radius: 10px; padding: 5px 0; }
.link:hover { text-decoration: underline; text-underline-offset: 3px; }
:focus-visible { outline: 2px solid var(--ink); outline-offset: 3px; }
.row { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
input, select {
  font: inherit; color: var(--ink); background: var(--paper);
  border: 1px solid var(--ash); border-radius: 10px; padding: 8px 12px;
}
input:focus, select:focus { outline: none; border-color: var(--ink); }

/* ---------- tabulky ---------- */
.scroll { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; }
th {
  text-align: left; font-weight: 400; font-size: 11px; letter-spacing: .05em;
  text-transform: uppercase; padding: 0 12px 10px 0; border-bottom: 1px solid var(--ink);
  white-space: nowrap;
}
td.num, th.num { white-space: nowrap; }
td { padding: 11px 12px 11px 0; border-bottom: 1px solid var(--ash); vertical-align: middle; }
td.num, th.num { text-align: right; }
tr:last-child td { border-bottom: 0; }

/* ---------- drobnosti ---------- */
.bar { display: grid; grid-template-columns: minmax(90px, 150px) 1fr 40px; gap: 12px; align-items: center; padding: 5px 0; }
.bar b { display: block; height: 6px; background: var(--ink); }
.bar .track { background: var(--parchment); }
.state { display: inline-flex; gap: 8px; align-items: center; }
.state::before { content: "○"; }
.state.on::before { content: "●"; }
.state.half::before { content: "◐"; }
.state.stop::before { content: "■"; }
.tag {
  display: inline-block; font-size: 11px; letter-spacing: .05em; text-transform: uppercase;
  border: 1px solid var(--ash); border-radius: 10px; padding: 1px 8px; white-space: nowrap;
}
.tag.ink { border-color: var(--ink); }
.notice { border: 1px solid var(--ink); padding: 15px 19px; margin-top: 30px; }
.notice a { font-weight: 700; }
.empty { padding: 30px 0; color: var(--ink-soft); }
.log {
  font-size: 13px; white-space: pre-wrap; max-height: 220px; overflow-y: auto;
  color: var(--ink-soft); margin: 0; font-family: var(--font);
}
svg.spark { width: 90px; height: 22px; display: block; }
svg.spark path { fill: none; stroke: var(--ink); stroke-width: 1.2; }
.skeleton { color: var(--ink-soft); }
@media (prefers-reduced-motion: reduce) { * { transition: none !important; } }
"""

JS = r"""
const $ = (s, el = document) => el.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const num = (v, d = 0) => v == null || isNaN(v) ? "—" : Number(v).toLocaleString("cs-CZ", {maximumFractionDigits: d, minimumFractionDigits: d});

async function api(url, opts = {}) {
  const r = await fetch(url, opts.body || opts.method ? {
    method: opts.method || "POST", headers: {"Content-Type": "application/json"},
    body: opts.body ? JSON.stringify(opts.body) : undefined
  } : {});
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || r.statusText);
  return data;
}

function when(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d)) return esc(iso);
  return d.toLocaleString("cs-CZ", {weekday: "short", day: "numeric", month: "numeric", hour: "2-digit", minute: "2-digit"});
}
function day(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return isNaN(d) ? esc(iso) : d.toLocaleDateString("cs-CZ", {day: "numeric", month: "numeric", year: "2-digit"});
}
function countdown(iso) {
  const ms = new Date(iso) - new Date();
  if (isNaN(ms)) return "—";
  if (ms <= 0) return "uzavřeno";
  const h = Math.floor(ms / 36e5), m = Math.floor(ms % 36e5 / 6e4);
  return h >= 48 ? `za ${Math.floor(h / 24)} dní` : h ? `za ${h} h ${m} min` : `za ${m} min`;
}
function spark(values) {
  const v = (values || []).filter(x => x != null);
  if (v.length < 2) return "";
  const max = Math.max(...v, 1), w = 90, h = 22, step = w / (v.length - 1);
  const d = v.map((x, i) => `${i ? "L" : "M"}${(i * step).toFixed(1)},${(h - 2 - x / max * (h - 4)).toFixed(1)}`).join("");
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" aria-hidden="true"><path d="${d}"/></svg>`;
}
function trend(t) {
  if (t == null) return "—";
  return (t > 0 ? "↑ " : t < 0 ? "↓ " : "") + num(Math.abs(t), 1);
}
function failed(el, err) {
  el.innerHTML = `<div class="empty">Nepodařilo se načíst: ${esc(err.message || err)}</div>`;
}
const JOB_LABEL = {
  QUEUED: "Ve frontě", CARDS: "Stahuji portfolio", SCORES: "Stahuji skóre",
  MLB: "Stahuji MLB data", OPTIMIZE: "Optimalizuji", VALIDATE: "Kontroluji",
  SUBMIT: "Odesílám", DONE: "Hotovo", NEEDS_REVIEW: "Čeká na potvrzení",
  FAILED: "Chyba", NONE: "Zatím neběželo"
};
const JOB_RUNNING = ["QUEUED","CARDS","SCORES","MLB","OPTIMIZE","VALIDATE","SUBMIT"];
function jobState(s) {
  const cls = s === "DONE" ? "on" : s === "FAILED" ? "stop" : s === "NEEDS_REVIEW" || JOB_RUNNING.includes(s) ? "half" : "";
  return `<span class="state ${cls}">${esc(JOB_LABEL[s] || s)}</span>`;
}
"""


def page(title: str, active: str, body: str, script: str = "", charts: bool = False) -> str:
    current = ' aria-current="page"'
    nav = "".join(
        f'<a href="{href}"{current if key == active else ""}>{escape(text)}</a>'
        for key, href, text in NAV
    )
    chart_tag = (
        '<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>'
        if charts else ""
    )
    return f"""<!doctype html>
<html lang="cs">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)} — Sorare</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter+Tight:wght@400;700&display=swap" rel="stylesheet">
<style>{CSS}</style>
{chart_tag}
</head>
<body>
<div class="page">
  <header class="top">
    <a class="mark" href="/">SORARE <span>/ KB</span></a>
    <nav aria-label="Hlavní">{nav}</nav>
  </header>
  <div id="authbar"></div>
  <main>{body}</main>
</div>
<script>{JS}
{JS_BACKFILL}
(async () => {{
  try {{
    const a = await api("/api/auth/status");
    const bar = document.getElementById("authbar");
    if (!a.authenticated) bar.innerHTML = `<div class="notice">Nejsi přihlášen k Sorare, data se nenačtou a automat nepoběží. <a href="/login">Přihlásit se</a></div>`;
    else if (a.needs_login) bar.innerHTML = `<div class="notice">Token vyprší za ${{a.days_left}} dní. <a href="/login">Obnovit přihlášení</a></div>`;
  }} catch (e) {{}}
}})();
</script>
<script>{script}</script>
</body>
</html>"""


JS_BACKFILL = r"""
async function runBackfill(url, button, status, reset) {
  button.disabled = true;
  let total = 0, round = 0, body = reset ? {reset: true, full: true} : {full: true};
  try {
    while (true) {
      round += 1;
      status.textContent = `Stahuji historii, dávka ${round}, zatím ${total} nových záznamů…`;
      const r = await api(url, {body});
      total += r.new || 0;
      body = {full: true};
      const err = (r.scopes || []).find(s => s.error);
      if (err) status.textContent = `Zdroj ${err.scope} selhal: ${err.error}`;
      if (r.done) break;
    }
    status.textContent = `Hotovo, staženo ${total} nových záznamů.`;
    return true;
  } catch (e) {
    status.textContent = "Stahování se zastavilo: " + e.message + ". Kliknutím navážeš.";
    return false;
  } finally { button.disabled = false; }
}
"""
