"""Hlavní stránka — souhrn napříč sporty."""
from .layout import page

BODY = """
<div class="head hero">
  <div class="rings" aria-hidden="true"><i></i><i></i><i></i></div>
  <div class="sphere" aria-hidden="true"></div>
  <h1 class="display">Sorare<br>přehled</h1>
  <p class="lede" id="hello">MLB a fotbal na jednom místě: uzávěrky, sestavy, forma karet a stav automatu.</p>
</div>

<section aria-labelledby="s-live">
  <div class="split">
    <h2 class="label" id="s-live">Právě se hraje</h2>
    <div id="live" class="skeleton">Načítám…</div>
  </div>
</section>

<section aria-labelledby="s-deadline">
  <div class="split">
    <h2 class="label" id="s-deadline">Nejbližší uzávěrky</h2>
    <div id="deadlines" class="skeleton">Načítám turnaje…</div>
  </div>
</section>

<section aria-labelledby="s-alerts">
  <div class="split">
    <h2 class="label" id="s-alerts">Potřebuje pozornost</h2>
    <div id="alerts" class="skeleton">Kontroluji…</div>
  </div>
</section>

<section aria-labelledby="s-win">
  <div class="split">
    <h2 class="label" id="s-win">Výhry</h2>
    <div id="wins" class="skeleton">Načítám…</div>
  </div>
</section>

<section aria-labelledby="s-coll">
  <h2 class="label" id="s-coll">Sbírky</h2>
  <div class="grid g2" id="collections"></div>
</section>

<section aria-labelledby="s-auto">
  <div class="split">
    <h2 class="label" id="s-auto">Automat MLB</h2>
    <div id="automat" class="skeleton">Načítám…</div>
  </div>
</section>

<section aria-labelledby="s-recent">
  <div class="split">
    <h2 class="label" id="s-recent">Poslední sestavy</h2>
    <div id="recent" class="skeleton">Načítám…</div>
  </div>
</section>
"""

