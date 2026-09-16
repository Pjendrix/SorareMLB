"""Historie jednoho sportu: vývoj sbírky, sestavy ze Sorare a běhy automatu."""
from .layout import page

BODY = """
<div class="head">
  <h1 class="display">Historie<br>{title}</h1>
  <p class="lede">Sorare vrací jen nedávné sestavy. Delší historii si aplikace skládá sama: denní snímek sbírky a záznam o každém běhu automatu.</p>
</div>

<section>
  <div class="split">
    <div>
      <h2 class="label">Vývoj sbírky</h2>
      <p class="muted">Snímek vzniká jednou denně při otevření přehledu.</p>
    </div>
    <div class="cell paper-grid" style="border:1px solid var(--ash)">
      <div id="chart-empty" class="empty" hidden>Zatím žádné snímky. První vznikne při otevření přehledu sbírky.</div>
      <canvas id="chart" height="260" aria-label="Počet karet a průměr L15 v čase" role="img"></canvas>
    </div>
  </div>
</section>

<section>
  <div class="split">
    <h2 class="label">Sestavy ze Sorare</h2>
    <div id="sorare" class="skeleton">Načítám…</div>
  </div>
</section>

<section {runs_hidden}>
  <div class="split">
    <h2 class="label">Běhy automatu</h2>
    <div id="runs" class="skeleton">Načítám…</div>
  </div>
</section>
"""

SCRIPT = r"""
const SPORT_KEY = "%(key)s";

function chart(snaps) {
  if (!snaps.length) { $("#chart-empty").hidden = false; $("#chart").hidden = true; return; }
  if (!window.Chart) return;
  Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
  Chart.defaults.color = "#1d1d1d";
  new Chart($("#chart"), {
    type: "line",
    data: {
      labels: snaps.map(s => day(s.date)),
      datasets: [
        {label: "Karet", data: snaps.map(s => s.count), borderColor: "#1d1d1d", borderWidth: 1.5, pointRadius: 0, yAxisID: "y"},
        {label: "Průměr L15", data: snaps.map(s => s.avg_l15), borderColor: "#1d1d1d", borderDash: [4, 4], borderWidth: 1.5, pointRadius: 0, yAxisID: "y1"},
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false, interaction: {mode: "index", intersect: false},
      plugins: {legend: {labels: {boxWidth: 18, boxHeight: 1}}},
      scales: {
        x: {grid: {color: "#e5e4e0"}},
        y: {grid: {color: "#e5e4e0"}, title: {display: true, text: "Karet"}},
        y1: {position: "right", grid: {display: false}, title: {display: true, text: "L15"}},
      }
    }
  });
}

function sorareTable(rec) {
  const el = $("#sorare");
  el.classList.remove("skeleton");
  if (rec.error) return failed(el, rec.error);
  const rows = rec.has_sport ? rec.lineups.filter(r => r.sport === SPORT_KEY) : rec.lineups;
  el.innerHTML = (rec.has_sport ? "" : `<p class="muted">Sorare nevrátil sport u sestav, zobrazuji všechny.</p>`) +
    (rows.length ? `<div class="scroll"><table><thead><tr><th>Turnaj</th><th>Rarita</th><th>GW</th><th>Období</th>
      ${rec.has_scores ? `<th class="num">Body</th><th class="num">Pořadí</th>` : ""}<th></th></tr></thead><tbody>
      ${rows.map(r => `<tr><td>${esc(r.tournament)}</td><td>${esc(r.rarity || "—")}</td><td>${esc(r.game_week ?? "—")}</td>
        <td>${day(r.start)} až ${day(r.end)}</td>
        ${rec.has_scores ? `<td class="num">${num(r.score, 1)}</td><td class="num">${num(r.ranking)}</td>` : ""}
        <td>${r.live ? `<span class="tag ink">Právě se hraje</span>` : ""}</td></tr>`).join("")}
      </tbody></table></div>` : `<div class="empty">Žádné nedávné sestavy.</div>`) +
    (rec.has_scores ? "" : `<p class="muted small">Body a pořadí Sorare pro tenhle dotaz nevrátil (varianta ${esc(rec.variant)}).</p>`);
}

function runsTable(runs) {
  const el = $("#runs");
  el.classList.remove("skeleton");
  if (!runs.length) { el.innerHTML = `<div class="empty">Automat zatím nic nezaznamenal.</div>`; return; }
  el.innerHTML = `<div class="scroll"><table><thead><tr><th>Kdy</th><th>GW</th><th>Režim</th><th>Stav</th>
    <th>Sestavy</th><th class="num">Odesláno</th><th class="num">Blokující</th></tr></thead><tbody>
    ${runs.map(r => `<tr><td>${when(r.at)}</td><td>${esc(r.game_week ?? "—")}</td>
      <td>${r.mode === "auto" ? "Auto" : "Návrh"}</td><td>${jobState(r.state)}</td>
      <td>${r.lineups.map(l => `<div>${esc(l.tournament)} #${l.index + 1} <span class="muted">proj. ${num(l.projected)}</span></div>`).join("") || "—"}</td>
      <td class="num">${r.submitted_ok}${r.submitted_failed ? ` / chyba ${r.submitted_failed}` : ""}</td>
      <td class="num">${r.blockers}</td></tr>`).join("")}</tbody></table></div>`;
}

(async () => {
  try {
    const h = await api(`/api/history/${SPORT_KEY}`);
    chart(h.snapshots);
    runsTable(h.jobs);
  } catch (e) { failed($("#runs"), e); }
  try { sorareTable(await api("/api/recent-lineups")); }
  catch (e) { failed($("#sorare"), e); }
})();
"""


def render(key: str, active: str, title: str, show_runs: bool) -> str:
    body = BODY.format(title=title, runs_hidden="" if show_runs else "hidden")
    return page(f"Historie {title}", active, body, SCRIPT % {"key": key}, charts=True)
