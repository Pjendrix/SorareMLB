"""Vercel entrypoint — FastAPI aplikace.

Endpointy:
    GET  /                webové rozhraní
    POST /api/build       spustí job (mode=auto|propose), vrátí job_id
    POST /api/continue    interní — navázání dalšího kroku (chráněno secretem)
    GET  /api/status      stav posledního / konkrétního jobu
    POST /api/submit      ručně odešle sestavy z jobu ve stavu NEEDS_REVIEW
    GET  /api/cron        volá cron (Vercel i GitHub Actions), chráněno secretem
    GET  /api/health      diagnostika konfigurace
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sorare_mlb import runner  # noqa: E402
from sorare_mlb.models import Config  # noqa: E402
from sorare_mlb.store import StoreUnavailable, get_store  # noqa: E402
from sorare_mlb.auth import AuthError, OtpRequired, complete_login, start_login, token_status  # noqa: E402
from sorare_mlb.login_ui import LOGIN_PAGE  # noqa: E402
from sorare_mlb.ui import PAGE  # noqa: E402

app = FastAPI(title="Sorare MLB Lineups", docs_url=None, redoc_url=None)


@app.middleware("http")
async def normalize_vercel_path(request: Request, call_next):
    """Srovná cestu, když ji Vercel přepíše na /api/index.

    Podle typu konfigurace (rewrites vs. routes) dorazí do funkce buď
    původní cesta, nebo cesta na samotný soubor. Tohle pojistí obojí, ať
    aplikace nevrací 404 kvůli detailu v vercel.json.
    """
    path = request.scope.get("path", "")
    if path in ("/api/index", "/api/index.py"):
        original = (
            request.headers.get("x-vercel-original-path")
            or request.headers.get("x-forwarded-uri")
            or "/"
        )
        request.scope["path"] = original.split("?")[0] or "/"
    return await call_next(request)

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.yaml"


def _config() -> Config:
    return Config.load(CONFIG_PATH)


def _require_auth() -> None:
    if not token_status()["authenticated"]:
        raise HTTPException(
            401, "Chybí platný Sorare token. Přihlas se na /login."
        )


def _check_secret(provided: str | None, request: Request) -> None:
    """Cron i interní navázání musí prokázat znalost secretu.

    Vercel cron posílá Authorization: Bearer <CRON_SECRET>, GitHub Actions
    posílá vlastní hlavičku. Přijímáme obojí.
    """
    expected = os.environ.get("INTERNAL_SECRET") or os.environ.get("CRON_SECRET")
    if not expected:
        raise HTTPException(500, "INTERNAL_SECRET není nastaven — endpoint je zakázaný.")

    auth = request.headers.get("authorization", "")
    bearer = auth[7:] if auth.lower().startswith("bearer ") else None

    if provided != expected and bearer != expected:
        raise HTTPException(401, "Neplatný secret.")


# --------------------------------------------------------------------- UI


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return PAGE


@app.get("/login", response_class=HTMLResponse)
def login_page() -> str:
    return LOGIN_PAGE


# --------------------------------------------------------------------- auth


@app.get("/api/auth/status")
def auth_status() -> JSONResponse:
    return JSONResponse(token_status())


@app.post("/api/auth/start")
def auth_start() -> JSONResponse:
    """První fáze přihlášení. Heslo bere z env, ne z formuláře."""
    try:
        token = start_login()
    except OtpRequired:
        return JSONResponse({"state": "otp_required"})
    except AuthError as exc:
        raise HTTPException(400, str(exc)) from exc
    except StoreUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — holá 500 se v prohlížeči špatně ladí
        raise HTTPException(500, f"{type(exc).__name__}: {exc}") from exc
    return JSONResponse(
        {"state": "authenticated", "nickname": token.nickname, "user_slug": token.user_slug}
    )


@app.post("/api/auth/otp")
def auth_otp(payload: dict) -> JSONResponse:
    code = (payload or {}).get("code", "")
    try:
        token = complete_login(code)
    except AuthError as exc:
        raise HTTPException(400, str(exc)) from exc
    except StoreUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"{type(exc).__name__}: {exc}") from exc
    return JSONResponse(
        {
            "state": "authenticated",
            "nickname": token.nickname,
            "user_slug": token.user_slug,
            "days_left": round(token.days_left, 1),
        }
    )


# --------------------------------------------------------------------- API


@app.post("/api/build")
def build(payload: dict | None = None, background: BackgroundTasks = None) -> JSONResponse:
    mode = (payload or {}).get("mode", "propose")
    if mode not in ("auto", "propose"):
        raise HTTPException(400, "mode musí být 'auto' nebo 'propose'")

    _require_auth()
    job = runner.create_job(mode=mode)
    background.add_task(runner.advance, job, _config())
    return JSONResponse({"job_id": job.id, "state": job.state})


@app.post("/api/continue")
def continue_job(
    payload: dict,
    request: Request,
    background: BackgroundTasks,
    x_internal_secret: str | None = Header(default=None),
) -> JSONResponse:
    _check_secret(x_internal_secret, request)
    job = runner.load(payload.get("job_id", ""))
    if job is None:
        raise HTTPException(404, "Job nenalezen (možná vypršel).")
    background.add_task(runner.advance, job, _config())
    return JSONResponse({"job_id": job.id, "state": job.state})


@app.get("/api/status")
def status(job_id: str | None = None) -> JSONResponse:
    job = runner.load(job_id) if job_id else runner.latest_job()
    if job is None:
        return JSONResponse({"state": "NONE", "message": "Zatím žádný běh."})
    return JSONResponse(
        {
            "job_id": job.id,
            "state": job.state,
            "mode": job.mode,
            "created_at": job.created_at,
            "updated_at": job.updated_at,
            "steps": job.steps[-15:],
            "error": job.error,
            "lineups": job.lineups,
            "issues": job.issues,
            "submitted": job.submitted,
            "progress_remaining": len(job.pending_player_slugs),
        }
    )


@app.post("/api/submit")
def submit(payload: dict | None, background: BackgroundTasks) -> JSONResponse:
    job = runner.load((payload or {}).get("job_id", "")) or runner.latest_job()
    if job is None:
        raise HTTPException(404, "Není co odesílat.")
    if job.state != "NEEDS_REVIEW":
        raise HTTPException(409, f"Job je ve stavu {job.state}, odeslat lze jen NEEDS_REVIEW.")
    if not job.lineups:
        raise HTTPException(409, "Job nemá žádné sestavy.")

    blockers = [i for i in job.issues if i.get("severity") == "blocker"]
    force = bool((payload or {}).get("force"))
    if blockers and not force:
        raise HTTPException(
            409,
            "Sestavy mají blokující nálezy. Odeslat je můžeš jen s force=true, "
            "ale radši je nejdřív oprav.",
        )

    job.state = "SUBMIT"
    job.log_step("Ruční odeslání potvrzeno")
    runner.save(job)
    background.add_task(runner.advance, job, _config())
    return JSONResponse({"job_id": job.id, "state": job.state})


@app.get("/api/cron")
def cron(
    request: Request,
    x_internal_secret: str | None = Header(default=None),
    background: BackgroundTasks = None,
) -> JSONResponse:
    _check_secret(x_internal_secret, request)

    # Pokud předchozí job ještě běží, nespouštíme druhý — jen ho postrčíme dál.
    previous = runner.latest_job()
    if previous and previous.state not in ("DONE", "FAILED", "NEEDS_REVIEW"):
        background.add_task(runner.advance, previous, _config())
        return JSONResponse({"job_id": previous.id, "state": previous.state, "resumed": True})

    status_ = token_status()
    if not status_["authenticated"]:
        # Cron nesmí tiše selhat — dej vědět, ať se stihne přihlásit.
        from sorare_mlb import notify

        notify.notify(
            "🔑 **Sorare token vypršel.** Přihlas se na /login, jinak se sestavy neodešlou."
        )
        raise HTTPException(401, "Chybí platný token, přihlas se na /login.")

    if status_["needs_login"]:
        from sorare_mlb import notify

        notify.notify(
            f"🔑 Sorare token platí ještě {status_['days_left']} dní — obnov ho na /login."
        )

    mode = os.environ.get("CRON_MODE", "auto")
    job = runner.create_job(mode=mode)
    background.add_task(runner.advance, job, _config())
    return JSONResponse({"job_id": job.id, "state": job.state, "mode": mode})


@app.get("/api/probe")
def probe(
    request: Request,
    secret: str | None = None,
    x_internal_secret: str | None = Header(default=None),
) -> JSONResponse:
    """Ověří, že dotazy v queries.py sedí na aktuální schéma Sorare.

    Baseballová část API se mění a hůř se dokumentuje než fotbalová — tohle
    spusť po každém delším výpadku, ideálně dřív než ti uteče gameweek.
    """
    _check_secret(x_internal_secret or secret, request)
    import traceback

    from sorare_mlb.client import SorareClient

    try:
        client = SorareClient(_config())
        schema = client.introspect_root()
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            {
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc()[-1500:],
            },
            status_code=200,
        )

    q = {f["name"] for f in (schema.get("queryType") or {}).get("fields", [])}
    m = {f["name"] for f in (schema.get("mutationType") or {}).get("fields", [])}

    # Vytáhneme jen to, co potřebujeme — schéma má přes milion znaků.
    def fields_of(type_name: str, contains: tuple[str, ...] = ()) -> list[str]:
        try:
            info = client.introspect_type(type_name)
        except Exception as exc:  # noqa: BLE001
            return [f"<chyba: {exc}>"]
        if not info:
            return ["<typ neexistuje>"]
        names = [f["name"] for f in (info.get("fields") or [])]
        if contains:
            names = [
                n for n in names
                if any(c.lower() in n.lower() for c in contains)
            ]
        return sorted(names)

    def input_fields_of(type_name: str) -> list[str]:
        try:
            info = client.introspect_type(type_name)
        except Exception as exc:  # noqa: BLE001
            return [f"<chyba: {exc}>"]
        if not info:
            return ["<typ neexistuje>"]
        return sorted(f["name"] for f in (info.get("inputFields") or []))

    relevant = ("baseball", "so5", "card", "fixture", "lineup", "competition")

    inputs = {
        name: input_fields_of(name)
        for name in (
            "So5LineupInput", "createOrUpdateSo5LineupInput",
            "submitSo5LineupInput", "So5AppearanceInput", "AppearanceInput",
            "createSo5LineupInput", "updateSo5LineupInput",
        )
    }

    types = {
        "Query": sorted(q),
        "So5Root": fields_of("So5Root"),
        "CurrentUser": fields_of("CurrentUser", relevant),
        "So5Fixture": fields_of("So5Fixture"),
        "So5Competition": fields_of("So5Competition"),
        "So5Lineup": fields_of("So5Lineup"),
        "Mutation": sorted(n for n in m if any(
            c in n.lower() for c in ("lineup", "so5", "baseball")
        )),
    }

    return JSONResponse(
        {
            "input_fields": inputs,
            "types": types,
            "hint": "Pošli tenhle výstup celý — podle něj se opraví queries.py.",
        }
    )


@app.get("/api/health")
def health() -> JSONResponse:
    required = ["SORARE_EMAIL", "SORARE_PASSWORD", "SORARE_API_KEY", "INTERNAL_SECRET"]
    auth = token_status()
    optional = [
        "KV_REST_API_URL", "KV_REST_API_TOKEN", "APP_BASE_URL",
        "DISCORD_WEBHOOK_URL", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
    ]
    store_kind = type(get_store()).__name__
    return JSONResponse(
        {
            "ok": all(os.environ.get(k) for k in required) and auth["authenticated"],
            "auth": auth,
            "store": store_kind,
            "store_warning": (
                "Chybí Redis. Přidej Upstash integraci ve Vercelu (Storage → "
                "Marketplace) a udělej redeploy — bez toho nefunguje přihlášení "
                "ani pipeline."
                if store_kind == "LocalStore" and os.environ.get("VERCEL") else None
            ),
            "required": {k: bool(os.environ.get(k)) for k in required},
            "optional": {k: bool(os.environ.get(k)) for k in optional},
            "config_found": CONFIG_PATH.exists(),
        }
    )
