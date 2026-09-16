"""Výhry: peníze, Essence, karty — po soutěžích, gameweecích, hráčích a kartách."""
from .layout import page

BODY = """
<div class="head">
  <h1 class="display">Výhry</h1>
  <p class="lede">Co ti přinesly soutěže, gameweeky, hráči a jednotlivé karty. Archiv se doplňuje při každé synchronizaci, takže historie roste i za hranici toho, co vrací Sorare.</p>
  <div class="row" style="margin-top:30px" role="tablist" aria-label="Sport">
    <button class="btn" data-sport="" aria-pressed="true">Vše</button>
    <button class="btn" data-sport="mlb" aria-pressed="false">MLB</button>
    <button class="btn" data-sport="football" aria-pressed="false">Fotbal</button>
    <button class="btn" id="sync">Stáhnout nové výhry</button>
    <span class="muted small" id="stamp"></span>
  </div>
</div>

<section>
  <h2 class="label">Celkem</h2>
  <div class="grid g5" id="totals"></div>
</section>

<section>
  <div class="split">
    <div>
      <h2 class="label">Po gameweecích</h2>
      <p class="muted">Plná čára peníze, přerušovaná Essence.</p>
    </div>
    <div class="cell paper-grid" style="border:1px solid var(--ash)">
      <canvas id="chart" height="260" role="img" aria-label="Výhry po gameweecích"></canvas>
      <div id="chart-empty" class="empty" hidden>Zatím žádné výhry.</div>
    </div>
  </div>
</section>

<section>
  <div class="split">
    <div>
      <h2 class="label">Essence podle rarity</h2>
    </div>
    <div class="cell" style="border:1px solid var(--ash)" id="essence"></div>
  </div>
</section>

<section>
  <div class="split">
    <h2 class="label">Soutěže</h2>
    <div class="scroll" id="comps"></div>
  </div>
</section>

<section>
  <div class="split">
    <div>
      <h2 class="label">Karty</h2>
      <p class="muted" id="method"></p>
    </div>
    <div class="scroll" id="cards"></div>
  </div>
</section>

<section>
  <div class="split">
    <h2 class="label">Hráči</h2>
    <div class="scroll" id="players"></div>
  </div>
</section>

<section>
  <div class="split">
    <h2 class="label">Poslední ocenění</h2>
    <div class="scroll" id="recent"></div>
  </div>
</section>
"""

