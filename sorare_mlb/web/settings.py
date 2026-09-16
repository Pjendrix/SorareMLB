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
const yes = (v) => `<span class="state ${v ? "on" : ""}">${v ? "Nastaveno" : "Chybí"}</span>`;
(async () => {
  try {
    const h = await api("/api/health");
    $("#acc").textContent = h.auth.authenticated ? (h.auth.nickname || h.auth.user_slug) : "Nepřihlášen";
    $("#acc-note").textContent = h.auth.authenticated ? `Token platí ještě ${h.auth.days_left} dní.` : "Bez přihlášení nic nepoběží.";
    const el = $("#health"); el.classList.remove("skeleton");
    el.innerHTML = `${h.store_warning ? `<div class="notice">${esc(h.store_warning)}</div>` : ""}
      <table><thead><tr><th>Proměnná</th><th>Stav</th></tr></thead><tbody>
      <tr><td>Úložiště</td><td>${esc(h.store)}</td></tr>
      ${Object.entries(h.required).map(([k, v]) => `<tr><td>${esc(k)}</td><td>${yes(v)}</td></tr>`).join("")}
      ${Object.entries(h.optional).map(([k, v]) => `<tr><td>${esc(k)} <span class="muted">volitelné</span></td><td>${yes(v)}</td></tr>`).join("")}
      </tbody></table>`;
  } catch (e) { failed($("#health"), e); }
  try {
    const f = await api("/api/schema-features");
    const el = $("#schema"); el.classList.remove("skeleton");
    el.innerHTML = `<table><tbody>
      <tr><td>Příznak trezoru</td><td>${f.vault_field ? `<span class="state on"><code>${esc(f.vault_field)}</code></span>` : `<span class="state">Nenalezen</span>`}</td></tr>
      <tr><td>Výhry (rewardedRankings)</td><td>${f.rewards_available ? `<span class="state on">Dostupné</span>` : `<span class="state stop">${esc(f.rewards_reason)}</span>`}</td></tr>
      </tbody></table>
      <p><button class="btn" id="schema-refresh">Načíst schéma znovu</button></p>`;
    $("#schema-refresh").onclick = async () => { await api("/api/schema-features?refresh=1"); location.reload(); };
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
