"""Kalibrace projekcí: kolik model čekal a kolik karty doopravdy uhrály."""
from .layout import page

BODY = """
<div class="head">
  <h1 class="display">Kalibrace</h1>
  <p class="lede">Projekce odeslaných sestav proti skutečným bodům po skončení gameweeku.
  Podle tohohle se ladí váhy v <code>config.yaml</code>.</p>
  <div class="row" style="margin-top:30px">
    <button class="btn" id="evaluate">Vyhodnotit skončené gameweeky</button>
    <span class="muted" id="status"></span>
  </div>
</div>

<section>
  <div class="grid g4" id="stats"><div class="cell skeleton">Načítám…</div></div>
</section>

<section>
  <div class="split">
    <div>
      <h2 class="label">Karty</h2>
      <p class="muted">Každá tečka je jedna karta v jedné sestavě. Na diagonále = trefa.
      Nad ní model podcenil, pod ní přecenil.</p>
    </div>
    <div class="cell" style="border:1px solid var(--ash)"><div id="scatter" class="scroll"></div></div>
  </div>
</section>

<section>
  <div class="split">
    <h2 class="label">Sestavy</h2>
    <div id="lineups" class="scroll"></div>
  </div>
</section>
"""

SCRIPT = r"""
function scatter(points) {
  if (!points.length) return `<p class="empty">Zatím nic vyhodnoceného. Data přibudou po prvním skončeném gameweeku s odeslanými sestavami.</p>`;
  const W = 620, H = 420, P = 40;
  const max = Math.max(10, ...points.map(p => Math.max(p.projected, p.actual))) * 1.05;
  const x = v => P + v / max * (W - 2 * P), y = v => H - P - v / max * (H - 2 * P);
  const ticks = [0, .25, .5, .75, 1].map(t => Math.round(max * t));
  return `<svg viewBox="0 0 ${W} ${H}" style="width:100%;max-width:${W}px" role="img" aria-label="Projekce vs. skutečnost">
    ${ticks.map(t => `<line x1="${x(0)}" x2="${x(max)}" y1="${y(t)}" y2="${y(t)}" stroke="var(--parchment)"/>
      <text x="${P - 6}" y="${y(t) + 4}" font-size="10" text-anchor="end" fill="currentColor">${t}</text>
      <text x="${x(t)}" y="${H - P + 16}" font-size="10" text-anchor="middle" fill="currentColor">${t}</text>`).join("")}
    <line x1="${x(0)}" y1="${y(0)}" x2="${x(max)}" y2="${y(max)}" stroke="var(--ink)" stroke-dasharray="4 4"/>
    ${points.map(p => `<circle cx="${x(p.projected)}" cy="${y(p.actual)}" r="3.5"
      fill="${p.inside ? "var(--ink)" : "none"}" stroke="var(--ink)">
      <title>${esc(p.player)} (${esc(p.slot)}) — projekce ${num(p.projected, 1)}, skutečnost ${num(p.actual, 1)}</title></circle>`).join("")}
    <text x="${W / 2}" y="${H - 6}" font-size="11" text-anchor="middle" fill="currentColor">projekce</text>
    <text x="12" y="${H / 2}" font-size="11" text-anchor="middle" fill="currentColor" transform="rotate(-90 12 ${H / 2})">skutečnost</text>
  </svg><p class="muted small">● v rozsahu floor–ceiling ○ mimo</p>`;
}

function render(d) {
  const s = d.stats || {};
  const cells = [
    ["Vyhodnocených karet", num(s.count)],
    ["Průměrná chyba (MAE)", s.mae != null ? num(s.mae, 1) + " b." : "—"],
    ["Bias", s.bias != null ? (s.bias > 0 ? "+" : "") + num(s.bias, 1) + " b." : "—"],
    ["V rozsahu floor–ceiling", s.inside_range != null ? num(s.inside_range * 100) + " %" : "—"],
  ];
  $("#stats").innerHTML = cells.map(([l, v]) => `<div class="cell"><div class="label">${l}</div><div class="mid">${v}</div></div>`).join("")
    + (s.mae_by_slot ? `<div class="cell"><div class="label">MAE podle slotu</div>${Object.entries(s.mae_by_slot)
      .map(([k, v]) => `<div class="bar"><span>${esc(k)}</span><span class="track"><b style="width:${Math.min(100, v / (s.mae * 2) * 100)}%"></b></span><span>${num(v, 1)}</span></div>`).join("")}</div>` : "");
  $("#scatter").innerHTML = scatter(d.points || []);
  const rows = d.lineups || [];
  $("#lineups").innerHTML = rows.length ? `<table><thead><tr><th>GW</th><th>Turnaj</th><th class="num">Projekce</th>
    <th class="num">Skutečnost</th><th class="num">Rozdíl</th><th>Práh</th></tr></thead><tbody>${rows.map(r => `<tr>
    <td>${esc(r.game_week ?? r.fixture ?? "—")}</td><td>${esc(r.tournament)} #${r.index + 1}</td>
    <td class="num">${num(r.projected, 1)}</td><td class="num">${r.actual == null ? "čeká" : num(r.actual, 1)}${r.complete || r.actual == null ? "" : " *"}</td>
    <td class="num">${r.actual == null ? "" : (r.actual - r.projected > 0 ? "+" : "") + num(r.actual - r.projected, 1)}</td>
    <td>${r.hit == null ? "" : `<span class="state ${r.hit ? "on" : "stop"}">${r.hit ? "překonán" : "nepřekonán"} (${num(r.target)})</span>`}</td>
    </tr>`).join("")}</tbody></table><p class="muted small">* někteří hráči už nejsou ve sbírce, jejich body chybí</p>`
    : `<p class="empty">Žádné odeslané sestavy.</p>`;
}

$("#evaluate").onclick = async () => {
  $("#evaluate").disabled = true; $("#status").textContent = "Stahuji skóre…";
  try { const d = await api("/api/calibration", {body: {}}); render(d);
    $("#status").textContent = `Vyhodnoceno ${d.evaluated} sestav.`; }
  catch (e) { $("#status").textContent = "Chyba: " + e.message; }
  finally { $("#evaluate").disabled = false; }
};
api("/api/calibration").then(render).catch(e => failed($("#stats"), e));
"""


def render() -> str:
    return page("Kalibrace", "mlb-calibration", BODY, SCRIPT)