SCRIPT = r"""
let SPORT = new URLSearchParams(location.search).get("sport") || "";
let chartRef = null;
const SPORT_LABEL = {mlb: "MLB", football: "Fotbal"};
const eur = (v) => v ? num(v, 2) + " €" : "—";
const ess = (v) => v ? num(v, 0) : "—";

function cell(label, value, note) {
  return `<div class="cell"><div class="label">${label}</div><div class="big">${value}</div>${note ? `<p class="muted">${note}</p>` : ""}</div>`;
}

function itemText(i) {
  if (i.kind === "money") return eur(i.eur || i.usd);
  if (i.kind === "essence") return `${num(i.qty)} Essence ${esc(i.rarity || "")}${i.player ? `, ${esc(i.player)}` : ""}`;
  if (i.kind === "card") return `Karta ${esc(i.rarity || "")}${i.player ? `, ${esc(i.player)}` : ""}`;
  if (i.kind === "coins") return `${num(i.coins || i.qty)} coinů`;
  if (i.kind === "pack") return "Balíček karet";
  if (i.kind === "xp") return "XP";
  return esc(i.type || "Jiná odměna");
}

function render(d) {
  const t = d.totals;
  $("#stamp").textContent = d.sync ? `Staženo ${d.sync.fetched} umístění` : "";
  $("#totals").innerHTML =
    cell("Peníze", eur(t.money)) +
    cell("Essence", ess(t.essence)) +
    cell("Karty a balíčky", num(t.cards)) +
    cell("Coiny", num(t.coins)) +
    cell("Oceněná umístění", num(d.count));

  const er = Object.entries(d.essence_by_rarity || {});
  const max = Math.max(...er.map(e => e[1]), 1);
  $("#essence").innerHTML = er.length ? er.map(([k, v]) => `<div class="bar"><span>${esc(k)}</span>
    <span class="track"><b style="width:${v / max * 100}%"></b></span><span class="num">${num(v)}</span></div>`).join("")
    : `<div class="empty">Žádná Essence.</div>`;

  $("#comps").innerHTML = d.competitions.length ? `<table><thead><tr><th>Soutěž</th><th class="num">Ocenění</th>
    <th class="num">Peníze</th><th class="num">Essence</th><th class="num">Karty</th><th class="num">Ø body</th><th class="num">Nejlepší</th></tr></thead><tbody>
    ${d.competitions.map(c => `<tr><td>${esc(c.tournament)}</td><td class="num">${c.count}</td><td class="num">${eur(c.money)}</td>
      <td class="num">${ess(c.essence)}</td><td class="num">${c.cards || "—"}</td><td class="num">${num(c.avg_score, 1)}</td>
      <td class="num">${c.best ? num(c.best) + "." : "—"}</td></tr>`).join("")}</tbody></table>`
    : `<div class="empty">Žádná ocenění.</div>`;

  const share = d.essence_exact_share;
  $("#method").textContent = share == null ? "" : share >= 0.99
    ? "Essence je přiřazená přesně podle hráče z odměny."
    : `Přesně podle hráče je přiřazeno ${Math.round(share * 100)} % Essence. Zbytek a peníze jsou rozpočítané podle podílu karty na bodech sestavy (sloupec odhad).`;

  const table = (rows, first) => rows.length ? `<table><thead><tr><th>${first}</th>${first === "Karta" ? "<th>Rarita</th>" : ""}
    <th class="num">Nasazení</th><th class="num">Body</th><th class="num">Essence</th><th class="num">z toho odhad</th><th class="num">Peníze (odhad)</th></tr></thead><tbody>
    ${rows.map(r => `<tr><td>${esc(r.player)}</td>${first === "Karta" ? `<td>${esc(r.rarity || "—")}</td>` : ""}
      <td class="num">${r.appearances}</td><td class="num">${num(r.points, 0)}</td>
      <td class="num">${ess(r.essence_total)}</td><td class="num muted">${ess(r.essence_est)}</td><td class="num">${eur(r.money)}</td></tr>`).join("")}
    </tbody></table>` : `<div class="empty">Sorare u ocenění nevrátil karty sestavy.</div>`;
  $("#cards").innerHTML = table(d.cards, "Karta");
  $("#players").innerHTML = table(d.players, "Hráč");

  $("#recent").innerHTML = d.recent.length ? `<table><thead><tr><th>Sport</th><th>Soutěž</th><th>GW</th>
    <th class="num">Body</th><th class="num">Pořadí</th><th>Odměna</th></tr></thead><tbody>
    ${d.recent.map(r => `<tr><td>${SPORT_LABEL[r.sport] || "—"}</td><td>${esc(r.tournament)}</td><td>${esc(r.game_week ?? "—")}</td>
      <td class="num">${num(r.score, 1)}</td><td class="num">${r.ranking ? num(r.ranking) + "." : "—"}</td>
      <td>${r.items.map(itemText).join("<br>") || "—"}</td></tr>`).join("")}</tbody></table>`
    : `<div class="empty">Žádná ocenění.</div>`;

  const gws = d.game_weeks.filter(g => g.game_week != null);
  $("#chart-empty").hidden = !!gws.length;
  $("#chart").hidden = !gws.length;
  if (window.Chart && gws.length) {
    if (chartRef) chartRef.destroy();
    Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
    Chart.defaults.color = "#1d1d1d";
    chartRef = new Chart($("#chart"), {
      type: "line",
      data: {labels: gws.map(g => "GW " + g.game_week), datasets: [
        {label: "Peníze (€)", data: gws.map(g => g.money), borderColor: "#1d1d1d", borderWidth: 1.5, pointRadius: 2, pointBackgroundColor: "#1d1d1d", yAxisID: "y"},
        {label: "Essence", data: gws.map(g => g.essence), borderColor: "#1d1d1d", borderDash: [4, 4], borderWidth: 1.5, pointRadius: 2, yAxisID: "y1"},
      ]},
      options: {responsive: true, maintainAspectRatio: false, interaction: {mode: "index", intersect: false},
        plugins: {legend: {labels: {boxWidth: 18, boxHeight: 1}}},
        scales: {x: {grid: {color: "#e5e4e0"}}, y: {grid: {color: "#e5e4e0"}, title: {display: true, text: "€"}},
                 y1: {position: "right", grid: {display: false}, title: {display: true, text: "Essence"}}}}
    });
  }
}

async function load(refresh) {
  document.querySelectorAll("[data-sport]").forEach(b => b.setAttribute("aria-pressed", b.dataset.sport === SPORT));
  $("#sync").disabled = true;
  try {
    const q = new URLSearchParams();
    if (SPORT) q.set("sport", SPORT);
    if (refresh) q.set("refresh", "1");
    render(await api("/api/rewards?" + q));
  } catch (e) { failed($("#totals"), e); }
  finally { $("#sync").disabled = false; }
}

document.querySelectorAll("[data-sport]").forEach(b => b.onclick = () => {
  SPORT = b.dataset.sport;
  history.replaceState(null, "", SPORT ? "?sport=" + SPORT : location.pathname);
  load(false);
});
$("#sync").onclick = () => load(true);
load(false);
"""


def render() -> str:
    return page("Výhry", "rewards", BODY, SCRIPT, charts=True)
