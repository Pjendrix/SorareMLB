"""Builder sestav MLB.

Každý slot ukazuje projekci, rozsah floor–ceiling, počet zápasů v gameweeku,
odznaky (start, bonus karty) a proč byl hráč vybrán. U navržených sestav jde
kartu prohodit za některého z náhradníků; kontrola se pak spustí znovu.
"""
from .layout import page

BODY = """
<style>
.slot-row td { vertical-align: top; }
.range { position: relative; height: 6px; background: var(--parchment); margin-top: 6px; min-width: 90px; }
.range b { position: absolute; top: 0; bottom: 0; background: var(--ash); }
.range i { position: absolute; top: -3px; width: 2px; height: 12px; background: var(--ink); }
.why { font-size: 12px; color: var(--ink-soft); margin-top: 4px; }
.alts { margin-top: 6px; display: flex; gap: 6px; flex-wrap: wrap; }
.alts button { font: inherit; font-size: 12px; background: none; border: 1px solid var(--ash);
  border-radius: 10px; padding: 2px 8px; cursor: pointer; }
.alts button:hover { border-color: var(--ink); }
.events { list-style: none; margin: 0; padding: 0; max-height: 260px; overflow-y: auto; font-size: 13px; }
.events li { padding: 3px 0; border-bottom: 1px solid var(--parchment); }
.events li.warn { font-weight: 700; }
.events li.error { font-weight: 700; text-decoration: underline; }
.events li.detail { padding-left: 16px; color: var(--ink-soft); }
.events.compact li.detail { display: none; }
.events time { color: var(--ink-soft); margin-right: 8px; }
</style>
<div class="head">
  <h1 class="display">Sestavy<br>MLB</h1>
  <p class="lede">Hot Streaks a Challenger. Navrhni sestavy ke kontrole, nebo je nech rovnou odeslat.</p>
  <p class="muted" id="plan"></p>
  <div class="row" style="margin-top:30px">
    <button class="btn" id="propose">Navrhnout sestavy</button>
    <button class="btn" id="auto">Navrhnout a odeslat</button>
    <button class="btn" id="refresh">Obnovit stav</button>
  </div>
</div>

<section>
  <div class="split">
    <div>
      <h2 class="label">Běh</h2>
      <div class="big" id="badge">…</div>
      <p class="muted" id="meta"></p>
      <label class="small"><input type="checkbox" id="details"> podrobnosti v logu</label>
    </div>
    <div class="cell" style="border:1px solid var(--ash)"><ul class="events compact" id="log"></ul></div>
  </div>
</section>

<section id="lineups-wrap" hidden>
  <h2 class="label">Navržené sestavy</h2>
  <div class="grid g2" id="lineups"></div>
</section>

<section id="issues-wrap" hidden>
  <div class="split">
    <h2 class="label">Nálezy z kontroly</h2>
    <div id="issues"></div>
  </div>
</section>

<section id="submitted-wrap" hidden>
  <div class="split">
    <h2 class="label">Odesláno</h2>
    <div id="submitted"></div>
  </div>
</section>

<div class="row" id="actions" style="margin-top:46px"></div>
"""

