"""Přihlašovací stránka pro 2FA.

Sorare JWT platí 30 dní, takže kód z autentikátoru se zadává jednou za měsíc.
Heslo se sem nezadává — bere se z env proměnných, aby se nikde neukládalo
v prohlížeči ani neputovalo formulářem.
"""

LOGIN_PAGE = r"""<!doctype html>
<html lang="cs">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Přihlášení — Sorare MLB</title>
<style>
  :root {
    --bg:#0d1117; --panel:#161b22; --line:#262d38; --text:#e6edf3;
    --dim:#8b949e; --ok:#3fb950; --bad:#f85149; --accent:#388bfd;
  }
  * { box-sizing:border-box; }
  body {
    margin:0; background:var(--bg); color:var(--text); min-height:100vh;
    display:flex; align-items:center; justify-content:center; padding:20px;
    font:15px/1.55 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
  }
  .card {
    background:var(--panel); border:1px solid var(--line); border-radius:12px;
    padding:28px; width:100%; max-width:400px;
  }
  h1 { font-size:19px; margin:0 0 6px; }
  p.sub { color:var(--dim); font-size:14px; margin:0 0 22px; }
  label { display:block; font-size:13px; color:var(--dim); margin-bottom:7px; }
  input {
    width:100%; background:#0d1117; border:1px solid var(--line); border-radius:7px;
    color:var(--text); padding:12px 14px; font-size:22px; letter-spacing:.32em;
    text-align:center; font-family:ui-monospace, monospace;
  }
  input:focus { outline:none; border-color:var(--accent); }
  button {
    width:100%; margin-top:16px; background:var(--accent); color:#fff; border:0;
    border-radius:7px; padding:12px; font-size:15px; font-weight:500; cursor:pointer;
  }
  button:disabled { opacity:.45; cursor:not-allowed; }
  button.ghost { background:transparent; border:1px solid var(--line); color:var(--text); }
  .msg { margin-top:16px; font-size:14px; padding:11px 13px; border-radius:7px; }
  .msg.err { background:#2d1416; color:var(--bad); }
  .msg.ok  { background:#12261a; color:var(--ok); }
  .status { font-size:14px; color:var(--dim); margin-bottom:20px; }
  a { color:var(--accent); text-decoration:none; }
  .hide { display:none; }
</style>
</head>
<body>
<div class="card">
  <h1>Přihlášení k Sorare</h1>
  <p class="sub">Token platí 30 dní — kód budeš potřebovat jednou za měsíc.</p>

  <div id="status" class="status">zjišťuji stav…</div>

  <div id="step1">
    <button id="start">Začít přihlášení</button>
  </div>

  <div id="step2" class="hide">
    <label for="code">Kód z autentikátoru</label>
    <input id="code" inputmode="numeric" autocomplete="one-time-code"
           maxlength="6" placeholder="······">
    <button id="verify">Potvrdit</button>
    <button id="cancel" class="ghost">Zpět</button>
  </div>

  <div id="msg"></div>
  <p class="sub" style="margin-top:20px"><a href="/">← zpět na sestavy</a></p>
</div>

<script>
const $ = (id) => document.getElementById(id);

function show(kind, text) {
  $("msg").innerHTML = text ? `<div class="msg ${kind}">${text}</div>` : "";
}

async function post(url, body) {
  const r = await fetch(url, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body || {})
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || r.statusText);
  return data;
}

async function refreshStatus() {
  const d = await (await fetch("/api/auth/status")).json();
  $("status").textContent = d.authenticated
    ? `Přihlášen jako ${d.nickname || d.user_slug} · token platí ještě ${d.days_left} dní`
    : "Nepřihlášen — pipeline zatím nemůže běžet.";
}

$("start").onclick = async () => {
  $("start").disabled = true;
  show("", "");
  try {
    const d = await post("/api/auth/start");
    if (d.state === "otp_required") {
      $("step1").classList.add("hide");
      $("step2").classList.remove("hide");
      $("code").focus();
      show("ok", "Opiš kód ze svého autentikátoru.");
    } else {
      show("ok", "Přihlášeno.");
      refreshStatus();
    }
  } catch (e) {
    show("err", e.message);
  } finally {
    $("start").disabled = false;
  }
};

$("verify").onclick = async () => {
  const code = $("code").value.replace(/\D/g, "");
  if (code.length !== 6) { show("err", "Kód musí mít šest číslic."); return; }
  $("verify").disabled = true;
  try {
    const d = await post("/api/auth/otp", {code});
    show("ok", `Hotovo — přihlášen jako ${d.nickname || d.user_slug}.`);
    $("step2").classList.add("hide");
    $("step1").classList.remove("hide");
    $("code").value = "";
    refreshStatus();
  } catch (e) {
    show("err", e.message);
  } finally {
    $("verify").disabled = false;
  }
};

$("code").onkeydown = (e) => { if (e.key === "Enter") $("verify").click(); };
$("cancel").onclick = () => {
  $("step2").classList.add("hide");
  $("step1").classList.remove("hide");
  show("", "");
};

refreshStatus();
</script>
</body>
</html>
"""
