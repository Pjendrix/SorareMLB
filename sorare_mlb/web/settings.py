"""Nastavení a diagnostika."""
from .layout import page

BODY = """
<div class="head">
  <h1 class="display">Nastavení</h1>
  <p class="lede">Přihlášení, stav konfigurace a sledované turnaje. Změny v turnajích se dělají v config.yaml.</p>
</div>

<section>
  <div class="split">
    <h2 class="label">Sorare účet</h2>
    <div>
      <div class="big" id="acc">…</div>
      <p class="muted" id="acc-note"></p>
      <a class="btn" href="/login">Přihlásit nebo obnovit token</a>
    </div>
  </div>
</section>

<section>
  <div class="split">
    <h2 class="label">Konfigurace</h2>
    <div id="health" class="skeleton">Kontroluji…</div>
  </div>
</section>

<section>
  <div class="split">
    <div>
      <h2 class="label">Schéma Sorare</h2>
      <p class="muted">Co aplikace našla ve veřejném schématu. Obnovuje se jednou denně.</p>
    </div>
    <div id="schema" class="skeleton">Kontroluji…</div>
  </div>
</section>

<section>
  <div class="split">
    <h2 class="label">Sledované turnaje</h2>
    <div id="tracked" class="skeleton">Načítám…</div>
  </div>
</section>

<section>
  <div class="split">
    <div>
      <h2 class="label">Ladění</h2>
      <p class="muted">Endpointy chráněné secretem. Přidej <code>?secret=…</code>.</p>
    </div>
    <table><tbody>
      <tr><td><code>/api/schema?type=So5Lineup</code></td><td class="muted">Definice typu z veřejného schématu Sorare.</td></tr>
      <tr><td><code>/api/debug</code></td><td class="muted">Slugy leaderboardů a pozice ve sbírce.</td></tr>
      <tr><td><code>/api/starters</code></td><td class="muted">Ohlášení nadhazovači pro gameweek.</td></tr>
    </tbody></table>
  </div>
</section>
"""

SCRIPT = r"""
function diag(d) {
  if (!d) return "";
  if (!d.ok) return `<div class="notice">${esc(d.error)}</div>`;
  const list = (a) => a && a.length ? a.map(esc).join(", ") : "nic";
  return `<table><tbody>
    <tr><td>Velikost schématu</td><td>${num(d.sdl_bytes)} znaků, ${num(d.types)} typů</td></tr>
    <tr><td>CurrentUser: související pole</td><td class="muted">${list(d.current_user_matches)}</td></tr>
    <tr><td>User: související pole</td><td class="muted">${list(d.user_matches)}</td></tr>
    <tr><td>Karta (${esc(d.card_type)})</td><td class="muted">${list(d.card_matches)}</td></tr>
  </tbody></table>`;
}
const yes = (v) => `<span class="state ${v ? "on" : ""}">${v ? "Nastaveno" : "Chybí"}</span>`;
(async () => {
  try {
    const h = await api("/api/health");
    $("#acc").textContent = h.auth.authenticated ? (h.auth.nickname || h.auth.user_slug) : "Nepřihlášen";
    $("#acc-note").textContent = h.auth.authenticated ? `Token platí ještě ${h.auth.days_left} dní.` : "Bez přihlášení nic nepoběží.";
    const el = $("#health"); el.classList.remove("skeleton");
    el.innerHTML = `${h.store_warning ? `<div class="notice">${esc(h.store_warning)}</div>` : ""}
      <table><thead><tr><th>Proměnná</th><th>Stav</th></tr></thead><tbody>
      <tr><td>Verze aplikace</td><td><code>${esc(h.app_version || "neznámá")}</code></td></tr>
      <tr><td>Úložiště</td><td>${esc(h.store)}</td></tr>
      ${Object.entries(h.required).map(([k, v]) => `<tr><td>${esc(k)}</td><td>${yes(v)}</td></tr>`).join("")}
      ${Object.entries(h.optional).map(([k, v]) => `<tr><td>${esc(k)} <span class="muted">volitelné</span></td><td>${yes(v)}</td></tr>`).join("")}
      </tbody></table>`;
  } catch (e) { failed($("#health"), e); }
  try {
    const f = await api("/api/schema-features");
    const el = $("#schema"); el.classList.remove("skeleton");
    el.innerHTML = `<table><tbody>
      <tr><td>Bonus karty (power)</td><td>${f.power_field ? `<span class="state on"><code>${esc(f.power_field)}</code></span>` : `<span class="state">Nenalezen — projekce bez bonusu</span>`}</td></tr>
      <tr><td>ID sestavy v mutaci</td><td>${f.lineup_id_field ? `<span class="state on"><code>${esc(f.lineup_id_field)}</code></span>` : `<span class="state">Nenalezeno — přestavění před uzávěrkou nepřepíše odeslané</span>`}</td></tr>
      <tr><td>Příznak trezoru</td><td>${f.vault_field ? `<span class="state on"><code>${esc(f.vault_field)}</code></span>` : `<span class="state">Nenalezen</span>`}</td></tr>
      <tr><td>Výhry (rewardedRankings)</td><td>${f.rewards_available ? `<span class="state on">Dostupné</span>` : `<span class="state stop">${esc(f.rewards_reason)}</span>`}</td></tr>
      <tr><td>Historie plateb</td><td>${f.ledger_available ? `<span class="state on">${f.ledger_sources.map(esc).join(", ")}</span>` : `<span class="state stop">${esc(f.ledger_reason)}</span>`}</td></tr>
      </tbody></table>
      ${diag(f.diagnostics)}
      <p class="muted">Ukázka surových plateb pro doladění: <code>/api/ledger/debug</code>. Celý výpis pro kontrolu: <a class="link" href="/api/schema-features">/api/schema-features</a></p>
      <p><button class="btn" id="schema-refresh">Načíst schéma znovu</button></p>`;
    $("#schema-refresh").onclick = async () => {
      $("#schema-refresh").disabled = true;
      try { await api("/api/schema-features?refresh=1"); location.reload(); }
      catch (e) { alert("Schéma se nenačetlo: " + e.message); $("#schema-refresh").disabled = false; }
    };
  } catch (e) { failed($("#schema"), e); }
  try {
    const c = await api("/api/config-summary");
    const el = $("#tracked"); el.classList.remove("skeleton");
    el.innerHTML = `<table><thead><tr><th>Sport</th><th>Turnaj</th><th>Slug obsahuje</th><th>Rarita</th><th class="num">Sestav</th></tr></thead><tbody>
      ${c.tournaments.map(t => `<tr><td>${esc(t.sport)}</td><td>${esc(t.name)}</td><td><code>${esc(t.slug_contains)}</code></td>
      <td>${esc(t.rarity || "—")}</td><td class="num">${esc(t.max_lineups ?? "—")}</td></tr>`).join("")}</tbody></table>
      <p class="muted">Sporty: ${c.sports.map(s => `${esc(s.label)} (${s.enabled ? "zapnuto" : "vypnuto"}, sestavy ${s.lineups ? "ano" : "ne"})`).join(", ")}</p>`;
  } catch (e) { failed($("#tracked"), e); }
})();
"""


def render() -> str:
    return page("Nastavení", "settings", BODY, SCRIPT)
