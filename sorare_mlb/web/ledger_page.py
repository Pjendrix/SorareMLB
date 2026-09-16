"""Bilance: kolik jsi do Sorare vložil, utratil, vybral a vyhrál."""
from .layout import page

BODY = """
<div class="head">
  <h1 class="display">Bilance</h1>
  <p class="lede">Vklady, výběry, nákupy a prodeje karet za celou dobu účtu. Co Sorare nevrátí, doplníš ručně.</p>
  <div class="row" style="margin-top:30px">
    <button class="btn" id="sync">Stáhnout nové platby</button>
    <button class="btn" id="full">Stáhnout celou historii</button>
  </div>
  <p class="muted" id="stamp"></p>
</div>

<section>
  <h2 class="label">Výsledek</h2>
  <div class="grid g3" id="result"></div>
</section>

<section>
  <h2 class="label">Pohyby</h2>
  <div class="grid g4" id="flows"></div>
  <p class="muted" id="notes"></p>
</section>

<section>
  <div class="split">
    <div>
      <h2 class="label">Po letech</h2>
    </div>
    <div class="scroll" id="years"></div>
  </div>
</section>

<section>
  <div class="split">
    <div>
      <h2 class="label">Po měsících</h2>
      <p class="muted">Plná čára vklady, přerušovaná výběry, tečkovaná nákupy.</p>
    </div>
    <div class="cell paper-grid" style="border:1px solid var(--ash)">
      <canvas id="chart" height="260" role="img" aria-label="Pohyby peněz po měsících"></canvas>
      <div id="chart-empty" class="empty" hidden>Zatím žádné pohyby.</div>
    </div>
  </div>
</section>

<section>
  <div class="split">
    <div>
      <h2 class="label">Ruční záznam</h2>
      <p class="muted">Pro platby, které API nevrátí, třeba nákup kartou přímo v aplikaci.</p>
    </div>
    <div class="row">
      <label class="small" for="m-cat">Typ</label>
      <select id="m-cat"></select>
      <label class="small" for="m-eur">Částka €</label>
      <input id="m-eur" type="number" step="0.01" min="0" style="width:9ch">
      <label class="small" for="m-date">Datum</label>
      <input id="m-date" type="date">
      <label class="small" for="m-note">Poznámka</label>
      <input id="m-note" type="text" maxlength="80">
      <button class="btn" id="m-add">Přidat záznam</button>
    </div>
  </div>
</section>

<section>
  <div class="split">
    <h2 class="label">Záznamy</h2>
    <div>
      <div class="row" style="margin-bottom:19px">
        <label class="small" for="f-cat">Typ</label>
        <select id="f-cat"><option value="">Všechny</option></select>
      </div>
      <div class="scroll" id="entries"></div>
    </div>
  </div>
</section>
"""

