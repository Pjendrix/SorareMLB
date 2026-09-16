"""Přihlašovací stránka pro 2FA.

Sorare JWT platí 30 dní, takže kód z autentikátoru se zadává jednou za měsíc.
Heslo se sem nezadává — bere se z env proměnných, aby se nikde neukládalo
v prohlížeči ani neputovalo formulářem.
"""
from .web.layout import page

_BODY = """
<div class="head">
  <h1 class="display">Přihlášení</h1>
  <p class="lede">Token platí 30 dní, kód z autentikátoru stačí zadat jednou za měsíc. Heslo se bere z nastavení serveru.</p>
</div>
<section>
  <div class="split">
    <h2 class="label">Stav</h2>
    <div>
      <div class="big" id="status">…</div>
      <div id="step1" style="margin-top:30px"><button class="btn" id="start">Přihlásit k Sorare</button></div>
      <div id="step2" hidden style="margin-top:30px">
        <label class="label" for="code">Kód z autentikátoru</label>
        <div class="row">
          <input id="code" inputmode="numeric" autocomplete="one-time-code" maxlength="6"
                 placeholder="000000" style="font-size:34px;width:9ch;letter-spacing:.2em">
          <button class="btn" id="verify">Ověřit kód</button>
          <button class="btn" id="cancel">Zpět</button>
        </div>
      </div>
      <p id="msg"></p>
    </div>
  </div>
</section>
"""

_SCRIPT = r"""
const show = (t) => { $("#msg").textContent = t || ""; };
async function refreshStatus() {
  const d = await api("/api/auth/status");
  $("#status").textContent = d.authenticated
    ? `${d.nickname || d.user_slug}, token platí ${d.days_left} dní`
    : "Nepřihlášen";
}
$("#start").onclick = async () => {
  $("#start").disabled = true; show("");
  try {
    const d = await api("/api/auth/start", {body: {}});
    if (d.state === "otp_required") {
      $("#step1").hidden = true; $("#step2").hidden = false; $("#code").focus();
      show("Opiš šestimístný kód ze svého autentikátoru.");
    } else { show("Přihlášeno."); refreshStatus(); }
  } catch (e) { show("Přihlášení selhalo: " + e.message); }
  finally { $("#start").disabled = false; }
};
$("#verify").onclick = async () => {
  const code = $("#code").value.replace(/\D/g, "");
  if (code.length !== 6) { show("Kód musí mít šest číslic."); return; }
  $("#verify").disabled = true;
  try {
    const d = await api("/api/auth/otp", {body: {code}});
    show(`Přihlášeno jako ${d.nickname || d.user_slug}.`);
    $("#step2").hidden = true; $("#step1").hidden = false; $("#code").value = "";
    refreshStatus();
  } catch (e) { show("Kód nebyl přijat: " + e.message); }
  finally { $("#verify").disabled = false; }
};
$("#code").onkeydown = (e) => { if (e.key === "Enter") $("#verify").click(); };
$("#cancel").onclick = () => { $("#step2").hidden = true; $("#step1").hidden = false; show(""); };
refreshStatus();
"""

LOGIN_PAGE = page("Přihlášení", "settings", _BODY, _SCRIPT)