SCRIPT = r"""
const SPORT = {mlb: "MLB", football: "Fotbal"};
const SPORT_URL = {mlb: "/mlb", football: "/fotbal"};
const alerts = [];

function renderAlerts() {
  $("#alerts").classList.remove("skeleton");
  $("#alerts").innerHTML = alerts.length
    ? `<table><tbody>${alerts.map(a => `<tr><td>${a}</td></tr>`).join("")}</tbody></table>`
    : `<div class="empty">Nic nečeká. Všechny sledované turnaje mají sestavu a automat doběhl bez nálezů.</div>`;
}

function renderDeadlines(up) {
  const el = $("#deadlines");
  el.classList.remove("skeleton");
  if (up.error) return failed(el, up.error);
  const fx = up.fixtures.slice(0, 6);
  if (!fx.length) { el.innerHTML = `<div class="empty">Žádný otevřený gameweek.</div>`; return; }
  el.innerHTML = `<div class="grid gfit">${fx.map(f => `
    <div class="cell">
      <div class="label">${SPORT[f.sport]}, GW ${esc(f.game_week ?? "?")}</div>
      <div class="mid">${countdown(f.cutoff)}</div>
      <p class="muted">${when(f.cutoff)}</p>
      <p>Sestava v ${f.with_lineup} z ${f.boards} turnajů</p>
      <a class="link" href="${SPORT_URL[f.sport]}">Turnaje</a>
    </div>`).join("")}</div>`;

  for (const [sport, boards] of Object.entries(up.boards)) {
    for (const b of boards.filter(b => b.tracked && !b.mine)) {
      alerts.push(`<span class="state stop">${SPORT[sport]}: ${esc(b.name)} nemá sestavu, uzávěrka ${countdown(b.cutoff)}</span>`);
    }
  }
}

function renderAutomat(job) {
  const el = $("#automat");
  el.classList.remove("skeleton");
  if (!job) {
    el.innerHTML = `<div class="empty">Automat zatím neběžel. <a class="link" href="/mlb/sestavy">Navrhnout sestavy</a></div>`;
    return;
  }
  el.innerHTML = `<div class="grid g3">
    <div class="cell"><div class="label">Stav</div><div class="big">${jobState(job.state)}</div>
      <p class="muted">${when(job.updated_at)}</p></div>
    <div class="cell"><div class="label">Sestavy</div><div class="big">${job.lineups}</div>
      <p class="muted">${job.mode === "auto" ? "automatické odeslání" : "jen návrh"}</p></div>
    <div class="cell"><div class="label">Nálezy</div><div class="big">${job.blockers} / ${job.warnings}</div>
      <p class="muted">blokující / varování</p></div>
  </div>
  <p><a class="btn" href="/mlb/sestavy">Otevřít sestavy MLB</a></p>`;
  if (job.state === "NEEDS_REVIEW") alerts.push(`<span class="state half">Návrh sestav MLB čeká na potvrzení. <a class="link" href="/mlb/sestavy">Zkontrolovat</a></span>`);
  if (job.state === "FAILED") alerts.push(`<span class="state stop">Poslední běh automatu skončil chybou. <a class="link" href="/mlb/sestavy">Detail</a></span>`);
}

function renderRecent(rec) {
  const el = $("#recent");
  el.classList.remove("skeleton");
  if (rec.error) return failed(el, rec.error);
  const rows = rec.lineups.slice(0, 10);
  if (!rows.length) { el.innerHTML = `<div class="empty">Sorare nevrátil žádné nedávné sestavy.</div>`; return; }
  el.innerHTML = `<div class="scroll"><table>
    <thead><tr><th>Sport</th><th>Turnaj</th><th>GW</th><th>Konec</th>
      ${rec.has_scores ? `<th class="num">Body</th><th class="num">Pořadí</th>` : ""}<th></th></tr></thead>
    <tbody>${rows.map(r => `<tr>
      <td>${SPORT[r.sport] || "—"}</td><td>${esc(r.tournament)}</td>
      <td>${esc(r.game_week ?? "—")}</td><td>${day(r.end)}</td>
      ${rec.has_scores ? `<td class="num">${num(r.score, 1)}</td><td class="num">${num(r.ranking)}</td>` : ""}
      <td>${r.live ? `<span class="tag ink">Právě se hraje</span>` : ""}</td>
    </tr>`).join("")}</tbody></table></div>`;
}

function renderLive(rec) {
  const el = $("#live");
  el.classList.remove("skeleton");
  if (rec.error) return failed(el, rec.error);
  const cur = rec.current || [];
  el.innerHTML = cur.length ? `<div class="grid gfit">${cur.map(c => `<div class="cell">
      <div class="label">${SPORT[c.sport] || "Sport"}, GW ${esc(c.game_week ?? "?")}</div>
      <div class="big">${c.lineups} ${c.lineups === 1 ? "sestava" : c.lineups < 5 ? "sestavy" : "sestav"}</div>
      <p class="muted">konec ${when(c.end)}${c.score != null ? `, zatím ${num(c.score, 1)} bodů celkem` : ""}</p>
      <p>${Object.entries(c.tournaments).map(([n, k]) => `${esc(n)}${k > 1 ? ` (${k}×)` : ""}`).join("<br>")}</p>
      <a class="link" href="${SPORT_URL[c.sport] || "/"}">Detail</a></div>`).join("")}</div>`
    : `<div class="empty">Teď se nehraje žádná tvoje sestava.</div>`;
}

async function loadWins() {
  const el = $("#wins");
  try {
    const w = await api("/api/rewards");
    el.classList.remove("skeleton");
    const t = w.totals;
    el.innerHTML = `<div class="grid g4">
      <div class="cell"><div class="label">Peníze</div><div class="big">${t.money ? num(t.money, 2) + " €" : "—"}</div></div>
      <div class="cell"><div class="label">Essence</div><div class="big">${num(t.essence)}</div></div>
      <div class="cell"><div class="label">Karty a balíčky</div><div class="big">${num(t.cards)}</div></div>
      <div class="cell"><div class="label">Nejvýnosnější soutěž</div><div class="mid">${esc((w.competitions[0] || {}).tournament || "—")}</div></div>
    </div><p><a class="btn" href="/vyhry">Všechny výhry</a></p>`;
  } catch (e) { el.classList.remove("skeleton"); failed(el, e); }
}

function collectionCell(sport) {
  return `<div class="cell paper-grid" id="col-${sport}">
    <div class="label">${SPORT[sport]}</div>
    <div class="skeleton">Stahuji sbírku…</div></div>`;
}

async function loadCollection(sport) {
  const el = $("#col-" + sport);
  try {
    const d = await api(`/api/overview/${sport}`);
    el.innerHTML = `
      <div class="label">${SPORT[sport]}</div>
      <h3 class="display" style="font-size:clamp(46px,6vw,76px)">${num(d.count)}</h3>
      <p class="muted">karet, průměr L15 ${num(d.avg_l15, 1)}, ${d.idle_count} bez zápasu ${d.idle_after_days}+ dní</p>
      <table><thead><tr><th>Ve formě</th><th class="num">L5</th><th class="num">L15</th></tr></thead>
      <tbody>${d.in_form.slice(0, 4).map(r => `<tr><td>${esc(r.player)}</td>
        <td class="num">${num(r.l5, 1)}</td><td class="num">${num(r.l15, 1)}</td></tr>`).join("")}</tbody></table>
      <p><a class="link" href="${SPORT_URL[sport]}">Celá sbírka</a></p>`;
    if (d.truncated) alerts.push(`<span class="state half">${SPORT[sport]}: sbírka je větší než limit stránek, přehled je neúplný (sports.${sport}.max_card_pages).</span>`);
    renderAlerts();
  } catch (e) { failed(el, e); }
}

(async () => {
  let d;
  try { d = await api("/api/dashboard"); }
  catch (e) { failed($("#deadlines"), e); return; }

  if (d.auth.authenticated && d.auth.nickname) $("#hello").textContent =
    `Ahoj ${d.auth.nickname}. MLB a fotbal na jednom místě: uzávěrky, sestavy, forma karet a stav automatu.`;
  if (d.auth.needs_login) alerts.push(`<span class="state stop">Sorare token ${d.auth.authenticated ? `vyprší za ${d.auth.days_left} dní` : "chybí"}. <a class="link" href="/login">Přihlásit</a></span>`);

  renderDeadlines(d.upcoming);
  renderAutomat(d.job);
  renderRecent(d.recent);
  renderLive(d.recent);
  if (d.auth.authenticated) loadWins(); else failed($("#wins"), "Nejsi přihlášen.");
  renderAlerts();

  $("#collections").innerHTML = d.sports.map(collectionCell).join("");
  if (d.auth.authenticated) d.sports.forEach(loadCollection);
  else d.sports.forEach(s => failed($("#col-" + s), "Nejsi přihlášen."));
})();
"""


def render() -> str:
    return page("Přehled", "dashboard", BODY, SCRIPT)
