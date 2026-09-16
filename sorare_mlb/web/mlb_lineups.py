"""Builder sestav MLB — původní jednostránkové UI v novém layoutu."""
from .layout import page

BODY = """
<div class="head">
  <h1 class="display">Sestavy<br>MLB</h1>
  <p class="lede">Hot Streaks a Challenger. Navrhni sestavy ke kontrole, nebo je nech rovnou odeslat.</p>
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
    </div>
    <div class="cell" style="border:1px solid var(--ash)"><pre class="log" id="log"></pre></div>
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
let timer = null, currentJob = null;

async function poll() {
  const d = await api("/api/status" + (currentJob ? "?job_id=" + currentJob : ""));
  render(d);
  if (JOB_RUNNING.includes(d.state)) timer = setTimeout(poll, 3000);
}

function render(d) {
  currentJob = d.job_id || currentJob;
  $("#badge").innerHTML = jobState(d.state);
  const meta = [];
  if (d.updated_at) meta.push("aktualizováno " + when(d.updated_at));
  if (d.progress_remaining) meta.push(`zbývá ${d.progress_remaining} hráčů`);
  if (d.mode) meta.push(d.mode === "auto" ? "režim automatické odeslání" : "režim jen návrh");
  $("#meta").textContent = meta.join(", ");
  $("#log").textContent = (d.steps || []).join("\n") + (d.error ? "\n\n" + d.error : "");

  const lineups = d.lineups || [];
  $("#lineups-wrap").hidden = !lineups.length;
  $("#lineups").innerHTML = lineups.map(lu => `<div class="cell">
    <div class="label">${esc(lu.tournament_name)} #${lu.index + 1}</div>
    <div class="big">${num(lu.total_projected)}</div><p class="muted">projekce bodů</p>
    <table><thead><tr><th>Slot</th><th>Hráč</th><th>Tým</th><th class="num">Proj.</th></tr></thead><tbody>
    ${lu.slots.map(s => `<tr><td>${esc(s.slot)}</td><td>${esc(s.player)}</td>
      <td class="muted">${esc(s.team || "—")}</td><td class="num">${num(s.projected)}</td></tr>`).join("")}
    </tbody></table></div>`).join("");

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

async function start(mode) {
  clearTimeout(timer);
  currentJob = null;
  ["#propose", "#auto"].forEach(s => $(s).disabled = true);
  try { currentJob = (await api("/api/build", {body: {mode}})).job_id; poll(); }
  catch (e) { alert("Běh se nespustil: " + e.message); }
  finally { ["#propose", "#auto"].forEach(s => $(s).disabled = false); }
}

$("#propose").onclick = () => start("propose");
$("#auto").onclick = () => { if (confirm("Sestavy se po dokončení rovnou odešlou na Sorare. Pokračovat?")) start("auto"); };
$("#refresh").onclick = () => { clearTimeout(timer); poll(); };
poll().catch(e => failed($("#log"), e));
"""


def render() -> str:
    return page("Sestavy MLB", "mlb-lineups", BODY, SCRIPT)