SCRIPT = r"""
let timer = null, currentJob = null, last = null;
const TRIGGER = {"manual": "ručně", "cron": "cron", "tick-build": "automat (návrh)", "tick-recheck": "automat (přestavění)"};

async function poll() {
  const d = await api("/api/status" + (currentJob ? "?job_id=" + currentJob : ""));
  render(d);
  if (JOB_RUNNING.includes(d.state)) timer = setTimeout(poll, 3000);
}

async function loadPlan() {
  try {
    const p = await api("/api/tick/plan");
    if (!p.cutoff) { $("#plan").textContent = p.reason || ""; return; }
    const auto = p.cron_mode === "auto";
    const parts = [`Uzávěrka GW ${esc(p.game_week ?? "?")} ${countdown(p.cutoff)} (${when(p.cutoff)}).`];
    parts.push(`Automat postaví sestavy ${countdown(new Date(p.build_at * 1000).toISOString())}`
      + (auto ? `, přestaví je ${countdown(new Date(p.recheck_at * 1000).toISOString())}.` : " a pošle je ke schválení."));
    $("#plan").innerHTML = parts.join(" ");
  } catch (e) { $("#plan").textContent = ""; }
}

function rangeBar(s, scale) {
  if (s.floor == null || s.ceiling == null || !scale) return "";
  const pct = v => Math.max(0, Math.min(100, v / scale * 100));
  return `<div class="range" title="floor ${num(s.floor, 1)} – ceiling ${num(s.ceiling, 1)}">
    <b style="left:${pct(s.floor)}%;width:${pct(s.ceiling) - pct(s.floor)}%"></b>
    <i style="left:${pct(s.projected)}%"></i></div>`;
}

function badges(s) {
  const out = [];
  const notes = (s.notes || []).join(" ");
  if (/ohlášen[ýé] (start|\d)/.test(notes)) out.push(`<span class="tag ink">PP</span>`);
  if (/bez ohlášeného startu|start zatím neohlášen/.test(notes)) out.push(`<span class="tag">start?</span>`);
  if (s.games != null && s.games !== 1) out.push(`<span class="tag">${num(s.games, 1)}× zápas</span>`);
  const bonus = notes.match(/bonus karty \+(\d+)/);
  if (bonus) out.push(`<span class="tag">+${bonus[1]} %</span>`);
  return out.join(" ");
}

function lineupCard(lu, pos, state, alternatives) {
  const scale = Math.max(...lu.slots.map(s => s.ceiling || s.projected || 0), 1);
  const prob = lu.win_probability != null
    ? `<p class="muted">šance na práh ${num(lu.target_score)}: <b>${num(lu.win_probability * 100)} %</b></p>` : "";
  const editable = state === "NEEDS_REVIEW";
  return `<div class="cell">
    <div class="label">${esc(lu.tournament_name)} #${lu.index + 1}</div>
    <div class="big">${num(lu.total_projected)}</div>
    <p class="muted">projekce bodů${lu.sigma ? ` ± ${num(lu.sigma)}` : ""}</p>${prob}
    <div class="scroll"><table><thead><tr><th>Slot</th><th>Hráč</th><th class="num">Proj.</th></tr></thead><tbody>
    ${lu.slots.map(s => {
      const alts = (alternatives[`${pos}:${s.slot}`] || []);
      return `<tr class="slot-row"><td>${esc(s.slot)}</td>
      <td><b>${esc(s.player)}</b> <span class="muted">${esc(s.team || "")}</span> ${badges(s)}
        ${rangeBar(s, scale)}
        ${(s.notes || []).length ? `<div class="why">${esc(s.notes.join(" · "))}</div>` : ""}
        ${editable && alts.length ? `<div class="alts">${alts.map(a =>
          `<button data-pos="${pos}" data-slot="${esc(s.slot)}" data-card="${esc(a.card_slug)}"
            title="${esc(a.team || "")}">${esc(a.player)} ${a.delta >= 0 ? "+" : ""}${num(a.delta, 1)}</button>`).join("")}</div>` : ""}
      </td><td class="num">${num(s.projected, 1)}</td></tr>`;
    }).join("")}
    </tbody></table></div></div>`;
}

function renderLog(d) {
  const events = d.events && d.events.length ? d.events
    : (d.steps || []).map(t => ({t: "", level: t.includes("VAROVÁNÍ") ? "warn" : "info", text: t}));
  $("#log").innerHTML = events.map(e => `<li class="${esc(e.level)}">${e.t
    ? `<time>${esc(e.t.slice(11, 19))}</time>` : ""}${esc(e.text)}</li>`).join("")
    + (d.error ? `<li class="error">${esc(d.error)}</li>` : "");
  $("#log").scrollTop = $("#log").scrollHeight;
}

function render(d) {
  last = d;
  currentJob = d.job_id || currentJob;
  $("#badge").innerHTML = jobState(d.state);
  const meta = [];
  if (d.updated_at) meta.push("aktualizováno " + when(d.updated_at));
  if (d.mode) meta.push(d.mode === "auto" ? "režim automatické odeslání" : "režim jen návrh");
  if (d.trigger) meta.push("spustil " + (TRIGGER[d.trigger] || d.trigger));
  if (d.fixture && d.fixture.gameWeek) meta.push("GW " + d.fixture.gameWeek);
  $("#meta").textContent = meta.join(", ");
  renderLog(d);

  const lineups = d.lineups || [];
  $("#lineups-wrap").hidden = !lineups.length;
  $("#lineups").innerHTML = lineups.map((lu, i) => lineupCard(lu, i, d.state, d.alternatives || {})).join("");
  document.querySelectorAll(".alts button").forEach(b => b.onclick = () => swap(b));

  const issues = d.issues || [];
  $("#issues-wrap").hidden = !issues.length;
  $("#issues").innerHTML = `<table><thead><tr><th>Závažnost</th><th>Hráč</th><th>Sestava</th><th>Nález</th></tr></thead><tbody>
    ${issues.map(i => `<tr>
      <td><span class="state ${i.severity === "blocker" ? "stop" : "half"}">${i.severity === "blocker" ? "Blokující" : "Varování"}</span></td>
      <td>${esc(i.player)}</td><td class="muted">${esc(i.lineup)} / ${esc(i.slot)}</td>
      <td>${esc(i.message)}${i.suggested_replacement ? `<div class="muted">Náhrada: ${esc(i.suggested_replacement)}</div>` : ""}</td>
    </tr>`).join("")}</tbody></table>`;

  const submitted = d.submitted || [];
  $("#submitted-wrap").hidden = !submitted.length;
  $("#submitted").innerHTML = `<table><tbody>${submitted.map(s => `<tr>
    <td><span class="state ${s.ok ? "on" : "stop"}">${esc(s.tournament_name)} #${(s.index || 0) + 1}</span></td>
    <td class="muted">${s.ok ? "id " + esc(s.lineup_id || "?") : esc(s.error)}</td></tr>`).join("")}</tbody></table>`;

  const blockers = issues.filter(i => i.severity === "blocker").length;
  $("#actions").innerHTML = d.state === "NEEDS_REVIEW"
    ? `<button class="btn" id="confirm">${blockers ? "Odeslat i přes nálezy" : "Odeslat na Sorare"}</button>` : "";
  if (d.state === "NEEDS_REVIEW") {
    $("#confirm").onclick = async () => {
      if (blockers && !confirm(`Sestavy mají ${blockers} blokujících nálezů. Opravdu odeslat?`)) return;
      $("#confirm").disabled = true;
      try { await api("/api/submit", {body: {job_id: currentJob, force: blockers > 0}}); poll(); }
      catch (e) { alert("Odeslání se nespustilo: " + e.message); $("#confirm").disabled = false; }
    };
  }
}

async function swap(btn) {
  document.querySelectorAll(".alts button").forEach(b => b.disabled = true);
  try {
    await api("/api/swap", {body: {job_id: currentJob, lineup: Number(btn.dataset.pos),
      slot: btn.dataset.slot, card_slug: btn.dataset.card}});
    await poll();
  } catch (e) {
    alert("Záměna se nepovedla: " + e.message);
    document.querySelectorAll(".alts button").forEach(b => b.disabled = false);
  }
}

async function start(mode) {
  clearTimeout(timer);
  currentJob = null;
  ["#propose", "#auto"].forEach(s => $(s).disabled = true);
  try { currentJob = (await api("/api/build", {body: {mode}})).job_id; poll(); }
  catch (e) { alert("Běh se nespustil: " + e.message); }
  finally { ["#propose", "#auto"].forEach(s => $(s).disabled = false); }
}

$("#details").onchange = e => $("#log").classList.toggle("compact", !e.target.checked);
$("#propose").onclick = () => start("propose");
$("#auto").onclick = () => { if (confirm("Sestavy se po dokončení rovnou odešlou na Sorare. Pokračovat?")) start("auto"); };
$("#refresh").onclick = () => { clearTimeout(timer); poll(); loadPlan(); };
poll().catch(e => failed($("#log"), e));
loadPlan();
"""


def render() -> str:
    return page("Sestavy MLB", "mlb-lineups", BODY, SCRIPT)
