"""Placeholder pro fotbalové sestavy.

Už teď ukazuje, co jde číst (otevřené turnaje), a jasně říká, co chybí.
"""
from .layout import page

BODY = """
<div class="head">
  <h1 class="display">Sestavy<br>fotbal</h1>
  <p class="lede">Automatické skládání fotbalových sestav zatím není hotové. Níž vidíš otevřené turnaje a to, co ještě chybí.</p>
</div>

<section>
  <div class="split">
    <h2 class="label">Plánovaný formát</h2>
    <div class="grid g5">
      {slots}
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
    <h2 class="label">Co zbývá</h2>
    <table><tbody>
      <tr><td><span class="state on">Čtení sbírky a turnajů</span></td><td class="muted">Hotovo, viz přehled Fotbal.</td></tr>
      <tr><td><span class="state on">Rozhraní sportu v kódu</span></td><td class="muted">FootballAdapter, metody pro sestavy zatím hlásí „nepodporováno“.</td></tr>
      <tr><td><span class="state">Projekce bodů</span></td><td class="muted">Forma, minuty, soupeř, pravděpodobnost nástupu.</td></tr>
      <tr><td><span class="state">Optimalizátor</span></td><td class="muted">Sloty GK, DEF, MID, FWD, Extra, kapitán, limity turnaje.</td></tr>
      <tr><td><span class="state">Kontrola a odeslání</span></td><td class="muted">Stejná mutace jako MLB, jen s kapitánem.</td></tr>
      <tr><td><span class="state">Zapnutí v configu</span></td><td class="muted"><code>sports.football.lineups_enabled: true</code></td></tr>
    </tbody></table>
  </div>
</section>
"""

SCRIPT = r"""
(async () => {
  const el = $("#boards");
  el.classList.remove("skeleton");
  try {
    const up = await api("/api/upcoming");
    const rows = up.boards.football || [];
    el.innerHTML = rows.length ? `<div class="scroll"><table><thead><tr><th>Turnaj</th><th>Rarita</th>
      <th>Uzávěrka</th><th class="num">Moje sestavy</th></tr></thead><tbody>
      ${rows.map(r => `<tr><td>${esc(r.name)}</td><td>${esc(r.rarity || "—")}</td>
        <td>${when(r.cutoff)} <span class="muted">${countdown(r.cutoff)}</span></td>
        <td class="num">${r.mine}</td></tr>`).join("")}</tbody></table></div>
      <p><a class="btn" href="https://sorare.com/football" target="_blank" rel="noopener">Sestavit na Sorare</a></p>`
      : `<div class="empty">Žádný otevřený fotbalový turnaj.</div>`;
  } catch (e) { failed(el, e); }
})();
"""


def render(slots: list[str]) -> str:
    cells = "".join(
        f'<div class="cell paper-grid" style="min-height:160px"><div class="label">{s}</div>'
        f'<div class="muted">Prázdné</div></div>'
        for s in slots
    )
    return page("Sestavy fotbal", "football-lineups", BODY.replace("{slots}", cells), SCRIPT)
