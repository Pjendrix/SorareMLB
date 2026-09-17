"""Vercel entrypoint — FastAPI aplikace.

Endpointy:
    GET  /                dashboard (další stránky: /mlb, /mlb/sestavy, /mlb/historie,
                          /fotbal, /fotbal/sestavy, /fotbal/historie, /nastaveni)
    GET  /api/dashboard   souhrn pro hlavní stránku
    GET  /api/overview/{sport}   přehled sbírky (mlb|football), ?refresh=1
    GET  /api/upcoming    otevřené turnaje napříč sporty
    GET  /api/recent-lineups     nedávné sestavy ze Sorare
    GET  /api/history/{sport}    snímky sbírky a běhy automatu
    GET  /api/config-summary     sledované turnaje a sporty
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
from fastapi.responses import HTMLResponse, JSONResponse, Response

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sorare_mlb import runner  # noqa: E402
from sorare_mlb.models import Config  # noqa: E402
from sorare_mlb.store import StoreUnavailable, get_store  # noqa: E402
from sorare_mlb.auth import AuthError, OtpRequired, complete_login, start_login, token_status  # noqa: E402
from sorare_mlb.login_ui import LOGIN_PAGE  # noqa: E402
from sorare_mlb.ui import PAGES  # noqa: E402
from sorare_mlb import history, overview  # noqa: E402
from sorare_mlb.sports import ADAPTERS  # noqa: E402

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

# Zvyšuje se při každé změně API — podle toho se pozná, jestli Vercel
# opravdu nasadil nové soubory.
APP_VERSION = "2026.09.17-probes"

PUBLIC_PATHS = ("/api/cron", "/api/continue")


@app.middleware("http")
async def password_gate(request: Request, call_next):
    """Volitelné heslo na celou aplikaci (APP_PASSWORD).

    Aplikace ukazuje peníze a historii účtu, takže bez hesla je vidí každý,
    kdo zná URL. Cron a interní navázání mají vlastní secret.
    """
    password = os.environ.get("APP_PASSWORD")
    if not password or request.url.path.startswith(PUBLIC_PATHS):
        return await call_next(request)

    import base64
    import secrets as _secrets

    header = request.headers.get("authorization", "")
    if header.lower().startswith("basic "):
        try:
            _, _, given = base64.b64decode(header[6:]).decode().partition(":")
            if _secrets.compare_digest(given, password):
                return await call_next(request)
        except Exception:  # noqa: BLE001
            pass
    return Response(
        "Přihlášení vyžadováno.", status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="Sorare", charset="UTF-8"'},
    )


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


def _page_route(path: str, html: str) -> None:
    app.add_api_route(
        path, lambda: HTMLResponse(html), methods=["GET"], include_in_schema=False
    )


for _path, _html in PAGES.items():
    _page_route(_path, _html)


# --------------------------------------------------------------------- přehledy


def _sport_or_404(sport: str) -> str:
    if sport not in ADAPTERS:
        raise HTTPException(404, f"Neznámý sport {sport}. Povolené: {', '.join(ADAPTERS)}.")
    return sport


def _sorare_call(fn, *args, **kwargs):
    """Chyby Sorare vrací jako čitelnou 502 místo holé 500."""
    _require_auth()
    try:
        return fn(*args, **kwargs)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        name = "" if isinstance(exc, RuntimeError) else f"{type(exc).__name__}: "
        raise HTTPException(502, f"{name}{exc}"[:500]) from exc


@app.get("/api/dashboard")
def api_dashboard() -> JSONResponse:
    return JSONResponse(overview.dashboard(_config()))


@app.get("/api/overview/{sport}")
def api_overview(sport: str, refresh: bool = False) -> JSONResponse:
    _sport_or_404(sport)
    return JSONResponse(_sorare_call(overview.sport_overview, sport, _config(), refresh))


@app.get("/api/upcoming")
def api_upcoming() -> JSONResponse:
    return JSONResponse(_sorare_call(overview.upcoming, _config()))


@app.get("/api/recent-lineups")
def api_recent_lineups() -> JSONResponse:
    return JSONResponse(_sorare_call(overview.recent_lineups, _config()))


@app.get("/api/history/{sport}")
def api_history(sport: str, limit: int = 60) -> JSONResponse:
    _sport_or_404(sport)
    jobs = [j for j in history.jobs(limit) if j.get("sport") == sport]
    return JSONResponse({"snapshots": history.snapshots(sport), "jobs": jobs})


@app.get("/api/rewards")
def api_rewards(sport: str | None = None, refresh: bool = False) -> JSONResponse:
    """Souhrn výher z archivu. `refresh=1` nejdřív stáhne nová umístění."""
    from sorare_mlb import rewards

    if sport:
        _sport_or_404(sport)
    config = _config()
    sync = None
    if refresh or not rewards.archive(sport):
        sync = _sorare_call(rewards.sync, config, sport)
    summary = rewards.summarize(_visible_rewards(config, sport), sport)
    summary["sync"] = sync
    # Výhry z historie účtu (peníze, Essence, gemy) — funguje i bez umístění.
    from sorare_mlb import ledger

    rows = ledger.ARCHIVE.rows()
    if sport:
        rows = [r for r in rows if r.get("sport") in (None, sport)]
    summary["account"] = ledger.reward_view(rows)
    summary["account"]["synced"] = bool(rows)
    summary["backfill"] = rewards.backfill_status()
    return JSONResponse(summary)


def _visible_rewards(config, sport: str | None = None) -> list[dict]:
    from sorare_mlb import rewards

    # Fotbal: jen rarity, které hraješ (sports.football.board_rarities).
    return [
        r for r in rewards.archive(sport)
        if r["sport"] not in ADAPTERS or overview._rarity_ok(r["sport"], r.get("rarity"), config)
    ]


@app.get("/api/rewards/debug")
def api_rewards_debug() -> JSONResponse:
    """Jedna stránka výher pro každý sport: chyba nebo ukázka syrových dat."""
    from sorare_mlb import rewards
    from sorare_mlb.client import SorareClient
    from sorare_mlb.features import get_features

    _require_auth()
    feats = get_features().get("rewards") or {}
    out = {"app_version": APP_VERSION, "available": feats.get("available"),
           "reason": feats.get("reason"), "sports": {}}
    if not feats.get("available"):
        return JSONResponse(out)
    client = SorareClient(_config())
    for sp, enum in rewards.SPORT_ENUM.items():
        variables: dict = {"after": None}
        if feats.get("sport_arg"):
            variables["sport"] = [enum] if feats.get("sport_is_list") else enum
        try:
            body = client.execute(feats["query"], variables, operation_name="RewardedRankings",
                                  tolerate_errors=True)
            block = ((body.get("data") or {}).get(feats["root_key"]) or {}).get("rewardedRankings") or {}
            nodes = block.get("nodes") or []
            out["sports"][sp] = {
                "errors": [e.get("message") for e in body.get("errors") or []][:5],
                "count_on_page": len(nodes),
                "has_next": (block.get("pageInfo") or {}).get("hasNextPage"),
                "sample": nodes[:1],
                "normalized": rewards.normalize(nodes[0], sp) if nodes else None,
            }
        except Exception as exc:  # noqa: BLE001
            out["sports"][sp] = {"error": f"{type(exc).__name__}: {exc}"[:500]}
    out["archived"] = len(rewards.archive())

    # Průzkum dalších míst, kde Sorare výhry drží.
    from sorare_mlb.features import describe, probe_selection
    from sorare_mlb.schema import load_schema

    try:
        schema = load_schema()
    except Exception as exc:  # noqa: BLE001
        out["probe_error"] = str(exc)[:300]
        return JSONResponse(out)

    probes = {}
    try:
        body = client.execute(
            "query P { currentUser { rewardedRankings(first: 3) { nodes { id } } } }",
            operation_name="P", tolerate_errors=True,
        )
        probes["rewardedRankings_bez_sportu"] = body
    except Exception as exc:  # noqa: BLE001
        probes["rewardedRankings_bez_sportu"] = str(exc)[:300]

    for name in ("rewards", "unclaimedSo5Rewards", "podiumRankings", "myWheelRewards", "cardsReferralRewards"):
        fdef = schema.field("CurrentUser", name)
        if not fdef:
            continue
        target = schema.node_type("CurrentUser", name)
        info = {
            "definition": f"{fdef.name}: {fdef.type} args={fdef.arg_types}",
            "node_type": describe(schema, target),
        }
        required = [a for a, t in fdef.arg_types.items() if t.endswith("!")
                    and a not in ("first", "after", "last", "before")]
        if required:
            info["skipped"] = f"povinné argumenty {required}"
        else:
            selection = probe_selection(schema, target, depth=3)
            conn = schema.has(fdef.base, "nodes")
            args = "(first: 3)" if "first" in fdef.args else ""
            inner = f"nodes {{ {selection} }}" if conn else selection
            query = f"query Probe {{ currentUser {{ {name}{args} {{ {inner} }} }} }}"
            info["query"] = query
            try:
                info["result"] = client.execute(query, operation_name="Probe", tolerate_errors=True)
            except Exception as exc:  # noqa: BLE001
                info["result"] = f"{type(exc).__name__}: {exc}"[:600]
        probes[name] = info
    out["probes"] = probes
    return JSONResponse(out)


@app.post("/api/rewards/backfill")
def api_rewards_backfill(payload: dict | None = None) -> JSONResponse:
    """Jedna dávka stahování celé historie výher. Volej, dokud `done` není true."""
    from sorare_mlb import rewards

    if (payload or {}).get("reset"):
        rewards.reset_backfill()
    return JSONResponse(_sorare_call(rewards.sync, _config(), None, True))


# --------------------------------------------------------------------- bilance


@app.get("/api/ledger")
def api_ledger() -> JSONResponse:
    from sorare_mlb import ledger, rewards

    config = _config()
    reward_money = rewards.summarize(_visible_rewards(config))["totals"]["money"]
    rows = ledger.ARCHIVE.rows() + ledger.manual_entries()
    rows.sort(key=lambda r: str(r.get("date") or ""), reverse=True)
    data = ledger.summarize(rows, reward_money)
    feats = get_features_safe().get("ledger") or {}
    data["available"] = feats.get("available", False)
    data["reason"] = feats.get("reason")
    data["sources"] = [src["field"] for src in feats.get("sources") or []]
    data["categories_map"] = ledger.CATEGORIES
    return JSONResponse(data)


def get_features_safe() -> dict:
    from sorare_mlb.features import get_features

    try:
        return get_features()
    except Exception:  # noqa: BLE001
        return {}


@app.post("/api/ledger/sync")
def api_ledger_sync(payload: dict | None = None) -> JSONResponse:
    from sorare_mlb import ledger

    payload = payload or {}
    if payload.get("reset"):
        ledger.reset_backfill()
    return JSONResponse(_sorare_call(ledger.sync, _config(), bool(payload.get("full"))))


@app.post("/api/ledger/manual")
def api_ledger_manual(payload: dict) -> JSONResponse:
    from sorare_mlb import ledger

    try:
        entry = ledger.add_manual(
            payload.get("category", ""), float(payload.get("eur")),
            str(payload.get("date", "")), str(payload.get("note", "")),
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, f"Záznam nejde uložit: {exc}") from exc
    return JSONResponse(entry)


@app.delete("/api/ledger/manual/{entry_id}")
def api_ledger_manual_delete(entry_id: str) -> JSONResponse:
    from sorare_mlb import ledger

    if not ledger.delete_manual(entry_id):
        raise HTTPException(404, "Záznam nenalezen.")
    return JSONResponse({"deleted": entry_id})


@app.get("/api/ledger/debug")
def api_ledger_debug(refresh: bool = False) -> JSONResponse:
    """Nalezené zdroje plateb a ukázka syrových dat — pro doladění třídění."""
    from sorare_mlb import ledger
    from sorare_mlb.features import get_features

    feats = get_features(refresh=refresh).get("ledger") or {}
    return JSONResponse({
        "app_version": APP_VERSION,
        "candidates": feats.get("candidates"),
        "sources": [{k: src.get(k) for k in ("field", "role", "query")} for src in feats.get("sources") or []],
        "skipped": feats.get("skipped"),
        "balance_query": feats.get("balance_query"),
        "samples": ledger.samples(),
        "types_seen": sorted({
            f"{r.get('type')} → {r.get('category')}" for r in ledger.ARCHIVE.rows()
        })[:80],
    })


@app.get("/api/schema-features")
def api_schema_features(refresh: bool = False) -> JSONResponse:
    """Co aplikace ve schématu Sorare našla (trezor, odměny)."""
    from sorare_mlb.features import get_features

    feats = get_features(refresh=refresh)
    rewards_info = feats.get("rewards") or {}
    return JSONResponse(
        {
            "app_version": APP_VERSION,
            "vault_field": (feats.get("vault") or {}).get("field"),
            "rewards_available": rewards_info.get("available", False),
            "rewards_reason": rewards_info.get("reason"),
            "rewards_query": rewards_info.get("query"),
            "ledger_available": (feats.get("ledger") or {}).get("available", False),
            "ledger_sources": [
                f"{x['field']} ({x.get('role') or 'podle typu'})"
                for x in (feats.get("ledger") or {}).get("sources") or []
            ],
            "ledger_reason": (feats.get("ledger") or {}).get("reason"),
            "ledger_candidates": (feats.get("ledger") or {}).get("candidates"),
            "diagnostics": feats.get("diagnostics"),
        }
    )


@app.get("/api/config-summary")
def api_config_summary() -> JSONResponse:
    config = _config()
    sports_cfg = config.get("sports") or {}
    tournaments = [dict(t, sport="MLB") for t in config.get("tournaments") or []]
    tournaments += [
        dict(t, sport="Fotbal")
        for t in (sports_cfg.get("football") or {}).get("tournaments") or []
    ]
    return JSONResponse(
        {
            "tournaments": [
                {k: t.get(k) for k in ("sport", "name", "slug_contains", "rarity", "max_lineups")}
                for t in tournaments
            ],
            "sports": [
                {
                    "key": key,
                    "label": cls.label,
                    "enabled": (sports_cfg.get(key) or {}).get("enabled", True),
                    "lineups": cls.lineups_supported
                    and (sports_cfg.get(key) or {}).get("lineups_enabled", True),
                }
                for key, cls in ADAPTERS.items()
            ],
        }
    )


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


@app.get("/api/schema")
def schema_dump(
    request: Request,
    type: str = "Query",
    grep: str | None = None,
    secret: str | None = None,
    x_internal_secret: str | None = Header(default=None),
) -> JSONResponse:
    """Vytáhne definici jednoho typu z veřejného SDL schématu Sorare.

    Introspekce (__schema) je pro API klíče zakázaná, ale celé schéma je
    ke stažení jako soubor. Stáhneme ho, najdeme blok `type <Name> {...}`
    a vrátíme jen ten — jinak by odpověď měla přes milion znaků.

    ?type=Query          definice typu
    ?grep=lineup         jen řádky obsahující řetězec (case-insensitive)
    """
    _check_secret(x_internal_secret or secret, request)

    import re

    import requests

    cached = get_store().get("sorare:sdl")
    if not cached:
        resp = requests.get("https://api.sorare.com/graphql/schema", timeout=60)
        resp.raise_for_status()
        cached = resp.text
        # Schéma se mění zřídka; hodina stačí a ušetří opakované stahování.
        try:
            get_store().set("sorare:sdl", cached, ttl_seconds=3600)
        except Exception:  # noqa: BLE001 — cache není kritická
            pass

    pattern = re.compile(
        r"^(?:type|input|interface|enum)\s+" + re.escape(type) + r"\b[^\n]*\{",
        re.MULTILINE,
    )
    match = pattern.search(cached)
    if not match:
        return JSONResponse({"type": type, "found": False})

    # Najdeme konec bloku podle vyvážených složených závorek.
    depth, i = 0, match.end() - 1
    while i < len(cached):
        if cached[i] == "{":
            depth += 1
        elif cached[i] == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1

    block = cached[match.start(): i + 1]
    lines = [ln.rstrip() for ln in block.splitlines()]

    if grep:
        needle = grep.lower()
        lines = [ln for ln in lines if needle in ln.lower()]

    return JSONResponse(
        {"type": type, "found": True, "lines": len(lines), "definition": lines}
    )


@app.get("/api/debug")
def debug(
    request: Request,
    secret: str | None = None,
    x_internal_secret: str | None = Header(default=None),
) -> JSONResponse:
    """Co reálně chodí ze Sorare — slugy leaderboardů a hodnoty pozic.

    Podle tohohle se nastavuje `tournaments.slug_contains` a `lineup.slots`
    v config.yaml. Čte z cache, takže nestahuje portfolio znovu.
    """
    _check_secret(x_internal_secret or secret, request)

    from collections import Counter

    from sorare_mlb.client import SorareClient, card_from_dict
    from sorare_mlb.store import K_CARDS

    config = _config()
    client = SorareClient(config)

    boards = client.fetch_leaderboards()
    raw_cards = get_store().get_json(K_CARDS) or []
    cards = [card_from_dict(c) for c in raw_cards]

    positions = Counter(pos for c in cards for pos in c.positions)
    configured = {
        p for allowed in config.get_path("lineup.slots", {}).values() for p in allowed
    }
    seen = set(positions)

    return JSONResponse(
        {
            "leaderboards": sorted(
                (
                    {
                        "id": b.get("id"),
                        "slug": b.get("slug"),
                        "name": b.get("displayName"),
                        "rarity": b.get("rarityType"),
                        "mine": b.get("mySo5LineupsCount"),
                    }
                    for b in boards
                ),
                key=lambda b: str(b["slug"]),
            ),
            "cards_cached": len(cards),
            "positions_seen": dict(positions.most_common()),
            "positions_in_config_but_unseen": sorted(configured - seen),
            "positions_seen_but_not_in_config": sorted(seen - configured),
            "rarities_seen": dict(Counter(c.rarity for c in cards).most_common()),
        }
    )


@app.get("/api/starters")
def starters(
    request: Request,
    slug: str | None = None,
    min_odds: int = 5000,
    secret: str | None = None,
    x_internal_secret: str | None = Header(default=None),
) -> JSONResponse:
    """Kdo podle Sorare v tomhle gameweeku startuje.

    Bez `slug` projde všechny otevřené baseballové leaderboardy, takže je
    hned vidět, na kterém dotaz projde a na kterém ne.
    """
    _check_secret(x_internal_secret or secret, request)

    from sorare_mlb.client import SorareClient

    client = SorareClient(_config())
    boards = client.fetch_leaderboards()

    # Fixture je jeden na gameweek, takže se ptáme jednou za fixture.
    fixtures = {}
    for board in boards:
        fixture = board.get("so5Fixture") or {}
        if fixture.get("slug") and (not slug or slug in fixture["slug"]):
            fixtures[fixture["slug"]] = fixture

    results = []
    for fixture_slug, fixture in list(fixtures.items())[:3]:
        found, error = client.fetch_probable_starters(fixture_slug)
        results.append(
            {
                "fixture": fixture_slug,
                "gameWeek": fixture.get("gameWeek"),
                "obdobi": f"{fixture.get('startDate')} – {fixture.get('endDate')}",
                "count": len(found),
                "players": sorted(found)[:60],
                "error": error,
            }
        )

    return JSONResponse({"results": results})


@app.get("/api/health")
def health() -> JSONResponse:
    required = ["SORARE_EMAIL", "SORARE_PASSWORD", "SORARE_API_KEY", "INTERNAL_SECRET"]
    auth = token_status()
    optional = [
        "KV_REST_API_URL", "KV_REST_API_TOKEN", "APP_BASE_URL", "APP_PASSWORD",
        "DISCORD_WEBHOOK_URL", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
    ]
    store_kind = type(get_store()).__name__
    return JSONResponse(
        {
            "app_version": APP_VERSION,
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
