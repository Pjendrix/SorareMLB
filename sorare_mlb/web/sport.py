"""Přehled sbírky jednoho sportu (MLB i fotbal sdílejí šablonu)."""
from html import escape

from .layout import page

BODY = """
<div class="head">
  <h1 class="display">{title}</h1>
  <p class="lede">{lede}</p>
  <div class="row" style="margin-top:30px">
    {actions}
    <button class="btn" id="refresh">Stáhnout znovu</button>
    <span class="muted small" id="stamp"></span>
  </div>
</div>

<section>
  <h2 class="label">Sbírka</h2>
  <div class="grid g4" id="stats">
    <div class="cell"><div class="label">Karet</div><div class="big" id="st-count">—</div></div>
    <div class="cell"><div class="label">Průměr L15</div><div class="big" id="st-l15">—</div></div>
    <div class="cell"><div class="label">Bez zápasu</div><div class="big" id="st-idle">—</div><p class="muted" id="st-idle-note"></p></div>
    <div class="cell"><div class="label">Sledované turnaje</div><div class="big" id="st-boards">—</div><p class="muted">se sestavou / otevřené</p></div>
  </div>
</section>

<section>
  <div class="split">
    <h2 class="label">Rozložení</h2>
    <div class="grid g2">
      <div class="cell"><div class="label">Rarita</div><div id="by-rarity"></div></div>
      <div class="cell"><div class="label">Pozice</div><div id="by-position"></div></div>
    </div>
  </div>
</section>

<section>
  <div class="split">
    <h2 class="label">Otevřené turnaje</h2>
    <div id="boards" class="skeleton">Načítám…</div>
  </div>
</section>

<section>
  <div class="split">
    <h2 class="label">Forma</h2>
    <div class="grid g2">
      <div class="cell"><div class="label">Ve formě (L5)</div><div id="in-form"></div></div>
      <div class="cell"><div class="label">Ochlazují se (L5 proti L15)</div><div id="cooling"></div></div>
    </div>
  </div>
</section>

<section>
  <div class="split">
    <div>
      <h2 class="label">Leží ladem</h2>
      <p class="muted">Karty bez zápasu déle než limit. Kandidáti na prodej nebo výměnu.</p>
    </div>
    <div id="idle"></div>
  </div>
</section>

<section>
  <h2 class="label">Všechny karty</h2>
  <div class="row" style="margin-bottom:19px">
    <label class="small" for="q">Hledat</label>
    <input id="q" type="search" placeholder="Hráč nebo tým">
    <label class="small" for="pos">Pozice</label>
    <select id="pos"><option value="">Všechny</option></select>
    <label class="small" for="rar">Rarita</label>
    <select id="rar"><option value="">Všechny</option></select>
  </div>
  <div class="scroll" id="all"></div>
</section>
"""