SCRIPT = r"""
let DATA = null, chartRef = null;
const money = (v) => v ? num(v, 2) + " €" : "—";
const signed = (v) => (v > 0 ? "+" : v < 0 ? "−" : "") + num(Math.abs(v || 0), 2) + " €";

function cell(label, value, note) {
  return `<div class="cell"><div class="label">${label}</div><div class="big">${value}</div>${note ? `<p class="muted">${note}</p>` : ""}</div>`;
}

function render(d) {
  DATA = d;
  const h = d.headline;
  const bal = d.balance && d.balance.values ? Object.entries(d.balance.values).filter(([k, v]) => v != null) : [];
  $("#stamp").textContent = !d.available
    ? `Sorare historii plateb ve schématu nenabízí (${d.reason || "neznámý důvod"}). Můžeš použít ruční záznamy.`
    : `Zdroj: ${d.sources.join(", ")}. ${d.count} záznamů${d.first_date ? `, nejstarší ${day(d.first_date)}` : ""}.`;

  $("#result").innerHTML =
    cell("Hotovost", signed(h.cash_result), "vybráno minus vloženo") +
    cell("Trh s kartami", signed(h.market_result), "prodeje minus nákupy a poplatky") +
    cell("Výhry v penězích", money(h.rewards_money), "ze stránky Výhry");

  $("#flows").innerHTML =
    cell("Vloženo", money(h.deposited)) +
    cell("Vybráno", money(h.withdrawn)) +
    cell("Utraceno za karty", money(h.spent)) +
    cell("Prodeje karet", money(h.sold));

  const notes = [];
  if (h.fees) notes.push(`Poplatky ${money(h.fees)}.`);
  if (h.refunds) notes.push(`Vrácení ${money(h.refunds)}.`);
  if (h.has_usd) notes.push("Část plateb je v USD, v součtech je počítaná 1:1 s eurem.");
  const eth = Object.entries(h.eth || {});
  if (eth.length) notes.push("V ETH: " + eth.map(([k, v]) => `${esc(d.categories_map[k])} ${num(v, 4)} ETH`).join(", ") + ".");
  if (bal.length) notes.push("Zůstatek podle Sorare: " + bal.map(([k, v]) => `${esc(k)} ${esc(v)}`).join(", ") + ".");
  $("#notes").textContent = notes.join(" ");

  const cols = ["deposit", "withdrawal", "purchase", "sale", "fee"];
  $("#years").innerHTML = d.years.length ? `<table><thead><tr><th>Rok</th>${cols.map(c => `<th class="num">${esc(d.categories_map[c])}</th>`).join("")}</tr></thead><tbody>
    ${d.years.map(y => `<tr><td>${esc(y.year)}</td>${cols.map(c => `<td class="num">${money(y[c])}</td>`).join("")}</tr>`).join("")}</tbody></table>`
    : `<div class="empty">Žádné pohyby.</div>`;

  const cat = $("#f-cat"), mcat = $("#m-cat");
  if (cat.options.length === 1) {
    for (const [k, v] of Object.entries(d.categories_map)) {
      cat.insertAdjacentHTML("beforeend", `<option value="${k}">${esc(v)}</option>`);
      mcat.insertAdjacentHTML("beforeend", `<option value="${k}">${esc(v)}</option>`);
    }
  }
  renderEntries();

  const months = d.months;
  $("#chart-empty").hidden = !!months.length;
  $("#chart").hidden = !months.length;
  if (window.Chart && months.length) {
    if (chartRef) chartRef.destroy();
    Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
    Chart.defaults.color = "#1d1d1d";
    const line = (label, key, dash) => ({label, data: months.map(m => m[key] || 0), borderColor: "#1d1d1d",
      borderDash: dash, borderWidth: 1.5, pointRadius: 0});
    chartRef = new Chart($("#chart"), {
      type: "line",
      data: {labels: months.map(m => m.month), datasets: [
        line("Vklady", "deposit", []), line("Výběry", "withdrawal", [6, 4]), line("Nákupy", "purchase", [1, 3])]},
      options: {responsive: true, maintainAspectRatio: false, interaction: {mode: "index", intersect: false},
        plugins: {legend: {labels: {boxWidth: 18, boxHeight: 1}}},
        scales: {x: {grid: {color: "#e5e4e0"}}, y: {grid: {color: "#e5e4e0"}, title: {display: true, text: "€"}}}}
    });
  }
}

function renderEntries() {
  const f = $("#f-cat").value;
  const rows = DATA.entries.filter(e => !f || e.category === f);
  $("#entries").innerHTML = rows.length ? `<table><thead><tr><th>Datum</th><th>Typ</th><th>Popis ze Sorare</th>
    <th class="num">Částka</th><th>Stav</th><th></th></tr></thead><tbody>
    ${rows.map(e => `<tr><td>${day(e.date)}</td><td>${esc(DATA.categories_map[e.category])}</td>
      <td class="muted">${esc(e.type || "—")}</td>
      <td class="num">${e.eur ? money(e.eur) : e.usd ? num(e.usd, 2) + " $" : e.eth ? num(e.eth, 4) + " ETH" : "—"}</td>
      <td class="muted">${esc(e.status || "")}</td>
      <td>${e.source === "manual" ? `<button class="btn" data-del="${esc(e.id)}">Smazat</button>` : ""}</td></tr>`).join("")}
    </tbody></table>${DATA.count > DATA.entries.length ? `<p class="muted">Zobrazeno posledních ${DATA.entries.length} z ${DATA.count}.</p>` : ""}`
    : `<div class="empty">Žádné záznamy.</div>`;
  document.querySelectorAll("[data-del]").forEach(b => b.onclick = async () => {
    if (!confirm("Smazat ruční záznam?")) return;
    await api("/api/ledger/manual/" + encodeURIComponent(b.dataset.del), {method: "DELETE"});
    load();
  });
}

async function load() {
  try { render(await api("/api/ledger")); }
  catch (e) { failed($("#result"), e); }
}

$("#f-cat").oninput = () => DATA && renderEntries();
$("#m-date").value = new Date().toISOString().slice(0, 10);
$("#m-add").onclick = async () => {
  const eur = parseFloat($("#m-eur").value);
  if (!(eur > 0)) { alert("Zadej částku větší než nula."); return; }
  try {
    await api("/api/ledger/manual", {body: {category: $("#m-cat").value, eur, date: $("#m-date").value, note: $("#m-note").value}});
    $("#m-eur").value = ""; $("#m-note").value = "";
    load();
  } catch (e) { alert("Záznam se neuložil: " + e.message); }
};
$("#sync").onclick = async () => {
  $("#sync").disabled = true;
  try {
    const r = await api("/api/ledger/sync", {body: {}});
    $("#stamp").textContent = `Staženo ${r.fetched} záznamů, nových ${r.new}.`;
    load();
  } catch (e) { $("#stamp").textContent = "Stahování selhalo: " + e.message; }
  finally { $("#sync").disabled = false; }
};
$("#full").onclick = async () => {
  await runBackfill("/api/ledger/sync", $("#full"), $("#stamp"), false);
  load();
};
load();
"""


def render() -> str:
    return page("Bilance", "ledger", BODY, SCRIPT, charts=True)
