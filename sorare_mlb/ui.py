"""Webové rozhraní — jedna stránka bez build stepu.

Záměrně bez frameworku: je to jedno tlačítko, tabulka a polling. Přidávat sem
React by znamenalo build pipeline kvůli padesáti řádkům JS.
"""

PAGE = r"""<!doctype html>
<html lang="cs">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sorare MLB — sestavy</title>
<style>
  :root {
    --bg: #0d1117; --panel: #161b22; --line: #262d38;
    --text: #e6edf3; --dim: #8b949e;
    --ok: #3fb950; --warn: #d29922; --bad: #f85149; --accent: #388bfd;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font: 15px/1.55 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
  }
  .wrap { max-width: 880px; margin: 0 auto; padding: 32px 20px 80px; }
  h1 { font-size: 22px; margin: 0 0 4px; letter-spacing: -0.01em; }
  .sub { color: var(--dim); font-size: 14px; margin-bottom: 28px; }
  .row { display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 24px; }
  button {
    background: var(--accent); color: #fff; border: 0; border-radius: 7px;
    padding: 10px 18px; font-size: 14px; font-weight: 500; cursor: pointer;
  }
  button.ghost { background: transparent; border: 1px solid var(--line); color: var(--text); }
  button:disabled { opacity: .45; cursor: not-allowed; }
  .panel {
    background: var(--panel); border: 1px solid var(--line);
    border-radius: 10px; padding: 18px; margin-bottom: 16px;
  }
  .state { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
  .badge {
    font-size: 12px; padding: 3px 9px; border-radius: 20px;
    background: #1f2937; color: var(--dim); font-weight: 500;
  }
  .badge.running { background: #10243e; color: var(--accent); }
  .badge.done { background: #12261a; color: var(--ok); }
  .badge.review { background: #2b2311; color: var(--warn); }
  .badge.failed { background: #2d1416; color: var(--bad); }
  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  th, td { text-align: left; padding: 7px 8px; border-bottom: 1px solid var(--line); }
  th { color: var(--dim); font-weight: 500; font-size: 12px; text-transform: uppercase; }
  td.num { text-align: right; font-variant-numeric: tabular-nums; }
  .slot { color: var(--dim); font-family: ui-monospace, monospace; font-size: 13px; }
  .log {
    font-family: ui-monospace, SFMono-Regular, monospace; font-size: 12px;
    color: var(--dim); white-space: pre-wrap; max-height: 190px; overflow-y: auto;
  }
  .issue { padding: 7px 0; border-bottom: 1px solid var(--line); font-size: 14px; }
  .issue:last-child { border: 0; }
  .blocker { color: var(--bad); }
  .warning { color: var(--warn); }
  .muted { color: var(--dim); font-size: 13px; }
  .authbar {
    padding: 11px 14px; border-radius: 8px; margin-bottom: 18px; font-size: 14px;
  }
  .authbar.bad { background: #2d1416; color: var(--bad); }
  .authbar.warn { background: #2b2311; color: var(--warn); }
  .authbar a { color: inherit; font-weight: 600; }
  h2 { font-size: 15px; margin: 0 0 12px; font-weight: 600; }
  h3 { font-size: 14px; margin: 18px 0 8px; font-weight: 600; }
  h3:first-of-type { margin-top: 0; }
</style>
</head>
<body>
<div class="wrap">
  <h1>Sorare MLB</h1>
  <div class="sub">Hot Streaks a Challenger — návrh i automatické odeslání.</div>

  <div id="authbar"></div>

  <div class="row">
    <button id="propose">Navrhnout sestavy</button>
    <button id="auto" class="ghost">Navrhnout a odeslat</button>
    <button id="refresh" class="ghost">Obnovit stav</button>
  </div>

  <div class="panel">
    <div class="state">
      <span id="badge" class="badge">načítám…</span>
      <span id="meta" class="muted"></span>
    </div>
    <div id="log" class="log"></div>
  </div>

  <div id="lineups"></div>
  <div id="issues"></div>
  <div id="actions" class="row"></div>
</div>

<script>
const $ = (id) => document.getElementById(id);
let timer = null;
let currentJob = null;

const RUNNING = ["QUEUED","CARDS","SCORES","MLB","OPTIMIZE","VALIDATE","SUBMIT"];

const badgeClass = (s) =>
  s === "DONE" ? "done" :
  s === "NEEDS_REVIEW" ? "review" :
  s === "FAILED" ? "failed" :
  RUNNING.includes(s) ? "running" : "";

const stateLabel = {
  QUEUED: "ve frontě", CARDS: "stahuji portfolio", SCORES: "stahuji skóre",
  MLB: "stahuji MLB data", OPTIMIZE: "optimalizuji", VALIDATE: "kontroluji",
  SUBMIT: "odesílám", DONE: "hotovo", NEEDS_REVIEW: "čeká na tebe",
  FAILED: "chyba", NONE: "nic neběželo"
};

async function post(url, body) {
  const r = await fetch(url, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body || {})
  });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
}

async function poll() {
  const r = await fetch("/api/status" + (currentJob ? "?job_id=" + currentJob : ""));
  const d = await r.json();
  render(d);
  if (RUNNING.includes(d.state)) {
    timer = setTimeout(poll, 3000);
  }
}

function render(d) {
  currentJob = d.job_id || currentJob;
  $("badge").textContent = stateLabel[d.state] || d.state;
  $("badge").className = "badge " + badgeClass(d.state);

  let meta = d.updated_at ? "aktualizováno " + d.updated_at.replace("T", " ") : "";
  if (d.progress_remaining) meta += ` · zbývá ${d.progress_remaining} hráčů`;
  if (d.mode) meta += ` · režim: ${d.mode === "auto" ? "auto-odeslání" : "jen návrh"}`;
  $("meta").textContent = meta;

  $("log").textContent = (d.steps || []).join("\n") + (d.error ? "\n\n" + d.error : "");

  $("lineups").innerHTML = (d.lineups || []).map(lu => `
    <div class="panel">
      <h2>${lu.tournament_name} #${lu.index + 1}
        <span class="muted">— projekce ${lu.total_projected.toFixed(0)}</span></h2>
      <table>
        <tr><th>Slot</th><th>Hráč</th><th>Tým</th><th class="num">Proj.</th></tr>
        ${lu.slots.map(s => `<tr>
          <td class="slot">${s.slot}</td>
          <td>${s.player}</td>
          <td class="muted">${s.team || "—"}</td>
          <td class="num">${s.projected.toFixed(0)}</td>
        </tr>`).join("")}
      </table>
    </div>`).join("");

  $("issues").innerHTML = (d.issues || []).length ? `
    <div class="panel">
      <h2>Nálezy z kontroly</h2>
      ${d.issues.map(i => `<div class="issue">
        <span class="${i.severity}">${i.severity === "blocker" ? "🔴" : "🟡"}</span>
        <strong>${i.player}</strong> <span class="muted">(${i.lineup} / ${i.slot})</span>
        — ${i.message}
        ${i.suggested_replacement ? `<div class="muted">↳ náhrada: ${i.suggested_replacement}</div>` : ""}
      </div>`).join("")}
    </div>` : "";

  const submitted = (d.submitted || []).filter(s => s.ok);
  if (submitted.length) {
    $("issues").innerHTML += `<div class="panel"><h2>Odesláno</h2>${
      submitted.map(s => `<div class="issue">✅ ${s.tournament_name}
        <span class="muted">id ${s.lineup_id || "?"}</span></div>`).join("")}</div>`;
  }

  const blockers = (d.issues || []).filter(i => i.severity === "blocker").length;
  $("actions").innerHTML = d.state === "NEEDS_REVIEW"
    ? `<button id="confirm">${blockers ? "Odeslat i přes nálezy" : "Odeslat na Sorare"}</button>`
    : "";
  if (d.state === "NEEDS_REVIEW") {
    $("confirm").onclick = async () => {
      if (blockers && !confirm(`Sestavy mají ${blockers} blokujících nálezů. Opravdu odeslat?`))
        return;
      $("confirm").disabled = true;
      try {
        await post("/api/submit", {job_id: currentJob, force: blockers > 0});
        poll();
      } catch (e) { alert("Chyba: " + e.message); $("confirm").disabled = false; }
    };
  }
}

async function start(mode) {
  clearTimeout(timer);
  currentJob = null;
  ["propose","auto"].forEach(id => $(id).disabled = true);
  try {
    const d = await post("/api/build", {mode});
    currentJob = d.job_id;
    poll();
  } catch (e) {
    alert("Nepodařilo se spustit: " + e.message);
  } finally {
    ["propose","auto"].forEach(id => $(id).disabled = false);
  }
}

$("propose").onclick = () => start("propose");
$("auto").onclick = () => {
  if (confirm("Sestavy se po dokončení rovnou odešlou na Sorare. Pokračovat?"))
    start("auto");
};
$("refresh").onclick = () => { clearTimeout(timer); poll(); };

async function checkAuth() {
  try {
    const d = await (await fetch("/api/auth/status")).json();
    if (!d.authenticated) {
      $("authbar").innerHTML = `<div class="authbar bad">
        Nejsi přihlášen k Sorare — <a href="/login">přihlas se</a>, jinak nic nepoběží.</div>`;
    } else if (d.needs_login) {
      $("authbar").innerHTML = `<div class="authbar warn">
        Token platí ještě ${d.days_left} dní — <a href="/login">obnov ho</a>.</div>`;
    } else {
      $("authbar").innerHTML = "";
    }
  } catch (e) { /* stav přihlášení není důvod rozbít stránku */ }
}

checkAuth();
poll();
</script>
</body>
</html>
"""