SCRIPT = r"""
const SPORT_KEY = "%(key)s";
let DATA = null;

function bars(obj) {
  const max = Math.max(...Object.values(obj), 1);
  return Object.entries(obj).map(([k, v]) => `<div class="bar">
    <span>${esc(k)}</span><span class="track"><b style="width:${v / max * 100}%%"></b></span>
    <span class="num">${v}</span></div>`).join("") || `<div class="empty">Žádné karty.</div>`;
}

function mini(rows, col) {
  if (!rows.length) return `<div class="empty">Nic k zobrazení.</div>`;
  return `<table><thead><tr><th>Hráč</th><th>Posledních 10</th><th class="num">${col}</th></tr></thead><tbody>
    ${rows.map(r => `<tr><td>${esc(r.player)}<div class="muted small">${esc(r.team || "")}</div></td>
      <td>${spark(r.scores)}</td>
      <td class="num">${col === "Trend" ? trend(r.trend) : num(r.l5, 1)}</td></tr>`).join("")}
  </tbody></table>`;
}

function renderAll() {
  const q = $("#q").value.toLowerCase(), pos = $("#pos").value, rar = $("#rar").value;
  const rows = DATA.cards.filter(r =>
    (!q || (r.player + " " + (r.team || "")).toLowerCase().includes(q)) &&
    (!pos || r.position === pos) && (!rar || r.rarity === rar));
  $("#all").innerHTML = `<table><thead><tr>
    <th>Hráč</th><th>Tým</th><th>Pozice</th><th>Rarita</th><th>Sezóna</th>
    <th>Forma</th><th class="num">L5</th><th class="num">L15</th><th class="num">Trend</th><th>Poslední zápas</th>
  </tr></thead><tbody>${rows.slice(0, 400).map(r => `<tr>
    <td>${esc(r.player)}</td><td>${esc(r.team || "—")}</td><td>${esc(r.position)}</td>
    <td><span class="tag">${esc(r.rarity)}</span></td><td>${esc(r.season ?? "—")}</td>
    <td>${spark(r.scores)}</td><td class="num">${num(r.l5, 1)}</td><td class="num">${num(r.l15, 1)}</td>
    <td class="num">${trend(r.trend)}</td><td>${day(r.last_game)}</td>
  </tr>`).join("")}</tbody></table>
  <p class="muted">${rows.length} z ${DATA.cards.length} karet${rows.length > 400 ? ", zobrazeno prvních 400" : ""}.</p>`;
}

function render(d) {
  DATA = d;
  $("#stamp").textContent = "Data z " + when(d.generated_at) + (d.truncated ? ", sbírka useknuta limitem stránek" : "");
  $("#st-count").textContent = num(d.count);
  $("#st-l15").textContent = num(d.avg_l15, 1);
  $("#st-idle").textContent = num(d.idle_count);
  $("#st-idle-note").textContent = `déle než ${d.idle_after_days} dní`;
  $("#by-rarity").innerHTML = bars(d.by_rarity);
  $("#by-position").innerHTML = bars(d.by_position);
  $("#in-form").innerHTML = mini(d.in_form, "L5");
  $("#cooling").innerHTML = mini(d.cooling, "Trend");
  $("#idle").innerHTML = d.idle.length ? `<table><thead><tr><th>Hráč</th><th>Rarita</th><th class="num">L15</th><th>Poslední zápas</th></tr></thead>
    <tbody>${d.idle.map(r => `<tr><td>${esc(r.player)}</td><td>${esc(r.rarity)}</td><td class="num">${num(r.l15, 1)}</td>
    <td>${r.days_since_game == null ? "nehrál" : `před ${r.days_since_game} dny`}</td></tr>`).join("")}</tbody></table>
    ${d.idle_count > d.idle.length ? `<p class="muted">a dalších ${d.idle_count - d.idle.length}</p>` : ""}`
    : `<div class="empty">Všechny karty v poslední době hrály.</div>`;

  const fill = (sel, values) => {
    const cur = sel.value;
    sel.innerHTML = `<option value="">Všechny</option>` + values.map(v => `<option>${esc(v)}</option>`).join("");
    sel.value = cur;
  };
  fill($("#pos"), Object.keys(d.by_position));
  fill($("#rar"), Object.keys(d.by_rarity));
  renderAll();
}

async function load(refresh) {
  $("#refresh").disabled = true;
  try { render(await api(`/api/overview/${SPORT_KEY}` + (refresh ? "?refresh=1" : ""))); }
  catch (e) { failed($("#all"), e); }
  finally { $("#refresh").disabled = false; }
}

async function loadBoards() {
  const el = $("#boards");
  el.classList.remove("skeleton");
  try {
    const up = await api("/api/upcoming");
    const rows = up.boards[SPORT_KEY] || [];
    const tracked = rows.filter(r => r.tracked);
    $("#st-boards").textContent = `${tracked.filter(r => r.mine).length} / ${tracked.length}`;
    el.innerHTML = rows.length ? `<div class="scroll"><table><thead><tr>
      <th>Turnaj</th><th>Rarita</th><th>GW</th><th>Uzávěrka</th><th class="num">Moje sestavy</th><th></th></tr></thead>
      <tbody>${rows.map(r => `<tr><td>${esc(r.name)}</td><td>${esc(r.rarity || "—")}</td>
        <td>${esc(r.game_week ?? "—")}</td><td>${when(r.cutoff)} <span class="muted">${countdown(r.cutoff)}</span></td>
        <td class="num">${r.mine ? `<span class="state on">${r.mine}</span>` : `<span class="state ${r.tracked ? "stop" : ""}">0</span>`}</td>
        <td>${r.tracked ? `<span class="tag ink">Sledovaný</span>` : ""}</td></tr>`).join("")}</tbody></table></div>`
      : `<div class="empty">Žádný otevřený turnaj.</div>`;
  } catch (e) { failed(el, e); }
}

$("#refresh").onclick = () => load(true);
["#q", "#pos", "#rar"].forEach(s => $(s).addEventListener("input", () => DATA && renderAll()));
load(false);
loadBoards();
"""


def render(key: str, active: str, title: str, lede: str, actions: str = "") -> str:
    body = BODY.format(title=escape(title), lede=escape(lede), actions=actions)
    return page(title, active, body, SCRIPT % {"key": key})
